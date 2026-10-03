"""数据模型：Song（一首歌）与 Platform（音源平台元信息）。

设计要点：Song 只承载「数据」，不关心怎么发；
sender 拿到 Song 后决定发语音还是发文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Song:
    id: str
    """歌曲 ID（平台内唯一）"""

    source: str | None = None
    """音源标识，如 netease / tencent"""

    name: str | None = None
    """歌名"""

    artists: str | None = None
    """歌手，多歌手用「、」连接"""

    duration: int | None = None
    """时长（毫秒）"""

    audio_url: str | None = None
    """音频直链（发语音/发文件都要用）"""

    cover_url: str | None = None
    """封面图"""

    lyrics: str | None = None
    """歌词文本或歌词 URL"""

    note: str | None = None
    """备注，例如来源平台"""

    def display_name(self) -> str:
        return f"{self.name or '未知'}" + (f" - {self.artists}" if self.artists else "")

    def to_lines(self) -> str:
        lines = [f"名称: {self.name or '未知'}", f"歌手: {self.artists or '未知'}"]
        if self.duration:
            mins, secs = divmod(self.duration // 1000, 60)
            lines.append(f"时长: {mins}:{secs:02d}")
        if self.note:
            lines.append(f"来源: {self.note}")
        return "\n".join(lines)


@dataclass(slots=True)
class Platform:
    """音源平台元信息，用于命令识别与展示。"""

    name: str
    """内部标识，如 netease"""

    display_name: str
    """展示名，如 网易点歌"""

    keywords: list[str] = field(default_factory=list)
    """触发关键词，如 ["网易点歌", "网易云"]"""
