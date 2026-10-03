"""骨架自测：用桩件模拟 AstrBot，跑通真实链路。

验证点：
  1. 多端点自动回退（第一个端点故意是坏的，必须快速失败并换到下一个）
  2. 音源搜索 + 解析
  3. 音频下载 + 魔数校验（真音频放行）
  4. 反例：HTML 页面必须被拒绝（这正是原版「把错误页当 mp3 发出去」的根因）
  5. ffmpeg 可用性探测 + 转码
  6. 自检报告
  7. 体积闸门：超限时必须拒绝发送并说明原因（而不是发出去撞协议端上限）
  8. 自动降档：16k wav 超限时自动降到 8k 重转后仍能发出

第 7、8 条针对的是「总是超时 + 最后发出来是个文件」这个症状：
本地语音会被 AstrBot 转成 wav 再 base64 塞进**一条** WebSocket 帧，
超限时协议端断开连接 -> AstrBot 等不到回包报超时 -> 降级发文件。
"""

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import types
from pathlib import Path

# .selftest/ 就在插件目录里，所以上一级就是插件根目录。
# 这样自测不依赖任何绝对路径，clone 下来就能跑。
PLUGIN = Path(__file__).resolve().parents[1]

# 测试产生的音频写进系统临时目录，不污染仓库。
TEST_TMP = Path(tempfile.gettempdir()) / "wb_voice_music_selftest"


