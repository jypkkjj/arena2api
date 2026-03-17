python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python server.py

.venv/bin/pip install curl-cffi

curl -s http://localhost:9090/v1/models | python3 -m json.tool



出现了 Cloudflare 验证弹窗，这解释了为什么一直 403——Cloudflare 拦截了来自 Python 的请求（IP/TLS 指纹与浏览器不同），需要先通过 CF 人机验证才能获得 cf_clearance cookie。

这个问题比较难绕过，因为 Cloudflare Bot Management 会检测 TLS 指纹（JA3/JA4）、HTTP/2 指纹等，Python 的 httpx 和浏览器的指纹完全不同。

几个可能的解决方向：

方案1：用 curl-impersonate
使用能模拟浏览器 TLS 指纹的 HTTP 客户端：


pip install curl-cffi
把 httpx 替换为 curl_cffi，指定 impersonate="chrome131"，可以绕过 Cloudflare 的 TLS 指纹检测。

方案2：通过扩展转发请求
不在 Python 里直接请求 arena.ai，而是让扩展在浏览器内发请求，Python 只负责协调——但这样架构改动很大。

方案3：让 Python 走浏览器代理
用 Chrome DevTools Protocol 让 Python 借用浏览器的网络栈发请求。

建议先试方案1，改动最小。你要试试吗？



### 1. 创建虚拟环境

```bash
uv venv .venv --python python3.10
```

source .venv/bin/activate

brew install libidn2 rtmpdump

uv pip install curl-cffi fastapi uvicorn

uv run python server.py




curl -s http://localhost:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Max","messages":[{"role":"user","content":"hi"}],"stream":false}'
