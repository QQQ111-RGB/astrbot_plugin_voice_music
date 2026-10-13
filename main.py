"""AstrBot 语音点歌插件。

架构总览：
    main.py              —— 命令注册、事件监听、选歌会话；只做「编排」
      └─ core/config.py  —— 配置读取
      └─ core/http.py    —— 统一 HTTP：秒级超时 + 重试 + 多端点回退（治「超时」）
      └─ core/platform/  —— 音源抽象 + 各平台实现（治「换音源」）
      └─ core/downloader—— 下载 + 内容校验（治「下到 HTML」）
      └─ core/audio.py   —— 魔数校验 + ffmpeg 转码 + PATH 垫片（治「发不出语音」）
      └─ core/sender.py  —— 发送策略：语音优先、严格模式不降级（治「变文件」）
      └─ core/diagnose.py—— 远程自检报告（治「服务器上没法排查」）

命令：
    点歌 <歌名> [序号]        使用默认平台
    网易点歌 <歌名>           指定平台
    网易web <歌名>            指定平台
    群聊发起和选择都需要当前唤醒词或真实 @；候选只属于发起人。
    （发出候选后）用同样方式回复「2」或「2 语音」
    音乐状态                  输出自检报告
"""

from __future__ import annotations

import asyncio
import time
import traceback
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Plain

from .core.audio import FfmpegInfo, ensure_ffmpeg_on_path
from .core.config import PLUGIN_NAME, PluginConfig
from .core.diagnose import build_report
from .core.downloader import Downloader
from .core.http import HttpClient
from .core.platform import BaseMusicPlayer
from .core.sender import MusicSender
from .core.utils import MODE_ALIAS, parse_index_and_modes

# 命令别名（装饰器在类定义时求值，所以写死；改这里即可增删）
# 与音源 keywords 对齐；群聊发起及选歌都要求配置的唤醒词或真实 @。
COMMAND_ALIAS = {
    # 网易系
    "网易点歌",
    "网易云",
    "网易web",
    "网易官方",
    # QQ 系
    "QQ点歌",
    "QQ音乐",
}
STATUS_ALIAS = {"点歌诊断", "音乐诊断", "音乐状态"}


def _no_llm(event: AstrMessageEvent) -> None:
    event.should_call_llm(False)


def _raw_text(event: AstrMessageEvent) -> str:
    return "".join(c.text for c in event.get_messages() if isinstance(c, Plain)).strip()


def _matching_wake_prefix(event: AstrMessageEvent, prefixes=()) -> str | None:
    messages = event.get_messages()
    # Match AstrBot's protection against commands addressed to a different user.
    if (not event.is_private_chat() and messages and isinstance(messages[0], At)
            and str(messages[0].qq) not in {str(event.get_self_id()), "all"}):
        return None
    text = _raw_text(event)
    return next((p for p in prefixes if text.startswith(p)), None)


def _addressed(event: AstrMessageEvent, prefixes=()) -> bool:
    """Only a real bot mention or configured prefix authorizes a group request."""
    if event.is_private_chat():
        return True
    bot_id = str(event.get_self_id())
    mentioned = bool(bot_id) and any(
        isinstance(c, At) and str(c.qq) == bot_id for c in event.get_messages()
    )
    return mentioned or _matching_wake_prefix(event, prefixes) is not None


def _request_text(event: AstrMessageEvent, prefixes=()) -> str:
    text = _raw_text(event)
    prefix = _matching_wake_prefix(event, prefixes)
    if prefix is not None:
        text = text[len(prefix):].strip()
    return text.removeprefix("/").strip()


def _session_key(event: AstrMessageEvent) -> tuple[str, str]:
    return event.unified_msg_origin, event.get_sender_id()


@dataclass
class PendingSelection:
    expires: float
    player: object
    songs: list | None = None


class VoiceMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)

        # 全局唯一的 HTTP 客户端：超时/重试/代理策略统一
        self.http = HttpClient(
            proxy=self.cfg.proxy,
            read_timeout=self.cfg.request_timeout,
            retries=self.cfg.request_retries,
        )
        self.downloader = Downloader(self.cfg, self.http)
        self.sender = MusicSender(self.cfg, self.downloader)

        self.players: list[BaseMusicPlayer] = []
        self.keywords: list[str] = []
        self.ffmpeg: FfmpegInfo | None = None
        self._pending: dict[tuple[str, str], PendingSelection] = {}
        self._send_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """插件加载后：自动发现并注册所有音源；做一次环境自检。"""
        for cls in BaseMusicPlayer.get_all_subclass():
            try:
                player = cls(self.cfg, self.http)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 音源 {cls.__name__} 初始化失败：{e}")
                continue
            self.players.append(player)
            self.keywords.extend(player.platform.keywords)

        logger.info(
            f"[{PLUGIN_NAME}] 已加载音源："
            f"{[p.platform.display_name for p in self.players]}"
        )
        self.cfg.warn_missing()

        # ★语音可用性的前置条件：AstrBot 内部发语音时会执行裸命令 ffmpeg，
        #   这里确保 PATH 上真的能解析到 `ffmpeg`（否则装了 pip 包也没用）。
        self.ffmpeg = await ensure_ffmpeg_on_path(
            self.cfg.temp_dir, self.cfg.ffmpeg_path
        )
        if self.ffmpeg.available:
            logger.info(f"[{PLUGIN_NAME}] ffmpeg 就绪：{self.ffmpeg.summary()}")
        else:
            logger.error(
                f"[{PLUGIN_NAME}] ffmpeg 不可用 —— 语音消息会发送失败。"
                "发「音乐状态」查看自检报告；修复方式见 README 的 Docker 部署一节。"
            )

    async def terminate(self) -> None:
        self._pending.clear()
        for player in self.players:
            try:
                await player.close()
            except Exception:  # noqa: BLE001
                pass
        await self.http.close()

    # ------------------------------------------------------------------
    # 音源选择
    # ------------------------------------------------------------------
    def get_player(
        self,
        *,
        name: str | None = None,
        word: str | None = None,
        default: bool = False,
    ) -> BaseMusicPlayer | None:
        if default:
            word = self.cfg.default_player_name
        for player in self.players:
            pl = player.platform
            if name:
                n = name.strip().lower()
                if pl.display_name.lower() == n or pl.name.lower() == n:
                    return player
            elif word:
                w = word.strip().lower()
                if any(kw.lower() in w for kw in pl.keywords):
                    return player
        return None

    # ------------------------------------------------------------------
    # 命令注册（仅用于让 AstrBot 认识唤醒词 / 生成帮助，不做事）
    # ------------------------------------------------------------------
    @filter.command("点歌", alias=COMMAND_ALIAS)
    async def _cmd_help(self, event: AstrMessageEvent):
        """点歌 <歌名> [序号]。群聊须使用唤醒词或 @机器人；发候选后由点歌者用相同方式回复序号选歌，可加「语音/文件/文本」指定发送方式。"""
        return

    @filter.command("音乐状态", alias=STATUS_ALIAS)
    async def cmd_status(self, event: AstrMessageEvent):
        """音乐状态：输出自检报告（ffmpeg / 音源端点连通性 / 缓存目录 / 发送策略）。"""
        prefixes = self._wake_prefixes(event)
        if not _addressed(event, prefixes):
            return
        _no_llm(event)
        if self.ffmpeg is None:
            self.ffmpeg = await ensure_ffmpeg_on_path(
                self.cfg.temp_dir, self.cfg.ffmpeg_path
            )
        try:
            report = await build_report(
                self.cfg, self.players, self.ffmpeg, self.http
            )
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            yield event.plain_result(f"自检失败：{type(e).__name__}: {e}")
            return
        yield event.plain_result(report)

    # ------------------------------------------------------------------
    # 真正的处理逻辑
    # ------------------------------------------------------------------
    def _wake_prefixes(self, event) -> tuple[str, ...]:
        # Resolve the current conversation's configuration, including overrides.
        try:
            configured = self.context.get_config(umo=event.unified_msg_origin).get("wake_prefix", [])
        except Exception:
            logger.warning("[voice_music] Cannot read wake prefixes; requiring an explicit mention")
            return ()
        if isinstance(configured, str):
            configured = [configured]
        if not isinstance(configured, (list, tuple)):
            return ()
        # Empty/whitespace prefixes must never authorize all group messages.
        return tuple(sorted({p for p in configured if isinstance(p, str) and p.strip()}, key=len, reverse=True))

    async def _send_selected(self, event, player, song, modes=None):
        # Serialize audio preparation/sending to avoid shared cache file races.
        async with self._send_lock:
            return await self.sender.send_song(event, player, song, modes=modes)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_song(self, event: AstrMessageEvent):
        now = time.monotonic()
        self._pending = {k: v for k, v in self._pending.items() if v.expires > now}
        prefixes = self._wake_prefixes(event)
        if not _addressed(event, prefixes):
            return
        text = _request_text(event, prefixes)
        parts = text.split(maxsplit=1)
        if not parts:
            return
        cmd = parts[0]
        key = _session_key(event)
        pending = self._pending.get(key)

        # Only exact, explicit commands can start a search. No history/LLM tools.
        if cmd not in {"点歌", *COMMAND_ALIAS}:
            if pending is None or pending.songs is None:
                return
            if text in {"取消", "取消点歌"}:
                _no_llm(event)
                self._pending.pop(key, None)
                yield event.plain_result("已取消本次点歌")
                event.stop_event()
                return
            # Ordinary chat, extra sentences and bare numbers without @ are ignored.
            selection_parts = text.split()
            if not (cmd.isascii() and cmd.isdigit()) or len(selection_parts) > 2:
                return
            if len(selection_parts) == 2 and selection_parts[1].lower() not in MODE_ALIAS:
                return
            idx, modes, error = parse_index_and_modes(text)
            _no_llm(event)
            if error:
                yield event.plain_result(error)
                event.stop_event()
                return
            if not (1 <= idx <= len(pending.songs)):
                yield event.plain_result(f"序号超出范围（1-{len(pending.songs)}），请重新选择")
                event.stop_event()
                return
            # Claim before any await; a second reply cannot send the same selection.
            self._pending.pop(key, None)
            try:
                await self._send_selected(event, pending.player, pending.songs[idx - 1], modes)
            except Exception:
                logger.error(traceback.format_exc())
                yield event.plain_result("歌曲发送失败，请重新点歌或查看音乐状态")
            finally:
                event.stop_event()
            return

        _no_llm(event)
        # A new explicit request invalidates this user's previous list immediately.
        self._pending.pop(key, None)
        arg = parts[1].strip() if len(parts) == 2 else ""
        if not arg:
            yield event.plain_result("用法：唤醒词 + 点歌 歌名；选歌：唤醒词 + 1 语音。也可用 @机器人 代替唤醒词")
            event.stop_event()
            return
        player = self.get_player(default=True) if cmd == "点歌" else self.get_player(word=cmd)
        if player is None:
            yield event.plain_result("当前音源不可用，请查看音乐状态")
            event.stop_event()
            return
        words = arg.split()
        index = int(words[-1]) if words[-1].isascii() and words[-1].isdigit() else 0
        song_name = " ".join(words[:-1]) if index else arg
        if not song_name:
            yield event.plain_result("未指定歌名")
            event.stop_event()
            return
        request = PendingSelection(time.monotonic() + self.cfg.selection_timeout, player)
        self._pending[key] = request
        try:
            songs = await player.fetch_songs(keyword=song_name, limit=self.cfg.song_limit, extra=cmd)
            # Slow earlier searches cannot overwrite a newer list or resurrect an expired one.
            if self._pending.get(key) is not request or request.expires <= time.monotonic():
                return
            if not songs:
                self._pending.pop(key, None)
                yield event.plain_result(f"没搜到「{song_name}」，请换关键词或音源重试")
                return
            if len(songs) == 1:
                index = 1
            if index and 1 <= index <= len(songs):
                self._pending.pop(key, None)
                await self._send_selected(event, player, songs[index - 1])
                return
            request.songs = songs
            request.expires = time.monotonic() + self.cfg.selection_timeout
            lines = [f"🔍 「{song_name}」找到 {len(songs)} 首（{self.cfg.selection_timeout} 秒内有效）："]
            for i, song in enumerate(songs, 1):
                duration = f"  [{song.duration // 1000}s]" if song.duration else ""
                lines.append(f"{i}. {song.display_name()}{duration}")
            lines.append("由点歌者使用唤醒词或 @机器人 回复「1 语音」；取消用同样方式发送「取消点歌」。私聊可直接回复。")
            yield event.plain_result("\n".join(lines))
        except Exception:
            if self._pending.get(key) is request:
                self._pending.pop(key, None)
            logger.error(traceback.format_exc())
            yield event.plain_result("点歌失败，请换音源重试或查看音乐状态")
        finally:
            event.stop_event()
