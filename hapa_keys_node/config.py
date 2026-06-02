from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .auth import env_truthy


def _default_token_file() -> Path:
    return (Path(__file__).resolve().parent.parent / ".node_token").resolve()


def _is_loopback_host(host: str) -> bool:
    host = str(host or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.startswith("127."):
        return True
    return False


def _read_text_file(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


@dataclass(frozen=True)
class Settings:
    api_version: str
    service_name: str
    host: str
    port: int
    token: str
    token_is_generated: bool
    token_file: Path
    allow_query_token: bool
    disable_admin_api: bool
    storage_dir: Path
    secrets_file: Path


def load_settings() -> Settings:
    host = str(os.environ.get("HAPA_KEYS_NODE_HOST", "127.0.0.1") or "").strip() or "127.0.0.1"
    allow_non_loopback = env_truthy(os.environ.get("HAPA_KEYS_NODE_ALLOW_NON_LOOPBACK")) or env_truthy(
        os.environ.get("HAPA_KEYS_ALLOW_NON_LOOPBACK")
    )
    if not allow_non_loopback and not _is_loopback_host(host):
        host = "127.0.0.1"

    port = int(os.environ.get("HAPA_KEYS_NODE_PORT", "8733"))

    default_token_file = _default_token_file()
    token_file = Path(os.environ.get("HAPA_KEYS_NODE_TOKEN_FILE", str(default_token_file))).expanduser()
    if token_file.is_absolute():
        token_file = token_file.resolve()
    else:
        token_file = (Path.cwd() / token_file).resolve()

    token = str(os.environ.get("HAPA_KEYS_NODE_TOKEN") or "").strip() or None
    token_is_generated = False
    if not token:
        file_tok = _read_text_file(token_file)
        if file_tok:
            token = file_tok

    if not token:
        token = secrets.token_hex(16)
        token_is_generated = True

    allow_query_token = env_truthy(os.environ.get("HAPA_KEYS_NODE_ALLOW_QUERY_TOKEN")) or env_truthy(
        os.environ.get("HAPA_KEYS_ALLOW_QUERY_TOKEN")
    )

    disable_admin_api = env_truthy(os.environ.get("HAPA_KEYS_NODE_DISABLE_ADMIN_API")) or env_truthy(
        os.environ.get("HAPA_KEYS_DISABLE_ADMIN_API")
    )

    storage_dir = Path(os.environ.get("HAPA_KEYS_NODE_STORAGE_DIR", str(Path.cwd() / "data" / "keys_node"))).expanduser()
    if storage_dir.is_absolute():
        storage_dir = storage_dir.resolve()
    else:
        storage_dir = (Path.cwd() / storage_dir).resolve()

    secrets_file = Path(os.environ.get("HAPA_KEYS_NODE_SECRETS_FILE", str(storage_dir / "secrets.json"))).expanduser()
    if secrets_file.is_absolute():
        secrets_file = secrets_file.resolve()
    else:
        secrets_file = (Path.cwd() / secrets_file).resolve()

    return Settings(
        api_version="v1",
        service_name="hapa-keys-node",
        host=host,
        port=port,
        token=token,
        token_is_generated=token_is_generated,
        token_file=token_file,
        allow_query_token=allow_query_token,
        disable_admin_api=disable_admin_api,
        storage_dir=storage_dir,
        secrets_file=secrets_file,
    )
