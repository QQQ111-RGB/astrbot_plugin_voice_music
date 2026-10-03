"""插件配置封装。

AstrBot 会把 _conf_schema.json 对应的配置以 AstrBotConfig（dict 子类）
传进来；这里包一层，提供带类型和默认值的属性访问，避免满屏 .get()。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .audio import DEFAULT_MAX_PAYLOAD_BYTES

PLUGIN_NAME = "astrbot_plugin_voice_music"

# core/config.py -> 上两级就是插件根目录
PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _resolve_temp_dir() -> Path:
    """优先用 AstrBot 的临时目录，取不到就退回系统临时目录。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        return Path(get_astrbot_temp_path()) / PLUGIN_NAME
    except Exception:  # noqa: BLE001
        return Path(tempfile.gettempdir()) / PLUGIN_NAME


class PluginConfig:
    def __init__(self, raw: AstrBotConfig, context: Context):
        self.raw = raw
        self.context = context
        self.plugin_dir = PLUGIN_DIR
        self.temp_dir = _resolve_temp_dir()
        self.audio_dir = self.temp_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 原始读取 ----------
    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def _str_list(self, key: str, default: list[str] | None = None) -> list[str]:
        value = self.raw.get(key, default or [])
        if not isinstance(value, list):
            return list(default or [])
        # 兼容 AstrBot 的 "text(文本模式)" 这种带说明的选项
        return [str(v).split("(", 1)[0].strip() for v in value if str(v).strip()]

    # ---------- 业务属性 ----------
    @property
    def default_player_name(self) -> str:
        return str(self.raw.get("default_player_name") or "网易点歌")

    @property
    def source_endpoints(self) -> list[str]:
        urls = self._str_list("source_endpoints")
        return [u for u in urls if u.startswith(("http://", "https://"))]

    @property
    def meting_token(self) -> str:
        """自建 Meting-API 的 HMAC 签名密钥（对应它的 METING_TOKEN 环境变量）。

        ★ 为什么需要这一项：
        metowolf/Meting-API（v1.11+）把 `url` / `pic` / `lrc` 列为**敏感接口**，
        强制校验 `auth` 参数：

            auth = HMAC-SHA1(METING_TOKEN, server + type + id)

        而 `search` 是**免鉴权**的。所以如果只把 source_endpoints 指向自建实例
        而不提供密钥，会出现最难排查的那种失败：**搜索正常返回候选、取直链却 401**
        —— 看起来像「歌搜到了但放不出来」。填上这个密钥后，插件会自动为每首歌
        算好 auth 拼进取链地址。

        留空表示端点不需要鉴权（公共端点如 api.qijieya.cn 就是这样）。
        注意：第三方托管的实例（如 api.i-meto.com）用的是**它自己的私有密钥**，
        你算不出来，只能直接使用它 search 响应里返回的完整 url —— 那种情况下本项留空。
        """
        return str(self.raw.get("meting_token") or "").strip()

    @property
    def request_timeout(self) -> float:
        return float(self.raw.get("request_timeout") or 8)

    @property
    def request_retries(self) -> int:
        return int(self.raw.get("request_retries") or 0)

    @property
    def song_limit(self) -> int:
        return int(self.raw.get("song_limit") or 5)

    @property
    def selection_timeout(self) -> int:
        return int(self.raw.get("selection_timeout") or 60)

    @property
    def send_modes(self) -> list[str]:
        """默认把 record_link 排第一。

        原因见 core/audio.py 的算式：record_local 走的是
        「下载 -> 转码 -> AstrBot 转 wav -> base64 -> 一条巨型 WS 帧」，
        帧体积是几十 MB 级别，很容易撞上协议端的 maxPayload 而超时。
        record_link 只把 URL 递过去，帧只有几十字节，由协议端自己去下，稳得多。
        """
        modes = self._str_list("send_modes")
        return modes or ["record_link", "record_local", "file_local", "text"]

    @property
    def voice_strict(self) -> bool:
        return bool(self.raw.get("voice_strict", True))

    @property
    def ffmpeg_convert(self) -> bool:
        return bool(self.raw.get("ffmpeg_convert", True))

    @property
    def ffmpeg_path(self) -> str:
        return str(self.raw.get("ffmpeg_path") or "ffmpeg")

    @property
    def voice_format(self) -> str:
        """auto | wav | wav8k | mp3。见 core/audio.py 的说明。"""
        value = str(self.raw.get("voice_format") or "auto").strip().lower()
        return value if value in ("auto", "wav", "wav8k", "mp3") else "auto"

    @property
    def max_voice_seconds(self) -> int:
        return int(self.raw.get("max_voice_seconds") or 0)

    @property
    def dedup_seconds(self) -> int:
        """同一会话内、同一首歌的发送去重窗口（秒）。0 表示关闭。

        为什么需要：AstrBot 的 session_waiter 截获消息后，会把它浅复制成新事件
        **重新投递走一遍完整流水线**，那条消息会触发 LLM 回复；LLM 看到
        「候选列表 + 1 语音」这样的上下文，很可能又调用一次本插件的点歌工具，
        于是同一首歌发两遍。见 README 的「为什么同一首歌会发两遍」。
        """
        value = self.raw.get("dedup_seconds")
        if value is None or value == "":
            return 20
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 20
        return parsed if parsed > 0 else 0

    @property
    def max_payload_bytes(self) -> int:
        """本地语音发出前的 base64 体积闸门（字节）。<=0 表示不限制。

        为什么需要这个闸门：
            超限时协议端会断开 WS 连接，AstrBot 的 API 调用等不到回包 → 报超时，
            然后插件降级去发文件。用户看到的是「超时 + 收到一个文件」，
            完全没有线索。在这里提前拦下来，就能给出「预计 12.3 MB，上限 8 MB」
            这种可操作的错误。
        """
        value = self.raw.get("max_payload_bytes")
        if value is None or value == "":
            return DEFAULT_MAX_PAYLOAD_BYTES
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_MAX_PAYLOAD_BYTES
        return parsed if parsed > 0 else 0

    @property
    def record_unsupported(self) -> list[str]:
        return self._str_list("record_unsupported")

    @property
    def proxy(self) -> str | None:
        return str(self.raw.get("proxy") or "").strip() or None

    @property
    def recall_select(self) -> bool:
        return bool(self.raw.get("recall_select", True))

    def warn_missing(self) -> None:
        """启动时做一次自检，把容易踩的配置问题直接打到日志。"""
        if not self.source_endpoints:
            logger.warning(f"[{PLUGIN_NAME}] source_endpoints 为空，点歌会直接失败")