def _pkg(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = m
    return m


def install_stubs() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    make_logger = logging.getLogger("astrbot")

    astrbot = _pkg("astrbot")
    api = _pkg("astrbot.api")
    api.logger = make_logger  # type: ignore[attr-defined]
    astrbot.api = api  # type: ignore[attr-defined]

    # astrbot.api.event
    event_mod = _pkg("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401
        """占位：本测试不涉及事件流。"""

    class _Filter:
        class EventMessageType:
            ALL = "all"

    event_mod.AstrMessageEvent = AstrMessageEvent  # type: ignore[attr-defined]
    event_mod.filter = _Filter()  # type: ignore[attr-defined]
    sys.modules["astrbot.api.event"] = event_mod

    # astrbot.api.star
    star_mod = _pkg("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    star_mod.Star = Star  # type: ignore[attr-defined]
    star_mod.Context = object  # type: ignore[attr-defined]

    # astrbot.core.*
    core = _pkg("astrbot.core")
    astrbot.core = core  # type: ignore[attr-defined]

    cfg_pkg = _pkg("astrbot.core.config")
    cfg_mod = _pkg("astrbot.core.config.astrbot_config")

    class AstrBotConfig(dict):
        def save_config(self):
            pass

    cfg_mod.AstrBotConfig = AstrBotConfig  # type: ignore[attr-defined]
    cfg_pkg.astrbot_config = cfg_mod  # type: ignore[attr-defined]

    star_pkg = _pkg("astrbot.core.star")
    ctx_mod = _pkg("astrbot.core.star.context")
    ctx_mod.Context = object  # type: ignore[attr-defined]
    star_pkg.context = ctx_mod  # type: ignore[attr-defined]

    msg_pkg = _pkg("astrbot.core.message")
    comp_mod = _pkg("astrbot.core.message.components")

    class _Comp:
        def __init__(self, **kw):
            self.kw = kw

        @classmethod
        def fromFileSystem(cls, path):
            return cls(file=path)

        @classmethod
        def fromURL(cls, url):
            return cls(url=url)

    comp_mod.Record = type("Record", (_Comp,), {})  # type: ignore[attr-defined]
    comp_mod.File = type("File", (_Comp,), {})  # type: ignore[attr-defined]
    comp_mod.Image = type("Image", (_Comp,), {})  # type: ignore[attr-defined]
    comp_mod.Plain = type("Plain", (_Comp,), {})  # type: ignore[attr-defined]
    msg_pkg.components = comp_mod  # type: ignore[attr-defined]

    utils_pkg = _pkg("astrbot.core.utils")
    sw_mod = _pkg("astrbot.core.utils.session_waiter")
    sw_mod.SessionController = object  # type: ignore[attr-defined]

    def session_waiter(**kw):
        def deco(fn):
            return fn

        return deco

    sw_mod.session_waiter = session_waiter  # type: ignore[attr-defined]
    utils_pkg.session_waiter = sw_mod  # type: ignore[attr-defined]

    path_mod = _pkg("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_temp_path = lambda: str(TEST_TMP)  # type: ignore[attr-defined]
    path_mod.get_astrbot_plugin_path = lambda: str(PLUGIN)  # type: ignore[attr-defined]


async def main() -> int:
    install_stubs()
    sys.path.insert(0, str(PLUGIN))

    from core.audio import (
        ensure_ffmpeg_on_path,
        estimate_voice_payload,
        is_valid_audio,
        resolve_voice_format,
        to_voice,
    )
    from core.config import PluginConfig
    from core.sender import MusicSender
    from core.diagnose import build_report
    from core.downloader import Downloader
    from core.http import HttpClient
    from core.platform import BaseMusicPlayer, NeteaseMeting, NeteaseWeb

    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    raw = {k: v.get("default") for k, v in schema.items()}

    # 故意把第一个端点设成必坏的地址，验证「快速失败 + 回退」
    raw["source_endpoints"] = [
        "http://127.0.0.1:9/dead-endpoint",
        "https://api.qijieya.cn/meting/",
    ]
    raw["request_timeout"] = 5

    ok = True

    cfg = PluginConfig(raw, None)
    http = HttpClient(
        proxy=cfg.proxy, read_timeout=cfg.request_timeout, retries=cfg.request_retries
    )
    dl = Downloader(cfg, http)

    print("\n" + "=" * 62)
    print(f"注册的音源类 : {[c.__name__ for c in BaseMusicPlayer.get_all_subclass()]}")
    print(f"端点列表     : {cfg.source_endpoints}")
    print("=" * 62)

    # --- 1. 多端点回退 + 搜索 ---
    player = NeteaseMeting(cfg, http)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    songs = await player.fetch_songs("晴天", limit=3)
    cost = loop.time() - t0
    print(f"\n[1] 搜索（含坏端点回退）耗时 {cost:.2f}s，返回 {len(songs)} 首")
    for s in songs:
        print(f"    - {s.display_name()}")
    if not songs:
        print("    ✗ 搜索失败")
        ok = False
    elif cost > 20:
        print("    ✗ 回退太慢，说明超时没生效")
        ok = False
    else:
        print("    ✓ 回退生效，未长时间挂起")

    # --- 2. 下载 + 魔数校验 ---
    if songs:
        path = await dl.download_audio(
            songs[0].audio_url, headers=player.audio_headers(songs[0])
        )
        if path:
            print(f"\n[2] 下载成功 {path.name}（{path.stat().st_size} 字节）")
            print(f"    ✓ 校验为有效音频：{is_valid_audio(path)}")
        else:
            print("\n[2] ✗ 下载失败")
            ok = False

    # --- 3. 反例：HTML 必须被拒绝 ---
    bad = await dl.download_audio("https://www.baidu.com")
    print(f"\n[3] 下载 HTML 的返回值：{bad}（应为 None）")
    if bad is None:
        print("    ✓ 非音频内容已被拦截，不会再被当成 mp3 发出去")
    else:
        print("    ✗ 校验失效！")
        ok = False

    # --- 4. ffmpeg：PATH 垫片 + 转码 ---
    shutil_which_before = shutil.which("ffmpeg")
    print(f"\n[4] 垫片前 shutil.which('ffmpeg') = {shutil_which_before}")
    info = await ensure_ffmpeg_on_path(cfg.temp_dir, cfg.ffmpeg_path)
    print(f"    FFmpegInfo.available = {info.available}")
    print(f"    FFmpegInfo.source    = {info.source}")
    print(f"    FFmpegInfo.on_path   = {info.on_path}")
    print(f"    FFmpegInfo.exe       = {info.exe}")
    print(f"    summary: {info.summary()}")
    after = shutil.which("ffmpeg")
    print(f"    垫片后 shutil.which('ffmpeg') = {after}")
    if info.available and info.on_path and after:
        print("    ✓ AstrBot 内部的裸 `ffmpeg` 调用现在也能解析到了")
    else:
        print("    ✗ PATH 垫片未生效（AstrBot 内部发语音仍会失败）")
        ok = False

    if songs:
        # auto 模式：必须是 wav，不能是 mp3。
        # 走 record_local 时 AstrBot 会把 target_format 写死成 wav，
        # 给它 mp3 只会被重新膨胀成 44.1kHz 立体声 wav（体积涨 5 倍）。
        auto_fmt = resolve_voice_format("auto")
        print(f"    resolve_voice_format('auto') = {auto_fmt}（期望 wav）")
        if auto_fmt != "wav":
            print("    ✗ auto 档位不对，会导致线上体积被 AstrBot 放大")
            ok = False
        voice = await to_voice(
            Path(path), cfg.audio_dir, ffmpeg=cfg.ffmpeg_path, fmt="auto"
        )
        print(f"    auto 转码结果：{voice.name if voice else None}")
        # 强制 wav —— 容器里没有 ffmpeg 时靠它绕过 AstrBot 的 ffmpeg 调用
        wav = await to_voice(
            Path(path), cfg.audio_dir, ffmpeg=cfg.ffmpeg_path, fmt="wav"
        )
        print(f"    wav 转码结果：{wav.name if wav else None}")
        if voice and wav and wav.suffix == ".wav":
            print("    ✓ 两条转码路径都可用（wav 可绕过 AstrBot 的 ffmpeg 依赖）")
        else:
            print("    ✗ 转码失败")
            ok = False

    # --- 5. 网易web 音源 + 候选直链 ---
    web = NeteaseWeb(cfg, http)
    web_songs = await web.fetch_songs("晴天", limit=1)
    if web_songs:
        cands = web.audio_url_candidates(web_songs[0])
        print(f"\n[5] 网易web：{web_songs[0].display_name()}")
        print(f"    候选直链 {len(cands)} 条，首条：{cands[0][:70] if cands else '无'}")
        if cands:
            print("    ✓ audio_url_candidates 生效（含多端点回退）")
        else:
            print("    ✗ 没有候选直链")
            ok = False
        # 用候选地址真下载一次，验证「第一条是坏端点也能回退」
        got = await dl.download_audio_multi(
            cands, headers=web.audio_headers(web_songs[0])
        )
        print(f"    候选地址下载结果：{got.name if got else None}")
        if got:
            print("    ✓ 候选地址回退下载成功")
        else:
            print("    ✗ 候选地址全部失败")
            ok = False
    else:
        print("\n[5] ✗ 网易web 搜索失败")
        ok = False

    # --- 6. 自检报告 ---
    print("\n[6] build_report() 输出：")
    report = await build_report(cfg, [player, web], info, http)
    print("-" * 62)
    print(report)
    print("-" * 62)
    if "自检报告" in report and "ffmpeg" in report and "结论" in report:
        print("    ✓ 自检报告生成正常")
    else:
        print("    ✗ 自检报告异常")
        ok = False

    # --- 7/8. 体积闸门 + 自动降档 ---
    class _FakeEvent:
        """最小事件桩：只需要 send / chain_result / plain_result。"""

        def __init__(self):
            self.sent: list = []

        def get_platform_name(self):
            return "aiocqhttp"

        def get_sender_name(self):
            return "tester"

        def chain_result(self, comps):
            return comps

        def plain_result(self, text):
            return text

        async def send(self, payload):
            self.sent.append(payload)

    if songs and wav:
        payload, readable = estimate_voice_payload(wav)
        print(f"\n[7] 体积换算：{wav.name} = {wav.stat().st_size} 字节")
        print(f"    预计线上 base64 体积 = {readable}（{payload} 字节）")

        # 7a. 上限设成 1KB -> 必须被闸门拦下，且不能真的发出去
        strict_raw = dict(raw)
        strict_raw["max_payload_bytes"] = 1024
        strict_raw["voice_format"] = "wav8k"  # 关掉自动降档，单测闸门本身
        strict_cfg = PluginConfig(strict_raw, None)
        ev = _FakeEvent()
        reason = await MusicSender(strict_cfg, dl)._record_local(ev, player, songs[0])
        print(f"    上限 1KB 时的返回：{(reason or 'None（竟然发出去了）')[:80]}")
        if reason and "超限" in reason and not any(
            getattr(c, "kw", {}).get("file") for c in ev.sent
        ):
            print("    ✓ 超限被拦下并说明了原因（不会再撞协议端上限后降级发文件）")
        else:
            print("    ✗ 体积闸门失效")
            ok = False

        # 7b. 上限卡在 8k 与 16k 之间 -> 应自动降到 wav8k 后成功发出
        wav8k_probe = await to_voice(
            Path(path), cfg.audio_dir, ffmpeg=cfg.ffmpeg_path, fmt="wav8k"
        )
        if wav8k_probe:
            p8, r8 = estimate_voice_payload(wav8k_probe)
            limit = int(payload * 0.75) if payload > p8 else int(p8 * 1.5)
            downgrade_raw = dict(raw)
            downgrade_raw["max_payload_bytes"] = limit
            downgrade_raw["voice_format"] = "auto"
            ev2 = _FakeEvent()
            reason2 = await MusicSender(
                PluginConfig(downgrade_raw, None), dl
            )._record_local(ev2, player, songs[0])
            print(f"\n[8] 16k 预计 {readable}，8k 预计 {r8}，上限设为 {limit} 字节")
            print(f"    返回值：{reason2 or 'None（发送成功）'}")
            if reason2 is None and ev2.sent:
                print("    ✓ 自动降档到 wav8k 后成功发出语音")
            else:
                print("    ✗ 自动降档未生效")
                ok = False

    await http.close()
    print("\n" + "=" * 62)
    print("自测结果：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
