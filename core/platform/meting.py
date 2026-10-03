"""基于 Meting API 的音源实现。

Meting 是一套「一个接口打通多站点」的规范，调用形如：
    {endpoint}?server=netease&type=search&id=关键词
    {endpoint}?server=netease&type=url&id=歌曲ID     -> 302 跳到音频直链

实测（2026-10）：
    - https://api.qijieya.cn/meting/      search: ✅ 返回 [name, artist, url, pic, lrc]
    - https://api.i-meto.com/meting/api   search: ✅ 返回 [title, author, url, pic, lrc]
    注意：不同端点支持的 server 不同，且不支持时可能返回 **空数组** 而不是报错，
    所以这里把「空结果」也当作失败，继续尝试下一个端点。
"""

from __future__ import annotations

from typing import ClassVar

from astrbot.api import logger

from ..config import PluginConfig
from ..http import HttpClient
from ..model import Platform, Song
from .base import BaseMusicPlayer

# 网易官方 web 搜索接口（无需鉴权）
_NETEASE_WEB_SEARCH = "http://music.163.com/api/search/get/web"


class MetingPlayer(BaseMusicPlayer):
    """Meting 系平台的公共实现。子类只需声明 server 和 platform。"""

    abstract: ClassVar[bool] = True

    server: ClassVar[str] = "netease"

    async def _search_endpoints(self, params: dict) -> list | None:
        """★多端点回退：按顺序尝试，跳过空结果，返回第一个有效列表。"""
        endpoints = self.cfg.source_endpoints
        for idx, base in enumerate(endpoints):
            data = await self.http.request_json(base, params=params)
            if isinstance(data, list) and data:
                if idx > 0:
                    logger.info(f"[{self.platform.name}] 回退到备用端点：{base}")
                return data
            if data is not None:
                logger.debug(f"[{self.platform.name}] {base} 返回空/异常结果，换下一个")
        logger.error(f"[{self.platform.name}] 所有端点均无有效结果")
        return None

    @staticmethod
    def _parse_item(item: dict, source: str, note: str) -> Song:
        """兼容两种字段命名：name/artist 与 title/author。"""
        return Song(
            id=str(item.get("id") or item.get("url_id") or ""),
            source=source,
            name=item.get("name") or item.get("title"),
            artists=item.get("artist") or item.get("author"),
            audio_url=item.get("url"),
            cover_url=item.get("pic"),
            lyrics=item.get("lrc"),
            note=note,
        )

    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        data = await self._search_endpoints(
            {"server": self.server, "type": "search", "id": keyword}
        )
        if not data:
            return []
        songs: list[Song] = []
        for item in data[:limit]:
            if isinstance(item, dict):
                songs.append(
                    self._parse_item(item, self.server, self.platform.display_name)
                )
        return songs

    def audio_headers(self, song: Song) -> dict[str, str]:
        return {"Referer": "https://music.163.com/"}

    def audio_url_candidates(self, song: Song) -> list[str]:
        """★同 ID 在所有端点上各生成一个取链地址，下载时逐个尝试。

        这样「第一个端点挂了 / 返回的直链要求签名过期」都不会直接失败。
        """
        urls: list[str] = []
        if song.audio_url:
            urls.append(song.audio_url)
        if song.id:
            for base in self.cfg.source_endpoints:
                url = f"{base}?server={self.server}&type=url&id={song.id}"
                if url not in urls:
                    urls.append(url)
        return urls


class NeteaseMeting(MetingPlayer):
    """网易云 · 走 Meting 聚合接口（默认音源，实测可用）。"""

    server: ClassVar[str] = "netease"
    platform: ClassVar[Platform] = Platform(
        name="netease",
        display_name="网易点歌",
        keywords=["网易点歌", "网易云", "netease"],
    )


class NeteaseWeb(MetingPlayer):
    """网易云 · 官方 web 搜索 + Meting 取直链。

    演示两个扩展点：
      1. 搜索接口可以完全自建（不用 Meting 的 search）；
      2. search 拿不到直链时，用 resolve_audio() 二次补全。
    """

    server: ClassVar[str] = "netease"
    platform: ClassVar[Platform] = Platform(
        name="netease_web",
        display_name="网易web",
        keywords=["网易web", "网易官方"],
    )

    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        data = await self.http.request_json(
            _NETEASE_WEB_SEARCH,
            method="POST",
            data={"s": keyword, "limit": limit, "type": 1, "offset": 0},
            headers={"Referer": "https://music.163.com/"},
            cookies={"appver": "2.0.2"},
        )
        songs_raw = (
            data.get("result", {}).get("songs", []) if isinstance(data, dict) else []
        )
        if not songs_raw:
            logger.warning(f"[netease_web] 搜索无结果或接口异常：{keyword}")
            return []

        songs: list[Song] = []
        for s in songs_raw[:limit]:
            artists = "、".join(a.get("name", "") for a in s.get("artists", []))
            songs.append(
                Song(
                    id=str(s.get("id")),
                    source="netease",
                    name=s.get("name"),
                    artists=artists,
                    duration=s.get("duration"),
                    note=self.platform.display_name,
                )
            )
        return songs

    # 搜索接口不返回直链，交给父类 MetingPlayer.audio_url_candidates()
    # 用「歌曲 ID + 各端点」自动补全并回退，无需在这里写 resolve_audio。


# ----------------------------------------------------------------------
# 加新平台的模板（复制即可）：
#
# class KugouMeting(MetingPlayer):
#     server: ClassVar[str] = "kugou"
#     platform: ClassVar[Platform] = Platform(
#         name="kugou", display_name="酷狗点歌", keywords=["酷狗点歌", "酷狗"]
#     )
#
# 然后把类名加进 core/platform/__init__.py 的 import 与 __all__ 即可，
# main.py 会自动发现并注册（见 base.BaseMusicPlayer.__init_subclass__）。
# ----------------------------------------------------------------------
