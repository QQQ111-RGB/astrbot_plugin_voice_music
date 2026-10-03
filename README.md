# astrbot_plugin_voice_music · 语音点歌

AstrBot 的语音点歌插件。群里发一句 `点歌 晴天`，回个序号，歌就以**语音**发出来。

照着 `Zhalslar/astrbot_plugin_music` 的架构思路重写，针对「**总是超时**」和
「**想要语音却收到文件**」这两个高频问题做了结构性修复。

> 本 README 的部署与排查步骤按「**云服务器 + Docker**」写的。
> 你本机有没有 ffmpeg 完全不重要 —— 相关的一切都发生在容器里。

**目录**

- [一、介绍](#一介绍)
- [二、安装](#二安装)
- [三、使用方法](#三使用方法)
- [四、原理与排查（可选深读）](#四原理与排查可选深读)
- [五、注意事项](#五注意事项)

---

## 一、介绍

### 1. 它解决什么问题

如果你只读一段，读这段：

**AstrBot 发本地语音时，会把音频转成 wav 再 base64 塞进一条 WebSocket 帧，
而那个 wav 会沿用源文件的采样率。一首 4 分钟的歌 ≈ 65 MB 一帧。
协议端（NapCat / Lagrange）的 `maxPayload` 收不下就直接断连 ——
AstrBot 侧表现为「超时」，然后插件降级去发文件，你就收到一个文件。**
「超时」和「收到文件」这两个症状，其实是同一个原因。

由此有两条反直觉的结论：

1. **把音频转成 mp3 交给本地语音发送是反向优化** —— AstrBot 会把它重新膨胀成
   44.1kHz 立体声 wav，比不转还大 5 倍。mp3 只对「语音链接 / 文件」方式有意义。
2. **唯一能钳住体积的杠杆，是自己先把音频转成低采样率 `.wav`** ——
   文件头魔数会被 AstrBot 直接复用、跳过二次转码，体积就由你说了算。
   16kHz 单声道 = 41.7 KB/s，8kHz 单声道 = 20.8 KB/s。

本插件默认把 **`record_link`（只把直链递过去，一帧几十字节）排在第一位**，
并在本地语音前加了一道**体积闸门**：超限先自动降到 8kHz 重转，仍超就拒绝发送并报出具体数字，
而不是让它变成一次「超时 + 默默发个文件」。

### 2. 功能特性

| 你的问题 | 根因 | 本插件的对策 |
|---|---|---|
| 总是显示超时 | ① `aiohttp.ClientSession()` 默认总超时 **300 秒**，接口挂住就一直等；② 公共音源域名时好时坏，单点无备用；③ **语音那一条 WebSocket 帧太大，协议端直接断连**，AstrBot 等不到回包 → 报超时（见第四节 1、3） | `core/http.py`：秒级三级超时 + 有限重试 + **多端点自动回退**；`core/sender.py` **体积闸门**（超限不发，先自动降档） |
| 想要语音却发来文件 | ① 下载到的其实是 HTML 错误页，被当成 mp3；② 语音环节失败后**静默降级**到 file，file 不校验内容所以「成功」了；③ 超时的连带后果 —— 语音发不出去就退到 file | `core/downloader.py` 魔数校验；`core/audio.py` 转码 + PATH 垫片；`core/sender.py` **严格语音模式，失败直接报错不降级** |
| 同一首歌发两遍 | 会话截获的那条回复会被 AstrBot **浅复制后重新投递**、再走一遍流水线触发 LLM，LLM 于是又调了一次点歌工具（见第四节 4） | `main.py` 的 `should_call_llm(False)`（治本）；`core/sender.py` 的**去重窗口**（兜底） |
| 某个音源端点挂了就整体失败 | Meting 的 search 响应**不含 `id` 字段**（ID 藏在 `url` 里），不抠出来 `Song.id` 恒为空 → 多端点回退被静默跳过，只剩一条候选地址（见第四节 5） | `core/platform/meting.py` 从 `url` 里正则抠出 ID |
| 服务器上没法排查 | Docker 里不方便随手开终端 | `core/diagnose.py`：发「**音乐状态**」输出自检报告 |
| 想换 / 加音源 | 原版音源写死在主流程里 | `core/platform/` 抽象基类 + 子类自动注册，加音源不用动主流程（见第三节 5） |

### 3. 工作原理

```
用户: 点歌 晴天
      │
      ▼
  main.py  ── 解析命令/序号 ──► 选定 player（音源）
      │
      ▼
  platform.fetch_songs()          ← http.request_json_multi()：多端点回退
      │  返回 list[Song]
      ▼
  未给序号 → 发候选列表 → session_waiter 等回复
  给了序号 → 直接发
      │
      ▼
  sender.send_song()
      ├─ 去重闸门                 ← 同一会话同一首歌在窗口内只发一次
      ├─ audio_url_candidates()   ← 同 ID × 多端点，逐个候选
      ├─ downloader.download_audio_multi()  ← 魔数校验，非音频丢弃
      ├─ audio.to_voice()         ← ffmpeg 转成单声道低采样率 wav
      ├─ ★ 体积闸门：估算 base64 体积
      │     ├─ 超限 → 自动降到 wav8k 重转一次
      │     └─ 仍超 → 拒绝发送并说明「预计 X MB > 上限 Y MB」
      └─ Record.fromFileSystem()  ← AstrBot 复用我们的 wav → base64 → 协议端转 SILK
      │
      ├─ 成功 → 发出语音
      └─ 失败 → 报错并列出每个环节的原因（严格模式下不发文件）
```

### 4. 目录结构

**仓库根目录就是插件目录**，克隆下来即可被 AstrBot 加载：

```
astrbot_plugin_voice_music/          ← 仓库根 = 插件目录，直接放进 data/plugins/
├── metadata.yaml                    插件元信息
├── _conf_schema.json                WebUI 配置面板定义
├── requirements.txt                 依赖
├── main.py                          ★ 编排：命令注册、事件监听、选歌会话、启动自检
├── core/
│   ├── config.py                    配置读取与自检
│   ├── model.py                     Song / Platform 数据模型
│   ├── http.py                      ★ 统一 HTTP：秒级超时 + 重试 + 多端点回退   → 治「超时」
│   ├── downloader.py                ★ 下载 + 内容校验（拒绝 HTML）            → 治「下到错误页」
│   ├── audio.py                     ★ 魔数校验 + ffmpeg 转码 + PATH 垫片       → 治「发不出语音」
│   ├── sender.py                    ★ 发送策略：语音优先 / 体积闸门 / 去重      → 治「变文件」
│   ├── diagnose.py                  ★ 远程自检报告                           → 治「没法排查」
│   ├── utils.py                     参数解析（「2 语音」这类输入）
│   └── platform/
│       ├── base.py                  ★ BaseMusicPlayer 抽象 + 子类自动注册
│       └── meting.py                MetingPlayer + NeteaseMeting + NeteaseWeb
├── tools/                           部署期诊断，不参与插件运行
│   ├── diagnose_in_container.py     ★ 容器内全量体检（8 项 PASS/FAIL，只用标准库）
│   └── probe_min.py                 ★ 极简体检，可直接 `docker exec -i ... python -` 粘贴运行
└── .selftest/
    └── run_selftest.py              端到端自测（桩件模拟 AstrBot，不依赖真实 AstrBot）
```

---

## 二、安装

### 1. 前置要求

| 项目 | 要求 |
|---|---|
| AstrBot | 能正常加载插件即可 |
| 依赖 | `aiohttp`、`aiofiles`（AstrBot 会自动装到 `data/site-packages`，容器重建不丢） |
| ffmpeg | **强烈建议容器里有**。没有也能跑（插件会自建 PATH 垫片），见下面第 3 节 |

### 2. 安装插件

**方式 1：克隆到插件目录**（推荐）

```bash
cd /AstrBot/data/plugins        # 宿主机上就是 $PWD/data/plugins/
git clone <本仓库地址> astrbot_plugin_voice_music
```

**方式 2：下载 ZIP**，解压后把整个目录放到 `AstrBot/data/plugins/astrbot_plugin_voice_music/`。

**方式 3：直接从本机传上去**（Docker + 云服务器，没走 GitHub 时用这个）

```bash
# ① 本机打包（在仓库的上一级目录执行）
tar --exclude='.git' -czf astrbot_plugin_voice_music.tar.gz astrbot_plugin_voice_music

# ② 传到服务器
scp astrbot_plugin_voice_music.tar.gz <用户>@<服务器IP>:~/

# ③ 服务器上解到 AstrBot 的数据卷里（路径按你的实际情况改）
cd /path/to/astrbot/data/plugins
tar -xzf ~/astrbot_plugin_voice_music.tar.gz
ls astrbot_plugin_voice_music/main.py     # 确认解出来了
```

**GitHub 不是运行条件，只是分发渠道。** 只要这个目录出现在 `data/plugins/` 下就能用。
反过来，如果你想以后在服务器上 `git pull` 更新，那就得先推到 GitHub（私有仓库也行）再 `git clone`。

装完重启容器：

```bash
docker restart <容器名>
```

### 3. 容器里没有 ffmpeg 怎么办

先确认一下：

```bash
# 1) 容器里有没有 ffmpeg？（官方镜像的 Dockerfile 里是装了 ffmpeg + libavcodec-extra 的）
docker exec -it <容器名> ffmpeg -version

# 2) 如果上一条报 command not found，再看 pip 侧的情况
docker exec -it <容器名> python -c "import astrbot; print(astrbot.__file__)"
```

- **第一条有输出** → ffmpeg 齐备，语音链路没问题，直接去验证。
- **第一条报 not found** → 按下面任一方式补上（推荐 A）。

#### 方式 A：让插件自己补上（推荐，零手工、重建容器也不丢）

1. 编辑插件目录里的 `requirements.txt`，把这一行的注释去掉：
   ```
   imageio-ffmpeg>=0.4.9
   ```
2. 重启 AstrBot。插件依赖会被装到 `data/site-packages`（**在挂载卷里，容器重建后仍在**）。
3. 插件启动时会自动做一件事：发现 PATH 上没有 `ffmpeg`，就用 imageio-ffmpeg 的二进制在
   `data/temp/astrbot_plugin_voice_music/bin/` 下建一个名为 `ffmpeg` 的垫片，并插到本进程的 PATH 最前面。

> **为什么必须建垫片？** 因为 AstrBot 内部执行的是裸命令 `ffmpeg`。
> pip 装的 imageio-ffmpeg 里的文件叫 `ffmpeg-linux-x86_64-v7.1`，PATH 上并没有 `ffmpeg` 这个名字 ——
> 不建垫片的话，**插件这层能转码，AstrBot 那层照样报 `ffmpeg not found`**。
> 垫片不需要 root、不动 `/usr/local/bin`，每次启动自动重建。

#### 方式 B：直接往容器里装（临时有效，重建容器会丢）

```bash
docker exec -u root <容器名> apt-get update && docker exec -u root <容器名> apt-get install -y ffmpeg
```

#### 方式 C：重建镜像（永久有效）

```dockerfile
FROM soulter/astrbot:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libavcodec-extra && rm -rf /var/lib/apt/lists/*
```

#### 方式 D：把宿主机的静态 ffmpeg 挂进去

```bash
# 下载静态构建后
docker run ... -v /opt/ffmpeg/ffmpeg:/usr/local/bin/ffmpeg:ro ...
```

### 4. 确认安装成功

```bash
# 1) 文件到位了吗
docker exec -it <容器名> ls /AstrBot/data/plugins/astrbot_plugin_voice_music/main.py

# 2) 依赖装了吗（AstrBot 通常会自动装到 data/site-packages）
docker exec -it <容器名> python -c "import aiohttp, aiofiles; print('deps ok')"

# 3) 重启后看加载日志
docker logs <容器名> 2>&1 | grep voice_music | tail -20
#   应该能看到「已加载音源：[...]」和「ffmpeg 就绪：...」
```

最后在群里发 **`音乐状态`**，能收到自检报告就说明整条链路通了。

### 5. Docker 相关注意点

- **插件目录**：`-v $PWD/data:/AstrBot/data`，插件放 `data/plugins/astrbot_plugin_voice_music/`。
- **缓存目录**：插件把音频缓存放 `data/temp/astrbot_plugin_voice_music/audio/`，也就是**挂载卷里**，不会撑爆容器可写层。
- **代理**：容器里的 `http://127.0.0.1:7890` 指的是容器自己，**不是宿主机**。要填宿主机上的代理得用
  `http://172.17.0.1:7890`（默认 bridge 网关，可用 `docker network inspect bridge` 确认）或
  `http://host.docker.internal:7890`（compose 里加 `extra_hosts`）。
- **看日志**：`docker logs -f <容器名> 2>&1 | grep voice_music` 能看到插件的启动自检输出。

---

## 三、使用方法

### 1. 命令

| 命令 | 说明 |
|---|---|
| `点歌 晴天` | 用默认平台搜索，回复序号选歌 |
| `点歌 晴天 1` | 直接选中第 1 首 |
| `网易点歌 晴天` / `网易web 晴天` / `QQ点歌 晴天` | 指定音源 |
| `2` / `2 语音` / `2 文件` | 回复序号，可指定发送方式 |
| `音乐状态` | 输出自检报告（ffmpeg / 端点连通性 / 缓存目录 / 发送策略） |

> 命令需要**@机器人或唤醒词**触发。回复序号时不需要再 @。

最基本的一条流程：

```
群里 -> 点歌 晴天
机器人 -> 🔍 「晴天」找到 5 首，回复序号选择（60 秒内）：
          1. 晴天 - 周杰伦  [269s]
          2. ...
机器人 -> （语音）
```

### 2. 配置项

在 AstrBot 的 WebUI 插件配置页里改，对应 `_conf_schema.json`。

| 配置项 | 默认 | 作用 |
|---|---|---|
| `default_player_name` | `网易点歌` | 「点歌 歌名」用哪个平台。可选 `网易点歌` / `QQ点歌`；也可在命令里直接指定（见上表） |
| `source_endpoints` | 两个实测可用端点 | **多端点回退列表**，建议自行部署 Meting 后替换 |
| `meting_token` | 空 | 只在把 `source_endpoints` 指向**自建** [Meting-API](https://github.com/metowolf/Meting-API) 时才填，须与它的 `METING_TOKEN` 一致。不填会出现「搜得到歌、一取直链就 401」（见第三节 6） |
| `request_timeout` | 8 | 单次请求超时（秒） |
| `request_retries` | 2 | 单端点重试次数 |
| `song_limit` | 5 | 搜索返回的候选数量 |
| `selection_timeout` | 60 | 选歌等待时长，治「点歌超时！」 |
| `voice_strict` | `true` | **语音失败不降级成文件**，直接报错说明原因 |
| `send_modes` | 语音链接 → 本地语音 → 文件 → 文本 | 优先级链。`record_link` 排第一是为了绕开单帧体积上限 |
| `ffmpeg_convert` | `true` | 发送前用 ffmpeg 转码 |
| `voice_format` | `auto` | `auto`＝`wav`(16kHz 单声道)。可选 `wav8k`（体积减半）/ `mp3`（**别配给 record_local**，会被 AstrBot 放大 5 倍） |
| `max_payload_bytes` | `8388608`（8 MiB） | **单帧体积闸门**。超限先自动降档一次，仍超就拒绝发送并报出具体数字。填 `0` 不限制 |
| `dedup_seconds` | `20` | **同一会话内同一首歌的去重窗口**，治「同一首歌发两遍」。填 `0` 关闭 |
| `max_voice_seconds` | 600 | 超长音频截断，避免发送失败 |
| `record_unsupported` | 空 | 不支持发语音的平台名单（如 `telegram`），留空表示都尝试 |
| `proxy` | 空 | 容器里填宿主机代理要写 `http://172.17.0.1:7890` |
| `recall_select` | `true` | 点歌成功后撤回候选列表 |

### 3. 发送方式优先级

`send_modes` 是一条**优先级链，任意一环失败就往后降级**。各档位含义：

| 档位 | 做什么 | 一帧多大 | 备注 |
|---|---|---|---|
| `record_link` | 只把音频直链递给协议端，让它自己去下 | 几十字节 | **最稳，默认排第一** |
| `record_local` | 下载 → 转码 → base64 塞进一帧发出去 | 几 MB ~ 几十 MB | 会撞协议端 `maxPayload`，见第四节 3 |
| `file_local` | 当文件发（本地文件） | — | 不校验内容，容易「静默成功」 |
| `file_link` | 当文件发（直链） | — | 同上 |
| `text` | 只发一个链接 | — | 兜底 |

> **想「只发语音、彻底不要文件」**：把 `send_modes` 里的 `file_local`/`file_link` 删掉，
> 并保持 `voice_strict = true`。这样语音发不出去时会**明确报错**，而不是偷偷换成文件。

### 4. 常见问题

**Q：歌发出去了，但过十几二十秒又发了一遍？**
A：AstrBot 的会话机制会把你回复的那条消息重新投递一遍、再触发一次 LLM，LLM 顺手又点了首歌。
本插件已用 `should_call_llm(False)` + `dedup_seconds` 治住。想手动「再放一遍」，
把 `dedup_seconds` 设成 `0`。原理见[第四节 4](#4-为什么同一首歌会发两遍)。

**Q：还是收到文件，不是语音？**
A：先发 `音乐状态` 看自检报告。按顺序检查：① 容器里 ffmpeg 是否就绪；
② `send_modes` 里语音档有没有被文件档压在前面；③ `record_unsupported` 里有没有写你的平台名；
④ `voice_strict` 是否为 `true`。

**Q：提示「语音体积超限：预计 X MB > 上限 Y MB」？**
A：这就是第四节 3 那个「单帧太大」的问题被提前拦下了。三个解法：
调小 `max_voice_seconds`、把 `voice_format` 设为 `wav8k`、
或调大 `max_payload_bytes`（前提是协议端的 `maxPayload` 也够大）。

**Q：发了没反应，几分钟后才报超时？**
A：多半是音源接口挂住（aiohttp 默认总超时 300 秒）。本插件已把它收紧到秒级并加了多端点回退。
如果仍慢，把 `request_timeout` 调大、多配几个 `source_endpoints`；
**境外服务器**建议加 `proxy`。详见[第四节 1](#1-总是超时的三个来源)。

**Q：`点歌超时！`？**
A：这是另一回事 —— 发出候选后 60 秒内没回序号就会取消。调 `selection_timeout` 即可。

**Q：选 `QQ点歌` 提示「无可用音源」/ 点了完全没反应？**
A：旧版本的 `_conf_schema.json` 允许把默认平台选成「QQ点歌」，但仓库里**并没有 QQ 音源**，
`get_player()` 匹配不到就返回 `None`，LLM 工具路径会直接回一句「无可用音源」。
现已内置 `TencentMeting`，升级后即可用。

**Q：QQ 音乐搜得到，但一放就失败？**
A：版权热门歌（周杰伦等）在公共端点上拿不到直链 —— 上游不给 `vkey`，Meting 返回一个
**0 字节的 HTML 页，而状态码是 200**。非版权歌正常。要放版权歌请自建 Meting-API
并注入 QQ 登录 Cookie，见[第三节 6](#6-让-qq-音乐真正能放自建-meting-api)。

**Q：自建了 Meting-API，搜索正常但取直链报 401？**
A：`meting_token` 没填或和服务的 `METING_TOKEN` 不一致。Meting-API 对
`url`/`pic`/`lrc` 强制校验 `auth=HMAC-SHA1(密钥, server+type+id)`，而 `search` 免鉴权，
所以只会在取链这一步炸。填上密钥即可，插件会自动算签名。

### 5. 加一个新音源（不用改主流程）

`core/platform/meting.py` 里的 `TencentMeting`（QQ 音乐）就是照这个方式加的，
可以直接拿它当范例：

```python
# core/platform/meting.py
class KugouMeting(MetingPlayer):
    server: ClassVar[str] = "kugou"
    platform: ClassVar[Platform] = Platform(
        name="kugou", display_name="酷狗点歌", keywords=["酷狗点歌", "酷狗"]
    )
```

然后在 `core/platform/__init__.py` 的 import 和 `__all__` 里加上类名即可 ——
`main.py` 会通过 `BaseMusicPlayer.get_all_subclass()` 自动发现（见 `base.py` 的 `__init_subclass__`）。

如果需要完全自建搜索（不走 Meting），参考 `NeteaseWeb`：覆盖 `fetch_songs()`，
直链用 `audio_url_candidates()` 补全。

> ⚠️ **写 `keywords` 时别用太短的词。** `get_player()` 对命令词做的是**子串匹配**，
> 而 `on_song` 只要消息以 @机器人 / 唤醒词开头就会接管。早期 `TencentMeting`
> 用过裸 `"qq"`，结果群里一句「@bot qq群 123」也会被命中、插件跑去搜「群 123」。
> 现在改成了 `["QQ点歌", "QQ音乐", "tencent"]`。

### 6. 让 QQ 音乐真正能放：自建 Meting-API

**先说结论：公共端点上的 QQ 音乐只有「非版权歌」能放，版权热门歌永远拿不到。**
这个不是本插件的 bug，也不是配置问题，实测数据如下（2026-10）：

| 探测目标 | 周杰伦《晴天》（版权热门） | 非版权翻唱 |
|---|---|---|
| `api.qijieya.cn` 取直链 | ❌ `HTTP 200` + `Content-Type: text/html` + **0 字节** | ✅ `audio/mpeg`，4.46 MB |
| `api.i-meto.com` 取直链 | ❌ `404`（带它自己发的 `auth`） | ❌ `404` |
| 两个端点的 `search` | ✅ 都能返回 30 条，ID 是 QQ 的字母数字 mid（如 `0039MnYb0qxYhV`） | ✅ |

为什么是**静默失败**：QQ 音乐对版权歌不签发 `vkey`，Meting 上游拿不到就回一个空的
HTML 页 —— 状态码是 200，看着完全正常，只能靠下载层的魔数校验识别出来。

**要拿到版权歌的直链，必须带上 QQ 音乐的登录态（Cookie）。** 公共端点不会替你带，
所以正路是自建一个：

```bash
# 1. 起一个自己的 Meting-API（官方镜像）
docker run -d --name meting-api -p 8080:80 \
  -e METING_TOKEN=你的密钥 \
  -e METING_COOKIE_TENCENT="从 y.qq.com 登录后 F12 复制的完整 cookie" \
  ghcr.io/metowolf/meting-api:latest

# 2. 验证搜索（免鉴权，应返回 JSON 数组）
curl "http://127.0.0.1:8080/api?server=tencent&type=search&id=晴天"

# 3. 验证取直链（敏感接口，须带 auth=HMAC-SHA1(你的密钥, server+type+id)）
#    这一步能不能出 302，就是「QQ 版权歌到底能不能放」的判据
```

然后在 AstrBot 的插件配置里：

```
source_endpoints = ["http://<你的服务器IP>:8080/api"]
meting_token     = 你的密钥          # 必须与上面 METING_TOKEN 一致
default_player_name = QQ点歌         # 可选
```

**为什么一定要填 `meting_token`**：Meting-API 把 `url` / `pic` / `lrc` 列为敏感接口，
强制校验 `auth`，而 `search` 是免鉴权的。只改 `source_endpoints` 不填密钥，
故障会表现成**「歌搜得到，一取直链就 401」**——看起来像「音源坏了」。
插件会在配置了该项时自动为每首歌算好签名（见 `meting.py` 的 `_auth()`）。

**两个容易忽略的前提：**

- **Cookie 需要会员账号。** 账号没有对应权益时，热门歌照样不给 `vkey`
  （上游返回的错误是「全部 quality 都被拒」，本质是账号没权限，不是密钥错）。
  Cookie 会过期，`meting-api` 的 Cookie 缓存 5 分钟，用文件方式挂载时改完即生效。
- **QQ 音乐有地区限制，出口 IP 必须在国内。** 服务器在境外时，QQ 音源基本取不到直链
  （网易云不受影响）。这也是为什么放在境内云服务器上的 AstrBot 反而更合适。

**不想折腾的替代方案**（按推荐度）：

1. **就用网易云。** 本插件默认音源实测可用，绝大多数歌曲都有；QQ 音源的价值主要在
   「只有 QQ 才有的独家/翻唱」和搜得准。
2. **用第三方 QQ 音乐 API 项目**：`jsososo/QQMusicApi`、`Rain120/qq-music-api`、
   `CZ-Gen/QQMusicApi` 这类项目可以直接拿 `vkey` 拼直链（`M500`/`M800`/`F000` 等档位），
   但它们同样**需要你提供登录 Cookie**，且返回结构不是 Meting 格式 —— 要用的话得
   自己写一个 `BaseMusicPlayer` 子类来对接（照 `NeteaseWeb` 的写法）。
3. **公有云函数 / Koyeb 一键部署 Meting-API**：Meting-API 官方 README 有一键部署按钮，
   但**免费区域多在境外**，会撞上上面那条地区限制，QQ 音源大概率不可用。

---

## 四、原理与排查（可选深读）

这一节解释「为什么这么设计」。**平时不用读** —— 但你如果遇到怪问题，
或者想给别的插件提 issue，这里的每一条都是核对过 AstrBot 源码 / 实测记录的。

### 1. 「总是超时」的三个来源

`send_modes` 是优先级链，任意一环失败就往后降级，所以「超时」可能来自不同地方：

1. **HTTP 没有设总超时。** aiohttp 默认 `total=300s`。网易 web 接口被限流、或 NodeJS 音源域名挂了，请求就一直挂着，表现为「发了没反应，几分钟后才报超时」。
2. **音源端点是单点。** 用「nj点歌」时走 `https://163api.qijieya.cn` 这类公共域名，挂了就是死等。
   → 另外，**如果云服务器在境外**，访问国内音源接口本身就可能很慢或不通，这是超时的高发原因。
3. **选歌等待超时。** 提示 `点歌超时！` 是另一回事：默认 30 秒内不回序号就取消，调 `selection_timeout` 即可。

### 2. 「发的是文件不是语音」

`send_modes` 默认 `record_link → record_local → file_local → text`，
**前面失败就往后降级**，而 file 模式不校验内容，所以最后往往「成功」发出去一个文件。

**语音链路里 ffmpeg 是必经环节**，这点我核对了 AstrBot 源码：

```
Record.convert_to_base64()
  -> MediaResolver(..., media_type="audio").to_base64(target_format="wav")
  -> astrbot/core/utils/media_utils.py:
        args = ["ffmpeg", "-y", "-i", audio_path]      # 裸命令，走 PATH
        except FileNotFoundError: raise Exception("ffmpeg not found")
```

所以容器里 ffmpeg 不可用时，你会看到 `语音组件发送失败：Exception: ffmpeg not found`。

**一个容易忽略的细节**：`media_utils.py:1628-1630` 有个短路逻辑 —— 如果源文件后缀是 `.wav` 且文件头魔数也是 wav，它**直接复用、完全不调用 ffmpeg**。
这正是本插件 `voice_format = wav` 存在的意义：容器里没有 ffmpeg 时的活路。

### 3. 真正的元凶：那条几十 MB 的 WebSocket 帧

这一条同时解释了「**总是超时**」和「**最后发出来是个文件**」，值得单独讲。

`Record.convert_to_base64()` 把 `target_format` **写死成 `"wav"`**（`components.py:262`），
而 `media_utils.convert_audio_format(output_format="wav")` **不给 ffmpeg 加任何 `-ar/-ac` 参数**
（`media_utils.py:1525` 往后根本没有 wav 分支）—— 于是 ffmpeg 沿用源文件的采样率与声道：

```
源是 44.1kHz 立体声的 mp3  ->  转出来的 wav 还是 44100Hz 立体声 = 176 KB/s
base64 再放大 4/3                                            ≈ 235 KB/s
一首 4~5 分钟的歌：49 MB 的 wav  ->  一条约 65 MB 的 WebSocket 帧
```

NapCat / Lagrange 的 WS 接收端有 `maxPayload` 上限。一超就抛：

```
RangeError: Max payload size exceeded
    at Receiver.getPayloadLength64 (node_modules/ws/lib/receiver.js:406:10)
[WebSocket Client] 反向WebSocket (ws://localhost:6199/ws) 连接错误
```

连接被断开 → AstrBot 那个 `send_group_msg` 的 API 调用**永远等不到回包 → 报超时**
→ 插件 `except Exception` 捕获 → 降级到下一个 `send_modes` → `file_local` 走的是另一条通道，**成功了**
→ 用户收到一个文件，日志里只有一句「超时」。

> 这也解释了为什么「**歌比较大**的时候发不出去」—— 小文件刚好卡在阈值以内，大歌必炸。

**由此推出两条反直觉的结论：**

1. **把音频转成 mp3 交给 `record_local` 是反向优化。** AstrBot 会把它重新膨胀成 44.1kHz 立体声 wav，比不转还大 5 倍。
   mp3 只对 `record_link` / `file_*` 有意义。
2. **唯一能钳住体积的杠杆是「我们自己先把它变成 `.wav`」** —— 因为魔数匹配会被 `ensure_wav()` 直接复用，
   此时体积由**我们**的 `-ar/-ac` 决定：

   | 档位 | 每秒 | 4 分钟的歌（base64） | 音质 |
   |---|---|---|---|
   | 不预处理（AstrBot 默认行为） | ≈ 235 KB/s | **≈ 65 MB** | 最好，但发不出去 |
   | `wav`＝16kHz 单声道（默认） | 41.7 KB/s | ≈ 9.8 MB | 可以 |
   | `wav8k`＝8kHz 单声道 | 20.8 KB/s | ≈ 4.9 MB | 发闷，但稳 |

**如果你在还用 `Zhalslar/astrbot_plugin_music`，不改代码的解法是把 `send_modes` 里
`record_link` 提到 `record_local` 前面**：`record_link` 只把直链 URL 递过去，一帧几十字节，
由协议端自己去下，完全绕开这个问题。（我们骨架的默认值已经这么设了。）

> 顺带一提：作者在 issue #67/#68 里提过 `napcat_record_source`（`base64`/`url`/`local_file` 三选一）
> 来解决这件事，但 **PR #68 至今是 closed、未合并**，main 分支上没有这个配置项 —— 别去配置里找它。

### 4. 为什么同一首歌会发两遍

现象：回了个序号，歌发出来了，**过十几二十秒又发了一遍**。两条日志一模一样：

```
[astrbot_plugin_voice_music] [core.sender] record_local 发送成功：...
[astrbot_plugin_voice_music] [core.sender] record_local 发送成功：...   ← 24 秒后又来一次
```

根因不在本插件，在 AstrBot 的会话机制：

`session_waiter` 截获一条消息（这里是用户的「1 语音」）后，**会把它浅复制成一个新事件，
重新投递走一遍完整流水线**。那条消息于是又触发了一次 LLM 回复 ——
而 LLM 看到的上下文是「候选列表 + 用户回复的 1 语音」，它很自然判断出用户想听第 1 首，
**于是调用本插件的点歌工具再发一次**。

两个关键点，都反直觉：

1. **`event.stop_event()` 挡不住它。** 它只阻止事件向后续 listener / handler 传播，
   而 LLM 请求是由另一个开关控制的 —— `event.should_call_llm(False)`。
2. **两条日志长得一样，所以看不出是两条路径。** 命令路径用的是你指定的 `record_local`；
   LLM 工具路径用的是默认链（`record_link` 先试，拉不到直链再退到 `record_local`），
   最后落在同一个 `record_local 发送成功` 上。这也是排查时最容易卡住的地方。

本插件的两道对策：

- **`main.py` 的 `_no_llm()`**：一旦确认消息属于本插件（`点歌 ...` 或选歌回复），
  立刻 `event.should_call_llm(False)` —— 从源头掐掉 LLM，不让它有机会再点一次。
- **`core/sender.py` 的去重窗口**（`dedup_seconds`，默认 20 秒）：
  同一会话内、同一首歌在窗口内只发一次。这是兜底，不依赖上面那条能不能生效。

> 想在窗口内「再放一遍」？把 `dedup_seconds` 设成 `0` 就关掉了。
> 另外 `sender.py` 里每种发送方式的失败原因现在用 **INFO** 级别记录，
> 所以你会在日志里看到完整链路（`record_link 失败：... → record_local 发送成功`），
> 而不是只有一个孤零零的成功 —— 这正是上面第 2 点难查的原因。

### 5. 一个静默失效：Meting 不给 `id`，多端点回退形同虚设

`MetingPlayer.audio_url_candidates()` 是这么写的：

```python
urls = [song.audio_url] if song.audio_url else []
if song.id:                                  # ← 守卫
    for base in self.cfg.source_endpoints:
        urls.append(f"{base}?server={server}&type=url&id={song.id}")
```

思路是「同一首歌在每个端点上各生成一条取链地址，下载时逐个试」。但实测两个公共端点的
**search 响应里根本没有 `id` 字段**（2026-10 实测）：

```json
{"name": "...", "artist": "...", "url": "https://api.qijieya.cn/meting/?server=netease&type=url&id=2652820720",
 "pic": "...", "lrc": "..."}
```

歌曲 ID 是被塞在 **`url` 里**的。不去抠它，`Song.id` 恒为空串，`if song.id:` 永远不成立 ——
**多端点回退一行都不会执行**，只剩 `song.audio_url` 一条候选。
第一个端点抽风就整体失败，而你从日志上完全看不出「回退根本没跑」。

修法是从 `url` 里正则抠出 ID（`core/platform/meting.py` 的 `_ID_IN_URL`），
取值顺序为 `id` → `url_id` → 从 `url` 抠。修完实测：

```
✓ 所有歌都解析出了 id（如 2652820720）
  audio_url_candidates 生成 3 条候选
    - https://api.qijieya.cn/meting/?server=netease&type=url&id=2652820720
    - https://api.i-meto.com/meting/api?server=netease&type=url&id=2652820720
    ✓ 多端点候选回退可用（单端点抽风时能自动换）
```

> 注意第二条候选实际会返回 401（i-meto 需要 `auth` 参数，而那个参数只存在于它自己给的
> `url` 里）。这没关系 —— 候选本来就是「逐个试，失败就下一个」，
> 而且**排在第一位的那条正是带 `auth` 的原始地址**。这也恰好说明多候选不是多余的。

### 6. 环境体检脚本（推荐先用它定位）

两个脚本都只用标准库，**不需要先装插件**：

| 脚本 | 特点 | 用法 |
|---|---|---|
| `tools/diagnose_in_container.py` | 全量：8 项 PASS/FAIL + 汇总 | 需要先传进容器 |
| `tools/probe_min.py` | 极简：60 行，**可以直接粘贴**，重点算「那条帧有多大」 | 无需传文件 |

#### 怎么把脚本弄进容器（`docker cp: no such file or directory` 的原因）

`docker cp` 的路径是**在宿主机上**解析的。如果你在本机改脚本、却把命令敲在云服务器上，
`docker cp tools/diag.py astrbot:/tmp/` 一定报 `lstat /home/<用户>/tools: no such file or directory`
—— 服务器上根本没有那个 `tools/` 目录。**先 scp 上去，再 docker cp 进容器。**

**方式 1：先传服务器，再传容器**（最常规）

```bash
# ① 从你自己的机器传到服务器（路径改成你本地仓库的实际位置）
scp tools/diagnose_in_container.py <服务器用户>@<服务器IP>:~/diag.py

# ② 在服务器上
docker cp ~/diag.py astrbot:/tmp/diag.py
docker exec -it astrbot python /tmp/diag.py
```

**方式 2：不传文件，直接粘进去**（最省事，推荐用来快速确认体积问题）

```bash
docker exec -i astrbot python - <<'PY'
# 把 tools/probe_min.py 的内容整段粘在这里
PY
```

`-i` 不能少（要把 stdin 喂给容器），也**不要加 `-t`**（会回显、把代码打乱）。

**方式 3：走挂载卷**（如果你给 AstrBot 挂了 data 目录）

```bash
# 先看容器挂了宿主机哪些目录
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' astrbot
# 假设输出里有 /mnt/nas/docker/astrbot/data -> /AstrBot/data
# 那就直接放到宿主机那个目录里，容器里立刻就能看见
cp diag.py /mnt/nas/docker/astrbot/data/diag.py
docker exec -it astrbot python /AstrBot/data/diag.py
```

#### 脚本会检查什么

1. ffmpeg 可用性与版本
2. 临时目录可写性 + 剩余磁盘
3. 各音源端点连通性与耗时
4. 能否取到音频直链
5. **下载到的内容是不是真音频**（识别文件头魔数）← 原版最常翻车的一环
6. ffmpeg 转 mp3 / wav
7. **AstrBot 自己的 `MediaResolver.to_base64(target_format="wav")`** ← 直接复刻 `Record` 实际走的代码。
   `probe_min.py` 在这一步会**同时**报「原始 mp3」和「已压到 8k 的 wav」两条 base64 长度 ——
   两个数字一对比，`Max payload size exceeded` 是不是你的根因就一目了然了
8. **读取现有音乐插件的配置**，检查 `send_modes` 里语音档是否被文件档压在后面、`record_unsupported` 是否非空

> 第 8 步排查的是最容易被忽略的一个原因：如果配置里 `file_local` 排在 `record_local` 之前，
> 或者 `record_unsupported` 里写了你的平台名，那不管别的多正常，**结果都会是发文件**。

**怎么读结果**：报 FAIL 的**第一项**就是根因所在，后面的 FAIL 往往只是它的连带效应。

### 7. 自测

`.selftest/run_selftest.py` 用桩件模拟 AstrBot，跑通真实链路：

```bash
pip install aiofiles aiohttp        # 只有这两个是自测需要的
python .selftest/run_selftest.py
```

跑完会在系统临时目录 `wb_voice_music_selftest/` 留一批测试音频（几十 MB），可以随手删掉。
它会真的联网下载音频，所以结果也受你的网络影响。

已验证通过的项目（共 10 组）：

- 第一个端点是坏地址时，**8 秒内快速失败并回退**到备用端点（而不是挂 5 分钟）
- 搜索解析出候选歌曲；音频下载 11MB 并通过魔数校验
- **Meting 的 `id` 能从 `url` 里抠出来**，`audio_url_candidates()` 生成多条候选（否则多端点回退静默失效）
- **下载 HTML 页面时被正确拒绝**（这正是原版「把错误页当 mp3 发出去」的根因）
- `audio_url_candidates` 在首条直链指向坏端点时自动回退到第二条并下载成功
- **ffmpeg PATH 垫片生效**：原本 `shutil.which("ffmpeg")` 为 `None`，垫片后能解析到，
  即 AstrBot 内部的裸 `ffmpeg` 调用也能用（并已验证垫片文件非 0 字节、可执行）
- `resolve_voice_format("auto") == "wav"`（不能被解析成 mp3，否则线上体积会被 AstrBot 放大 5 倍）
- 转码产出：wav 16k = `16000Hz / mono / pcm_s16le`，wav8k = `8000Hz / mono / pcm_s16le`
- `音乐状态` 自检报告生成正常
- **体积闸门**：上限设成 1KB 时被拦下，返回「语音体积超限：预计 5.7 MB > 上限 1.0 KB…」，且**确实没有发出任何组件**
- **自动降档**：16k 预计 11.4 MB、8k 预计 5.7 MB，上限卡在中间时自动降到 wav8k 并成功发出
- **`main.py` 能被加载**：相对导入正常、命令 / 事件监听 / LLM 工具三类钩子全部注册，
  音源自动发现到 3 个（网易点歌 / 网易web / QQ点歌），
  并且 `点歌 晴天 1` 与 `音乐状态` 两条命令都真跑了一遍
- **QQ 音源可用**：`TencentMeting` 被自动注册；搜索返回真 QQ 数据、ID 从 url 里解析出
  字母数字 mid；非版权歌能真的下到音频（版权热门歌取不到，原因见第三节 6）
- **LLM 链路已被掐掉**：`点歌` 与选歌回复都会调 `should_call_llm(False)`，
  这是防「同一首歌发两遍」最关键的一层（第 9 组断言 `call_llm is False`）
- **去重闸门**：同一会话内同一首歌连发两次，第一次发出 1 条、第二次 0 条；
  换个会话点同一首则照常发出 1 条（不误伤别的群）；`dedup_seconds = 0` 时去重完全关闭
- **自建端点签名**：`meting_token` 生成的 `auth` 与
  `HMAC-SHA1(token, server+type+id)` 独立复算结果一致；未配置密钥时不加 `auth`；
  端点自带查询串时用 `&` 续接（不会拼出两个 `?`）

> 第 9 组是专门为「部署到服务器」加的：前 8 组只覆盖 `core/`，
> 入口文件如果装饰器签名或相对导入有问题，你在服务器上只会看到一句泛泛的加载失败。
> 第 10 组针对的是运行期才会暴露的「同一首歌发两遍」—— 见[第四节 4](#4-为什么同一首歌会发两遍)。

真实跑出来的数字（`晴天` 那首 11.2 MB 的 mp3）：

| 处理方式 | 预计线上 base64 体积 |
|---|---|
| 不预处理（AstrBot 默认会转 44.1kHz 立体声 wav） | ≈ 49 MB 的 wav → **≈ 65 MB 一帧** |
| 本插件 `wav` 档（16kHz 单声道） | 8.9 MB → **11.4 MB** |
| 本插件 `wav8k` 档（8kHz 单声道） | 4.5 MB → **5.7 MB** |

---

## 五、注意事项

- 公开音源端点随时可能失效或限速，**长期稳定请自建 [Meting-API](https://github.com/metowolf/Meting-API)** 并填入 `source_endpoints` + `meting_token`（见第三节 6）。
- 本插件默认带三个音源：网易点歌、网易web（实测可用）、QQ点歌。**QQ 音源在公共端点上的搜索没问题，但版权热门歌拿不到直链**（上游不给 `vkey`，Meting 返回空 HTML，状态码却是 200）。要放版权歌必须自建 Meting-API 并注入 QQ 登录 Cookie，且出口 IP 需在国内 —— 详见第三节 6。
- 语音能否送达最终取决于协议端（NapCat / Lagrange）。若某平台适配器不支持语音组件，把平台名填入 `record_unsupported`。
- 临时音频文件延迟 60 秒清理（`sender.py` 的 `_defer_cleanup`），避免适配器异步读取的竞态。
- **境外服务器**：国内音源接口可能不稳定，建议把 `request_timeout` 调大一点、多配几个 `source_endpoints`，
  必要时用 `proxy` 走宿主机代理。QQ 音源在境外取直链基本不可用。
