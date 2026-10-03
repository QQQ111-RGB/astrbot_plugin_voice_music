"""统一 HTTP 客户端 —— 骨架里解决「总是超时」的核心。

为什么原版会「卡很久才超时」：
    aiohttp.ClientSession() 默认的总超时是 300 秒（5 分钟）。
    公共音源域名一旦被墙/被限流/返回半截响应，请求就会一直挂着，
    表现为「发了点歌没反应，很久之后才报超时」。

这里做了三件事：
    1. 显式设置 connect / read / total 三级超时（默认秒级），
       任何一步超时都会立刻中断，而不是等满 5 分钟。
    2. 有限次重试 + 指数退避，抖动导致的偶发失败能自愈。
    3. request_json_multi()：多端点自动回退 —— 一个域名挂了立刻换下一个。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

from astrbot.api import logger

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


class HttpClient:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        connect_timeout: float = 3.0,
        read_timeout: float = 8.0,
        retries: int = 2,
        backoff: float = 0.5,
        ssl_verify: bool = False,
    ) -> None:
        self.proxy = proxy or None
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retries = retries
        self.backoff = backoff
        self.ssl_verify = ssl_verify
        self._session: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.read_timeout * 2,
                connect=self.connect_timeout,
                sock_read=self.read_timeout,
            )
            connector = aiohttp.TCPConnector(
                limit=32, ttl_dns_cache=300, ssl=self.ssl_verify
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout, connector=connector, proxy=self.proxy
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # 底层：带超时 + 重试的单次请求，失败返回 None（不抛异常，方便降级）
    # ------------------------------------------------------------------
    async def request_text(
        self,
        url: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        retries: int | None = None,
        read_timeout: float | None = None,
    ) -> str | None:
        session = await self.get_session()
        attempts = (self.retries if retries is None else retries) + 1
        merged_headers = {**DEFAULT_HEADERS, **(headers or {})}
        last_err: str = "unknown"

        for i in range(attempts):
            try:
                extra: dict[str, Any] = {}
                if read_timeout is not None:
                    extra["timeout"] = aiohttp.ClientTimeout(
                        total=read_timeout * 2,
                        connect=self.connect_timeout,
                        sock_read=read_timeout,
                    )
                async with session.request(
                    method.upper(),
                    url,
                    params=params,
                    data=data,
                    headers=merged_headers,
                    cookies=cookies,
                    **extra,
                ) as resp:
                    text = await resp.text(errors="ignore")
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        logger.warning(
                            f"[http] {resp.status} {url} -> {text[:120]!r}"
                        )
                        break  # 4xx/5xx 重试意义不大，直接换端点
                    return text
            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as e:
                last_err = f"超时: {type(e).__name__}"
                logger.warning(f"[http] 超时 ({i + 1}/{attempts}) {url}")
            except aiohttp.ClientError as e:
                last_err = f"网络错误: {e}"
                logger.warning(f"[http] 网络错误 ({i + 1}/{attempts}) {url}: {e}")
            except Exception as e:  # noqa: BLE001
                last_err = f"未知错误: {e}"
                logger.warning(f"[http] 异常 ({i + 1}/{attempts}) {url}: {e}")

            if i < attempts - 1:
                await asyncio.sleep(self.backoff * (2**i))

        logger.debug(f"[http] 放弃 {url}（{last_err}）")
        return None

    # ------------------------------------------------------------------
    # 中层：解析 JSON
    # ------------------------------------------------------------------
    async def request_json(self, url: str, **kwargs: Any) -> Any | None:
        text = await self.request_text(url, **kwargs)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"[http] 非 JSON 响应：{url} -> {text[:120]!r}")
            return None

    # ------------------------------------------------------------------
    # 上层：★多端点自动回退 —— 一个挂了立刻换下一个
    # ------------------------------------------------------------------
    async def request_json_multi(
        self, urls: Sequence[str], **kwargs: Any
    ) -> Any | None:
        for idx, url in enumerate(urls):
            data = await self.request_json(url, **kwargs)
            if data is not None:
                if idx > 0:
                    logger.info(f"[http] 已回退到备用端点（第 {idx + 1} 个）：{url}")
                return data
        logger.error(f"[http] {len(urls)} 个端点全部失败")
        return None
