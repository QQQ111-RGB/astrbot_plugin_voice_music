# astrbot_plugin_voice_music · 语音点歌

AstrBot 的语音点歌插件。照着 `Zhalslar/astrbot_plugin_music` 的思路重写，
针对「**总是超时**」和「**想要语音却发来文件**」这两个高频问题做了结构性修复。

```bash
cd /AstrBot/data/plugins && git clone <本仓库地址> astrbot_plugin_voice_music
```

群里发 `点歌 晴天` → 回序号选歌 → 发出**语音**。发 `音乐状态` 可看自检报告。

> **本 README 的排查步骤是按「云服务器 + Docker」部署写的。**
> 你本机有没有 ffmpeg **完全不重要** —— 相关的一切都发生在容器里。

## 这个插件的核心结论

如果你只读一段，读这段：

**AstrBot 发本地语音时，会把音频转成 wav 再 base64 塞进一条 WebSocket 帧，
而那个 wav 会沿用源文件的采样率。一首 4 分钟的歌 ≈ 65 MB 一帧。
协议端（NapCat / Lagrange）的 `maxPayload` 收不下就直接断连 ——
AstrBot 侧表现为「超时」，然后插件降级去发文件，你就收到一个文件。**
两个症状是同一个原因。

由此有两条反直觉的结论：

1. **把音频转成 mp3 交给本地语音发送是反向优化** —— AstrBot 会把它重新膨胀成
   44.1kHz 立体声 wav，比不转还大 5 倍。mp3 只对「语音链接 / 文件」方式有意义。
2. **唯一能钳住体积的杠杆，是自己先把音频转成低采样率 `.wav`** ——
   文件头魔数会被 AstrBot 直接复用、跳过二次转码，体积就由你说了算。
   16kHz 单声道 = 41.7 KB/s，8kHz 单声道 = 20.8 KB/s。

本插件默认把 **`record_link`（只把直链递过去，一帧几十字节）排在第一位**，
并在本地语音前加了一道**体积闸门**：超限先自动降到 8kHz 重转，仍超就拒绝发送并报出具体数字，
而不是让它变成一次「超时 + 默默发个文件」。完整推演见下面第一节。

| 你的问题 | 根因 | 本插件的对策 |
|---|---|---|
| 总是显示超时 | ① `aiohttp.ClientSession()` 默认总超时 **300 秒**，接口挂住就一直等；② 公共音源域名时好时坏，单点无备用；③ **语音那一条 WebSocket 帧太大，协议端直接断连**，AstrBot 等不到回包 → 报超时（见下面第二节） | `core/http.py`：秒级三级超时 + 有限重试 + **多端点自动回退**；`core/sender.py` **体积闸门**（超限不发，先自动降档） |
| 想要语音却发来文件 | ① 下载到的其实是 HTML 错误页，被当成 mp3；② 语音环节失败后**静默降级**到 file，file 不校验内容所以「成功」了；③ 超时的连带后果 —— 语音发不出去就退到 file | `core/downloader.py` 魔数校验；`core/audio.py` 转码 + PATH 垫片；`core/sender.py` **严格语音模式，失败直接报错不降级** |
| 服务器上没法排查 | Docker 里不方便随手开终端 | `core/diagnose.py`：发「**音乐状态**」输出自检报告 |

---

## 一、根因诊断（先看这段，能省很多时间）

### 1. 「总是超时」的三个来源

`send_modes` 是优先级链，任意一环失败就往后降级，所以「超时」可能来自不同地方：

1. **HTTP 没有设总超时。** aiohttp 默认 `total=300s`。网易 web 接口被限流、或 NodeJS 音源域名挂了，请求就一直挂着，表现为「发了没反应，几分钟后才报超时」。
2. **音源端点是单点。** 用「nj点歌」时走 `https://163api.qijieya.cn` 这类公共域名，挂了就是死等。
   → 另外，**如果云服务器在境外**，访问国内音源接口本身就可能很慢或不通，这是超时的高发原因。
3. **选歌等待超时。** 提示 `点歌超时！` 是另一回事：默认 30 秒内不回序号就取消，调 `selection_timeout` 即可。

### 2. 「发的是文件不是语音」

`send_modes` 默认 `ark_card → record_local → record_link → file_local → …`，
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

