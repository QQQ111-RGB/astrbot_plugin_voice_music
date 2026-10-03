#!/usr/bin/env python3
"""AstrBot 语音点歌链路体检脚本（只用标准库，可在容器内直接跑）。

目的：不依赖插件，直接把「搜索 → 取直链 → 下载 → 校验 → 转码 → AstrBot 转 wav」
这条链路一步步跑一遍，定位到底断在哪一环。

用法（在宿主机执行）：
    # 注意 docker cp 的路径是在**宿主机**上解析的。
    # 如果你是从 Windows 写代码、跑到云服务器上执行，得先把文件传上去：
    #   scp tools/diagnose_in_container.py ubuntu@<服务器IP>:~/diag.py
    docker cp ~/diag.py astrbot:/tmp/
    docker exec -it astrbot python /tmp/diag.py

    不想传文件的话用 tools/probe_min.py，它可以整段粘进容器：
    docker exec -i astrbot python - <<'PY'
    ...probe_min.py 的内容...
    PY

可选参数：
    --endpoints URL [URL ...]   覆盖默认音源端点
    --keyword 晴天              搜索关键词
    --song-url URL              跳过搜索，直接测这个音频直链
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINTS = [
    "https://api.qijieya.cn/meting/",
    "https://api.i-meto.com/meting/api",
]
DEFAULT_KEYWORD = "晴天"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
MAGIC = [
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
    (b"\xff\xfa", "mp3"),
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"RIFF", "wav"),
    (b"#!AMR", "amr"),
    (b"\x02#!S", "silk"),
]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {note}" if note else ""))


def sniff(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            h = f.read(16)
    except OSError:
        return None
    for magic, fmt in MAGIC:
        if h.startswith(magic):
            return fmt
    if len(h) >= 12 and h[4:8] == b"ftyp":
        return "m4a"
    return None


def http_get(url: str, timeout: float = 15, referer: str | None = None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def http_json(url: str, timeout: float = 12):
    with http_get(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoints", nargs="*", default=DEFAULT_ENDPOINTS)
    ap.add_argument("--keyword", default=DEFAULT_KEYWORD)
    ap.add_argument("--song-url", default=None)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    args = ap.parse_args()

    print("=" * 66)
    print("AstrBot 语音链路体检")
    print(f"Python {sys.version.split()[0]} | cwd={os.getcwd()}")
    print(f"是否容器：{'是' if os.path.exists('/.dockerenv') else '否'}")
    print("=" * 66)

    workdir = os.path.join(tempfile.gettempdir(), "astrbot_voice_probe")
    os.makedirs(workdir, exist_ok=True)

    # ---------- 1. ffmpeg ----------
    print("\n--- 1. ffmpeg ---")
    ff = shutil.which(args.ffmpeg) or args.ffmpeg
    print(f"可执行文件：{ff}")
    ff_ok = False
    try:
        p = subprocess.run([ff, "-version"], capture_output=True, timeout=20)
        first = (p.stdout or p.stderr).decode("utf-8", errors="ignore").splitlines()
        print(f"版本：{first[0] if first else '?'}")
        ff_ok = p.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"执行失败：{e}")
    record("ffmpeg 可用", ff_ok)
    if not ff_ok:
        print(">>> ffmpeg 不可用，AstrBot 内部发语音必然失败。到此即可停止排查。")

    # ---------- 2. 缓存目录 ----------
    print("\n--- 2. 临时目录可写性 ---")
    writable = False
    try:
        probe = os.path.join(workdir, ".w")
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        writable = True
    except OSError as e:
        print(f"不可写：{e}")
    if writable:
        du = shutil.disk_usage(workdir)
        print(f"{workdir} 可用，剩余 {du.free // 1024 // 1024} MB")
    record("临时目录可写", writable, workdir)

    # ---------- 3. 音源端点连通性 ----------
    songs: list[dict] = []
    if not args.song_url:
        print("\n--- 3. 音源端点连通性 ---")
        for base in args.endpoints:
            q = urllib.parse.urlencode(
                {"server": "netease", "type": "search", "id": args.keyword}
            )
            url = f"{base}?{q}"
            t0 = time.monotonic()
            try:
                data = http_json(url)
                cost = time.monotonic() - t0
                if isinstance(data, list) and data:
                    print(f"  [OK ] [{cost:.1f}s] {base} -> {len(data)} 条")
                    record(f"端点可用 {base}", True, f"{cost:.1f}s")
                    if not songs:
                        songs = [d for d in data if isinstance(d, dict)]
                else:
                    print(f"  [空 ] [{cost:.1f}s] {base} -> 返回空/异常")
                    record(f"端点可用 {base}", False, "返回空结果")
            except Exception as e:  # noqa: BLE001
                cost = time.monotonic() - t0
                print(f"  [ERR] [{cost:.1f}s] {base} -> {type(e).__name__}: {e}")
                record(f"端点可用 {base}", False, f"{type(e).__name__}")

    # ---------- 4. 取音频直链 ----------
    print("\n--- 4. 音频直链 ---")
    audio_url = args.song_url
    if not audio_url:
        if not songs:
            record("取得音频直链", False, "前面没有搜到歌，无法继续")
            audio_url = None
        else:
            first = songs[0]
            audio_url = first.get("url") or first.get("link")
            name = first.get("name") or first.get("title")
            artist = first.get("artist") or first.get("author")
            print(f"选中的歌：{name} - {artist}")
            print(f"直链：{audio_url}")
            record("取得音频直链", bool(audio_url))
    else:
        print(f"使用指定直链：{audio_url}")

    # ---------- 5. 下载 + 魔数校验 ----------
    print("\n--- 5. 下载 + 内容校验（原版插件最常翻车的地方） ---")
    src_path = None
    if audio_url:
        dst = os.path.join(workdir, "probe_download")
        t0 = time.monotonic()
        try:
            with http_get(audio_url, timeout=60, referer="https://music.163.com/") as r:
                print(f"HTTP {r.status} | Content-Type: {r.headers.get('Content-Type')}")
                final = r.geturl()
                if final != audio_url:
                    print(f"跟随跳转后：{final}")
                total = 0
                with open(dst, "wb") as f:
                    while True:
                        chunk = r.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        f.write(chunk)
            cost = time.monotonic() - t0
            print(f"已下载 {total} 字节，耗时 {cost:.1f}s")
            fmt = sniff(dst)
            if fmt:
                print(f"魔数识别：{fmt}")
                record("下载内容是真音频", True, fmt)
                src_path = dst
            else:
                with open(dst, "rb") as f:
                    head = f.read(160)
                print(f"魔数识别：失败！开头内容 = {head[:120]!r}")
                print(">>> 这就是「点了歌却发出一个文件」的根因：下载到的不是音频。")
                print(">>> 常见原因：直链需要 Referer/UA、已过期、或端点返回了错误页。")
                record("下载内容是真音频", False, "内容不是音频")
        except Exception as e:  # noqa: BLE001
            print(f"下载失败：{type(e).__name__}: {e}")
            print(">>> 直链在当前网络下不可达。若服务器在境外，考虑配 proxy。")
            record("下载内容是真音频", False, f"{type(e).__name__}")
    else:
        record("下载内容是真音频", False, "没有可用的直链")

    # ---------- 6. ffmpeg 转码 ----------
    print("\n--- 6. ffmpeg 转码 ---")
    wav_path = wav8k_path = None
    if src_path and ff_ok:
        # 只测 wav 两档：走 record_local 时 AstrBot 会把 target_format 写死成 wav，
        # 给它 mp3 反而会被重新膨胀成 44.1kHz 立体声 wav（体积涨 5 倍），测了没意义。
        for fmt, out_name, extra in (
            ("wav16k", "probe_voice.wav", ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]),
            ("wav8k", "probe_voice_8k.wav", ["-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le"]),
        ):
            out = os.path.join(workdir, out_name)
            try:
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", src_path]
                cmd += ["-vn"] + extra + [out]
                p = subprocess.run(cmd, capture_output=True, timeout=180)
                if p.returncode == 0 and sniff(out):
                    size = os.path.getsize(out)
                    print(f"  {fmt} -> 成功，{size} 字节")
                    record(f"转码 {fmt}", True, f"{size} 字节")
                    if fmt == "wav16k":
                        wav_path = out
                    else:
                        wav8k_path = out
                else:
                    err = (p.stderr or b"").decode("utf-8", errors="ignore")[:200]
                    print(f"  {fmt} -> 失败：{err}")
                    record(f"转码 {fmt}", False, err[:80])
            except Exception as e:  # noqa: BLE001
                print(f"  {fmt} -> 异常：{e}")
                record(f"转码 {fmt}", False, str(e)[:80])
    else:
        print("  跳过（前一步没拿到音频或 ffmpeg 不可用）")

    # ---------- 7. AstrBot 自己的 MediaResolver（终极验证：这条帧到底多大） ----------
    print("\n--- 7. AstrBot 内部语音转换（Record 实际走的路径） ---")
    # 三个样本一起测，区别就在这里：
    #   原始 mp3          -> AstrBot 会转成 44.1kHz 立体声 wav（体积暴涨 5 倍）
    #   我们转好的 16k wav -> 魔数匹配，直接复用，不再转码
    if src_path or wav_path:
        try:
            sys.path.insert(0, os.getcwd())
            from astrbot.core.utils.media_utils import MediaResolver  # type: ignore

            import asyncio

            async def run_resolver(path: str) -> str:
                resolver = MediaResolver(
                    path, media_type="audio", default_suffix=".wav"
                )
                return await resolver.to_base64(target_format="wav")

            print("  下面每行就是「这一首歌会占多少 WebSocket 帧」：")
            for cand, label in (
                (src_path, "原始 mp3（不预处理）"),
                (wav_path, "本插件默认档 wav16k"),
                (wav8k_path, "本插件省流档 wav8k"),
            ):
                if not cand or not os.path.exists(cand):
                    continue
                try:
                    n = len(asyncio.run(run_resolver(cand)))
                    note = ""
                    if n > 50 * 1024 * 1024:
                        note = "  <-- 超过 NapCat 的 50MB 上限，必然 Max payload size exceeded"
                    elif n > 8 * 1024 * 1024:
                        note = "  <-- 偏大，很多部署在这个量级开始断连/超时"
                    print(f"  {label:20s} {n:>12,} 字符 = {n / 1048576:.1f} MB{note}")
                    record(f"MediaResolver[{label}]", True, f"{n / 1048576:.1f} MB")
                except Exception as e:  # noqa: BLE001
                    print(f"  {label:20s} 失败：{type(e).__name__}: {e}")
                    record(f"MediaResolver[{label}]", False, str(e)[:60])

            print(">>> 如果「原始 mp3」那一行大得离谱、而两行 wav 很小，")
            print(">>> 那 Max payload size exceeded 就是你的根因，且预处理是必须的。")
            print(">>> 反之三行都正常，问题就在协议端配置（maxPayload）或发送顺序上。")
        except ImportError as e:
            print(f"  无法导入 astrbot（需在容器内、AstrBot 工作目录下运行）：{e}")
            record("AstrBot MediaResolver 转换", False, "导入失败")
        except Exception as e:  # noqa: BLE001
            print(f"  转换失败：{type(e).__name__}: {e}")
            if "ffmpeg not found" in str(e):
                print(">>> 报 ffmpeg not found：AstrBot 进程的 PATH 里没有 ffmpeg。")
                print(">>> 注意 docker exec 用的是镜像 ENV 的 PATH，可能与主进程不同。")
            record("AstrBot MediaResolver 转换", False, f"{type(e).__name__}")
    else:
        print("  跳过（没有可用的转码产物）")

    # ---------- 8. 现有插件的配置（最容易忽略的「总是发文件」原因） ----------
    print("\n--- 8. 现有音乐插件的配置 ---")
    cfg_dirs = [
        "/AstrBot/data/config",
        os.path.join(os.getcwd(), "data", "config"),
        os.path.join(os.getcwd(), "AstrBot", "data", "config"),
    ]
    found_cfg = False
    for d in cfg_dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if "music" not in fn.lower() or not fn.endswith(".json"):
                continue
            path = os.path.join(d, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:  # noqa: BLE001
                print(f"  读取 {path} 失败：{e}")
                continue
            found_cfg = True
            sm = cfg.get("send_modes") or []
            rs = cfg.get("record_unsupported") or []
            print(f"  配置文件：{path}")
            print(f"    default_player_name = {cfg.get('default_player_name')}")
            print(f"    send_modes          = {sm}")
            print(f"    record_unsupported  = {rs}")
            print(f"    ark_key 是否填写    = {bool(str(cfg.get('ark_key') or '').strip())}")

            if rs:
                print("    ⚠ record_unsupported 非空 —— 名单里的平台会被直接跳过语音！")
                record("配置：语音未被禁用", False, f"record_unsupported={rs}")
            else:
                record("配置：语音未被禁用", True)

            idx_rec = next(
                (i for i, m in enumerate(sm) if "record" in str(m).lower()), None
            )
            idx_file = next(
                (i for i, m in enumerate(sm) if "file" in str(m).lower()), None
            )
            if idx_rec is None:
                print("    ⚠ send_modes 里完全没有语音档，必然发文件！")
                record("配置：语音档在文件档之前", False, "没有 record 档")
            elif idx_file is not None and idx_file < idx_rec:
                print("    ⚠ 文件档排在语音档前面 —— 这就是「总是发文件」的直接原因")
                record("配置：语音档在文件档之前", False, "file 排在 record 前")
            else:
                print("    OK 语音档排在文件档之前")
                record("配置：语音档在文件档之前", True)
    if not found_cfg:
        print("  未找到音乐插件的配置文件（插件可能未安装，或配置在别处）")

    # ---------- 汇总 ----------
    print("\n" + "=" * 66)
    print("汇总")
    print("=" * 66)
    for name, ok, note in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({note})" if note else ""))
    failed = [n for n, ok, _ in results if not ok]
    print()
    if not failed:
        print("全部通过 —— 音源与转码链路正常，问题应出在协议端或原插件的发送顺序上。")
    else:
        print(f"{len(failed)} 项失败，最先失败的那一项就是根因所在。")
    print(f"\n临时文件保留在：{workdir}（可自行删除）")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
