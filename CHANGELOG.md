# 更新日志

## v0.3.4 — 唤醒词与 @ 等效、选歌隔离、NapCat 完整语音

- 群聊发起、选择、取消与状态命令要求真实 @ 或当前会话的 `wake_prefix`；唤醒词与 @ 可混用，裸命令不授权。空前缀不会放开所有消息。
- 用“聊天 + 发起人”独立候选替换 `session_waiter`，普通消息不改变候选及 LLM 开关；支持过期、取消、并发领取一次和慢旧搜索保护。
- 移除 LLM 点歌工具，防止模型依据历史上下文发起播放。
- 新增可选 `onebot_shared_audio_root`。在确认 AstrBot/NapCat 相同路径共享同一目录后，使用 OneBot 原生 record 文件路径发送完整源音频，不执行插件时长裁剪，也不传整首 WAV/base64。
- 所有语音别名统一走安全本地路径；`record_link` 保留为兼容别名。纠正旧文档“Record URL 一定只传链接”的不准确描述。
- 新增可移植离线回归：19 组选歌行为，以及 330 秒完整音频、群聊/私聊路由、目录边界和错误处理；更新旧联网测试桩件。
- 重写 README，新增部署/升级/回滚和测试说明。共享模式默认关闭，旧用户需按实际挂载条件启用；不强制覆盖已有配置。

实机验收：NapCat 单条语音时长 302.12 秒，路径请求约 213 字节，客户端确认能播放到 4 分钟后。该结果不代表所有音源、协议端或任意超长音频均受支持。

以下为历史版本记录；涉及 record_link 的旧机制描述以 v0.3.4 README 与部署说明为准。

## v0.3.3 — 治「点歌卡三分钟才报 WebSocket API call timeout」

### 线上实测到的事实

在一台 2 核 2G 的 ECS（AstrBot 4.28.2 + NapCat，Docker）上连续抓了 6 小时日志：

| 现象 | 实测值 |
|---|---|
| `WebSocket API call timeout` | 22 次 |
| 两次「准备发送 → 发送失败」的间隔 | **精确 180.000 秒**（不是"变慢"，是等满了写死的上限） |
| 语音体积超限触发 wav8k 重转 | 6 次（每次两趟 ffmpeg） |
| `record_link` / `record_local` | 全败，只有 `file_local` 成功 |

同时用 AstrBot 自带的看门狗日志（`data/logs/event_loop_watchdog.log`）取到了
「事件循环卡住那一刻，主线程停在哪」的栈，把两个**互相独立**的问题分开了：

- **链 A（180 秒超时）**：本插件的发送路径。`record_link` 要把直链交给协议端自己去下、
  `record_local` 要把整首歌 base64 塞进一条帧再由协议端整体上传 —— 两者都是"几十秒起步"的长任务，
  一旦协议端不返回，AstrBot 就会一直等到自己写死的 180 秒。
- **链 B（Event loop stalled 30~64 秒，32 次）**：**与本插件无关**。
  AstrBot 看门狗里 32 次卡顿的主线程栈逐字相同，停在
  `hypercorn/asyncio/tcp_server.py: protocol_send → transport.write → _maybe_pause_protocol` ——
  是 **AstrBot 自己的 WebUI 日志页 SSE 流（`/api/v1/log/live`）**在往一个不读数据的客户端狂写。
  整份栈里本插件一次都没出现。

> 结论：**别再把事件循环卡顿算到本插件头上**。本版本只负责把链 A 修死。

### 本版本改了什么

| # | 改动 | 文件 | 为什么 |
|---|---|---|---|
| 1 | **每种发送方式加最长等待 `send_timeout`（默认 25 秒）**，超时立刻放弃并换下一种 | `core/sender.py`、`core/config.py`、`_conf_schema.json` | 把最坏耗时从 **180 秒**压到 `send_timeout × 方式数`。这是本版本最关键的一条 |
| 2 | `send_modes` 默认**去掉 `record_local`** → `record_link → file_local → text` | `core/config.py`、`_conf_schema.json` | 巨型帧那条路在几分钟的歌上本来就发不出去，留着只会把用户拖进 3 分钟的超时 |
| 3 | `max_payload_bytes` 默认 **8 MiB → 2 MiB** | `core/audio.py`、`_conf_schema.json` | 8 MiB 会让插件"看起来在尝试"，实际把本地语音放行到 5 MB 级。2 MiB 相当于自动限制在 ~40 秒短音频 |
| 4 | `voice_strict` 默认 **true → false** | `core/config.py`、`_conf_schema.json` | 默认 true 时 `send_song()` 会把候选过滤成只剩 `record_*`，`file_local` **永远不会被尝试** —— 语音一失败用户什么都收不到 |
| 5 | 「语音」别名从 `record_local` 改为 `record_link` | `core/utils.py` | 用户说"语音"要的是语音消息，而 `record_link` 就是语音消息、且不会撞巨型帧 |
| 6 | 体积闸门 2 移到 `if ffmpeg_convert:` **之外**，无条件生效 | `core/sender.py` | 关掉转码时也必须拦住超大帧 |
| 7 | 发送前把**这一帧到底多大**打进日志 | `core/sender.py` | 线上排查超时时，一眼看出推出去的是几百 KB 还是几 MB |
| 8 | 超限/超时文案改成可直接照做 | `core/sender.py` | 明确告诉用户「改走 record_link/file_*」或「调小 max_voice_seconds」 |
| 9 | 自测新增第 13 组：把某方式做成「卡住 30 秒」，断言 `send_timeout=1` 时约 1 秒就降级 | `.selftest/run_selftest.py` | 让「超时会被主动打断」这件事变成可回归的用例 |

### 自测

13 组全部通过（`python .selftest/run_selftest.py`）：

```
[7] 体积换算：... = 8926884 字节
    ✓ 超限被拦下并说明了原因（不会再撞协议端上限后降级发文件）
[8] 16k 预计 11.4 MB，8k 预计 5.7 MB，上限设为 8926887 字节
    ✓ 自动降档到 wav8k 后成功发出语音
[13] 单次发送超时（send_timeout）—— 治「180 秒才报错」
    record_link 挂了 30 秒，实际 1.01s 就返回；成功=True
    ✓ 超时被主动打断并降级到下一种方式（不会再干等 180 秒）
自测结果：全部通过 ✓
```

### 行为变化（升级前请确认）

- 升级后 `send_modes` 等**默认值只影响新装**；已有实例的配置不会被改。
  老实例想拿到同样的效果，请手动把配置调成：
  `send_modes = [record_link(语音链接), file_local(本地文件), text(文本模式)]`、
  `max_payload_bytes = 2097152`、`voice_strict = false`、`send_timeout = 25`。
- 输入「2 语音」现在等价于「2 语音链接」。想要本地那一份请用「2 本地语音」。
- `send_timeout = 0` 表示不限制，即退回旧行为（可能会等满 180 秒）。

### 顺带一提：宿主机的 ffmpeg 没用

ffmpeg 是在**容器内**被调用的（`core/audio.py` 用 `asyncio.create_subprocess_exec` 起子进程，
AstrBot 内部发语音也执行裸命令 `ffmpeg`）。容器有独立 rootfs，**看不到宿主机的 `/usr/bin/ffmpeg`**；
而且它跑在独立进程里，本来就不占事件循环。想省 CPU 应该**少转码**（少走 `record_local`），
而不是多装一个 ffmpeg。
