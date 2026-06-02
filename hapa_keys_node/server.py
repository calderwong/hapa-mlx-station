from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from .auth import verify_request_token
from .config import Settings, load_settings

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_BASE_URL = "https://api.openai.com/v1"

SECRET_GEMINI_API_KEY = "gemini_api_key"
SECRET_OPENAI_API_KEY = "openai_api_key"

_SECRET_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_auth(request: Request) -> None:
    settings: Settings = request.app.state.settings
    verify_request_token(request, settings.token, allow_query_token=settings.allow_query_token)


def _require_admin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if settings.disable_admin_api:
        raise HTTPException(status_code=404, detail="Not Found")
    _require_auth(request)


def _normalize_secret_name(name: str) -> str:
    name = str(name or "").strip()
    if not name or not _SECRET_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid secret name")
    return name


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass


def _ensure_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _load_secrets_from_disk(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)

    if isinstance(data, dict) and isinstance(data.get("secrets"), dict):
        secrets = data["secrets"]
    elif isinstance(data, dict):
        secrets = data
    else:
        raise RuntimeError("Invalid secrets file")

    cleaned: dict[str, str] = {}
    for k, v in secrets.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        name = k.strip()
        if not name:
            continue
        cleaned[name] = v

    _ensure_private_file(path)
    return cleaned


def _write_secrets_to_disk(path: Path, secrets: dict[str, str]) -> None:
    _ensure_private_dir(path.parent)

    payload = {"version": 1, "secrets": dict(sorted(secrets.items()))}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    _ensure_private_file(tmp)

    tmp.replace(path)
    _ensure_private_file(path)


def _get_secret(request: Request, name: str) -> str:
    secrets: dict[str, str] = request.app.state.secrets
    value = secrets.get(name)
    if not value:
        raise HTTPException(status_code=500, detail=f"Missing secret: {name}")
    return value


class SecretPutRequest(BaseModel):
    value: str


class GeminiGenerateContentRequest(BaseModel):
    model: str
    contents: Any
    config: Optional[dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


def _normalize_contents(contents: Any) -> list[dict[str, Any]]:
    if isinstance(contents, str):
        return [{"parts": [{"text": contents}]}]

    if isinstance(contents, dict):
        return [contents]

    if isinstance(contents, list):
        return contents

    raise HTTPException(status_code=400, detail="Invalid contents type")


def _system_instruction_to_content(system_instruction: Any) -> dict[str, Any]:
    if isinstance(system_instruction, str):
        return {"parts": [{"text": system_instruction}]}

    if isinstance(system_instruction, dict):
        return system_instruction

    raise HTTPException(status_code=400, detail="Invalid systemInstruction type")


def _extract_gemini_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""

    cand0 = candidates[0]
    if not isinstance(cand0, dict):
        return ""

    content = cand0.get("content")
    if not isinstance(content, dict):
        return ""

    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""

    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])

    return "".join(texts)


class OpenAIChatCompletionsRequest(BaseModel):
    model: str
    messages: Any
    stream: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


def _extract_openai_chat_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    c0 = choices[0]
    if not isinstance(c0, dict):
        return ""

    msg = c0.get("message")
    if not isinstance(msg, dict):
        return ""

    content = msg.get("content")
    return content if isinstance(content, str) else ""


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.secrets_lock = asyncio.Lock()

    try:
        secrets = _load_secrets_from_disk(settings.secrets_file)
    except Exception as exc:
        raise RuntimeError(f"Failed to load secrets file: {settings.secrets_file}") from exc

    app.state.secrets = secrets
    app.state.http = httpx.AsyncClient(timeout=60.0)

    if settings.token_is_generated:
        try:
            settings.token_file.parent.mkdir(parents=True, exist_ok=True)
            settings.token_file.write_text(settings.token, encoding="utf-8")
            _ensure_private_file(settings.token_file)
        except Exception as exc:
            print(f"[hapa-keys-node] Failed to write token file: {exc}")

    base_url = f"http://{settings.host}:{settings.port}"
    print(f"[hapa-keys-node] baseUrl={base_url}")
    print(f"[hapa-keys-node] token_file={settings.token_file}")
    print(f"[hapa-keys-node] secrets_file={settings.secrets_file}")

    yield

    client = getattr(app.state, "http", None)
    if client:
        await client.aclose()


app = FastAPI(title="Hapa Keys Node", lifespan=_lifespan)
index_path = Path(__file__).parent / "web" / "index.html"


@app.get("/")
def get_index():
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(index_path)


@app.get("/health")
async def get_health(request: Request):
    settings: Settings = request.app.state.settings
    secrets: dict[str, str] = request.app.state.secrets

    return {
        "ok": True,
        "service": settings.service_name,
        "api_version": settings.api_version,
        "time": utc_now_iso(),
        "admin_enabled": (not settings.disable_admin_api),
        "gemini_configured": bool(secrets.get(SECRET_GEMINI_API_KEY)),
        "openai_configured": bool(secrets.get(SECRET_OPENAI_API_KEY)),
    }


