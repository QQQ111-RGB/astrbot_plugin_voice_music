"""命令参数解析工具。"""

from __future__ import annotations

# 用户可用的中文/英文发送方式别名 -> 内部 mode 名
#
# ★ v0.3.3：把「语音」从 record_local 改成 record_link。
#   record_link 同样是**以语音消息形式**发出去（只是把音频直链递给协议端，
#   由它自己去下），帧只有几十字节，不会撞上「巨型帧 → 断连 → 180 秒超时」。
#   想看/发本地那一份就用「本地语音」（record_local，会被体积闸门限制在短音频）。
MODE_ALIAS: dict[str, str] = {
    "语音": "record_link",
    "语音链接": "record_link",
    "record_link": "record_link",
    "本地语音": "record_local",
    "record": "record_local",
    "record_local": "record_local",
    "文件": "file_local",
    "本地文件": "file_local",
    "file": "file_local",
    "file_local": "file_local",
    "文件链接": "file_link",
    "file_link": "file_link",
    "文本": "text",
    "text": "text",
}

MODE_HELP = "语音(=语音链接) / 本地语音 / 文件 / 文件链接 / 文本"


def parse_index_and_modes(arg: str) -> tuple[int, list[str] | None, str | None]:
    """解析选歌回复。

    支持的格式：
        "2"           -> 选中第 2 首，用默认发送方式
        "2 语音"      -> 选中第 2 首，强制发语音
        "2 file"      -> 选中第 2 首，强制发文件

    返回 (序号, 发送方式列表或 None, 错误信息或 None)。
    """
    parts = arg.strip().split()
    if not parts:
        return 0, None, None

    if not parts[0].isdigit():
        return 0, None, f"请输入序号，或「序号 + 方式」，如「2 语音」。可用方式：{MODE_HELP}"

    index = int(parts[0])
    if index == 0:
        return 0, None, None

    if len(parts) == 1:
        return index, None, None

    way = parts[1].strip().lower()
    mode = MODE_ALIAS.get(way) or MODE_ALIAS.get(parts[1].strip())
    if mode is None:
        return 0, None, f"未知发送方式「{parts[1]}」。可用方式：{MODE_HELP}"
    return index, [mode], None
