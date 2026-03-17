# arena2api 实现原理

## 概述

arena2api 是一个本地代理服务，将 [arena.ai](https://arena.ai) 的 AI 模型接口转换为标准 OpenAI API 格式，使任何兼容 OpenAI 的客户端（如 ChatBox、OpenCode、Cherry Studio 等）都可以免费使用 arena.ai 上的模型。

核心思路：用浏览器扩展"借用"已登录用户的身份（cookies + reCAPTCHA token），由本地 Python 服务代为发起请求。

---

## 架构总览

```
OpenAI 客户端
    │  HTTP (OpenAI 格式)
    ▼
┌─────────────────────────────┐
│   本地代理服务 server.py      │  :9090
│   (FastAPI + uvicorn)        │
└────────────┬────────────────┘
             │  HTTPS (arena.ai 内部格式)
             ▼
        arena.ai API
             ▲
             │  token / cookies 推送
┌────────────┴────────────────┐
│   Chrome/Firefox 扩展        │
│   background.js              │
│   content.js                 │
│   injector.js                │
└─────────────────────────────┘
        （运行在浏览器中，须打开 arena.ai 页面）
```

---

## 各模块详解

### 1. 浏览器扩展（三层架构）

扩展分三个脚本，运行在不同的隔离环境中：

#### injector.js — 页面主世界（MAIN world）

- **权限最高**，可直接访问页面所有全局变量
- **提取模型列表**：读取 `window.__NEXT_DATA__` 或 `window.__next_f`（Next.js 数据）中的 `initialModels`
- **获取 reCAPTCHA token**：调用 `window.grecaptcha.enterprise.execute(sitekey, {action})` 生成 V3 token
- **提取 cookies**：读取 `document.cookie`（非 HttpOnly 部分）
- 通过 `window.postMessage` 与 content.js 通信

#### content.js — 内容脚本（ISOLATED world）

- 作为 injector.js 与 background.js 之间的**消息桥梁**
- 监听 background.js 发来的需求（`NEED_TOKEN`、`NEED_MODELS`、`NEED_COOKIES`）
- 通过 `window.postMessage` 转发给 injector.js，再将结果回传
- 页面加载完成后定期轮询 reCAPTCHA 是否就绪，就绪后主动获取 token

#### background.js — Service Worker

- **管理 token 池**：维护最多 10 个 V3 token，每个有效期约 2 分钟；每 80 秒检查是否需要补充
- **收集 cookies**：通过 `chrome.cookies.getAll` 读取 `arena.ai` 域下的所有 cookies，并与页面 cookies 合并；特别处理分片存储的 auth token（`arena-auth-prod-v1.0` + `arena-auth-prod-v1.1`）
- **定期推送**：每 30 秒将 token、cookies、模型列表推送到本地代理的 `/v1/extension/push` 端点
- **响应服务器反馈**：服务器若返回 `need_tokens: true`，立即再请求一个新 token

---

### 2. 本地代理服务（server.py）

基于 **FastAPI + uvicorn**，监听 `0.0.0.0:9090`。

#### Store（状态存储）

单例对象，保存扩展推送来的所有状态：

| 字段 | 说明 |
|------|------|
| `cookies` | arena.ai 的完整 cookie 字典 |
| `auth_token` | JWT 认证 token |
| `cf_clearance` | Cloudflare 验证 cookie |
| `v3_tokens` | reCAPTCHA V3 token 池（最多 10 个，有效期 120s） |
| `v2_token` | reCAPTCHA V2 token（备用） |
| `text_models` | 文本模型映射：publicName → id |
| `image_models` | 图像模型映射：publicName → id |

`active` 属性：扩展在 120 秒内有过推送则视为在线。

#### API 端点

| 端点 | 说明 |
|------|------|
| `POST /v1/extension/push` | 扩展推送数据入口 |
| `GET /v1/extension/status` | 查看扩展连接状态 |
| `GET /v1/models` | 列出可用模型（OpenAI 格式） |
| `POST /v1/chat/completions` | 聊天补全（OpenAI 格式） |
| `GET /health` 或 `GET /` | 健康检查 |

#### 请求处理流程

```
POST /v1/chat/completions
    │
    ├─ 验证 API Key（可选，由 API_KEY 环境变量控制）
    ├─ 检查扩展是否在线（store.active）
    ├─ 解析模型名 → arena.ai model_id（支持模糊匹配）
    ├─ 构建 prompt（合并 system + history + user 消息）
    ├─ 弹出一个 reCAPTCHA token（V3 优先，V2 备用）
    ├─ 生成 UUIDv7 作为 eval_id / message_id
    ├─ 构建 arena.ai 请求体（mode: "direct"）
    ├─ 附加 cookie / auth header
    │
    ├─ stream=true  → StreamingResponse（SSE 流）
    └─ stream=false → 等待完整响应后返回
```

#### arena.ai 流协议解析

arena.ai 使用自定义的行格式流（非标准 SSE）：

| 前缀 | 含义 |
|------|------|
| `a0:` | 文本内容片段（JSON 字符串） |
| `ag:` | 推理内容（reasoning，如 o1 系列模型） |
| `ad:` | 完成信号，含 `finishReason` 和 `usage` |
| `a2:` | 图片 URL 列表，或 heartbeat |
| `a3:` | 错误信息 |

代理将上述格式实时转换为标准 OpenAI SSE chunk 格式输出。

#### UUIDv7 生成

arena.ai 要求 UUIDv7 格式的 ID（含毫秒时间戳）。`uuid7()` 函数自行实现：时间戳左移 80 位，拼接版本号 `0x7` 和随机位，再拼接变体位 `0x8`。

---

## 关键设计决策

### reCAPTCHA 绕过方式

arena.ai 每次请求需要有效的 reCAPTCHA V3 token。项目的做法是：
- 由运行在 arena.ai 页面中的扩展，**直接调用页面内已加载的 grecaptcha**，生成合法 token
- Token 有效期约 2 分钟，扩展提前批量获取并缓存，服务器每次请求消耗一个

### 身份认证

服务器向 arena.ai 发请求时，携带：
1. 完整的 `Cookie` 请求头（含 session、cf_clearance 等）
2. `Authorization: Bearer <auth_token>`（JWT，从 cookie 中提取）
3. 伪造正常 Chrome 浏览器的 `User-Agent`

### 多客户端兼容

通过 `User-Agent` 判断接入的客户端类型（claude/gemini/codex/openai），对 Claude/Anthropic 客户端额外添加 `type: "message"` 等字段以兼容其格式差异。

---

## 启动与使用

```bash
# 安装依赖
pip install -r requirements.txt

# 启动代理（默认端口 9090）
python server.py

# 可选环境变量
PORT=9090       # 监听端口
API_KEY=xxx     # 为 OpenAI 端点启用 Bearer 认证
DEBUG=1         # 开启调试日志
```

在 OpenAI 客户端中配置：
- **Base URL**: `http://localhost:9090/v1`
- **API Key**: 任意值（未设置 API_KEY 时不校验）

安装浏览器扩展并打开 `https://arena.ai`，等待扩展连接后即可使用。