@app.get("/capabilities", dependencies=[Depends(_require_auth)])
async def get_capabilities(request: Request):
    settings: Settings = request.app.state.settings
    return {
        "api_version": settings.api_version,
        "time": utc_now_iso(),
        "service": settings.service_name,
        "auth": {"query_token": bool(settings.allow_query_token)},
        "admin": {"enabled": (not settings.disable_admin_api)},
        "proxies": {
            "gemini": {"generateContent": True},
            "openai": {"chat_completions": True},
        },
    }


@app.get("/v1/admin/secrets", dependencies=[Depends(_require_admin)])
async def admin_list_secrets(request: Request):
    secrets: dict[str, str] = request.app.state.secrets

    items: list[dict[str, Any]] = []
    for name in sorted(secrets.keys()):
        value = secrets.get(name) or ""
        last4 = value[-4:] if len(value) >= 4 else ""
        items.append({"name": name, "configured": bool(value), "length": len(value), "last4": last4})

    return {"secrets": items}


@app.get("/v1/admin/secrets/{name}", dependencies=[Depends(_require_admin)])
async def admin_get_secret_meta(name: str, request: Request):
    name = _normalize_secret_name(name)
    secrets: dict[str, str] = request.app.state.secrets

    value = secrets.get(name) or ""
    last4 = value[-4:] if len(value) >= 4 else ""
    return {"name": name, "configured": bool(value), "length": len(value), "last4": last4}


@app.put("/v1/admin/secrets/{name}", dependencies=[Depends(_require_admin)])
async def admin_put_secret(name: str, body: SecretPutRequest, request: Request):
    name = _normalize_secret_name(name)
    value = str(body.value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="Missing secret value")

    settings: Settings = request.app.state.settings
    lock: asyncio.Lock = request.app.state.secrets_lock
    secrets: dict[str, str] = request.app.state.secrets

    async with lock:
        secrets[name] = value
        try:
            _write_secrets_to_disk(settings.secrets_file, secrets)
        except Exception as exc:
            secrets.pop(name, None)
            raise HTTPException(status_code=500, detail=f"Failed to persist secrets: {exc.__class__.__name__}")

    return {"ok": True}


@app.delete("/v1/admin/secrets/{name}", dependencies=[Depends(_require_admin)])
async def admin_delete_secret(name: str, request: Request):
    name = _normalize_secret_name(name)

    settings: Settings = request.app.state.settings
    lock: asyncio.Lock = request.app.state.secrets_lock
    secrets: dict[str, str] = request.app.state.secrets

    async with lock:
        if name not in secrets:
            return {"ok": True}
        old = secrets.pop(name, None)
        try:
            _write_secrets_to_disk(settings.secrets_file, secrets)
        except Exception as exc:
            if old is not None:
                secrets[name] = old
            raise HTTPException(status_code=500, detail=f"Failed to persist secrets: {exc.__class__.__name__}")

    return {"ok": True}


@app.post("/v1/gemini/generateContent", dependencies=[Depends(_require_auth)])
async def gemini_generate_content(body: GeminiGenerateContentRequest, request: Request):
    api_key = _get_secret(request, SECRET_GEMINI_API_KEY)

    contents = _normalize_contents(body.contents)
    cfg = dict(body.config or {})

    system_instruction = cfg.pop("systemInstruction", None)

    req_body: dict[str, Any] = {"contents": contents}
    if system_instruction is not None:
        req_body["systemInstruction"] = _system_instruction_to_content(system_instruction)

    if cfg:
        req_body["generationConfig"] = cfg

    upstream_url = f"{GEMINI_BASE_URL}/models/{body.model}:generateContent"

    client: httpx.AsyncClient = request.app.state.http
    try:
        res = await client.post(
            upstream_url,
            params={"key": api_key},
            json=req_body,
            headers={"Content-Type": "application/json"},
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc.__class__.__name__}")

    if res.status_code >= 400:
        text = res.text
        if len(text) > 2000:
            text = text[:2000]
        raise HTTPException(status_code=502, detail={"upstream_status": res.status_code, "body": text})

    data = res.json()
    return {"text": _extract_gemini_text(data), "raw": data}


@app.post("/v1/openai/chat/completions", dependencies=[Depends(_require_auth)])
async def openai_chat_completions(body: OpenAIChatCompletionsRequest, request: Request):
    api_key = _get_secret(request, SECRET_OPENAI_API_KEY)

    if bool(body.stream):
        raise HTTPException(status_code=400, detail="Streaming is not supported")

    payload = body.model_dump(exclude_none=True)

    client: httpx.AsyncClient = request.app.state.http
    try:
        res = await client.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc.__class__.__name__}")

    if res.status_code >= 400:
        text = res.text
        if len(text) > 2000:
            text = text[:2000]
        raise HTTPException(status_code=502, detail={"upstream_status": res.status_code, "body": text})

    data = res.json()
    return {"text": _extract_openai_chat_text(data), "raw": data}
