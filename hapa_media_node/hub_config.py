from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class HubNode:
    node_id: str
    base_url: str
    token: str


@dataclass(frozen=True)
class HubSettings:
    api_version: str
    service_name: str
    host: str
    port: int
    token: str
    token_is_generated: bool
    token_file: Path
    nodes: list[HubNode]


def _default_token_file() -> Path:
    return (Path(__file__).resolve().parent.parent / ".node_token").resolve()


def _read_text_file(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def _load_nodes() -> list[HubNode]:
    raw = os.environ.get("HAPA_MEDIA_HUB_NODES")

    nodes_file = os.environ.get("HAPA_MEDIA_HUB_NODES_FILE")
    if nodes_file:
        raw = Path(nodes_file).expanduser().read_text("utf-8")

    if not raw:
        raise RuntimeError("Missing HAPA_MEDIA_HUB_NODES (or HAPA_MEDIA_HUB_NODES_FILE)")

    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError("HAPA_MEDIA_HUB_NODES must be a JSON array")

    nodes: list[HubNode] = []
    for item in data:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid node entry in HAPA_MEDIA_HUB_NODES")

        node_id = str(item.get("id") or item.get("node_id") or "").strip()
        base_url = str(item.get("base_url") or item.get("url") or "").strip().rstrip("/")
        token = str(item.get("token") or "").strip()

        if not node_id:
            raise RuntimeError("Each node requires id")
        if not base_url:
            raise RuntimeError("Each node requires base_url")
        if not token:
            raise RuntimeError("Each node requires token")

        nodes.append(HubNode(node_id=node_id, base_url=base_url, token=token))

    if not nodes:
        raise RuntimeError("At least one node is required")

    return nodes


def load_hub_settings() -> HubSettings:
    host = os.environ.get("HAPA_MEDIA_HUB_HOST", "127.0.0.1")
    port = int(os.environ.get("HAPA_MEDIA_HUB_PORT", "8723"))

    default_token_file = _default_token_file()
    token_file = Path(os.environ.get("HAPA_MEDIA_HUB_TOKEN_FILE", str(default_token_file))).expanduser()
    if token_file.is_absolute():
        token_file = token_file.resolve()
    else:
        token_file = (Path.cwd() / token_file).resolve()

    token = str(os.environ.get("HAPA_MEDIA_HUB_TOKEN") or "").strip() or None
    token_is_generated = False
    if not token:
        file_tok = _read_text_file(token_file)
        if file_tok:
            token = file_tok

    if not token:
        token = secrets.token_hex(16)
        token_is_generated = True

    nodes = _load_nodes()

    return HubSettings(
        api_version="v1",
        service_name="hapa-media-hub",
        host=host,
        port=port,
        token=token,
        token_is_generated=token_is_generated,
        token_file=token_file,
        nodes=nodes,
    )
