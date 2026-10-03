"""远程自检 —— 给 Docker / 云服务器部署用。

在服务器上你没法随手开个终端排查，所以把关键状态收敛成一条命令：
发「音乐状态」即可看到 ffmpeg、音源端点、缓存目录、发送策略的真实情况。
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from .audio import FfmpegInfo, human_size, resolve_voice_format
from .config import PLUGIN_NAME, PluginConfig
from .http import HttpClient
from .platform import BaseMusicPlayer

_PROBE_TIMEOUT = 6.0
_PROBE_KEYWORD = "晴天"


def _container_hint() -> str:
    """粗略判断当前是否跑在容器里。"""
    if Path("/.dockerenv").exists():
        return "是（检测到 /.dockerenv）"
    if Path("/proc/1/cgroup").exists():
        try:
            if "docker" in Path("/proc/1/cgroup").read_text(errors="ignore"):
                return "是（cgroup 含 docker）"
        except OSError:
            pass
    return "否"


def _disk_hint(path: Path) -> str:
    try:
        usage = shutil.disk_usage(str(path))
        return f"剩余 {usage.free / 1024 / 1024:.0f} MB / 共 {usage.total / 1024 / 1024:.0f} MB"
    except OSError as e:
        return f"无法读取磁盘信息：{e}"


_WAV_BYTES_PER_SECOND = {"wav": 32_000, "wav8k": 16_000}


def _bytes_per_second_note(fmt: str) -> str:
    """把档位换算成「每秒多少线上体积」，这是判断会不会被拒收的最直观数字。"""
    real = resolve_voice_format(fmt)
    raw = _WAV_BYTES_PER_SECOND.get(real)
    if raw is None:  # mp3：走 record_local 时会被 AstrBot 膨胀，没有稳定值
        return "取决于 AstrBot 二次转码（走 record_local 时 ≈ 235 KB/s，很大）"
    b64 = int(raw * 4 / 3)
    return f"约 {human_size(b64)}/s（约 4 分钟的歌 = {human_size(b64 * 240)}）"


async def _probe_endpoint(
    http: HttpClient, base: str
) -> tuple[str, bool, float, str]:
    t0 = time.monotonic()
    data = await http.request_json(
        base,
        params={"server": "netease", "type": "search", "id": _PROBE_KEYWORD},
        retries=0,
        read_timeout=_PROBE_TIMEOUT,
    )
    cost = time.monotonic() - t0
    if isinstance(data, list) and data:
        return base, True, cost, f"可用（返回 {len(data)} 条）"
    if data is None:
        return base, False, cost, "无响应 / 非 JSON（超时或返回了 HTML）"
    return base, False, cost, "返回空结果（该端点可能不支持此音源）"


async def build_report(
    cfg: PluginConfig,
    players: list[BaseMusicPlayer],
    ffmpeg: FfmpegInfo,
    http: HttpClient,
) -> str:
    lines: list[str] = [f"【{PLUGIN_NAME} 自检报告】"]

    # ---------- 运行环境 ----------
    lines.append("")
    lines.append("· 运行环境")
    lines.append(f"  Python {sys.version.split()[0]} / {platform.system()} {platform.machine()}")
    lines.append(f"  容器：{_container_hint()}")
    lines.append(f"  工作目录：{os.getcwd()}")
    if os.environ.get("ASTRBOT_ROOT"):
        lines.append(f"  ASTRBOT_ROOT={os.environ['ASTRBOT_ROOT']}")

    # ---------- ffmpeg（语音链路的关键） ----------
    lines.append("")
    lines.append("· ffmpeg（发语音的必经环节）")
    lines.append(f"  {ffmpeg.summary()}")
    lines.append(f"  配置的 voice_format={cfg.voice_format}，实际将输出 {resolve_voice_format(cfg.voice_format)}")
    if not ffmpeg.available:
        lines.append("  ⚠ 结论：语音发不出去。AstrBot 发语音时会执行裸命令 ffmpeg，")
        lines.append("    必须在 PATH 上可解析。见 README 的「Docker 部署」一节。")
    elif not ffmpeg.on_path:
        lines.append("  ⚠ 插件这层能用，但 AstrBot 内部的 `ffmpeg` 调用会失败。")

    # ---------- 缓存目录 ----------
    lines.append("")
    lines.append("· 音频缓存目录")
    lines.append(f"  {cfg.audio_dir}")
    writable = False
    try:
        cfg.audio_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.audio_dir / ".write_test"
        probe.write_bytes(b"ok")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    lines.append(f"  可写：{'是' if writable else '否 ✗'} | {_disk_hint(cfg.audio_dir)}")

    # ---------- 发送策略 ----------
    lines.append("")
    lines.append("· 发送策略")
    lines.append(f"  send_modes={cfg.send_modes}")
    lines.append(f"  voice_strict={cfg.voice_strict}（true=语音失败不降级成文件）")
    lines.append(f"  record_unsupported={cfg.record_unsupported or '（空）'}")
    lines.append(f"  selection_timeout={cfg.selection_timeout}s / song_limit={cfg.song_limit}")
    lines.append(f"  proxy={cfg.proxy or '（直连）'}")

    lines.append("")
    lines.append("· 语音体积（决定「超时后变成文件」的那一环）")
    lines.append(
        f"  voice_format={cfg.voice_format} -> 实际 {resolve_voice_format(cfg.voice_format)}"
    )
    limit = cfg.max_payload_bytes
    lines.append(
        f"  单帧上限 max_payload_bytes={human_size(limit)}"
        + ("" if limit else "（不限制）")
    )
    lines.append(f"  预计每秒线上体积：{_bytes_per_second_note(cfg.voice_format)}")
    if "record_link" not in cfg.send_modes:
        lines.append(
            "  ⚠ send_modes 里没有 record_link：本地语音要把整首歌塞进一帧，"
        )
        lines.append(
            "    很容易触发协议端的 Max payload size exceeded，然后降级成发文件。"
        )

    # ---------- 音源 ----------
    lines.append("")
    lines.append("· 已注册音源")
    if players:
        for p in players:
            lines.append(f"  - {p.platform.display_name}（关键词：{'、'.join(p.platform.keywords)}）")
    else:
        lines.append("  ✗ 没有音源被注册！")

    # ---------- 端点连通性（并行探测） ----------
    lines.append("")
    lines.append(f"· 音源端点连通性（用「{_PROBE_KEYWORD}」探测，超时 {_PROBE_TIMEOUT:.0f}s）")
    endpoints = cfg.source_endpoints
    if not endpoints:
        lines.append("  ✗ source_endpoints 为空，点歌一定失败")
    else:
        results = await _probe_many(http, endpoints)
        for base, ok, cost, note in results:
            mark = "✓" if ok else "✗"
            lines.append(f"  {mark} [{cost:.1f}s] {base} —— {note}")
        # ★这里探的是 search，而 search 在自建 Meting-API 上是**免鉴权**的。
        #   所以「端点全部 ✓」并不代表取直链也没问题 —— 敏感接口还要过 auth 校验。
        #   不把这条写出来，用户会以为端点全绿就等于能放歌。
        if cfg.meting_token:
            lines.append("  （已配置 meting_token：取直链会自动附 auth=HMAC-SHA1 签名）")
        else:
            lines.append(
                "  （未配置 meting_token：仅适合免鉴权的公共端点；"
                "若 source_endpoints 是自建 Meting-API，取直链会 401）"
            )

    lines.append("")
    lines.append("· 结论")
    lines.append(f"  {_verdict(players, endpoints, ffmpeg)}")
    return "\n".join(lines)


async def _probe_many(
    http: HttpClient, endpoints: list[str]
) -> list[tuple[str, bool, float, str]]:
    return await asyncio.gather(*(_probe_endpoint(http, e) for e in endpoints))


def _verdict(
    players: list[BaseMusicPlayer], endpoints: list[str], ffmpeg: FfmpegInfo
) -> str:
    problems: list[str] = []
    if not players:
        problems.append("没有可用音源")
    if not endpoints:
        problems.append("没有配置音源端点")
    if not ffmpeg.available:
        problems.append("ffmpeg 不可用（语音必失败）")
    elif not ffmpeg.on_path:
        problems.append("ffmpeg 未在 PATH 上（AstrBot 内部会失败）")
    if problems:
        return "存在问题：" + "；".join(problems)
    return "关键依赖齐备，可以直接点歌"
