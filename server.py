"""
arena2api - Arena.ai to OpenAI API Proxy
=========================================

极简设计：Chrome 扩展提供 reCAPTCHA token 和 cookies，
本服务器负责 OpenAI 格式转换和 arena.ai API 调用。

使用方式：
  1. pip install -r requirements.txt
  2. python server.py
  3. 安装 Chrome 扩展，打开 arena.ai
  4. 在 OpenAI 客户端中配置 http://localhost:9090/v1
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Optional

import uvicorn
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse, JSONResponse

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("arena2api")

# ============================================================
# 配置
# ============================================================
PORT = int(os.environ.get("PORT", "9090"))
API_KEY = os.environ.get("API_KEY", "").strip()
ARENA_BASE = "https://arena.ai"
ARENA_CREATE_EVAL = f"{ARENA_BASE}/nextjs-api/stream/create-evaluation"
ARENA_POST_EVAL = f"{ARENA_BASE}/nextjs-api/stream/post-to-evaluation"  # + /{id}

# reCAPTCHA
RECAPTCHA_V3_SITEKEY = "6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I"

# ============================================================
# UUIDv7
# ============================================================
def uuid7() -> str:
    ts = int(time.time() * 1000)
    ra = secrets.randbits(12)
    rb = secrets.randbits(62)
    u = ts << 80 | (0x7000 | ra) << 64 | (0x8000000000000000 | rb)
    h = f"{u:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# ============================================================
# Token / Cookie Store（从扩展接收）
# ============================================================
class Store:
    def __init__(self):
        self.cookies: dict = {}
        self.auth_token: str = ""
        self.cf_clearance: str = ""
        self.v3_tokens: list = []  # [{token, action, ts}]
        self.v2_token: Optional[dict] = None
        self.last_push: float = 0
        self.models: list = []
        self.text_models: dict = {}  # publicName -> id
        self.image_models: dict = {}
        self.vision_models: list = []
        self.next_actions: dict = {}  # action name -> hash

    @property
    def active(self) -> bool:
        return self.last_push > 0 and (time.time() - self.last_push < 120)

    def push(self, data: dict):
        self.last_push = time.time()
        if data.get("cookies"):
            self.cookies = data["cookies"]
        if data.get("auth_token"):
            self.auth_token = data["auth_token"]
        if data.get("cf_clearance"):
            self.cf_clearance = data["cf_clearance"]
        # V3 tokens
        if data.get("v3_tokens"):
            for t in data["v3_tokens"]:
                tok = t.get("token", "")
                if not tok or len(tok) < 20:
                    continue
                age = t.get("age_ms", 0)
                if age > 120000:
                    continue
                if any(x["token"] == tok for x in self.v3_tokens):
                    continue
                self.v3_tokens.append({
                    "token": tok,
                    "action": t.get("action", "chat_submit"),
                    "ts": time.time() - age / 1000,
                })
            while len(self.v3_tokens) > 10:
                self.v3_tokens.pop(0)
        # V2 token
        if data.get("v2_token"):
            v2 = data["v2_token"]
            if v2.get("token") and v2.get("age_ms", 0) < 120000:
                self.v2_token = {
                    "token": v2["token"],
                    "ts": time.time() - v2.get("age_ms", 0) / 1000,
                }
        # Models
        if data.get("models"):
            self._update_models(data["models"])
        # Next actions
        if data.get("next_actions"):
            self.next_actions.update(data["next_actions"])

    def _update_models(self, models: list):
        self.models = models
        self.text_models = {}
        self.image_models = {}
        self.vision_models = []
        for m in models:
            name = m.get("publicName", "")
            mid = m.get("id", "")
            caps = m.get("capabilities", {})
            out_caps = caps.get("outputCapabilities", [])
            in_caps = caps.get("inputCapabilities", [])
            if "text" in out_caps:
                self.text_models[name] = mid
            if "image" in out_caps:
                self.image_models[name] = mid
            if "image" in in_caps:
                self.vision_models.append(name)

    def pop_v3_token(self) -> Optional[str]:
        now = time.time()
        self.v3_tokens = [t for t in self.v3_tokens if now - t["ts"] < 120]
        if not self.v3_tokens:
            return None
        return self.v3_tokens.pop(0)["token"]

    def pop_v2_token(self) -> Optional[str]:
        if not self.v2_token:
            return None
        if time.time() - self.v2_token["ts"] > 120:
            self.v2_token = None
            return None
        tok = self.v2_token["token"]
        self.v2_token = None
        return tok

    def build_cookie_header(self) -> str:
        parts = []
        for k, v in self.cookies.items():
            parts.append(f"{k}={v}")
        return "; ".join(parts)

    def status(self) -> dict:
        now = time.time()
        valid_v3 = [t for t in self.v3_tokens if now - t["ts"] < 120]
        return {
            "active": self.active,
            "last_push_ago": round(now - self.last_push, 1) if self.last_push else None,
            "v3_tokens": len(valid_v3),
            "has_v2": bool(self.v2_token and now - self.v2_token["ts"] < 120),
            "has_auth": bool(self.auth_token),
            "has_cf": bool(self.cf_clearance),
            "text_models": len(self.text_models),
            "image_models": len(self.image_models),
            "next_actions": list(self.next_actions.keys()),
            "cookies": list(self.cookies.keys()),
        }


store = Store()


# ============================================================
# 请求队列（扩展转发模式）
# ============================================================
class RequestQueue:
    def __init__(self):
        self._tasks: dict = {}   # task_id -> {payload, headers, url, event, chunks, done, error}

    def put(self, task_id: str, url: str, payload: dict, headers: dict):
        event = asyncio.Event()
        self._tasks[task_id] = {
            "url": url,
            "payload": payload,
            "headers": headers,
            "event": event,
            "chunks": [],
            "done": False,
            "error": None,
        }

    def get_pending(self) -> Optional[dict]:
        """取一个待处理任务（返回给扩展）"""
        for task_id, task in self._tasks.items():
            if not task["done"] and task["error"] is None and not task.get("claimed"):
                task["claimed"] = True
                return {
                    "task_id": task_id,
                    "url": task["url"],
                    "payload": task["payload"],
                    "headers": task["headers"],
                }
        return None

    def append_chunk(self, task_id: str, chunk: str):
        task = self._tasks.get(task_id)
        if task:
            task["chunks"].append(chunk)
            task["event"].set()

    def finish(self, task_id: str, error: Optional[str] = None):
        task = self._tasks.get(task_id)
        if task:
            task["done"] = True
            task["error"] = error
            task["event"].set()

    async def iter_chunks(self, task_id: str, timeout: float = 300):
        task = self._tasks.get(task_id)
        if not task:
            return
        idx = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            while idx < len(task["chunks"]):
                yield task["chunks"][idx]
                idx += 1
            if task["done"]:
                if task["error"]:
                    raise Exception(task["error"])
                break
            task["event"].clear()
            try:
                await asyncio.wait_for(task["event"].wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
        self._tasks.pop(task_id, None)


rq = RequestQueue()

# ============================================================
# FastAPI
# ============================================================
app = FastAPI(title="arena2api", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def verify_api_key(request: Request):
    """Optional API key auth for OpenAI endpoints.

    If API_KEY is set, require: Authorization: Bearer <API_KEY>
    """
    if not API_KEY:
        return

    auth_header = request.headers.get("authorization", "")
    expected = f"Bearer {API_KEY}"
    if auth_header != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ============================================================
# 扩展端点
# ============================================================
@app.post("/v1/extension/push")
async def extension_push(request: Request):
    """接收扩展推送的 token、cookies、models"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    store.push(data)
    need = len([t for t in store.v3_tokens if time.time() - t["ts"] < 120]) < 3
    return {
        "status": "ok",
        "need_tokens": need,
        "v3_count": len(store.v3_tokens),
    }