---

## 二、Docker 部署：先做这一步诊断

```bash
# 1) 容器里有没有 ffmpeg？（官方镜像的 Dockerfile 里是装了 ffmpeg + libavcodec-extra 的）
docker exec -it <容器名> ffmpeg -version

# 2) 如果上一条报 command not found，再看 pip 侧的情况
docker exec -it <容器名> python -c "import astrbot; print(astrbot.__file__)"
```

- **第一条有输出** → ffmpeg 齐备，语音链路没问题，直接去装插件。
- **第一条报 not found** → 按下面任一方式补上（推荐 A）。

### 方式 A：让插件自己补上（推荐，零手工、重建容器也不丢）

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

### 方式 B：直接往容器里装（临时有效，重建容器会丢）

```bash
docker exec -u root <容器名> apt-get update && docker exec -u root <容器名> apt-get install -y ffmpeg
```

### 方式 C：重建镜像（永久有效）

```dockerfile
FROM soulter/astrbot:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libavcodec-extra && rm -rf /var/lib/apt/lists/*
```

### 方式 D：把宿主机的静态 ffmpeg 挂进去

```bash
# 下载静态构建后
docker run ... -v /opt/ffmpeg/ffmpeg:/usr/local/bin/ffmpeg:ro ...
```

### Docker 相关注意点

- **插件目录**：`-v $PWD/data:/AstrBot/data`，插件放 `data/plugins/astrbot_plugin_voice_music/`。
- **缓存目录**：插件把音频缓存放 `data/temp/astrbot_plugin_voice_music/audio/`，也就是**挂载卷里**，不会撑爆容器可写层。
- **代理**：容器里的 `http://127.0.0.1:7890` 指的是容器自己，**不是宿主机**。要填宿主机上的代理得用
  `http://172.17.0.1:7890`（默认 bridge 网关，可用 `docker network inspect bridge` 确认）或
  `http://host.docker.internal:7890`（compose 里加 `extra_hosts`）。
- **看日志**：`docker logs -f <容器名> 2>&1 | grep voice_music` 能看到插件的启动自检输出。

### 环境体检脚本（推荐先用它定位）

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

---

## 三、目录结构

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
│   ├── sender.py                    ★ 发送策略：语音优先 / 体积闸门 / 不降级    → 治「变文件」
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

### 数据流

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

默认 `send_modes` 是 `record_link → record_local → file_local → text`：
先用**只递 URL**的轻量方式（一帧几十字节，永不触发体积上限），不行再走本地语音。

---

## 四、安装

**方式 1：克隆到插件目录**（推荐）

```bash
cd /AstrBot/data/plugins        # 宿主机上就是 $PWD/data/plugins/
git clone <本仓库地址> astrbot_plugin_voice_music
```

**方式 2：下载 ZIP**，解压后把整个目录放到 `AstrBot/data/plugins/astrbot_plugin_voice_music/`。

之后：

1. AstrBot 会自动安装插件依赖（装到 `data/site-packages`）。若没自动装，可在 WebUI 插件页手动触发，
   或 `docker exec -it <容器名> pip install -r /AstrBot/data/plugins/astrbot_plugin_voice_music/requirements.txt`。
2. 按第二节确认 ffmpeg。
3. 重启 AstrBot，在群里发 **`音乐状态`** 看自检报告。

## 五、命令

| 命令 | 说明 |
|---|---|
| `点歌 晴天` | 用默认平台搜索，回复序号选歌 |
| `点歌 晴天 1` | 直接选中第 1 首 |
| `网易点歌 晴天` / `网易web 晴天` | 指定音源 |
| `2` / `2 语音` / `2 文件` | 回复序号，可指定发送方式 |
| `音乐状态` | 输出自检报告（ffmpeg / 端点连通性 / 缓存目录 / 发送策略） |

## 六、关键配置

