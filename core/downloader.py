"""下载器：负责把音频/图片落盘，并在落盘后做内容校验。

关键点：download_audio() 会校验下载结果是不是真音频，
不是就删掉并返回 None —— 从源头上避免「把 HTML 错误页当 mp3 发出去」。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import aiofiles
import aiohttp

from astrbot.api import logger

from .audio import describe_invalid, is_valid_audio
from .config import PluginConfig
from .http import DEFAULT_HEADERS, HttpClient


class Downloader:
    def __init__(self, cfg: PluginConfig, http: HttpClient):
        self.cfg = cfg
        self.http = http
        self.audio_dir = cfg.audio_dir

    async def download_image(self, url: str) -> bytes | None:
        session = await self.http.get_session()
        try:
            async with session.get(url, headers=DEFAULT_HEADERS) as resp:
                if resp.status != 200:
                    logger.warning(f"[downloader] 图片下载失败 HTTP {resp.status}")
                    return None
                return await resp.read()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[downloader] 图片下载异常：{e}")
            return None

    async def download_audio(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        suffix: str = ".mp3",
    ) -> Path | None:
        """流式下载音频并校验内容；失败返回 None。"""
        session = await self.http.get_session()
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        path = self.audio_dir / f"{uuid.uuid4().hex}{suffix}"
        merged = {**DEFAULT_HEADERS, **(headers or {})}

        try:
            async with session.get(url, headers=merged) as resp:
                if resp.status != 200:
                    logger.error(f"[downloader] 音频下载失败 HTTP {resp.status}：{url}")
                    return None
                async with aiofiles.open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        await f.write(chunk)
        except asyncio.TimeoutError:
            logger.error(f"[downloader] 音频下载超时：{url}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"[downloader] 音频下载异常：{e}")
            return None

        # ★内容校验：不是音频就删掉
        if not is_valid_audio(path):
            logger.error(
                f"[downloader] 下载到的不是音频，已丢弃：{describe_invalid(path)}"
            )
            path.unlink(missing_ok=True)
            return None

        logger.debug(f"[downloader] 音频已下载：{path}（{path.stat().st_size} 字节）")
        return path

    async def download_audio_multi(
        self,
        urls: Sequence[str],
        *,
        headers: Mapping[str, str] | None = None,
        suffix: str = ".mp3",
    ) -> Path | None:
        """★依次尝试多个候选直链，返回第一个「下载成功且是真音频」的结果。"""
        for idx, url in enumerate(urls):
            path = await self.download_audio(url, headers=headers, suffix=suffix)
            if path:
                if idx > 0:
                    logger.info(f"[downloader] 已回退到备用音频地址（第 {idx + 1} 个）")
                return path
        logger.error(f"[downloader] {len(urls)} 个候选音频地址全部失败")
        return None

    async def cleanup(self, path: Path | None) -> None:
        """发送完成后清理临时文件。"""
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
