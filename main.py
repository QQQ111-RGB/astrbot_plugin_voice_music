"""AstrBot 语音点歌插件（骨架）。

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
    （发出候选后）回复「2」或「2 语音」
    音乐状态                  输出自检报告
"""

from __future__ import annotations

import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .core.audio import FfmpegInfo, ensure_ffmpeg_on_path
from .core.config import PLUGIN_NAME, PluginConfig
from .core.diagnose import build_report
from .core.downloader import Downloader
from .core.http import HttpClient
from .core.platform import BaseMusicPlayer
from .core.sender import MusicSender
from .core.utils import parse_index_and_modes

# 命令别名（装饰器在类定义时求值，所以写死；改这里即可增删）
COMMAND_ALIAS = {"网易点歌", "网易云", "网易web", "网易官方"}
STATUS_ALIAS = {"点歌诊断", "音乐诊断", "音乐状态"}


def _no_llm(event: AstrMessageEvent) -> None:
    """禁止这条消息再走 AstrBot 默认的 LLM 链路。

    ★ 为什么必须有这一步（真实踩过的坑）：

    AstrBot 的 session_waiter 截获一条消息后，会把它 **浅复制成一个新事件，
    重新投递走一遍完整流水线**。那条消息同样会触发 LLM 回复 —— 而 LLM 看到的上下文是
    「候选列表 + 用户回复的 1 语音」，它很可能判断用户想听第 1 首，
    于是再调用一次本插件的点歌工具，**同一首歌就发了两遍**。
    两遍的日志长得一模一样（都是 record_local 发送成功），极难定位。

    `event.stop_event()` 挡不住这件事：它只影响后续的 listener/handler 传播，
    LLM 请求由 `should_call_llm()` 单独控制。
    """
    try:
        event.should_call_llm(False)
    except Exception:  # noqa: BLE001
        # 老版本 AstrBot 可能没有这个方法；退化成不做任何事，不影响主流程
        pass


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
        """点歌 <歌名> [序号]。发候选后回复序号选歌，可加「语音/文件/文本」指定发送方式。"""
        return

    @filter.command("音乐状态", alias=STATUS_ALIAS)
    async def cmd_status(self, event: AstrMessageEvent):
        """音乐状态：输出自检报告（ffmpeg / 音源端点连通性 / 缓存目录 / 发送策略）。"""
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
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_song(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command:
            return

        cmd, _, arg = event.message_str.partition(" ")
        cmd, arg = cmd.strip(), arg.strip()
        if not arg:
            return

        player = self.get_player(word=cmd)
        if cmd == "点歌":
            player = self.get_player(default=True)
        if player is None:
            return  # 不是本插件的命令，静默放过

        # 确认是我们自己的命令：接管这条消息，别再让它触发 LLM
        _no_llm(event)

        # 解析「歌名 + 可选尾部序号」
        parts = arg.split()
        index = int(parts[-1]) if parts[-1].isdigit() else 0
        song_name = arg.removesuffix(str(index)).strip() if index else arg
        if not song_name:
            yield event.plain_result("未指定歌名")
            return

        logger.debug(f"[{PLUGIN_NAME}] {player.platform.display_name} 搜索：{song_name}")
        songs = await player.fetch_songs(
            keyword=song_name, limit=self.cfg.song_limit, extra=cmd
        )
        if not songs:
            yield event.plain_result(
                f"没搜到「{song_name}」。"
                "常见原因：音源接口临时不可用 —— 检查 source_endpoints 是否可用，"
                "或换一个平台试试。"
            )
            return

        if len(songs) == 1:
            index = 1

        # 直接点了序号：立即发送
        if index and 1 <= index <= len(songs):
            await self.sender.send_song(event, player, songs[index - 1])
            event.stop_event()
            return

        # 否则列出候选，等待用户回复序号
        lines = [
            f"🔍 「{song_name}」找到 {len(songs)} 首，"
            f"回复序号选择（{self.cfg.selection_timeout} 秒内）："
        ]
        for i, song in enumerate(songs, 1):
            extra_txt = ""
            if song.duration:
                extra_txt = f"  [{song.duration // 1000}s]"
            lines.append(f"{i}. {song.display_name()}{extra_txt}")
        lines.append("可在序号后加发送方式，如「1 语音」")
        yield event.plain_result("\n".join(lines))

        @session_waiter(timeout=self.cfg.selection_timeout)
        async def selection_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            # ★ 这条消息是被会话截获的，AstrBot 之后还会把它浅复制重新投递一次
            #   （见 _no_llm 的注释）。在这里就掐掉 LLM，否则会重复发一遍歌。
            _no_llm(ev)

            text = ev.message_str.strip()
            # 用户又发起了新的点歌：让给新会话
            if any(kw in text.lower() for kw in self.keywords):
                controller.stop()
                return

            idx, modes, error = parse_index_and_modes(text)
            if error:
                await ev.send(ev.plain_result(error))
                return
            if idx == 0:
                return
            if not (1 <= idx <= len(songs)):
                await ev.send(ev.plain_result(f"序号超出范围（1-{len(songs)}）"))
                controller.stop()
                return

            controller.stop()
            await self.sender.send_song(ev, player, songs[idx - 1], modes=modes)

        try:
            await selection_waiter(event)
        except TimeoutError:
            yield event.plain_result("点歌超时，已取消（可调大 selection_timeout）")
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            logger.error(f"[{PLUGIN_NAME}] 点歌出错：{e}")

        event.stop_event()

    # ------------------------------------------------------------------
    # LLM 工具：让 AI 可以自己点歌
    # ------------------------------------------------------------------
    @filter.llm_tool()
    async def play_song_by_name(
        self, event: AstrMessageEvent, song_name: str, platform: str = ""
    ):
        """当用户想听歌时，根据歌名（可含歌手）搜索并以语音播放。

        Args:
            song_name(string): 歌曲名称或包含歌手的关键词
            platform(string): 可选。指定音源平台，严格匹配：网易点歌 / 网易web。留空用默认平台
        """
        player = (
            self.get_player(name=platform) if platform else self.get_player(default=True)
        )
        if player is None:
            return f"无可用音源：{platform}" if platform else "无可用音源"
        songs = await player.fetch_songs(keyword=song_name, limit=1, extra=platform or None)
        if not songs:
            return "没找到相关歌曲"
        ok = await self.sender.send_song(event, player, songs[0])
        # 歌已经发出去了，就别再让 LLM 补一句「好的，这就为你播放」之类的废话，
        # 也顺手挡掉其他插件对同一条消息的重复响应。
        # 注意：真正防「同一首歌发两遍」的是 sender 里的去重闸门（dedup_seconds），
        # stop_event 只作用于当前这条事件的后续 listener，挡不住被重新投递的那条。
        event.stop_event()
        return None if ok else "歌曲发送失败（详见聊天里的失败原因）"
