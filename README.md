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

### 安全与数据

服务在局域网内监听，使用明文 HTTP。默认未设置口令，能够连接服务的设备可以访问共享内容。口令可限制访问，但不会加密传输。请在可信网络内使用，避免将端口直接暴露到公网。`shared/`、消息记录、同步数据和日志等运行数据已由 `.gitignore` 排除。

### 测试

```bash
python3 -m unittest -q
python3 test_sync_e2e.py
```

端到端同步测试会在 8130 端口启动隔离服务。
