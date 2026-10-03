#!/usr/bin/env python3
"""极简体检：可以直接粘贴进容器跑，不需要先传文件。

用法（在宿主机）：
    docker exec -i astrbot python - <<'PY'
    ...本文件内容...
    PY

或者先传进去：
    docker cp probe_min.py astrbot:/tmp/ && docker exec -it astrbot python /tmp/probe_min.py

它只做一件事：把「搜索 -> 取直链 -> 下载 -> 校验 -> 转码 -> AstrBot 转 base64」
这条链一路跑通，并**算出最终那条 WebSocket 帧有多大**。
「总是超时 + 最后发出来是个文件」基本都出在这个体积上。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ENDS = ["https://api.qijieya.cn/meting/", "https://api.i-meto.com/meting/api"]
KW = "晴天"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"
MAGIC = [(b"ID3", "mp3"), (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"),
         (b"fLaC", "flac"), (b"OggS", "ogg"), (b"RIFF", "wav"), (b"#!AMR", "amr"), (b"\x02#!S", "silk")]
WD = os.path.join(tempfile.gettempdir(), "probe_min")
os.makedirs(WD, exist_ok=True)


def sniff(p):
    with open(p, "rb") as f:
        h = f.read(16)
    for m, n in MAGIC:
        if h.startswith(m):
            return n
    return "m4a" if h[4:8] == b"ftyp" else None


def get(u, t=60):
    r = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": "https://music.163.com/"})
    return urllib.request.urlopen(r, timeout=t)


def mb(n):
    return f"{n / 1048576:.1f} MB"


print("=" * 60)
print(f"Python {sys.version.split()[0]} | 容器={os.path.exists('/.dockerenv')} | cwd={os.getcwd()}")
print("=" * 60)

# 1. ffmpeg
ff = shutil.which("ffmpeg")
ferr = None
if not ff:
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        ferr = e
print(f"\n[1] ffmpeg: {ff or f'未找到 ({ferr})'}")
if ff:
    v = subprocess.run([ff, "-version"], capture_output=True, text=True)
    print("   ", (v.stdout or v.stderr).splitlines()[0][:70])
print(f"[2] 临时目录 {WD} 可写={os.access(WD, os.W_OK)} "
      f"剩余 {shutil.disk_usage(WD).free // 1048576} MB")

# 3. 端点
songs = []
for base in ENDS:
    q = urllib.parse.urlencode({"server": "netease", "type": "search", "id": KW})
    t0 = time.monotonic()
    try:
        with get(f"{base}?{q}", 15) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        cost = time.monotonic() - t0
        if isinstance(data, list) and data:
            print(f"[3] OK  [{cost:.1f}s] {base} -> {len(data)} 条")
            songs = songs or [d for d in data if isinstance(d, dict)]
        else:
            print(f"[3] 空  [{cost:.1f}s] {base}")
    except Exception as e:
        print(f"[3] ERR [{time.monotonic() - t0:.1f}s] {base} -> {type(e).__name__}: {e}")

if not songs:
    print("\n>>> 没有搜到歌，后面没法测。先修端点/网络。")
    raise SystemExit(1)

s0 = songs[0]
url = s0.get("url") or s0.get("link")
print(f"\n[4] 选中：{s0.get('name') or s0.get('title')} - {s0.get('artist') or s0.get('author')}")
print(f"    直链：{str(url)[:110]}")

# 5. 下载 + 魔数
src = os.path.join(WD, "probe_dl")
t0 = time.monotonic()
try:
    with get(url) as r:
        print(f"[5] HTTP {r.status} | Content-Type: {r.headers.get('Content-Type')}")
        with open(src, "wb") as f:
            total = 0
            while True:
                c = r.read(65536)
                if not c:
                    break
                total += len(c)
                f.write(c)
    fmt = sniff(src)
    print(f"    下载 {total} 字节 / {time.monotonic() - t0:.1f}s | 魔数={fmt or '不是音频！'}")
    if not fmt:
        raise SystemExit(1)
except Exception as e:
    print(f"    下载失败：{type(e).__name__}: {e}")
    raise SystemExit(1)

# 6. 转码：算出 record_local 那条帧到底多大
print("\n[6] 转码 + 线上体积估算（base64 会放大到 4/3）")
for name, args, bps in (
    ("wav16k", ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"], 32000),
    ("wav8k", ["-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le"], 16000),
):
    out = os.path.join(WD, f"probe_{name}.wav")
    p = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", src] + args + [out],
                       capture_output=True, timeout=300)
    if p.returncode != 0:
        print(f"    {name}: 失败 {p.stderr.decode('utf-8', 'ignore')[:90]}")
        continue
    size = os.path.getsize(out)
    print(f"    {name}: {mb(size)} -> base64 约 {mb(size * 4 / 3)}  （{bps // 1000} KB/s，"
          f"比 44.1k 立体声小 {int(176000 * 4 / 3 / (bps * 4 / 3))} 倍）")

# 7. AstrBot 自己的 MediaResolver —— 这才是真正上线的值
print("\n[7] AstrBot MediaResolver（record_local 实际走的路径）")
try:
    sys.path.insert(0, os.getcwd())
    import asyncio
    from astrbot.core.utils.media_utils import MediaResolver
    for cand, label in ((os.path.join(WD, "probe_wav8k.wav"), "已压到 8k 的 wav"),
                        (src, "原始 mp3（未预处理）")):
        if not os.path.exists(cand):
            continue
        b64 = asyncio.run(MediaResolver(cand, media_type="audio", default_suffix=".wav")
                          .to_base64(target_format="wav"))
        n = len(b64)
        print(f"    {label}: base64 {n} 字符 ≈ {mb(n)}")
        if n > 50 * 1048576:
            print("      -> 超过 NapCat 的 50MB 上限，必然 Max payload size exceeded")
        elif n > 8 * 1048576:
            print("      -> 偏大，很多部署在这个量级开始断连/超时")
    print("    >>> 这里显示「原始 mp3」特别大而「已压到 8k」很小的话，")
    print("        就证明预处理（先转低采样率 wav）是必须的。")
except ImportError as e:
    print(f"    跳过（导入不到 astrbot，需在容器内且工作目录为 AstrBot 根）：{e}")
except Exception as e:
    print(f"    失败：{type(e).__name__}: {e}")

print(f"\n临时文件：{WD}")
