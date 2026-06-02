from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional


def _read_text_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        return text or None
    except Exception:
        return None


def _node_token_file_paths() -> list[str]:
    paths: list[str] = []

    env_path = os.environ.get("HAPA_KEYS_NODE_TOKEN_FILE")
    if env_path:
        paths.append(env_path)

    paths.append(os.path.join(os.getcwd(), ".node_token"))
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    paths.append(os.path.join(repo_root, ".node_token"))

    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _read_node_token_file() -> Optional[str]:
    for path in _node_token_file_paths():
        tok = _read_text_file(path)
        if tok:
            return tok
    return None


def _get_token(arg_token: Optional[str]) -> Optional[str]:
    if arg_token:
        return arg_token
    env_tok = os.environ.get("HAPA_KEYS_NODE_TOKEN")
    if env_tok:
        return env_tok
    return _read_node_token_file()


def _require_token(token: Optional[str]) -> str:
    if not token:
        raise RuntimeError("Missing token (set HAPA_KEYS_NODE_TOKEN, create .node_token, or pass --token)")
    return token


def _http_json(method: str, url: str, *, token: Optional[str], payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}")


def _default_base_url() -> str:
    return os.environ.get("HAPA_KEYS_NODE_BASE_URL", "http://127.0.0.1:8733")


def run_self_test(
    base_url: str,
    token: Optional[str],
    *,
    require_gemini: bool,
    require_openai: bool,
    gemini_model: str,
    openai_model: str,
) -> dict[str, Any]:
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("Missing base_url")

    result: dict[str, Any] = {
        "api_version": "v1",
        "base_url": base_url,
        "ok": False,
        "steps": [],
    }

    def _step(name: str, *, ok: bool, data: Any = None, error: Optional[str] = None, skipped: bool = False) -> None:
        rec: dict[str, Any] = {"name": name, "ok": bool(ok)}
        if skipped:
            rec["skipped"] = True
        if data is not None:
            rec["data"] = data
        if error:
            rec["error"] = error
        result["steps"].append(rec)

    try:
        health = _http_json("GET", base_url + "/health", token=None, payload=None)
        if not isinstance(health, dict) or health.get("ok") is not True:
            raise RuntimeError(f"Unexpected /health response: {health}")
        _step("health", ok=True, data=health)

        tok = _require_token(token)
        caps = _http_json("GET", base_url + "/capabilities", token=tok, payload=None)
        if not isinstance(caps, dict) or not caps.get("service"):
            raise RuntimeError(f"Unexpected /capabilities response: {caps}")
        _step("capabilities", ok=True, data=caps)

        admin_enabled = bool(health.get("admin_enabled")) if isinstance(health, dict) else False
        if admin_enabled:
            admin_secrets = _http_json("GET", base_url + "/v1/admin/secrets", token=tok, payload=None)
            _step("admin_list_secrets", ok=True, data={"count": len(admin_secrets.get("secrets", []))})
        else:
            _step("admin_list_secrets", ok=True, skipped=True)

        gemini_configured = bool(health.get("gemini_configured")) if isinstance(health, dict) else False
        if require_gemini or gemini_configured:
            payload = {
                "model": str(gemini_model or "gemini-1.5-flash"),
                "contents": "Reply with OK.",
                "config": {"temperature": 0.0, "maxOutputTokens": 16},
            }
            gen = _http_json("POST", base_url + "/v1/gemini/generateContent", token=tok, payload=payload)
            if not isinstance(gen, dict) or not isinstance(gen.get("text"), str) or not gen.get("text"):
                raise RuntimeError(f"Unexpected /v1/gemini/generateContent response: {gen}")
            _step("gemini_generateContent", ok=True, data={"text": gen.get("text")})
        else:
            _step("gemini_generateContent", ok=True, skipped=True)

        openai_configured = bool(health.get("openai_configured")) if isinstance(health, dict) else False
        if require_openai:
            if not openai_configured:
                raise RuntimeError("OpenAI not configured")

            payload = {
                "model": str(openai_model or "gpt-4o-mini"),
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "temperature": 0.0,
            }
            out = _http_json("POST", base_url + "/v1/openai/chat/completions", token=tok, payload=payload)
            if not isinstance(out, dict) or not isinstance(out.get("text"), str) or not out.get("text"):
                raise RuntimeError(f"Unexpected /v1/openai/chat/completions response: {out}")
            _step("openai_chat_completions", ok=True, data={"text": out.get("text")})
        else:
            _step("openai_chat_completions", ok=True, skipped=True)

        result["ok"] = True
        return result

    except Exception as exc:
        _step("error", ok=False, error=str(exc))
        result["ok"] = False
        return result


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="hapa-keys-self-test")
    p.add_argument("--base-url", default=_default_base_url())
    p.add_argument("--token")
    p.add_argument("--require-gemini", action="store_true")
    p.add_argument("--require-openai", action="store_true")
    p.add_argument("--gemini-model", default="gemini-1.5-flash")
    p.add_argument("--openai-model", default="gpt-4o-mini")
    args = p.parse_args(argv)

    tok = _get_token(args.token)
    result = run_self_test(
        str(args.base_url),
        tok,
        require_gemini=bool(args.require_gemini),
        require_openai=bool(args.require_openai),
        gemini_model=str(args.gemini_model),
        openai_model=str(args.openai_model),
    )

    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
