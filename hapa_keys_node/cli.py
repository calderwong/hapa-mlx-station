from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

import uvicorn

from .config import load_settings
from .self_test import run_self_test


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


def _http_json(
    method: str,
    url: str,
    *,
    token: Optional[str],
    payload: Optional[dict[str, Any]],
    timeout_seconds: Optional[float] = None,
) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)

    try:
        if timeout_seconds is None:
            res = urllib.request.urlopen(req)
        else:
            res = urllib.request.urlopen(req, timeout=float(timeout_seconds))
        with res:
            body = res.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}")


def _default_base_url() -> str:
    env_url = os.environ.get("HAPA_KEYS_NODE_BASE_URL")
    if env_url:
        return env_url
    settings = load_settings()
    return f"http://{settings.host}:{settings.port}"


def _read_secret_value_from_stdin() -> str:
    value = sys.stdin.read()
    if value.endswith("\n"):
        value = value.rstrip("\n")
    return value.strip()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="hapa-keys")

    p.add_argument("--base-url", default=_default_base_url())
    p.add_argument("--token")

    sub = p.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--reload", action="store_true")

    sub.add_parser("health")
    sub.add_parser("capabilities")

    p_secrets = sub.add_parser("secrets")
    secrets_sub = p_secrets.add_subparsers(dest="secrets_cmd")
    secrets_sub.add_parser("list")

    p_get = secrets_sub.add_parser("get")
    p_get.add_argument("name")

    p_set = secrets_sub.add_parser("set")
    p_set.add_argument("name")
    p_set.add_argument("--value")
    p_set.add_argument("--stdin", action="store_true")

    p_del = secrets_sub.add_parser("delete")
    p_del.add_argument("name")

    p_self = sub.add_parser("self-test")
    p_self.add_argument("--require-gemini", action="store_true")
    p_self.add_argument("--require-openai", action="store_true")
    p_self.add_argument("--gemini-model", default="gemini-1.5-flash")
    p_self.add_argument("--openai-model", default="gpt-4o-mini")

    args = p.parse_args(argv)

    cmd = args.cmd or "serve"

    if cmd == "serve":
        settings = load_settings()
        reload_flag = bool(args.reload) or bool(os.environ.get("HAPA_KEYS_NODE_RELOAD"))
        uvicorn.run(
            "hapa_keys_node.server:app",
            host=settings.host,
            port=settings.port,
            reload=reload_flag,
        )
        return 0

    base_url = str(args.base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("Missing base_url")

    if cmd == "health":
        data = _http_json("GET", base_url + "/health", token=None, payload=None, timeout_seconds=10.0)
        print(json.dumps(data, indent=2))
        return 0

    tok = _require_token(_get_token(args.token))

    if cmd == "capabilities":
        data = _http_json("GET", base_url + "/capabilities", token=tok, payload=None, timeout_seconds=10.0)
        print(json.dumps(data, indent=2))
        return 0

    if cmd == "secrets":
        if args.secrets_cmd == "list":
            data = _http_json("GET", base_url + "/v1/admin/secrets", token=tok, payload=None)
            print(json.dumps(data, indent=2))
            return 0

        if args.secrets_cmd == "get":
            name = str(args.name)
            data = _http_json("GET", base_url + f"/v1/admin/secrets/{name}", token=tok, payload=None)
            print(json.dumps(data, indent=2))
            return 0

        if args.secrets_cmd == "set":
            name = str(args.name)
            if args.stdin:
                value = _read_secret_value_from_stdin()
            else:
                value = str(args.value or "").strip()

            if not value:
                raise RuntimeError("Missing secret value (use --value or --stdin)")

            data = _http_json(
                "PUT",
                base_url + f"/v1/admin/secrets/{name}",
                token=tok,
                payload={"value": value},
            )
            print(json.dumps(data, indent=2))
            return 0

        if args.secrets_cmd == "delete":
            name = str(args.name)
            data = _http_json("DELETE", base_url + f"/v1/admin/secrets/{name}", token=tok, payload=None)
            print(json.dumps(data, indent=2))
            return 0

        raise RuntimeError("Missing secrets subcommand")

    if cmd == "self-test":
        result = run_self_test(
            base_url,
            tok,
            require_gemini=bool(args.require_gemini),
            require_openai=bool(args.require_openai),
            gemini_model=str(args.gemini_model),
            openai_model=str(args.openai_model),
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") is True else 1

    raise RuntimeError(f"Unknown command: {cmd}")
