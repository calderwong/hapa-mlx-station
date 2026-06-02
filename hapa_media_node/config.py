from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Optional


@dataclass(frozen=True)
class Settings:
    api_version: str
    service_name: str
    host: str
    port: int
    token: str
    token_is_generated: bool
    token_file: Path
    storage_dir: Path
    db_path: Path
    artifacts_dir: Path
    mflux_bin: str


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    return Path(value).expanduser()


def _default_mflux_bin() -> str:
    candidate = Path(sys.executable).with_name("mflux-generate")
    if candidate.exists():
        return str(candidate)
    found = which("mflux-generate")
    return found or "mflux-generate"


def _default_token_file() -> Path:
    return (Path(__file__).resolve().parent.parent / ".node_token").resolve()


def _read_text_file(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def load_settings() -> Settings:
    host = os.environ.get("HAPA_MEDIA_NODE_HOST", "127.0.0.1")
    port = int(os.environ.get("HAPA_MEDIA_NODE_PORT", "8723"))

    default_token_file = _default_token_file()
    token_file = Path(os.environ.get("HAPA_MEDIA_NODE_TOKEN_FILE", str(default_token_file))).expanduser()
    if token_file.is_absolute():
        token_file = token_file.resolve()
    else:
        token_file = (Path.cwd() / token_file).resolve()

    token = str(os.environ.get("HAPA_MEDIA_NODE_TOKEN") or "").strip() or None
    token_is_generated = False
    if not token:
        file_tok = _read_text_file(token_file)
        if file_tok:
            token = file_tok

    if not token:
        token = secrets.token_hex(16)
        token_is_generated = True

    storage_dir = _env_path("HAPA_MEDIA_NODE_STORAGE_DIR", Path.cwd() / "data").resolve()
    db_path = _env_path("HAPA_MEDIA_NODE_DB_PATH", storage_dir / "hapa_media_node.sqlite3").resolve()
    artifacts_dir = _env_path("HAPA_MEDIA_NODE_ARTIFACTS_DIR", storage_dir / "artifacts").resolve()

    mflux_bin = os.environ.get("HAPA_MEDIA_NODE_MFLUX_BIN") or _default_mflux_bin()

    return Settings(
        api_version="v1",
        service_name="hapa-media-gen-node",
        host=host,
        port=port,
        token=token,
        token_is_generated=token_is_generated,
        token_file=token_file,
        storage_dir=storage_dir,
        db_path=db_path,
        artifacts_dir=artifacts_dir,
        mflux_bin=mflux_bin,
    )
