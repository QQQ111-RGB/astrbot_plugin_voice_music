"""发送层 —— 骨架里「要语音不要文件」的核心。

原版的行为：
    send_modes 是一个优先级链，任意一环失败就往后降级。
    语音环节一旦失败（下载到非音频 / 转码失败 / 平台不支持），
    就会静默退到 file_local —— 而 file 模式不校验内容，于是「成功」了，
    用户看到的却是一个文件（有时还是个损坏的文件），完全无从排查。

这里的行为：
    1. 每种发送方式都返回「失败原因」，失败原因会被收集起来一并告诉用户；
    2. voice_strict = True 时，只保留 record_* 方式：
       语音发不出去就直接报错并说明原因，绝不偷偷改成发文件；
    3. 本地语音会先用 ffmpeg 统一转码，再交给协议端转 SILK。
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Record

from .audio import (
    estimate_voice_payload,
    human_size,
    resolve_voice_format,
    to_voice,
)
from .config import PluginConfig
from .downloader import Downloader
from .model import Song
from .platform import BaseMusicPlayer

# 发送方式函数签名：成功返回 None，失败返回原因字符串
SendFn = Callable[[AstrMessageEvent, BaseMusicPlayer, Song], Awaitable[str | None]]


class MusicSender:
    def __init__(self, cfg: PluginConfig, downloader: Downloader):
        self.cfg = cfg
        self.downloader = downloader
        # 去重窗口：key -> 上次发送的 monotonic 时间戳
        self._recent: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 去重闸门
    #
    # 为什么需要这一道：AstrBot 的 session_waiter 截获一条消息后，会把它
    # **浅复制成一个新事件重新投递，走一遍完整流水线**，那条消息同样会触发
    # LLM 回复。LLM 看到「候选列表 + 1 语音」这种上下文，很可能认为用户想听
    # 第 1 首，于是又调用一次本插件的点歌工具 —— 同一首歌就发了两遍，
    # 而且两条日志长得一模一样，很难看出是两条不同路径。
    # ------------------------------------------------------------------
    def _dedup_key(self, event: AstrMessageEvent, song: Song) -> str | None:
        if self.cfg.dedup_seconds <= 0:
            return None
        try:
            umo = event.unified_msg_origin
        except AttributeError:
            return None
        if not umo:
            return None
        return f"{umo}|{song.source or ''}|{song.id or song.name or ''}"

    def _hit_dedup(self, key: str) -> float | None:
        """命中返回「距今多少秒」，未命中返回 None。"""
        window = self.cfg.dedup_seconds
        now = time.monotonic()
        # 顺手清理过期项，避免长跑后字典无限增长
        self._recent = {k: t for k, t in self._recent.items() if now - t < window}
        last = self._recent.get(key)
        return None if last is None else now - last

    def _release_dedup(self, key: str | None) -> None:
        """彻底失败时把占位撤回，否则用户 20 秒内重试会被静默吞掉。"""
        if key:
            self._recent.pop(key, None)

    # ------------------------------------------------------------------
    # 各发送方式：成功 -> None，失败 -> 原因
    # ------------------------------------------------------------------
    async def _record_local(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> str | None:
        """下载到本地 → ffmpeg 转码 → 以语音组件发出。"""
        urls = player.audio_url_candidates(song)
        if not urls:
            return "没有可用的音频直链"

        path = await self.downloader.download_audio_multi(
            urls, headers=player.audio_headers(song)
        )
        if not path:
            return "音频下载失败，或下载到的内容根本不是音频（常见于直链需要 Referer/已失效）"

        voice = path
        if self.cfg.ffmpeg_convert:
            converted = await to_voice(
                path,
                self.cfg.audio_dir,
                ffmpeg=self.cfg.ffmpeg_path,
                max_seconds=self.cfg.max_voice_seconds,
                fmt=self.cfg.voice_format,
            )
            if converted is None:
                self._defer_cleanup(path)
                return (
                    "ffmpeg 转码失败（最常见原因是容器/系统里没有 ffmpeg，"
                    "或音频文件损坏）"
                )
            voice = converted

            # ---- 体积闸门 1：超限时自动降档重转一次（16k -> 8k，体积减半）----
            sizes = self._payload_check(voice)
            if sizes is not None and self._is_auto_downgradable():
                logger.info(
                    f"[sender] 语音体积 {sizes[1]} 超限，自动降到 wav8k 重转"
                )
                retry = await to_voice(
                    path,
                    self.cfg.audio_dir,
                    ffmpeg=self.cfg.ffmpeg_path,
                    max_seconds=self.cfg.max_voice_seconds,
                    fmt="wav8k",
                )
                if retry is not None:
                    self._defer_cleanup(voice)
                    voice = retry
                    sizes = self._payload_check(voice)

            # ---- 体积闸门 2：还是超就直接不发，说明原因交给下一种发送方式 ----
            if sizes is not None:
                payload, readable = sizes
                limit = self.cfg.max_payload_bytes
                self._defer_cleanup(path, voice if voice != path else None)
                return (
                    f"语音体积超限：预计 {readable} > 上限 {human_size(limit)}。"
                    "协议端会因单帧过大断开连接（NapCat 报 Max payload size exceeded），"
                    "在 AstrBot 侧表现为超时，然后降级成发文件。"
                    "可调小 max_voice_seconds、把 voice_format 设为 wav8k，"
                    "或调大 max_payload_bytes（需协议端 maxPayload 也够大）"
                )

        try:
            seg = Record.fromFileSystem(str(voice.resolve()))
            await event.send(event.chain_result([seg]))
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            # AstrBot 内部刻意把「找不到 ffmpeg」抛成这句，单独点出来
            if "ffmpeg not found" in msg:
                return (
                    "AstrBot 内部转码失败：ffmpeg not found "
                    "（AstrBot 发语音时会执行裸命令 ffmpeg，PATH 上必须有它）"
                )
            return f"语音组件发送失败：{msg}"
        finally:
            # 延迟清理：部分适配器可能在 send 返回后仍异步读取文件
            self._defer_cleanup(path, voice if voice != path else None)
        return None

    async def _record_link(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> str | None:
        """把音频直链交给协议端自己去拉。快，但依赖直链对协议端可达。"""
        if not song.audio_url:
            return "没有音频直链"
        try:
            seg = Record.fromURL(song.audio_url)
            await event.send(event.chain_result([seg]))
        except Exception as e:  # noqa: BLE001
            return f"语音链接发送失败：{type(e).__name__}: {e}"
        return None

    async def _file_local(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> str | None:
        urls = player.audio_url_candidates(song)
        if not urls:
            return "没有可用的音频直链"
        path = await self.downloader.download_audio_multi(
            urls, headers=player.audio_headers(song)
        )
        if not path:
            return "音频下载失败"
        try:
            name = f"{song.display_name()}{path.suffix}"
            await event.send(
                event.chain_result([File(name=name, file=str(path.resolve()))])
            )
        except Exception as e:  # noqa: BLE001
            return f"文件发送失败：{type(e).__name__}: {e}"
        finally:
            self._defer_cleanup(path)
        return None

    async def _file_link(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> str | None:
        if not song.audio_url:
            return "没有音频直链"
        try:
            seg = File(name=f"{song.display_name()}.mp3", url=song.audio_url)
            await event.send(event.chain_result([seg]))
        except Exception as e:  # noqa: BLE001
            return f"文件链接发送失败：{type(e).__name__}: {e}"
        return None

    async def _text(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> str | None:
        if not song.audio_url:
            return "没有音频直链"
        try:
            await event.send(event.plain_result(song.audio_url))
        except Exception as e:  # noqa: BLE001
            return f"文本发送失败：{type(e).__name__}: {e}"
        return None

    # ------------------------------------------------------------------
    # 模式调度
    # ------------------------------------------------------------------
    def _get_sender(self, mode: str):
        return {
            "record_local": self._record_local,
            "record_link": self._record_link,
            "file_local": self._file_local,
            "file_link": self._file_link,
            "text": self._text,
        }.get(mode)

    def _is_mode_supported(self, mode: str, event: AstrMessageEvent) -> bool:
        platform = event.get_platform_name()
        if mode == "text":
            return True
        if mode in ("record_local", "record_link"):
            return platform not in self.cfg.record_unsupported
        return True

    # ------------------------------------------------------------------
    # 体积闸门
    # ------------------------------------------------------------------
    def _payload_check(self, voice: Path) -> tuple[int, str] | None:
        """返回 (预计 base64 字节数, 可读文本)；在限额内或未设限时返回 None。"""
        limit = self.cfg.max_payload_bytes
        if not limit:
            return None
        payload, readable = estimate_voice_payload(voice)
        return (payload, readable) if payload > limit else None

    def _is_auto_downgradable(self) -> bool:
        """当前档位是不是「还能再压一档」的 auto/16k wav。"""
        return resolve_voice_format(self.cfg.voice_format) == "wav"

    def _defer_cleanup(self, *paths: Path | None) -> None:
        """延迟 60 秒清理临时文件，避开适配器异步读取的竞态。"""
        valid = [p for p in paths if p is not None]

        async def _clean() -> None:
            await asyncio.sleep(60)
            for p in valid:
                await self.downloader.cleanup(p)

        asyncio.create_task(_clean())

    def _fail_text(
        self, song: Song, modes: list[str], reasons: list[str]
    ) -> str:
        lines = [f"❌ 点歌失败：{song.display_name()}"]
        lines.extend(f"· {r}" for r in reasons)
        if self.cfg.voice_strict:
            lines.append(
                "提示：voice_strict 已开启，语音失败时不会自动改成发文件。"
                "发「音乐状态」可查看 ffmpeg / 音源端点的自检结果。"
            )
        else:
            lines.append(f"已尝试的方式：{' → '.join(modes)}")
        return "\n".join(lines)

    async def send_song(
        self,
        event: AstrMessageEvent,
        player: BaseMusicPlayer,
        song: Song,
        modes: list[str] | None = None,
    ) -> bool:
        """按策略发送一首歌。返回是否成功。"""
        # 0) 去重闸门（见上面 _dedup_key 的注释）
        dedup_key = self._dedup_key(event, song)
        if dedup_key:
            ago = self._hit_dedup(dedup_key)
            if ago is not None:
                logger.info(
                    f"[sender] 去重命中：{song.display_name()} 在 {ago:.1f}s 前刚发过，"
                    f"本次跳过（窗口 {self.cfg.dedup_seconds}s，"
                    f"设 dedup_seconds=0 可关闭）"
                )
                return True
            self._recent[dedup_key] = time.monotonic()

        # 1) 补全音频直链：先走平台解析钩子，再收集候选地址
        if not song.audio_url:
            try:
                song = await player.resolve_audio(song)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[sender] 解析直链异常：{e}")

        urls = player.audio_url_candidates(song)
        if not urls:
            self._release_dedup(dedup_key)
            await event.send(
                event.plain_result(f"【{song.display_name()}】获取音频直链失败")
            )
            return False
        # link / text 类发送方式直接用首个候选地址
        song.audio_url = song.audio_url or urls[0]

        # 2) 决定候选发送方式
        target = list(modes) if modes else list(self.cfg.send_modes)
        if self.cfg.voice_strict and modes is None:
            target = [m for m in target if m.startswith("record")] or ["record_local"]

        logger.info(
            f"[sender] {event.get_sender_name()} 点歌 "
            f"{player.platform.display_name} -> {song.display_name()}，"
            f"候选方式：{target}"
        )

        # 3) 逐个尝试，收集失败原因
        reasons: list[str] = []
        for mode in target:
            if not self._is_mode_supported(mode, event):
                reasons.append(f"{mode}：当前平台({event.get_platform_name()})不支持")
                continue
            sender = self._get_sender(mode)
            if sender is None:
                reasons.append(f"{mode}：未知的发送方式")
                continue
            try:
                reason = await sender(event, player, song)
            except Exception as e:  # noqa: BLE001
                logger.error(traceback.format_exc())
                reason = f"{type(e).__name__}: {e}"

            if reason is None:
                logger.info(f"[sender] {mode} 发送成功：{song.display_name()}")
                return True
            # 用 INFO 而不是 DEBUG：排查「为什么最后发了文件」时必须看到
            # 前面那些方式各自失败在哪一步（record_link 拉不到直链是最常见的）
            logger.info(f"[sender] {mode} 失败：{reason}")
            reasons.append(f"{mode}：{reason}")

        # 4) 全部失败，给出可诊断的信息，而不是静默发个文件
        self._release_dedup(dedup_key)
        await event.send(event.plain_result(self._fail_text(song, target, reasons)))
        return False
