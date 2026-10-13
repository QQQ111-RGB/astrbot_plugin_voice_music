# 部署、升级与排查

## NapCat 单条完整语音

本模式适用于 OneBot v11（AstrBot 平台名 `aiocqhttp`）和 NapCat。AstrBot 下载并校验完整源音频，再调用 OneBot `send_group_msg` / `send_private_msg`，其中仅包含一条 `record` 和 `file:///…` 路径。NapCat 从共享目录读取音频并转为 QQ 语音，避免 AstrBot Record 的 WAV/base64 展开。

**启用前必须确认两个容器能通过相同绝对路径读取同一份文件。** 只是在两边创建同名目录不够；需要共享同一份宿主机目录或 Docker volume。配置共享路径不会自动创建挂载，也不会改变容器权限。

已有 Compose 服务可以参考以下挂载片段合并，保留自己的镜像、网络、登录资料和其他配置：

```yaml
services:
  astrbot:
    volumes:
      - ./data:/AstrBot/data
  napcat:
    volumes:
      - ./data:/AstrBot/data:ro
```

NapCat 只需要读取源音频，自己的缓存目录仍须可写。对共享数据目录的挂载会让 NapCat 能读取其中数据，应仅在信任该协议端的部署中使用。

检查实际挂载：

```bash
docker inspect astrbot napcat --format '{{.Name}}{{range .Mounts}}{{println .Source "=>" .Destination}}{{end}}'
```

插件默认缓存位于 `/AstrBot/data/temp/astrbot_plugin_voice_music/audio`（以当前 AstrBot 环境为准）。两个容器都应能读取该位置同一测试文件。确认后在插件配置页设置：

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

共享模式不执行插件的 WAV 转码和时长裁剪，因此 `voice_format`、`ffmpeg_convert`、`max_voice_seconds` 不影响它。`max_payload_bytes` 检查的是小型 JSON 路径请求，不是源文件体积。`send_timeout` 仍包含下载、NapCat 转换及上传所需时间。

未确认共享目录时，保持 `onebot_shared_audio_root` 为空，插件使用通用路径：下载 → ffmpeg → WAV → 体积检查 → AstrBot Record。这条路径受 `max_voice_seconds` 与 `max_payload_bytes` 限制；不建议关掉大小保护来强行发送长歌。`record_link` 在本版只是安全本地语音的兼容入口，不再假设 Record URL 一定会直接递交给协议端。

共享模式发送的是音源返回的完整文件。音源只给试听片段时无法补全；QQ/NapCat 的时长、格式及上传限制仍适用。当前已实测约 302 秒的单条语音，不能据此保证任意超长音频都可接收。

## 升级步骤

1. 保存当前插件目录及插件配置备份；不要将运行配置、Cookie、令牌提交到 GitHub。
2. 更新插件到 v0.3.4。旧配置默认保留，共享模式需要按上节显式启用。
3. 群聊发起和选歌都须带当前会话的唤醒词或真实 @。例如 `*点歌 晴天` → `*1 语音`。`*` 只是示例，以 AstrBot `wake_prefix` 为准。
4. 重载插件或重启 AstrBot，并检查插件加载、音源初始化与 OneBot 连接。
5. 按 [测试说明](TESTING.md) 复验普通聊天隔离、唤醒词与 @ 混用，以及超过 4 分钟歌曲的客户端播放。

本版移除了 LLM 点歌工具。历史聊天或模型自行判断不再能触发本插件播放。`recall_select` 仅保留配置兼容，当前不自动撤回候选列表。

## 日志与常见问题

```bash
docker logs --tail 500 astrbot 2>&1 | grep -E 'onebot_shared|sender|voice_music|ERROR|Traceback' | tail -60
docker logs --tail 200 napcat 2>&1 | grep -E '语音|时长|Max payload|WebSocket|error|Error' | tail -40
```

按日志时间区分旧报错和本次报错。共享模式成功应有 `onebot_shared 完整语音准备发送` 与 `onebot_shared 完整语音发送成功`，NapCat 可显示转换后的时长。服务器成功还需结合客户端能否播放及是否完整确认。

- **裸序号没反应**：群聊必须再带唤醒词或 @，且由发起人在原聊天的有效期内选择。
- **普通聊天被机器人回复**：插件不接管普通聊天；AstrBot 自身聊天设置仍可能允许模型回复。
- **找不到共享文件**：检查两边 Mounts、缓存路径和读取权限，不能通过扩大 WebSocket 上限解决。
- **仍出现体积超限**：确认平台为 `aiocqhttp` 且 `onebot_shared_audio_root` 已设置；否则使用的是通用 WAV 路径。
- **发成文件**：默认 `voice_strict=false` 允许按配置降级。要只发语音请开启严格模式；显式选择“文件”仍有效。
- **发送超时**：超时只终止本插件等待，不保证协议端已取消上传，先核对客户端和日志再重试，避免重复消息。
- **音源不可用或版权限制**：更换有权访问的音源，或维护自己的音源端点与授权；接口返回列表不代表一定能取得完整音频。

## 回滚

恢复升级前的插件代码与配置，然后重启 AstrBot。手动恢复配置时先停止 AstrBot，以免内存中的旧配置写回覆盖文件。恢复旧版本会同时恢复旧版本的点歌触发与发送行为。

如用 Git 管理插件，可在干净工作目录切到升级前记录的提交；不要对未备份的本地修改执行强制重置。生产环境专用的主机校验、备份路径和一键脚本不属于公共插件仓库，应由部署者独立保存。
