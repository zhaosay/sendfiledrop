# SendFileDrop

[English](#english) · [中文](#中文)

## English

SendFileDrop is a browser-based tool for sharing messages and files between devices on the same local network.

### Features

- Share text, clickable links, images, videos, audio, and other files in a shared timeline.
- Connect phones and computers with a LAN address or QR code.
- Search, filter, preview media, and bookmark items in the current browser.
- Paste screenshots or select multiple files, review the queue, and send them.
- Extract text from supported images on macOS using Apple Vision.
- Organize text, extract links, or translate text into Chinese using a compatible local AI service.
- Synchronize folders with the included sync client.

The interface uses the bundled ZhaoUI assets and supports light and dark themes. The current interface labels are in Chinese.

### Requirements

The core server requires Python 3 and uses the standard library. Frontend tests also require Node.js. Image recognition requires macOS, Swift, and Apple Vision.

AI processing requires an OpenAI-compatible chat endpoint at `http://127.0.0.1:21435/v1/chat/completions`, the `qwen2.5:3b` model, and the scheduler state file `~/ai/.state/current`. These environment-specific integrations are not included in this repository. The app reports when the service is unavailable or another configured AI service is active.

### Quick start

```bash
git clone https://github.com/zhaosay/sendfiledrop.git
cd sendfiledrop
python3 server.py
```

The default port is 8000. To use another port, run `python3 server.py 9000`. On macOS, you can also double-click `启动.command`. Open the displayed LAN address on another device on the same network, or scan the QR code under **连接设备**.

### Using the app

1. Type a message. Enter inserts a new line. Click **发送** to send.
2. Select files, drag them into the page, or paste an image. Review the pending list and click **发送**.
3. Click the text-recognition icon beside an image. Edit, copy, or place the result in the composer.
4. Enter text, choose an action under **AI 内容处理**, and click **处理输入框内容**. Review the result before copying it or replacing the composer text.
5. Use the sidebar to search, filter by sender, view favorites, change your nickname, or connect another device.

### File retention

Files smaller than 1 GiB expire after 3 days. Files of 1 GiB or larger expire after 1 day. An expired entry remains for 30 days and can be reshared. Text messages remain until deleted. The default upload-request limit is 10 GiB, and a text message can contain up to 20,000 characters.

### Configuration

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `SFD_DATA_DIR` | Directory for uploads, messages, and sync data | Project directory |
| `SFD_MAX_UPLOAD_BYTES` | Maximum upload request size in bytes | 10 GiB |
| `SFD_PASSCODE` | Optional access passcode for non-host devices | Unset |

```bash
SFD_DATA_DIR="$HOME/sendfiledrop-data" python3 server.py
```

When configured, non-host devices must enter the passcode. The sync client accepts `--passcode` or the `SFD_PASSCODE` environment variable.

### Folder synchronization

Create a sync folder on the host, then run the client on a device with a local folder:

```bash
python3 sync_client.py http://HOST:8000 --folder FOLDER_NAME --dir /path/to/local/folder
```

Add `--once` to run one synchronization pass. The client records revisions and creates conflict copies when both sides have changed.


### Interface and everyday workflows

- **Timeline:** text and files appear in one chronological feed. Your own entries appear on the right, and entries from other devices appear on the left.
- **Find an item:** search message text or filenames, select a sender, and combine content-type filters with today, this week, or this month.
- **Mobile layout:** the sidebar contains search, nicknames, favorites, recent senders, and the connection QR code. The top filter bar can be collapsed, and its state is saved in the browser.
- **Image preview:** double-click an image thumbnail, or double-tap it on a touch screen. Click outside the image, press Escape, or choose Close to dismiss the preview.
- **Favorites:** select the star on an entry to add it to the sidebar. Favorites are associated with the current browser device.
- **Devices and notifications:** each browser receives an editable nickname. The page shows online devices and can signal new content with a sound and a flashing title.
- **Deletion:** a device can delete its own entries. The host has additional controls, including clearing text messages.
- **Themes:** choose light, dark, or system appearance. The preference is saved in the browser.

The built-in QR generator and bundled ZhaoUI CSS and JavaScript do not require a network connection. Image recognition runs locally on macOS. The optional AI actions depend on the local integration described above.

### Project structure

```text
server.py       HTTP routes, static assets, access checks, and long polling
config.py       Port, data directory, limits, and environment settings
store.py        Text and file records, presence, and expiration
render.py       Timeline items and page template rendering
uploads.py      Streaming multipart upload parser
sync_store.py   SQLite-backed folder synchronization records
sync_client.py  Folder synchronization client
ocr.swift       macOS Vision image-text recognition
qr.py           Offline QR code generator
templates/      HTML page template
static/         Frontend scripts, styles, and bundled ZhaoUI assets
启动.command    macOS launch script
使用说明.md     Detailed Chinese usage guide
```

At runtime, the data directory contains `shared/`, `texts.json`, `files.json`, and synchronization data such as `sync.db`. The old `sync.json` format is imported into SQLite when appropriate, with a migrated copy retained.

### Troubleshooting

- **Other devices cannot connect:** check that they can reach the host on the same network, use the LAN address printed by the server, and check the host firewall and selected port.
- **Port 8000 is in use:** start the server on another port, for example `python3 server.py 9000`, and use that port on the other device.
- **Image recognition fails:** check that the image has not expired, is at most 20 MB, and that Swift and Apple Vision are available on the macOS host.
- **AI processing is unavailable:** check the local endpoint, model, and scheduler state described under Requirements. Retry when the configured AI service is available.
- **macOS blocks the launcher:** inspect the message in Privacy & Security and use the normal macOS controls to allow the script if you trust it.

### Version history

- **v0.3.3-dev** adds the collapsible mobile filter bar and remembers its state.
- **v0.3.2-dev** adds enlarged image previews.
- **v0.3.1-dev** improves streaming uploads, limits, pagination, long polling, SQLite synchronization records, and access controls.
- **v0.2.0** adds image paste, sender filtering, time filters, and interface updates.
- **v0.1.0** introduces the shared timeline, file retention, favorites, and search.

### Security and data

The server listens on the local network and uses plain HTTP. By default, devices that can reach it can access shared content. A passcode restricts access but does not encrypt traffic. Use a trusted network and avoid exposing the port directly to the public internet. Runtime data, including `shared/`, message records, sync data, and logs, is excluded by `.gitignore`.

### Tests

```bash
python3 -m unittest -q
python3 test_sync_e2e.py
```

The end-to-end sync test starts an isolated server on port 8130.

## 中文

SendFileDrop 是用于局域网内传送文件和文字的网页工具。Mac、电脑、平板和手机打开同一个地址，即可在共享时间线上交换消息与文件。

### 功能

- 在电脑或手机浏览器中分享文字、可点击链接、图片、视频、音频和其他文件。
- 使用局域网地址或二维码连接其他设备。
- 搜索和筛选记录、预览媒体，并在当前浏览器中收藏内容。
- 粘贴截图或选择多个文件，确认待发送列表后一起发送。
- 在 macOS 上使用 Apple Vision 提取支持格式的图片文字。
- 通过兼容的本地 AI 服务整理文字、提取链接或翻译成中文。
- 使用附带的同步客户端同步本地文件夹。

界面使用项目内置的 ZhaoUI 资源，支持浅色和深色主题。

### 环境要求

核心服务使用 Python 3 和标准库。前端测试需要 Node.js。图片文字识别需要 macOS、Swift 和 Apple Vision 框架。

AI 内容处理需要在 `http://127.0.0.1:21435/v1/chat/completions` 提供兼容 OpenAI 格式的本地聊天接口，安装 `qwen2.5:3b` 模型，并使用 `~/ai/.state/current` 中的本地 AI 调度状态。这些是特定环境的集成，未包含在本仓库中。如果集成不可用或其他已配置的 AI 服务正在运行，页面会显示提示。

### 快速开始

```bash
git clone https://github.com/zhaosay/sendfiledrop.git
cd sendfiledrop
python3 server.py
```

默认端口为 8000。如需使用其他端口，可运行 `python3 server.py 9000`。macOS 用户也可以双击 `启动.command`。其他设备连接同一个网络后，可以打开服务打印的局域网地址，或使用「连接设备」里的二维码。

### 使用方法

1. 在输入框输入文字。按 Enter 换行，点击「发送」发送。
2. 选择文件、拖入文件，或粘贴图片。确认待发送列表后点击「发送」。
3. 点击图片旁的文字识别图标，提取图片文字。结果可以编辑、复制或放入输入框。
4. 在输入框填写文字，在「AI 内容处理」中选择操作，再点击「处理输入框内容」。检查结果后可以复制或替换输入框内容。
5. 使用侧栏搜索、按发送者筛选、查看收藏、修改昵称或连接其他设备。

更详细的中文使用说明见 [使用说明.md](./使用说明.md)。

### 文件保留时间

小于 1 GiB 的文件在上传 3 天后过期，1 GiB 及以上文件在 1 天后过期。过期记录保留 30 天，可以通过重新上传续用。文字消息会保留到手动删除为止。默认单次上传请求上限为 10 GiB，一条文字消息最多 20,000 个字符。

### 配置

| 环境变量 | 作用 | 默认值 |
| --- | --- | --- |
| `SFD_DATA_DIR` | 上传文件、消息记录和同步数据的目录 | 项目目录 |
| `SFD_MAX_UPLOAD_BYTES` | 单次上传请求的字节数上限 | 10 GiB |
| `SFD_PASSCODE` | 非主机设备访问时使用的可选口令 | 未设置 |

```bash
SFD_DATA_DIR="$HOME/sendfiledrop-data" python3 server.py
```

设置口令后，非主机设备需要输入口令。同步客户端可以通过 `--passcode` 或 `SFD_PASSCODE` 环境变量提供口令。

### 文件夹同步

在主机上创建同步文件夹后，在需要同步本地目录的设备上运行：

```bash
python3 sync_client.py http://HOST:8000 --folder FOLDER_NAME --dir /path/to/local/folder
```

添加 `--once` 可只执行一次同步。客户端会记录版本，并在两端均有修改时创建冲突副本。


### 界面和日常使用

- **时间线：**文字和文件按时间显示在同一个列表中，自己的内容显示在右侧，其他设备的内容显示在左侧。
- **查找内容：**可以搜索文字或文件名，选择发送者，并结合内容类型以及今天、本周、本月筛选。
- **手机界面：**侧栏集中显示搜索、昵称、收藏、最近活跃设备和连接二维码。顶部筛选栏可以折叠，状态会保存在浏览器中。
- **图片预览：**双击图片缩略图，触屏设备可双击轻点。点击图片外区域、按 Esc 或选择关闭即可退出。
- **收藏：**点击记录旁的星形按钮，内容会出现在侧栏收藏列表中。收藏与当前浏览器设备关联。
- **设备和通知：**每个浏览器都有可修改的昵称。页面显示在线设备数量，并可通过提示音和标题闪烁提示新内容。
- **删除：**设备可以删除自己发送的记录。主机本机还有清空文字消息等管理操作。
- **主题：**可以选择浅色、深色或跟随系统，偏好保存在浏览器中。

内置二维码生成器和 ZhaoUI 的 CSS、JavaScript 运行时不需要联网。图片识别在 macOS 本机运行，AI 内容处理需要满足上文所列的本地集成条件。

### 项目结构

```text
server.py       HTTP 路由、静态资源、访问校验和长轮询
config.py       端口、数据目录、限制和环境配置
store.py        文字与文件记录、在线状态和过期清理
render.py       时间线记录和页面模板渲染
uploads.py      流式 multipart 上传解析
sync_store.py   基于 SQLite 的文件夹同步记录
sync_client.py  文件夹同步客户端
ocr.swift       macOS Vision 图片文字识别
qr.py           离线二维码生成器
templates/      HTML 页面模板
static/         前端脚本、样式和内置 ZhaoUI 资源
启动.command    macOS 启动脚本
使用说明.md     中文详细使用说明
```

运行时，数据目录包含 `shared/`、`texts.json`、`files.json` 和 `sync.db` 等同步数据。符合迁移条件的旧版 `sync.json` 会导入 SQLite，并保留迁移后的副本。

### 常见问题

- **其他设备无法连接：**检查设备是否能在同一网络中访问主机，使用服务打印的局域网地址，并检查主机防火墙和端口。
- **8000 端口被占用：**可以运行 `python3 server.py 9000`，然后在其他设备上使用新的端口。
- **图片识别失败：**检查图片是否已过期、大小是否不超过 20 MB，以及 macOS 主机是否具备 Swift 和 Apple Vision。
- **AI 内容处理不可用：**检查环境要求中列出的本地接口、模型和调度状态，待已配置的 AI 服务可用后重试。
- **macOS 阻止启动脚本：**检查「隐私与安全性」中的提示，并在确认信任脚本后使用系统提供的选项。

### 版本历史

- **v0.3.3-dev** 增加可折叠的手机顶部筛选栏，并记住折叠状态。
- **v0.3.2-dev** 增加图片放大预览。
- **v0.3.1-dev** 改进流式上传、大小限制、分页、长轮询、SQLite 同步记录和访问控制。
- **v0.2.0** 增加粘贴图片、发送者筛选、时间筛选和界面更新。
- **v0.1.0** 首次提供共享时间线、文件保留规则、收藏和搜索。

### 安全与数据

服务在局域网内监听，使用明文 HTTP。默认未设置口令，能够连接服务的设备可以访问共享内容。口令可限制访问，但不会加密传输。请在可信网络内使用，避免将端口直接暴露到公网。`shared/`、消息记录、同步数据和日志等运行数据已由 `.gitignore` 排除。

### 测试

```bash
python3 -m unittest -q
python3 test_sync_e2e.py
```

端到端同步测试会在 8130 端口启动隔离服务。
