"""音频处理 —— 骨架里解决「发的是文件不是语音」的核心。

原版为什么最后发了文件：
    下载逻辑只检查 HTTP 200 就把响应落盘成 xxx.mp3。
    但音乐直链经常需要 UA / Referer，或者返回 302 跳转到一个 HTML 页面，
    于是本地那个 .mp3 其实是一段 HTML/错误页。
    Record.fromFileSystem() 拿到非音频 → 发送失败 → 自动降级到 file 模式，
    而 file 模式不校验内容，于是「成功」发出去了一个不是歌的文件。

Docker 部署下的额外事实（已核对 AstrBot 源码）：
    Record.convert_to_base64() -> MediaResolver.to_base64(target_format="wav")
    -> astrbot/core/utils/media_utils.py 里 args = ["ffmpeg", "-y", "-i", ...]
    即 **AstrBot 内部也是裸 "ffmpeg" 走 PATH**，找不到就抛 Exception("ffmpeg not found")。
    所以「装个 pip 包」并不够 —— 必须让 "ffmpeg" 这个名字在 PATH 里能被解析到，
    否则插件这层能转码、AstrBot 那层仍然会失败。
    本模块的 ensure_ffmpeg_on_path() 就是为了补上这一环。

★ 最重要的一条：本地语音的「线上体积」是被 AstrBot 重新决定的
    Record.convert_to_base64() 把 target_format **写死成 "wav"**（components.py:262），
    而 media_utils.convert_audio_format(output_format="wav") **不加任何 -ar/-ac 参数**
    （media_utils.py:1525 之后没有 wav 分支），ffmpeg 会沿用源文件的采样率与声道：

        源 44.1kHz 立体声 mp3  ->  wav 仍 44100Hz 立体声 = 176 KB/s
        base64 后 ≈ 235 KB/s  ->  一首 4 分钟的歌 = 56 MB 的一条 WebSocket 帧

    NapCat / Lagrange 的 WS 接收端有 maxPayload 上限，超了直接抛
    `RangeError: Max payload size exceeded`，连接断掉 -> AstrBot 那边的 API 调用
    **永远等不到回包，于是报超时** -> 插件捕获异常 -> 降级到 file 模式 -> 用户收到一个文件。
    「总是超时 + 发出来是文件」这两个症状是同一个原因。

    唯一能钳住这个体积的杠杆：**我们自己先把音频转成 .wav**。
    因为 ensure_wav() 见到 RIFF 魔数会直接复用、跳过二次转码（media_utils.py:1628-1630），
    此时体积由下面 VOICE_PROFILES 里的 -ar/-ac 说了算：

        16kHz 单声道 -> 32 KB/s -> base64 约 43 KB/s（4 分钟歌 ≈ 7.7 MB）
         8kHz 单声道 -> 16 KB/s -> base64 约 21 KB/s（4 分钟歌 ≈ 3.9 MB）

    反过来说：**转成 mp3 交给 record_local 是反向优化** —— AstrBot 会把它重新
    膨胀成 44.1kHz 立体声 wav，比不转还大。mp3 只对 record_link / file_* 有意义。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

# 文件头魔数 -> 格式名
_MAGIC: list[tuple[bytes, str]] = [
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

MIN_AUDIO_BYTES = 1024

# 转码档位：输出扩展名 + ffmpeg 参数
#   wav   —— ★默认档。AstrBot 的 media_utils 见到 .wav 且魔数匹配会**直接复用、
#            跳过自己的 ffmpeg 调用**（media_utils.py:1628-1630），
#            所以最终线上体积就由这里的 -ar/-ac 决定，可控。
#   wav8k —— 更狠的一档，体积再减半，音质够「听清是什么歌」但明显发闷。
#            直链下载慢/协议端 maxPayload 很小时用它。
#   mp3   —— 体积小，但**只对 record_link / file_* 有意义**。
#            走 record_local 时 AstrBot 会把它重新转成 44.1kHz 立体声 wav，
#            反而比直接给 wav 大 5 倍。除非你明确知道自己在做什么，别选它。
VOICE_PROFILES: dict[str, tuple[str, list[str]]] = {
    "wav": (".wav", ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]),
    "wav8k": (".wav", ["-vn", "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le"]),
    "mp3": (".mp3", ["-vn", "-ac", "1", "-ar", "44100", "-b:a", "96k"]),
}

# base64 会把体积放大到 4/3
B64_RATIO = 4 / 3

# 走 record_local 时建议的上限。AstrBot 会把本地语音转成 wav 再 base64
# 塞进**一条** WebSocket 帧，协议端 maxPayload 常见是 10~50 MB。
# 默认按 8 MiB 卡，比常见上限留出余量（帧里还有 JSON 包装与其它消息段）。
DEFAULT_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


@dataclass
class FfmpegInfo:
    available: bool
    exe: str
    source: str  # system / configured / imageio-ffmpeg / missing
    on_path: bool  # "ffmpeg" 这个名字能否被解析到（AstrBot 内部也依赖这一点）
    version: str = ""

    def summary(self) -> str:
        if not self.available:
            return "不可用（未找到 ffmpeg）"
        ver = "版本未知"
        if self.version:
            ver = self.version.splitlines()[0].split(" Copyright")[0].strip()
        path_state = "已在 PATH" if self.on_path else "未在 PATH（AstrBot 内部会失败）"
        return f"{ver} | 来源={self.source} | {path_state} | {self.exe}"


# ----------------------------------------------------------------------
# 音频内容校验
# ----------------------------------------------------------------------
def sniff_audio_format(path: Path) -> str | None:
    """按文件头判断真实音频格式；识别不出返回 None。"""
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None

    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    return None


def is_valid_audio(path: Path | None, min_bytes: int = MIN_AUDIO_BYTES) -> bool:
    if not path or not path.exists() or path.stat().st_size < min_bytes:
        return False
    return sniff_audio_format(path) is not None


def describe_invalid(path: Path | None) -> str:
    """给「不是音频」的文件一个可读的诊断信息。"""
    if not path or not path.exists():
        return "文件不存在"
    size = path.stat().st_size
    try:
        head = path.read_bytes()[:200]
    except OSError:
        return f"无法读取（大小 {size} 字节）"
    preview = head[:120].decode("utf-8", errors="ignore").replace("\n", " ").strip()
    return f"大小 {size} 字节，开头内容：{preview!r}"


# ----------------------------------------------------------------------
# ffmpeg 定位与「让 PATH 认得它」
# ----------------------------------------------------------------------
def _locate_exe(ffmpeg_path: str = "ffmpeg") -> tuple[str, str]:
    """返回 (可执行文件路径, 来源)。不修改任何全局状态。"""
    found = shutil.which(ffmpeg_path)
    if found:
        return found, "system"
    if ffmpeg_path and Path(ffmpeg_path).is_file():
        return ffmpeg_path, "configured"
    try:
        import imageio_ffmpeg  # 可选依赖

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe, "imageio-ffmpeg"
    except Exception:  # noqa: BLE001
        pass
    return "", "missing"


async def _run(cmd: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


async def _version(exe: str) -> str:
    try:
        code, out, err = await asyncio.wait_for(_run([exe, "-version"]), timeout=15)
        # ffmpeg 把版本信息打到 stdout，不是 stderr
        text = out or err
        return text.decode("utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return ""


def _make_shim(src: Path, dest: Path) -> bool:
    """在 dest 处造一个可用的「指向 src」的垫片。

    依次尝试 软链 -> 硬链 -> 复制，每一步都**实际校验结果**。
    必须校验的原因（真踩过）：某些 Windows 卷上 Path.symlink_to() 既不抛异常
    也不生成软链，而是留下一个 0 字节的普通文件 —— 只看有没有抛异常会误判成功。
    """
    makers = (
        lambda: dest.symlink_to(src),
        lambda: os.link(src, dest),
        lambda: shutil.copy2(src, dest),
    )
    for make in makers:
        try:
            make()
        except OSError:
            pass
        else:
            try:
                if dest.exists() and dest.stat().st_size > 0:
                    return True
            except OSError:
                pass
        # 清理本次失败留下的残留，再试下一种
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
    return False


async def ensure_ffmpeg_on_path(
    temp_dir: Path, ffmpeg_path: str = "ffmpeg"
) -> FfmpegInfo:
    """确保 `ffmpeg` 这个名字在当前进程的 PATH 上可解析。

    为什么必须做这件事：
        AstrBot 内部发语音时会执行裸命令 `ffmpeg`（media_utils.py）。
        如果你是通过 pip 装 imageio-ffmpeg 拿到二进制的，那个文件叫
        `ffmpeg-linux-x86_64-v7.1`，PATH 上并没有 `ffmpeg` 这个名字，
        于是插件这层能转码、AstrBot 那层照样报 "ffmpeg not found"。

    做法：在插件自己的临时目录里建一个名为 `ffmpeg` 的软链（失败则复制），
    然后把它所在目录插到 os.environ["PATH"] 最前面。
    因为 AstrBot 与插件同进程，子进程会继承这个 PATH —— 不需要 root、
    不需要改 /usr/local/bin、容器重建后每次启动自动重建。
    """
    system_exe = shutil.which("ffmpeg")
    if system_exe:
        return FfmpegInfo(True, system_exe, "system", True, await _version(system_exe))

    exe, source = _locate_exe(ffmpeg_path)
    if not exe:
        logger.error(
            "[audio] 容器/系统里找不到 ffmpeg。"
            "语音消息将无法发送（AstrBot 内部也依赖它）。"
        )
        return FfmpegInfo(False, "", "missing", False)

    shim_dir = Path(temp_dir) / "bin"
    try:
        shim_dir.mkdir(parents=True, exist_ok=True)
        # Windows 上 shutil.which 靠 PATHEXT 找 ffmpeg.exe，垫片必须带 .exe 后缀
        shim = shim_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not shim.exists() or shim.stat().st_size == 0:
            shim.unlink(missing_ok=True)
            if not _make_shim(Path(exe), shim):
                logger.warning(f"[audio] 无法为 ffmpeg 建立垫片，将直接使用 {exe}")
                return FfmpegInfo(
                    True, exe, source, False, await _version(exe)
                )
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[audio] 为 ffmpeg 建立 PATH 垫片失败：{e}")
        return FfmpegInfo(True, exe, source, False, await _version(exe))

    # 实测一遍垫片是否真的能跑，不能只看文件存在
    on_path = shutil.which("ffmpeg") is not None
    version = await _version(str(shim))
    if not version:
        logger.warning(f"[audio] 垫片 {shim} 无法执行，回退为直接使用 {exe}")
        return FfmpegInfo(True, exe, source, on_path, await _version(exe))

    logger.info(f"[audio] 未找到系统 ffmpeg，已用 {source} 建立 PATH 垫片：{shim}")
    return FfmpegInfo(True, str(shim), source, on_path, version)


async def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    exe, _ = _locate_exe(ffmpeg)
    if not exe:
        return False
    try:
        code, _, _ = await asyncio.wait_for(_run([exe, "-version"]), timeout=15)
        return code == 0
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# 转码
# ----------------------------------------------------------------------
def resolve_voice_format(fmt: str = "auto") -> str:
    """把配置值解析成实际的转码档位。

    auto -> wav（16kHz 单声道）。

    注意这里**不能**选 mp3：record_local 走到 AstrBot 时，
    convert_to_base64() 会把 target_format 写死成 "wav"，mp3 会被重新膨胀成
    44.1kHz 立体声 wav，线上体积反而涨 5 倍 —— 详见模块开头的算式。
    而 wav 档因为魔数匹配会被 ensure_wav() 直接复用，体积可控。
    """
    if fmt in VOICE_PROFILES:
        return fmt
    return "wav"


def estimate_base64_bytes(path: Path | None) -> int:
    """估算这个文件以 record(base64) 发出时占用的线上字节数。

    这是「会不会被协议端拒收」的直接判据：base64 体积 ≈ 文件大小 × 4/3。
    """
    if path is None:
        return 0
    try:
        return int(path.stat().st_size * B64_RATIO) + 4
    except OSError:
        return 0


def human_size(num_bytes: int) -> str:
    """1536000 -> '1.5 MB'。"""
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def estimate_voice_payload(path: Path | None) -> tuple[int, str]:
    """(估算的 base64 字节数, 人类可读形式)。"""
    size = estimate_base64_bytes(path)
    return size, human_size(size)


async def to_voice(
    src: Path,
    out_dir: Path,
    *,
    ffmpeg: str = "ffmpeg",
    max_seconds: int = 0,
    fmt: str = "auto",
) -> Path | None:
    """把任意音频转成「适合当语音发」的文件。

    返回转码后的路径；失败返回 None（调用方据此决定是否报错）。
    """
    exe, _ = _locate_exe(ffmpeg)
    if not exe:
        logger.error("[audio] 转码失败：找不到 ffmpeg")
        return None

    real_fmt = resolve_voice_format(fmt)
    suffix, profile = VOICE_PROFILES[real_fmt]

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}_voice{suffix}"

    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    cmd += profile
    if max_seconds > 0:
        cmd += ["-t", str(max_seconds)]
    cmd.append(str(out))

    try:
        code, _, err = await asyncio.wait_for(_run(cmd), timeout=180)
    except asyncio.TimeoutError:
        logger.error("[audio] ffmpeg 转码超时")
        return None
    except FileNotFoundError:
        logger.error(f"[audio] 找不到 ffmpeg 可执行文件：{exe}")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error(f"[audio] 调用 ffmpeg 异常：{type(e).__name__}: {e}")
        return None

    if code != 0 or not is_valid_audio(out):
        logger.error(
            f"[audio] ffmpeg 转码失败：{err.decode('utf-8', errors='ignore')[:300]}"
        )
        return None
    logger.debug(f"[audio] 转码成功（{real_fmt}）：{out}")
    return out
