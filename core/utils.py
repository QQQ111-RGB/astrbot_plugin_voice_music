"""命令参数解析工具。"""

from __future__ import annotations

# Voice aliases share the native shared-file or bounded local-transcoding path.
MODE_ALIAS: dict[str, str] = {
    "语音": "record_local", "本地语音": "record_local",
    "语音链接": "record_local", "record_link": "record_local",
    "record": "record_local", "record_local": "record_local",
    "文件": "file_local", "本地文件": "file_local", "file": "file_local",
    "file_local": "file_local", "文件链接": "file_link", "file_link": "file_link",
    "文本": "text", "text": "text",
}
MODE_HELP = "语音 / 文件 / 文件链接 / 文本"


def parse_index_and_modes(arg: str) -> tuple[int, list[str] | None, str | None]:
    parts = arg.strip().split()
    if not parts:
        return 0, None, None
    if len(parts) > 2 or not (parts[0].isascii() and parts[0].isdigit()):
        return 0, None, "请输入序号，或「序号 语音」，如「2 语音」"
    index = int(parts[0])
    if len(parts) == 1:
        return index, None, None
    mode = MODE_ALIAS.get(parts[1].lower())
    if mode is None:
        return 0, None, f"未知发送方式「{parts[1]}」。可用方式：{MODE_HELP}"
    return index, [mode], None
