# astrbot_plugin_voice_music · 语音点歌

AstrBot 的语音点歌插件，支持网易和 QQ 音源、按用户隔离的选歌候选，以及 NapCat 单条完整语音。

## v0.3.4 修复

- 群聊发起、选择与取消都要求当前会话的**唤醒词或真实 @机器人**，两种方式可混用。
- 候选按“聊天 + 点歌者”保存；普通聊天、裸序号、其他人的回复不会选择或覆盖你的候选。
- 移除 `session_waiter` 和 LLM 点歌工具，防止历史上下文误触发、重复播放。
- 新增 `onebot_shared_audio_root`：确认 AstrBot/NapCat 共享目录后，直接发送完整音频文件路径，由 NapCat 转语音，不在 4 分钟处截断，也不把整个 WAV/base64 放进 WebSocket 帧。
- 所有语音别名走同一安全路径，旧 `record_link` 配置保持兼容。

完整改动见 [CHANGELOG.md](CHANGELOG.md)，部署与升级见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 使用

假设 AstrBot 当前唤醒词为 `*`（插件读取每个聊天的配置，不硬编码此字符）：

```text
*点歌 海阔天空
*1 语音
```

也可以真正 @ 机器人后发送 `点歌 海阔天空`，收到候选后再次 @ 并发送 `1 语音`。默认候选有效期 60 秒，发起和选歌必须是同一人在同一聊天中操作。

| 命令正文 | 用途 |
|---|---|
| `点歌 歌名` | 默认音源搜索 |
| `点歌 歌名 1` | 直接选择搜索结果第一首 |
| `网易点歌 歌名` / `网易web 歌名` / `QQ点歌 歌名` | 指定音源 |
| `1` / `1 语音` | 选择候选；不写类型时使用配置的默认方式 |
| `1 文件` / `1 文本` | 明确指定音频文件或直链文本 |
| `取消点歌` | 取消自己的候选 |
| `音乐状态` | 环境、音源和发送配置诊断 |

表中命令在群聊中都需要唤醒词或真实 @；私聊可直接发送。新点歌替换自己的旧候选，其他人的候选相互独立。普通消息仍按 AstrBot 自身聊天设置处理。

## 安装

在 AstrBot 插件管理中安装本仓库，或克隆到插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/QQQ111-RGB/astrbot_plugin_voice_music.git
```

依赖为 `aiohttp`、`aiofiles`；由 AstrBot 安装，或执行 `python -m pip install -r requirements.txt`。通用 Record 语音需要可用 ffmpeg；可配置 `ffmpeg_path`，或安装 `imageio-ffmpeg` 让插件建立 PATH 垫片。NapCat 共享模式的音频转换由 NapCat 负责。

## 配置完整语音

`onebot_shared_audio_root` 默认为空，**只有确认 AstrBot 与 NapCat 通过相同绝对路径访问同一份音频后才能启用**。迁移部署不会自动创建共享挂载。

例如双方都挂载同一个数据目录到 `/AstrBot/data`，可在插件配置中设置：

```json
{
  "onebot_shared_audio_root": "/AstrBot/data",
  "send_modes": ["record_local(本地语音)", "file_local(本地文件)", "text(文本模式)"],
  "voice_strict": true,
  "send_timeout": 60,
  "max_voice_seconds": 0,
  "max_payload_bytes": 5242880
}
```

共享模式下，源音频不经插件裁剪，WebSocket 只发送 `record` 文件路径。`voice_strict=true` 使默认语音失败时报告错误；明确选择“文件”仍可发送文件。若不具备共享挂载，保持该配置为空，使用通用 WAV 转码和大小保护。

| 配置 | 含义 |
|---|---|
| `source_endpoints` / `meting_token` | 音源端点及自建端点签名；私有密钥勿提交仓库 |
| `selection_timeout` | 候选有效期，默认 60 秒 |
| `onebot_shared_audio_root` | OneBot 完整语音共享路径，默认空 |
| `send_modes` | 默认发送优先级；旧 `record_link` 现在是 `record_local` 兼容别名 |
| `voice_strict` | 默认 false；true 时默认不降级文件，显式模式仍生效 |
| `send_timeout` | 每种方式的下载、转换和发送总超时，默认 25 秒；共享模式建议 60 秒 |
| `voice_format` / `ffmpeg_convert` | 通用语音转码参数，共享模式不使用 |
| `max_voice_seconds` | 通用 WAV 路径时长上限，共享模式不使用 |
| `max_payload_bytes` | 通用路径检查 base64；共享路径检查 JSON 请求大小 |
| `dedup_seconds` | 同一聊天同一首歌去重窗口，默认 20 秒，0 关闭 |
| `record_unsupported` | 不支持语音的平台名单 |
| `recall_select` | 仅为兼容保留，当前不自动撤回候选 |

默认值与所有配置项以 [_conf_schema.json](_conf_schema.json) 为准。本版不强制修改既有用户配置，完整语音需要按部署条件启用。

## 测试与限制

```bash
python .selftest/test_selection.py
python .selftest/test_full_voice.py
```

两项测试离线运行，不向 QQ 发送消息。覆盖 19 组选歌/唤醒/隔离场景和 330 秒完整音频路径发送。实际 Docker + AstrBot + NapCat 环境已验证 302.12 秒单条语音，用户确认能播放到 4 分钟之后；这不保证任意协议端或任意长度都支持。

共享路径避免的是大帧和插件裁剪；音源试听限制、NapCat 转换、QQ 上传与播放限制仍可能影响结果。发送超时不保证协议端已取消上传，重试前请检查客户端。更多测试和验收步骤见 [docs/TESTING.md](docs/TESTING.md)。

旧版文档曾将 `Record.fromURL` 描述为只传 URL；在已核对的 AstrBot 实现中它仍可能下载并转换为 WAV/base64，不能依靠它规避大帧。本版用明确的 OneBot 原生共享文件路径解决这一点。

## 目录

- `main.py`：授权、候选隔离、命令编排。
- `core/sender.py`：原生共享语音、通用发送、超时与去重。
- `core/audio.py`、`downloader.py`：通用转码、下载与内容校验。
- `core/platform/`：网易/QQ 音源与候选直链。
- `.selftest/`：离线回归及可选联网测试。
- `tools/`：容器诊断脚本。
- `docs/`：部署、升级、回滚与验收说明。

MIT License。音源接口可用性及访问权限由部署者自行维护。
