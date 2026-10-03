"""音源平台抽象基类 —— 骨架的「可扩展」核心。

思路（借鉴 Zhalslar/astrbot_plugin_music）：
    - 每个音源是一个 BaseMusicPlayer 子类；
    - 子类只负责「把关键词变成 list[Song]」，不关心怎么发送；
    - 通过 __init_subclass__ 自动注册，main.py 遍历注册表即可，
      新增平台不用改主流程（开闭原则）。

    HttpClient 由插件统一持有并注入，子类不自建 session，
    这样超时/重试/代理策略全局一致。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..config import PluginConfig
from ..http import HttpClient
from ..model import Platform, Song


class BaseMusicPlayer(ABC):
    # 已注册的子类（自动填充）
    _registry: ClassVar[list[type["BaseMusicPlayer"]]] = []

    # 子类必须声明：平台元信息
    platform: ClassVar[Platform]

    # 中间抽象类可以置 True，避免被注册
    abstract: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # 只跳过「自己声明了 abstract = True」的中间类与仍含抽象方法的类；
        # 注意不能用 cls.abstract —— 类属性会被子类继承，导致具体类也被跳过。
        if ABC in cls.__bases__ or cls.__dict__.get("abstract", False):
            return
        BaseMusicPlayer._registry.append(cls)

    def __init__(self, cfg: PluginConfig, http: HttpClient):
        self.cfg = cfg
        self.http = http

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseMusicPlayer"]]:
        return list(cls._registry)

    # ------------------------------------------------------------------
    # 子类必须实现：搜索
    # ------------------------------------------------------------------
    @abstractmethod
    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        """把关键词变成候选歌曲列表。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 可选钩子：默认实现够用，需要时子类覆盖
    # ------------------------------------------------------------------
    async def resolve_audio(self, song: Song) -> Song:
        """补全/刷新音频直链的钩子（例如搜索接口不返回 url 时）。

        默认不做事。若你的平台需要额外解析，覆盖它即可。
        """
        return song

    def audio_url_candidates(self, song: Song) -> list[str]:
        """★返回候选音频直链，按优先级排列。

        下载时会依次尝试，第一个「下载成功且确实是音频」的胜出。
        这让单个直链失效/单个端点挂掉时仍能自动恢复。
        """
        return [song.audio_url] if song.audio_url else []

    def audio_headers(self, song: Song) -> dict[str, str]:
        """下载该平台音频时需要的额外请求头（Referer 等）。

        ★很多「下载到 HTML」就是因为缺 Referer/UA。
        """
        return {}

    async def close(self) -> None:
        """释放平台私有资源（共享的 HttpClient 由插件统一关闭）。"""
        return