| 配置项 | 默认 | 作用 |
|---|---|---|
| `source_endpoints` | 两个实测可用端点 | **多端点回退列表**，建议自行部署 Meting 后替换 |
| `request_timeout` | 8 | 单次请求超时（秒） |
| `request_retries` | 2 | 单端点重试次数 |
| `selection_timeout` | 60 | 选歌等待时长，治「点歌超时！」 |
| `voice_strict` | `true` | **语音失败不降级成文件**，直接报错说明原因 |
| `send_modes` | 语音链接 → 本地语音 → 文件 → 文本 | 优先级链。`record_link` 排第一是为了绕开单帧体积上限 |
| `ffmpeg_convert` | `true` | 发送前用 ffmpeg 转码 |
| `voice_format` | `auto` | `auto`＝`wav`(16kHz 单声道)。可选 `wav8k`（体积减半）/ `mp3`（**别配给 record_local**，会被 AstrBot 放大 5 倍） |
| `max_payload_bytes` | `8388608`（8 MiB） | **单帧体积闸门**。超限先自动降档一次，仍超就拒绝发送并报出具体数字。填 `0` 不限制 |
| `max_voice_seconds` | 600 | 超长音频截断，避免发送失败 |
| `proxy` | 空 | 容器里填宿主机代理要写 `http://172.17.0.1:7890` |

> 想「只发语音、彻底不要文件」：把 `send_modes` 里的 `file_local`/`file_link` 删掉，保持 `voice_strict = true`。

## 七、加一个新音源（不用改主流程）

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

## 八、自测

`.selftest/run_selftest.py` 用桩件模拟 AstrBot，跑通真实链路：

```bash
pip install aiofiles aiohttp        # 只有这两个是自测需要的
python .selftest/run_selftest.py
```

跑完会在系统临时目录 `wb_voice_music_selftest/` 留一批测试音频（几十 MB），可以随手删掉。
它会真的联网下载音频，所以结果也受你的网络影响。

已验证通过的项目：

- 第一个端点是坏地址时，**8 秒内快速失败并回退**到备用端点（而不是挂 5 分钟）
- 搜索解析出候选歌曲；音频下载 11MB 并通过魔数校验
- **下载 HTML 页面时被正确拒绝**（这正是原版「把错误页当 mp3 发出去」的根因）
- `audio_url_candidates` 在首条直链指向坏端点时自动回退到第二条并下载成功
- **ffmpeg PATH 垫片生效**：原本 `shutil.which("ffmpeg")` 为 `None`，垫片后能解析到，
  即 AstrBot 内部的裸 `ffmpeg` 调用也能用（并已验证垫片文件非 0 字节、可执行）
- `resolve_voice_format("auto") == "wav"`（不能被解析成 mp3，否则线上体积会被 AstrBot 放大 5 倍）
- 转码产出：wav 16k = `16000Hz / mono / pcm_s16le`，wav8k = `8000Hz / mono / pcm_s16le`
- `音乐状态` 自检报告生成正常
- **体积闸门**：上限设成 1KB 时被拦下，返回「语音体积超限：预计 5.7 MB > 上限 1.0 KB…」，且**确实没有发出任何组件**
- **自动降档**：16k 预计 11.4 MB、8k 预计 5.7 MB，上限卡在中间时自动降到 wav8k 并成功发出

真实跑出来的数字（`晴天` 那首 11.2 MB 的 mp3）：

| 处理方式 | 预计线上 base64 体积 |
|---|---|
| 不预处理（AstrBot 默认会转 44.1kHz 立体声 wav） | ≈ 49 MB 的 wav → **≈ 65 MB 一帧** |
| 本插件 `wav` 档（16kHz 单声道） | 8.9 MB → **11.4 MB** |
| 本插件 `wav8k` 档（8kHz 单声道） | 4.5 MB → **5.7 MB** |

---

## 九、注意事项

- 公开音源端点随时可能失效或限速，**长期稳定请自建 [Meting-API](https://github.com/metowolf/Meting)** 并填入 `source_endpoints`。
- 本骨架默认只带网易云两个音源（实测可用）。QQ 音乐/酷狗在公开端点上普遍返回空结果或失效直链，需要自建服务。
- 语音能否送达最终取决于协议端（NapCat / Lagrange）。若某平台适配器不支持语音组件，把平台名填入 `record_unsupported`。
- 临时音频文件延迟 60 秒清理（`sender.py` 的 `_defer_cleanup`），避免适配器异步读取的竞态。
- **境外服务器**：国内音源接口可能不稳定，建议把 `request_timeout` 调大一点、多配几个 `source_endpoints`，
  必要时用 `proxy` 走宿主机代理。
