# Arena2API

将 [arena.ai](https://arena.ai) 的 300+ 模型通过 **OpenAI 兼容 API** 代理出来。

## 技术原理

### 整体架构

```mermaid
graph LR
    Client["OpenAI 客户端<br/>(SDK / curl / 任意工具)"]
    Server["Python 代理服务器<br/>localhost:9090"]
    Ext["Chrome 扩展<br/>(arena.ai 页面内)"]
    Arena["arena.ai API"]

    Client -- "OpenAI 格式请求<br/>POST /v1/chat/completions" --> Server
    Ext -- "定时推送 cookies / models<br/>POST /v1/extension/push" --> Server
    Server -- "任务入队<br/>GET /v1/extension/fetch" --> Ext
    Ext -- "浏览器内 fetch 转发<br/>(携带真实 cookies + reCAPTCHA)" --> Arena
    Ext -- "SSE 数据块回传<br/>POST /v1/extension/fetch_chunk" --> Server
    Server -- "OpenAI SSE 格式<br/>data: {choices: [...]}" --> Client
```

### 扩展内部通信

Chrome 扩展由三层脚本组成，通过消息桥接实现跨 world 通信：

```mermaid
sequenceDiagram
    participant I as injector.js<br/>(MAIN World)
    participant C as content.js<br/>(ISOLATED World)
    participant B as background.js<br/>(Service Worker)
    participant S as Python Server
    participant A as arena.ai API

    Note over I: 页面加载完成
    I->>I: 提取 models (Next.js 数据)
    I->>I: 提取 cookies (document.cookie)
    I->>C: window.postMessage({type: INIT})
    C->>B: chrome.runtime.sendMessage({type: PAGE_INIT})
    B->>B: 合并 cookies + 存储 models
    B->>S: POST /v1/extension/push

    Note over B: 每 500ms 轮询任务队列
    B->>S: GET /v1/extension/fetch
    S-->>B: {task_id, url, payload, headers}
    B->>A: fetch(arena.ai, payload) — 浏览器内发出
    A-->>B: SSE 流式响应
    B->>S: POST /v1/extension/fetch_chunk × N
    B->>S: POST /v1/extension/fetch_chunk (done=true)
```

### 请求处理流程

当客户端发起 `/v1/chat/completions` 请求时：

```mermaid
flowchart TD
    A["客户端请求<br/>POST /v1/chat/completions"] --> B{扩展是否连接?}
    B -- "否 (last_push > 120s)" --> B1["返回 503"]
    B -- 是 --> C{模型是否存在?}
    C -- 否 --> C1["模糊匹配模型名"]
    C1 -- 仍未找到 --> C2["返回 404 + 可用模型列表"]
    C1 -- 找到 --> D
    C -- 是 --> D["构建 arena.ai 请求体"]
    D --> E["生成 UUIDv7 (eval_id)"]
    E --> F["拼接多轮对话历史"]
    F --> G["任务入队 RequestQueue"]
    G --> H["等待扩展轮询并转发"]
    H --> I["扩展用浏览器 fetch 发送到 arena.ai"]
    I --> J["扩展逐块回传 SSE 数据"]
    J --> K{stream 参数?}
    K -- true --> L["逐行解析 SSE → 转换为 OpenAI chunk 格式"]
    K -- false --> M["累积全部内容 → 返回完整 OpenAI 响应"]
```

### 关键技术点

| 技术点             | 说明                                                                                                           |
| --------------- | ------------------------------------------------------------------------------------------------------------ |
| **浏览器内转发**      | 所有请求由扩展的 background.js 用浏览器原生 `fetch` 发出，携带真实浏览器 TLS 指纹、cookies 和 reCAPTCHA 上下文，完全绕过 Cloudflare Bot 检测       |
| **任务队列轮询**      | Python 服务器将请求放入内存队列，扩展每 500ms 轮询 `/v1/extension/fetch` 取任务，完成后逐块通过 `/v1/extension/fetch_chunk` 回传 SSE 数据     |
| **双 World 注入**  | `injector.js` 运行在 MAIN world 可访问页面全局变量（grecaptcha、Next.js 数据）；`content.js` 运行在 ISOLATED world 可访问 Chrome API |
| **Cookie 分片处理** | arena.ai 的 auth cookie 可能被分片存储为 `arena-auth-prod-v1.0` + `arena-auth-prod-v1.1`，服务器自动拼接                      |
| **SSE 协议转换**    | arena.ai 使用自定义前缀（`a0:` 文本、`ag:` 推理、`ad:` 完成、`a2:` 心跳/图片、`a3:` 错误），服务器转换为标准 OpenAI SSE 格式                     |
| **模型自动发现**      | 从 Next.js 的 `__NEXT_DATA__` 或 `__next_f` 中提取 `initialModels`，自动分类 text / image / vision 模型                   |

## 快速开始

### 前置要求

- Python 3.10+
- Chrome 或 Firefox 浏览器
- 一个 [arena.ai](https://arena.ai) 账号（免费注册）
- macOS 用户需额外安装系统依赖：`brew install libidn2 rtmpdump`

### Step 1: 启动服务器

```bash
# 创建虚拟环境
uv venv .venv --python python3.10
source .venv/bin/activate

# macOS 系统依赖
brew install libidn2 rtmpdump

# 安装 Python 依赖
uv pip install curl-cffi fastapi uvicorn

# 启动服务
uv run python server.py

DEBUG=1 uv run python server.py

```

服务器默认监听 `http://localhost:9090`，启动后会等待扩展连接。

### Step 2: 安装浏览器扩展

#### Chrome 扩展

1. 打开 Chrome，地址栏输入 `chrome://extensions/`
2. 开启右上角的 **开发者模式**
3. 点击 **加载已解压的扩展程序**
4. 选择项目中的 `extension/` 目录

#### Firefox 扩展

1. 打开 Firefox，地址栏输入 `about:debugging#/runtime/this-firefox`
2. 点击 **临时载入附加组件**
3. 选择项目中的 `extension-firefox/manifest.json` 文件

### Step 3: 连接 Arena.ai

1. 点击扩展图标，点击 **Open Arena.ai** 按钮（或手动打开 `https://arena.ai/?mode=direct`）
2. 等待页面完全加载（约 3-5 秒）
3. 再次点击扩展图标，确认以下状态均为绿色：
   - **Server** → Connected
   - **Arena Tab** → Active
   - **Auth Cookie** → Yes
   - **Models** → 数量 > 0

### Step 4: 调用 API

```bash
# 查看所有可用模型
curl http://localhost:9090/v1/models

curl -s http://localhost:9090/v1/models | python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data'] if 'claude' in m['id'].lower()]"


# 非流式聊天
curl http://localhost:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "用python写一个排序算法"}]
  }'

# 流式聊天
curl http://localhost:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'

curl http://localhost:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"Hello!"}],"stream":true}'

```

### 在 OpenAI SDK 中使用

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9090/v1",
    api_key="not-needed",  # 无需 API Key
)

# 流式输出
response = client.chat.completions.create(
    model="GPT-4o",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### 支持的客户端

本服务兼容以下客户端和API网关：

- **OpenAI SDK** - 标准 OpenAI 格式
- **Claude Code** - Anthropic Claude 客户端
- **Gemini API** - Google Gemini 客户端
- **Codex** - OpenAI Codex 客户端
- **OpenCode** - 开源代码助手
- **NewAPI** - OpenAI API 网关
- **OneAPI** - 统一 API 网关

服务器会自动检测客户端类型（通过 User-Agent）并适配相应的响应格式。

## API 端点

| 端点                          | 方法   | 说明                           |
| --------------------------- | ---- | ---------------------------- |
| `/v1/models`                | GET  | 列出所有可用模型（OpenAI 格式）          |
| `/v1/chat/completions`      | POST | 聊天补全，支持 `stream: true/false` |
| `/v1/extension/push`        | POST | 扩展推送 cookies/models（内部使用）    |
| `/v1/extension/status`      | GET  | 查看扩展连接状态信息                   |
| `/v1/extension/fetch`       | GET  | 扩展轮询取待转发任务（内部使用）             |
| `/v1/extension/fetch_chunk` | POST | 扩展回传 SSE 数据块（内部使用）           |
| `/health`                   | GET  | 健康检查                         |

## 配置

### 服务器

| 环境变量      | 默认值    | 说明                                                      |
| --------- | ------ | ------------------------------------------------------- |
| `PORT`    | `9090` | 服务器监听端口                                                 |
| `API_KEY` | -      | （可选）启用 OpenAI 风格鉴权，要求 `Authorization: Bearer <API_KEY>` |
| `DEBUG`   | -      | 设置任意值开启调试日志                                             |

### 扩展

点击扩展图标，在弹窗中可修改 **Server URL**（默认 `http://127.0.0.1:9090`）。

### Chrome vs Firefox 差异

| 特性          | Chrome 扩展           | Firefox 扩展                  |
| ----------- | ------------------- | --------------------------- |
| Manifest 版本 | V3 (Service Worker) | V2 (Background Script)      |
| API 命名空间    | `chrome.*`          | `browser.*` (兼容 `chrome.*`) |
| 目录          | `extension/`        | `extension-firefox/`        |

## 项目结构

```
arena2api/
├── server.py              # FastAPI 代理服务器（OpenAI 格式转换 + arena.ai 调用）
├── requirements.txt       # Python 依赖（fastapi, uvicorn, httpx）
├── extension/             # Chrome 扩展 (Manifest V3)
│   ├── manifest.json      # 扩展清单（权限、脚本注入配置）
│   ├── background.js      # Service Worker — token 池管理、cookie 刷新、定时推送
│   ├── content.js         # Content Script (ISOLATED) — injector ↔ background 消息桥
│   ├── injector.js        # Page Script (MAIN) — reCAPTCHA 调用、模型提取、cookie 读取
│   ├── popup.html/js      # 扩展弹窗 UI（状态监控、手动操作）
│   └── icons/             # 扩展图标
├── extension-firefox/     # Firefox 扩展 (Manifest V2)
│   ├── manifest.json      # Firefox 扩展清单
│   ├── background.js      # Background Script — 兼容 Firefox API
│   ├── content.js         # Content Script — 兼容 Firefox API
│   ├── injector.js        # Page Script（与 Chrome 版本相同）
│   ├── popup.html/js      # 扩展弹窗 UI
│   └── icons/             # 扩展图标
└── README.md
```

## 常见问题

### 扩展状态显示 Disconnected

确认 Python 服务器已启动且端口正确。扩展每 30 秒自动推送一次，也可点击 **Push** 按钮手动触发。

### 模型列表为空

确保 arena.ai 页面已完全加载。模型列表从页面的 Next.js 数据中提取，如果页面未加载完成则无法获取。尝试刷新 arena.ai 页面。

### 请求返回 503

扩展超过 120 秒未推送数据，服务器判定连接断开。检查 arena.ai 标签页是否仍然打开，或重新打开页面。

### Token 不足导致请求失败

reCAPTCHA token 有效期约 2 分钟，扩展每 80 秒自动补充。如果短时间内大量请求耗尽 token 池，稍等片刻即可恢复。

## 注意事项

- **需要保持 arena.ai 标签页打开** — 扩展依赖页面环境获取 reCAPTCHA token
- **Token 有效期约 2 分钟** — 扩展自动刷新，高频请求时可能需要等待补充
- **免费使用** — arena.ai 本身免费，本工具仅做协议格式转换
- **模型名称** — 使用 arena.ai 原始名称（如 `GPT-4o`、`Claude 3.5 Sonnet`），通过 `/v1/models` 查看完整列表
- **仅限本地使用** — 服务器默认监听 `0.0.0.0`，生产环境请注意网络安全

在 Claude Code 客户端里，把 API Base URL 设置为你的本地服务：

方法1：环境变量

export ANTHROPIC\_BASE\_URL=<http://localhost:9090/v1>
export ANTHROPIC\_API\_KEY=any-string
方法2：\~/.claude/settings.json

{
"env": {
"ANTHROPIC\_BASE\_URL": "<http://localhost:9090/v1>",
"ANTHROPIC\_API\_KEY": "any-string"
}
}
然后在请求时指定模型，比如 claude-sonnet-4-6。

注意：Claude Code 默认用 Anthropic 的 /messages 接口格式，而你的服务是 OpenAI 的 /chat/completions 格式，两者不兼容，直接配置可能无法工作，需要在 server.py 里额外实现 /v1/messages 端点来兼容 Anthropic SDK 格式。

curl -s <http://localhost:9090/v1/messages> \
-H "Content-Type: application/json" \
-H "x-api-key: test" \
-H "anthropic-version: 2023-06-01" \
-d '{"model":"claude-sonnet-4-6","max\_tokens":1024,"messages":\[{"role":"user","content":"Hello!"}]}' | python3 -m json.tool