@app.get("/v1/extension/status")
async def extension_status():
    return store.status()


@app.get("/v1/extension/fetch")
async def extension_fetch():
    """扩展轮询：取一个待处理的请求任务"""
    task = rq.get_pending()
    if task:
        return task
    return {"task_id": None}


@app.post("/v1/extension/fetch_chunk")
async def extension_fetch_chunk(request: Request):
    """扩展推送流式数据块"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    task_id = data.get("task_id")
    chunk = data.get("chunk")
    done = data.get("done", False)
    error = data.get("error")
    if not task_id:
        raise HTTPException(400, "task_id required")
    if chunk is not None:
        rq.append_chunk(task_id, chunk)
    if done or error:
        rq.finish(task_id, error)
    return {"ok": True}


# ============================================================
# OpenAI 兼容端点
# ============================================================
@app.get("/v1/models")
async def list_models(request: Request):
    """列出可用模型"""
    verify_api_key(request)
    all_models = {}
    all_models.update(store.text_models)
    all_models.update(store.image_models)
    data = []
    for name in sorted(all_models.keys()):
        data.append({
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "arena.ai",
        })
    if not data:
        # 返回一个占位模型
        data.append({
            "id": "waiting-for-extension",
            "object": "model",
            "created": 0,
            "owned_by": "arena.ai",
        })
    return {"object": "list", "data": data}


def detect_client(request: Request) -> str:
    """检测客户端类型"""
    ua = request.headers.get("user-agent", "").lower()
    if "claude" in ua or "anthropic" in ua:
        return "claude"
    if "gemini" in ua or "google" in ua:
        return "gemini"
    if "codex" in ua:
        return "codex"
    if "opencode" in ua:
        return "opencode"
    # NewAPI/OneAPI 通常使用标准 OpenAI 格式
    return "openai"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的聊天补全"""
    verify_api_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    client_type = detect_client(request)
    model_name = body.get("model", "")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    if not messages:
        raise HTTPException(400, "messages is required")

    # 检查扩展是否连接
    if not store.active:
        raise HTTPException(503, "Extension not connected. Please open arena.ai in Chrome with the extension installed.")

    # 解析模型
    model_id = store.text_models.get(model_name) or store.image_models.get(model_name)
    if not model_id:
        # 尝试模糊匹配
        for name, mid in {**store.text_models, **store.image_models}.items():
            if model_name.lower() in name.lower() or name.lower() in model_name.lower():
                model_id = mid
                model_name = name
                break
    if not model_id:
        available = list(store.text_models.keys()) + list(store.image_models.keys())
        raise HTTPException(404, f"Model '{model_name}' not found. Available: {available[:20]}")

    # 构建 prompt（取最后一条 user 消息）
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # 多模态消息
                text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                prompt = "\n".join(text_parts)
            else:
                prompt = content
            break
    if not prompt:
        prompt = messages[-1].get("content", "")

    # 如果有 system message，拼接到 prompt 前面
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    if system_parts:
        prompt = "\n".join(system_parts) + "\n\n" + prompt

    # 如果有多轮对话，拼接历史
    if len(messages) > 1:
        history_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
            if role == "system":
                continue  # 已经处理
            history_parts.append(f"<|{role}|>\n{content}")
        prompt = "\n".join(history_parts)

    # 获取 reCAPTCHA token
    v3_token = store.pop_v3_token()
    v2_token = store.pop_v2_token() if not v3_token else None

    is_image = model_name in store.image_models
    modality = "image" if is_image else "chat"

    # 构建 arena.ai 请求
    eval_id = uuid7()
    user_msg_id = uuid7()
    model_a_msg_id = uuid7()

    # 从 cookies 中提取 userId
    user_id = store.cookies.get("arena-user-id", "")
    if not user_id:
        # 尝试从其他 cookie 中提取
        for key, value in store.cookies.items():
            if "user" in key.lower() and len(value) > 20:
                user_id = value
                break

    arena_payload = {
        "id": eval_id,
        "mode": "direct",
        "modelAId": model_id,
        "userMessageId": user_msg_id,
        "modelAMessageId": model_a_msg_id,
        "userMessage": {
            "content": prompt,
            "experimental_attachments": [],
            "metadata": {},
        },
        "modality": modality,
    }

    # 添加 userId（如果有）
    if user_id:
        arena_payload["userId"] = user_id

    if v2_token:
        arena_payload["recaptchaV2Token"] = v2_token
        arena_payload["recaptchaV3Token"] = None
    elif v3_token:
        arena_payload["recaptchaV3Token"] = v3_token
    else:
        log.warning("No reCAPTCHA token available, sending without token")

    # 构建 headers（origin/referer 由浏览器扩展自动附加）
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "cookie": store.build_cookie_header(),
    }

    # 添加认证 header（如果有 auth_token）
    if store.auth_token:
        headers["authorization"] = f"Bearer {store.auth_token}"

    url = ARENA_CREATE_EVAL
    log.info(f"Sending to arena.ai: model={model_name}, eval_id={eval_id}, has_v3={bool(v3_token)}, has_v2={bool(v2_token)}")
    log.debug(f"Arena payload: {json.dumps({k: (v[:20]+'...' if isinstance(v, str) and len(v)>20 else v) for k, v in arena_payload.items()})}")
    log.debug(f"Request headers (partial): cookie_len={len(headers.get('cookie',''))}, has_auth={bool(headers.get('authorization'))}")

    if stream:
        return StreamingResponse(
            stream_response(url, arena_payload, headers, model_name, eval_id, client_type),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await non_stream_response(url, arena_payload, headers, model_name, eval_id, client_type)


async def stream_response(url, payload, headers, model_name, eval_id, client_type="openai"):
    """流式响应生成器（通过扩展转发）"""
    chat_id = f"chatcmpl-{eval_id}"
    created = int(time.time())

    rq.put(eval_id, url, payload, headers)
    log.info(f"Task queued for extension: {eval_id}")

    try:
        async for line in rq.iter_chunks(eval_id):
            if not line.strip():
                continue

            content = None
            reasoning = None
            finish = None

            if line.startswith("a0:"):
                try:
                    content = json.loads(line[3:])
                    if content == "hasArenaError":
                        content = "[Arena Error]"
                        finish = "stop"
                except json.JSONDecodeError:
                    continue
            elif line.startswith("ag:"):
                try:
                    reasoning = json.loads(line[3:])
                except json.JSONDecodeError:
                    continue
            elif line.startswith("ad:"):
                finish = "stop"
                try:
                    data = json.loads(line[3:])
                    if data.get("finishReason"):
                        finish = data["finishReason"]
                except json.JSONDecodeError:
                    pass
            elif line.startswith("a2:"):
                if "heartbeat" in line:
                    continue
                try:
                    data = json.loads(line[3:])
                    images = [img.get("image") for img in data if img.get("image")]
                    if images:
                        content = "\n".join(f"![image]({img_url})" for img_url in images)
                except json.JSONDecodeError:
                    continue
            elif line.startswith("a3:"):
                try:
                    content = f"[Error: {json.loads(line[3:])}]"
                except Exception:
                    content = f"[Error: {line[3:]}]"
                finish = "stop"
            else:
                continue

            if content is not None:
                chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": None,
                    }],
                }
                if client_type == "claude":
                    chunk["type"] = "content_block_delta"
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            if reasoning is not None:
                chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning_content": reasoning},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            if finish:
                chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": finish,
                    }],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return

    except Exception as e:
        log.error(f"Stream error: {e}")
        error_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{
                "index": 0,
                "delta": {"content": f"[Stream Error: {e}]"},
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"


async def non_stream_response(url, payload, headers, model_name, eval_id, client_type="openai"):
    """非流式响应（通过扩展转发）"""
    content_parts = []
    reasoning_parts = []
    finish_reason = "stop"
    usage = {}

    rq.put(eval_id, url, payload, headers)
    log.info(f"Task queued for extension (non-stream): {eval_id}")

    try:
        async for line in rq.iter_chunks(eval_id):
            if not line.strip():
                continue
            if line.startswith("a0:"):
                try:
                    text = json.loads(line[3:])
                    if isinstance(text, str) and text != "hasArenaError":
                        content_parts.append(text)
                except json.JSONDecodeError:
                    pass
            elif line.startswith("ag:"):
                try:
                    text = json.loads(line[3:])
                    if isinstance(text, str):
                        reasoning_parts.append(text)
                except json.JSONDecodeError:
                    pass
            elif line.startswith("ad:"):
                try:
                    data = json.loads(line[3:])
                    if data.get("finishReason"):
                        finish_reason = data["finishReason"]
                    if data.get("usage"):
                        usage = data["usage"]
                except json.JSONDecodeError:
                    pass
            elif line.startswith("a2:"):
                if "heartbeat" in line:
                    continue
                try:
                    data = json.loads(line[3:])
                    images = [img.get("image") for img in data if img.get("image")]
                    for img_url in images:
                        content_parts.append(f"![image]({img_url})")
                except json.JSONDecodeError:
                    pass
            elif line.startswith("a3:"):
                try:
                    content_parts.append(f"[Error: {json.loads(line[3:])}]")
                except Exception:
                    content_parts.append(f"[Error: {line[3:]}]")

    except Exception as e:
        log.error(f"Non-stream error: {e}")
        raise HTTPException(500, str(e))

    full_content = "".join(content_parts)
    full_reasoning = "".join(reasoning_parts)

    message = {"role": "assistant", "content": full_content}
    if full_reasoning:
        message["reasoning_content"] = full_reasoning

    response = {
        "id": f"chatcmpl-{eval_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": usage or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }

    # Claude 格式兼容
    if client_type == "claude":
        response["type"] = "message"
        response["role"] = "assistant"
        response["content"] = [{"type": "text", "text": full_content}]

    return response


# ============================================================
# Anthropic 兼容端点
# ============================================================
@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages API 兼容端点"""
    verify_api_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # 转换 Anthropic 格式到内部格式
    model_name = body.get("model", "")
    stream = body.get("stream", False)
    max_tokens = body.get("max_tokens", 1024)

    # Anthropic messages 格式转换
    messages = []
    system = body.get("system", "")
    if system:
        if isinstance(system, list):
            system = "\n".join(s.get("text", "") for s in system if s.get("type") == "text")
        messages.append({"role": "system", "content": system})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            content = "\n".join(text_parts)
        messages.append({"role": role, "content": content})

    if not messages:
        raise HTTPException(400, "messages is required")

    if not store.active:
        raise HTTPException(503, "Extension not connected.")

    model_id = store.text_models.get(model_name) or store.image_models.get(model_name)
    if not model_id:
        for name, mid in {**store.text_models, **store.image_models}.items():
            if model_name.lower() in name.lower() or name.lower() in model_name.lower():
                model_id = mid
                model_name = name
                break
    if not model_id:
        raise HTTPException(404, f"Model '{model_name}' not found")

    # 构建 prompt
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            prompt = msg.get("content", "")
            break
    if len(messages) > 1:
        history_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                continue
            history_parts.append(f"<|{role}|>\n{content}")
        prompt = "\n".join(history_parts)
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    if system_parts:
        prompt = "\n".join(system_parts) + "\n\n" + prompt

    v3_token = store.pop_v3_token()
    v2_token = store.pop_v2_token() if not v3_token else None
    eval_id = uuid7()
    user_msg_id = uuid7()
    model_a_msg_id = uuid7()

    arena_payload = {
        "id": eval_id,
        "mode": "direct",
        "modelAId": model_id,
        "userMessageId": user_msg_id,
        "modelAMessageId": model_a_msg_id,
        "userMessage": {"content": prompt, "experimental_attachments": [], "metadata": {}},
        "modality": "chat",
    }
    if v2_token:
        arena_payload["recaptchaV2Token"] = v2_token
        arena_payload["recaptchaV3Token"] = None
    elif v3_token:
        arena_payload["recaptchaV3Token"] = v3_token

    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "cookie": store.build_cookie_header(),
    }
    if store.auth_token:
        headers["authorization"] = f"Bearer {store.auth_token}"

    if stream:
        return StreamingResponse(
            anthropic_stream_response(ARENA_CREATE_EVAL, arena_payload, headers, model_name, eval_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    else:
        return await anthropic_non_stream_response(ARENA_CREATE_EVAL, arena_payload, headers, model_name, eval_id)


async def anthropic_stream_response(url, payload, headers, model_name, eval_id):
    """Anthropic SSE 格式流式响应"""
    msg_id = f"msg_{eval_id.replace('-', '')[:24]}"
    created = int(time.time())

    # message_start
    yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'id':msg_id,'type':'message','role':'assistant','content':[],'model':model_name,'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}})}\n\n"
    yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
    yield "event: ping\ndata: {\"type\":\"ping\"}\n\n"

    rq.put(eval_id, url, payload, headers)

    stop_reason = "end_turn"
    try:
        async for line in rq.iter_chunks(eval_id):
            if not line.strip():
                continue
            if line.startswith("a0:"):
                try:
                    text = json.loads(line[3:])
                    if isinstance(text, str) and text != "hasArenaError":
                        yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n"
                except json.JSONDecodeError:
                    pass
            elif line.startswith("ad:"):
                try:
                    data = json.loads(line[3:])
                    if data.get("finishReason"):
                        stop_reason = data["finishReason"]
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        log.error(f"Anthropic stream error: {e}")

    yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
    yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason':stop_reason,'stop_sequence':None},'usage':{'output_tokens':0}})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"


async def anthropic_non_stream_response(url, payload, headers, model_name, eval_id):
    """Anthropic 非流式响应"""
    content_parts = []
    stop_reason = "end_turn"
    msg_id = f"msg_{eval_id.replace('-', '')[:24]}"

    rq.put(eval_id, url, payload, headers)
    try:
        async for line in rq.iter_chunks(eval_id):
            if not line.strip():
                continue
            if line.startswith("a0:"):
                try:
                    text = json.loads(line[3:])
                    if isinstance(text, str) and text != "hasArenaError":
                        content_parts.append(text)
                except json.JSONDecodeError:
                    pass
            elif line.startswith("ad:"):
                try:
                    data = json.loads(line[3:])
                    if data.get("finishReason"):
                        stop_reason = data["finishReason"]
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        log.error(f"Anthropic non-stream error: {e}")
        raise HTTPException(500, str(e))

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "".join(content_parts)}],
        "model": model_name,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ============================================================
# 健康检查
# ============================================================
@app.get("/health")
@app.get("/")
async def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "extension": store.status(),
    }


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    log.info(f"Starting arena2api on port {PORT}")
    log.info(f"OpenAI API: http://localhost:{PORT}/v1")
    log.info("Waiting for Chrome extension to connect...")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
