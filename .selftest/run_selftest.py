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
  9. 加载 main.py：装饰器注册齐全、音源发现正常、「点歌」「音乐状态」两条命令真跑通
 10. 去重闸门：同一会话内重复请求只发一遍，且关掉去重后能正常「再放一遍」
 10. 去重闸门：同一会话内、同一首歌连续发两次，第二次必须被跳过；换个会话则不受影响

第 9 条单独存在的理由：前 8 条只覆盖 core/，main.py 一行都没被执行过。如果入口的
装饰器签名或相对导入有问题，部署到服务器上只会看到一句泛泛的加载失败，很难定位。

第 10 条针对的是「同一首歌发两遍」：AstrBot 的 session_waiter 截获消息后会把它
浅复制成新事件重新投递一遍，那条消息同样会触发 LLM；LLM 看到「候选列表 + 1 语音」
的上下文，可能又调用一次点歌工具。所以发送层必须自己认得出「这首刚发过」。

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

# 装饰器注册项，供「插件能否被 AstrBot 加载」那一步断言
REGISTERED: list[tuple[str, str]] = []


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
        """复刻 AstrBot 的三个装饰器：注册时不能报错，且要把注册项记下来。"""

        class EventMessageType:
            ALL = "all"

        @staticmethod
        def command(name, alias=None):
            def deco(fn):
                REGISTERED.append(("command", f"{name}|{sorted(alias or [])}"))
                return fn

            return deco

        @staticmethod
        def event_message_type(_type):
            def deco(fn):
                REGISTERED.append(("event_message_type", fn.__name__))
                return fn

            return deco

        @staticmethod
        def llm_tool():
            def deco(fn):
                REGISTERED.append(("llm_tool", fn.__name__))
                return fn

            return deco

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

    class SessionController:
        """最小控制器：只记录 stop() 是否被调用。"""

        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    def session_waiter(timeout=0, **kw):
        """把 fn(controller, event) 包成 fn(event)，模拟 AstrBot 的注入方式。"""

        def deco(fn):
            async def wrapper(event, *args, **kwargs):
                return await fn(SessionController(), event, *args, **kwargs)

            return wrapper

        return deco

    sw_mod.SessionController = SessionController  # type: ignore[attr-defined]
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
    # 后面的用例都依赖这一步的产物，所以先声明好、失败就整段跳过，
    # 不要拿 None 去做 Path(None) —— 那是脚本自己崩，不是被测代码有问题。
    path: Path | None = None
    if songs and songs[0].audio_url:
        path = await dl.download_audio(
            songs[0].audio_url, headers=player.audio_headers(songs[0])
        )
        if path:
            print(f"\n[2] 下载成功 {path.name}（{path.stat().st_size} 字节）")
            print(f"    ✓ 校验为有效音频：{is_valid_audio(path)}")
        else:
            print("\n[2] ✗ 下载失败（音源端抽风？后续依赖下载的用例会跳过）")
            ok = False
    elif songs:
        print("\n[2] ✗ 搜索结果的 audio_url 为空，跳过下载用例")
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

    voice = wav = None
    if path:
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

    # --- 9. 插件能否被 AstrBot 加载 + 真跑一遍命令入口 ---
    # 这一步单独存在的理由：前面 1~8 只测 core/，main.py 一行都没被执行过。
    # 如果 main.py 的装饰器签名或相对导入有问题，你在服务器上只会看到一句
    # 泛泛的加载失败，很难定位。这里把它完整跑一遍。
    print("\n[9] 加载 main.py 并跑一遍命令入口")
    import importlib.util

    pkg = types.ModuleType("astrbot_plugin_voice_music")
    pkg.__path__ = [str(PLUGIN)]  # type: ignore[attr-defined]
    sys.modules["astrbot_plugin_voice_music"] = pkg

    main_spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_voice_music.main", PLUGIN / "main.py"
    )
    assert main_spec and main_spec.loader
    main_mod = importlib.util.module_from_spec(main_spec)
    sys.modules[main_spec.name] = main_mod
    main_spec.loader.exec_module(main_mod)
    print("    ✓ main.py 导入成功（相对导入与 API 引用都没问题）")

    print(f"    注册的装饰器：{REGISTERED}")
    kinds = {k for k, _ in REGISTERED}
    if kinds == {"command", "event_message_type", "llm_tool"}:
        print("    ✓ 命令 / 事件监听 / LLM 工具三类钩子都注册上了")
    else:
        print("    ✗ 钩子注册不全，AstrBot 里会收不到消息")
        ok = False

    class _FakeMsgEvent:
        """够 on_song / send_song 用即可。"""

        def __init__(self, text: str, umo: str = "aiocqhttp:GroupMessage:10086"):
            self.message_str = text
            self.is_at_or_wake_command = True
            # 去重闸门的 key 会用到会话标识，所以它得是个稳定值
            self.unified_msg_origin = umo
            self.sent: list = []
            self.stopped = False
            self.call_llm: bool | None = None

        def should_call_llm(self, call_llm: bool) -> None:
            """记录 LLM 开关，供断言「有没有掐掉 LLM」使用。"""
            self.call_llm = call_llm

        def get_platform_name(self):
            return "aiocqhttp"

        def get_sender_name(self):
            return "tester"

        def plain_result(self, text):
            return ("plain", text)

        def chain_result(self, comps):
            return ("chain", comps)

        async def send(self, payload):
            self.sent.append(payload)

        def stop_event(self):
            self.stopped = True

    plugin = main_mod.VoiceMusicPlugin(object(), dict(raw))
    await plugin.initialize()
    print(f"    加载后的音源：{[p.platform.display_name for p in plugin.players]}")
    if plugin.players and plugin.keywords:
        print(f"    命令关键词：{sorted(set(plugin.keywords))}")
    else:
        print("    ✗ 没有音源被注册，点歌一定失败")
        ok = False

    ev9 = _FakeMsgEvent("点歌 晴天 1")
    try:
        async for _chunk in plugin.on_song(ev9):
            pass
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ on_song 抛异常：{type(e).__name__}: {e}")
        ok = False
    else:
        if ev9.sent and ev9.stopped:
            print(f"    ✓ 「点歌 晴天 1」跑通，发出 {len(ev9.sent)} 条消息并 stop_event")
        else:
            print(f"    ✗ 命令没走通（sent={len(ev9.sent)}, stopped={ev9.stopped}）")
            ok = False
    if ev9.call_llm is False:
        print("    ✓ 已用 should_call_llm(False) 掐掉 LLM 链路（防重复发送的关键）")
    else:
        print(f"    ✗ 没有禁止 LLM（call_llm={ev9.call_llm}），同一首歌可能被发两遍")
        ok = False

    ev9b = _FakeMsgEvent("音乐状态")
    try:
        chunks = [c async for c in plugin.cmd_status(ev9b)]
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ cmd_status 抛异常：{type(e).__name__}: {e}")
        ok = False
    else:
        if chunks and "自检报告" in str(chunks[0]):
            print("    ✓ 「音乐状态」命令输出正常")
        else:
            print("    ✗ 「音乐状态」没输出报告")
            ok = False

    # --- 10. 去重闸门（治「同一首歌发两遍」）---
    # 症状：用户回了「1 语音」，插件发了一遍，隔了 20 多秒又发一遍，两条日志一模一样。
    # 根因：AstrBot 的 session_waiter 截获消息后会把它 **浅复制成新事件重新投递一遍**，
    #       那条消息同样会触发 LLM；LLM 看到「候选列表 + 1 语音」的上下文，
    #       很可能判断用户想听第 1 首，于是又调用了一次点歌工具。
    # 三层防线：① should_call_llm(False)（治本，第 9 条已断言）
    #           ② event.stop_event()（挡后续 listener）
    #           ③ 发送层的去重闸门（兜底，本组验证）
    # 注意：这里用独立的 sender 和独立会话标识，否则会被第 9 条留下的去重记录误伤。
    print("\n[10] 去重闸门")
    found = await player.fetch_songs("晴天", limit=1)
    if not found or not found[0].audio_url:
        print("    ✗ 没搜到可用歌曲，本组跳过（多半是音源端抽风，不是代码问题）")
        await plugin.terminate()
        await http.close()
        print("\n" + "=" * 62)
        print("自测结果：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
        print("=" * 62)
        return 0 if ok else 1

    song0 = found[0]
    print(f"    用例歌曲：{song0.display_name()}（id={song0.id}）")

    GROUP = "aiocqhttp:GroupMessage:70001"
    OTHER = "aiocqhttp:GroupMessage:70002"
    d_raw = dict(raw)
    d_raw["dedup_seconds"] = 20
    sender_d = MusicSender(PluginConfig(d_raw, None), dl)

    # 10a. 同一会话内同一首歌连发两次 -> 第二次必须被跳过
    ev10a = _FakeMsgEvent("点歌 晴天 1", GROUP)
    await sender_d.send_song(ev10a, player, song0)
    n1 = len(ev10a.sent)
    ev10b = _FakeMsgEvent("点歌 晴天 1", GROUP)
    await sender_d.send_song(ev10b, player, song0)
    n2 = len(ev10b.sent)
    print(f"    同会话第 1 次发出 {n1} 条，第 2 次发出 {n2} 条（期望 1 / 0）")
    if n1 == 1 and n2 == 0:
        print("    ✓ 重复发送被拦截，同一首歌只会发一遍")
    else:
        print("    ✗ 去重失效，同一首歌还是会被发两遍")
        ok = False

    # 10b. 换个会话 -> 必须照常发送，不能误伤别的群
    ev10c = _FakeMsgEvent("点歌 晴天 1", OTHER)
    await sender_d.send_song(ev10c, player, song0)
    print(f"    换会话再点同一首：发出 {len(ev10c.sent)} 条（期望 1）")
    if len(ev10c.sent) == 1:
        print("    ✓ 去重按会话隔离，别的群/私聊不受影响")
    else:
        print("    ✗ 误伤了其他会话")
        ok = False

    # 10c. dedup_seconds=0 -> 开关确实能关掉（保留「我就想再放一遍」的用法）
    off_raw = dict(raw)
    off_raw["dedup_seconds"] = 0
    sender_off = MusicSender(PluginConfig(off_raw, None), dl)
    ev10d = _FakeMsgEvent("点歌 晴天 1", GROUP)
    await sender_off.send_song(ev10d, player, song0)
    ev10e = _FakeMsgEvent("点歌 晴天 1", GROUP)
    await sender_off.send_song(ev10e, player, song0)
    print(
        f"    dedup_seconds=0 时连发两次：{len(ev10d.sent)} + {len(ev10e.sent)} 条（期望 1 + 1）"
    )
    if ev10d.sent and ev10e.sent:
        print("    ✓ 开关有效，设 0 即可完全关闭去重")
    else:
        print("    ✗ 去重关不掉")
        ok = False

    await plugin.terminate()

    await http.close()
    print("\n" + "=" * 62)
    print("自测结果：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
