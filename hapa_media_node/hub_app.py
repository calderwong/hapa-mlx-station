from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import env_truthy, verify_request_token
from .db import utc_now_iso
from .hub_config import HubNode, HubSettings, load_hub_settings
from . import port_manager
from .self_test import run_self_test


def _get_total_memory_bytes() -> Optional[int]:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        phys_pages = int(os.sysconf("SC_PHYS_PAGES"))
        total = page_size * phys_pages
        return total if total > 0 else None
    except Exception:
        return None


def _get_process_tree_rss_bytes(root_pid: int) -> tuple[Optional[int], Optional[int]]:
    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True)
    except Exception:
        return None, None

    procs: dict[int, tuple[int, int]] = {}
    children: dict[int, list[int]] = {}

    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_kb = int(parts[2])
        except Exception:
            continue

        rss_bytes = max(0, rss_kb) * 1024
        procs[pid] = (ppid, rss_bytes)
        children.setdefault(ppid, []).append(pid)

    root = int(root_pid)
    rss_bytes = procs.get(root, (0, 0))[1] if root in procs else None

    total = 0
    stack = [root]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        info = procs.get(pid)
        if not info:
            continue
        total += int(info[1])
        stack.extend(children.get(pid, []))

    return rss_bytes, total


def _get_process_tree_stats(root_pid: int) -> dict[str, Any]:
    root = int(root_pid)

    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,pcpu=,comm="], text=True)
    except Exception:
        return {
            "pid": root,
            "rss_bytes": None,
            "tree_rss_bytes": None,
            "pcpu": None,
            "tree_pcpu": None,
            "processes": [],
        }

    procs: dict[int, tuple[int, int, Optional[float], str]] = {}
    children: dict[int, list[int]] = {}

    for line in out.splitlines():
        parts = line.strip().split(maxsplit=4)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_kb = int(parts[2])
        except Exception:
            continue

        pcpu: Optional[float] = None
        if len(parts) >= 4:
            try:
                pcpu = float(parts[3])
            except Exception:
                pcpu = None

        comm = str(parts[4]) if len(parts) >= 5 else ""

        rss_bytes = max(0, rss_kb) * 1024
        procs[pid] = (ppid, rss_bytes, pcpu, comm)
        children.setdefault(ppid, []).append(pid)

    rss_bytes = procs.get(root, (0, 0, None, ""))[1] if root in procs else None
    pcpu_root = procs.get(root, (0, 0, None, ""))[2] if root in procs else None

    total_rss = 0
    total_pcpu = 0.0
    any_pcpu = False
    stack = [root]
    seen: set[int] = set()
    processes: list[dict[str, Any]] = []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        info = procs.get(pid)
        if not info:
            continue
        ppid, prss, ppcpu, comm = info
        total_rss += int(prss)
        if ppcpu is not None:
            any_pcpu = True
            total_pcpu += float(ppcpu)

        processes.append({"pid": pid, "ppid": ppid, "rss_bytes": prss, "pcpu": ppcpu, "comm": comm})
        stack.extend(children.get(pid, []))

    processes.sort(key=lambda p: int(p.get("rss_bytes") or 0), reverse=True)

    return {
        "pid": root,
        "rss_bytes": rss_bytes,
        "tree_rss_bytes": int(total_rss) if seen else None,
        "pcpu": pcpu_root,
        "tree_pcpu": float(total_pcpu) if any_pcpu else None,
        "processes": processes,
    }


def _require_auth(request: Request) -> None:
    settings: HubSettings = request.app.state.settings
    allow_query_token = env_truthy(os.environ.get("HAPA_MEDIA_HUB_ALLOW_QUERY_TOKEN")) or env_truthy(
        os.environ.get("HAPA_MEDIA_ALLOW_QUERY_TOKEN")
    )
    verify_request_token(request, settings.token, allow_query_token=allow_query_token)


def _require_admin(request: Request) -> None:
    if env_truthy(os.environ.get("HAPA_MEDIA_HUB_DISABLE_ADMIN_API")) or env_truthy(
        os.environ.get("HAPA_MEDIA_DISABLE_ADMIN_API")
    ):
        raise HTTPException(status_code=404, detail="Not found")
    _require_auth(request)


def _get_nodes_state(app: FastAPI) -> tuple[list[HubNode], set[str]]:
    lock = getattr(app.state, "nodes_lock", None)
    if lock is None:
        settings: HubSettings = app.state.settings
        return list(settings.nodes), set()

    with lock:
        nodes = list(getattr(app.state, "nodes", []))
        disabled = set(getattr(app.state, "disabled_node_ids", set()))
        return nodes, disabled


def _enabled_nodes(app: FastAPI) -> list[HubNode]:
    nodes, disabled = _get_nodes_state(app)
    return [n for n in nodes if n.node_id not in disabled]


def _parse_composite_id(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if not value or ":" not in value:
        raise HTTPException(status_code=400, detail="Invalid id")

    node_id, inner = value.split(":", 1)
    node_id = node_id.strip()
    inner = inner.strip()
    if not node_id or not inner:
        raise HTTPException(status_code=400, detail="Invalid id")
    return node_id, inner


def _node_by_id(app: FastAPI, node_id: str) -> HubNode:
    node_id = str(node_id or "").strip()
    nodes, _ = _get_nodes_state(app)
    for n in nodes:
        if n.node_id == node_id:
            return n
    raise HTTPException(status_code=404, detail="Unknown node")


class NodeAddRequest(BaseModel):
    node_id: str
    base_url: str
    token: str
    enabled: bool = True


class WorkerScaleRequest(BaseModel):
    max_workers: int = Field(ge=1)


class LocalNodeSpawnRequest(BaseModel):
    node_id: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    base_port: Optional[int] = None
    token: Optional[str] = None
    max_workers: Optional[int] = Field(default=None, ge=1)
    enabled: bool = True


class LocalNodeTerminateRequest(BaseModel):
    remove: bool = True


class LocalProcessTerminateRequest(BaseModel):
    remove: bool = True


class SelfTestRequest(BaseModel):
    mode: str = "txt2img"
    model: str = "schnell"
    steps: int = Field(default=2, ge=1)
    copies: int = Field(default=2, ge=1)
    spawn_nodes: int = Field(default=1, ge=0)
    spawn_max_workers: Optional[int] = Field(default=1, ge=1)
    cleanup: bool = True
    startup_timeout_seconds: float = 30.0
    timeout_seconds: float = 600.0
    poll_interval_seconds: float = 1.0


def _http_json(
    method: str,
    url: str,
    *,
    token: str,
    body: Optional[bytes],
    content_type: Optional[str] = None,
    timeout_seconds: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = content_type or "application/json"

    req = urllib.request.Request(url=url, method=method, data=body, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as res:
            raw = res.read()
            data = json.loads(raw.decode("utf-8")) if raw else {}
            return int(res.status), data
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        text = raw.decode("utf-8", errors="replace")
        detail: Any = text
        try:
            parsed = json.loads(text) if text else {}
            if isinstance(parsed, dict) and "detail" in parsed:
                detail = parsed["detail"]
            else:
                detail = parsed
        except Exception:
            detail = text

        raise HTTPException(status_code=int(exc.code), detail=detail)
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503, detail=f"Node unavailable: {exc}")


def _http_stream(
    url: str,
    *,
    token: str,
    timeout_seconds: float = 30.0,
) -> tuple[Any, str, dict[str, str]]:
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url=url, method="GET", headers=headers)

    try:
        res = urllib.request.urlopen(req, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        text = raw.decode("utf-8", errors="replace")
        raise HTTPException(status_code=int(exc.code), detail=text)
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503, detail=f"Node unavailable: {exc}")

    content_type = str(res.headers.get("Content-Type") or "application/octet-stream")
    extra_headers: dict[str, str] = {}

    cd = res.headers.get("Content-Disposition")
    if cd:
        extra_headers["Content-Disposition"] = str(cd)

    return res, content_type, extra_headers


def _port_from_base_url(base_url: str) -> Optional[int]:
    base_url = str(base_url or "").strip()
    if not base_url:
        return None
    try:
        parsed = urllib.parse.urlparse(base_url)
    except Exception:
        return None
    port = parsed.port
    try:
        return int(port) if port is not None else None
    except Exception:
        return None


def _is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((str(host), int(port)))
            return True
    except Exception:
        return False


def _pick_free_port(host: str, *, base_port: int, avoid: set[int], max_scan: int = 256) -> int:
    start = max(1, int(base_port))
    for i in range(int(max_scan)):
        port = start + i
        if port in avoid:
            continue
        if _is_port_available(host, port):
            return port
    raise HTTPException(status_code=503, detail="No available ports")


def _wait_for_health(base_url: str, *, timeout_seconds: float = 30.0) -> None:
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="Missing base_url")

    deadline = time.time() + float(timeout_seconds)
    last_err: Optional[str] = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url=base_url + "/health", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as res:
                raw = res.read()
                data = json.loads(raw.decode("utf-8")) if raw else {}
                if isinstance(data, dict) and data.get("ok") is True:
                    return
                last_err = f"Unexpected /health response: {data}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(0.25)

    raise HTTPException(
        status_code=503,
        detail=f"Timed out waiting for /health at {base_url}: {last_err}",
    )


def _terminate_process(proc: subprocess.Popen, *, timeout_seconds: float = 5.0) -> None:
    if proc.poll() is not None:
        return

    try:
        if hasattr(os, "killpg"):
            try:
                os.killpg(int(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
        else:
            proc.terminate()
    except Exception:
        pass

    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)

    try:
        if hasattr(os, "killpg"):
            try:
                os.killpg(int(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
        else:
            proc.kill()
    except Exception:
        pass


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _pid_pgid(pid: int) -> Optional[int]:
    try:
        out = subprocess.check_output(["ps", "-p", str(int(pid)), "-o", "pgid="], text=True)
        text = str(out or "").strip()
        return int(text) if text else None
    except Exception:
        return None


def _terminate_pid(pid: int, *, timeout_seconds: float = 5.0) -> None:
    target = int(pid)
    if target <= 0:
        return
    if not _pid_exists(target):
        return

    pgid = _pid_pgid(target)
    use_group = hasattr(os, "killpg") and pgid is not None and int(pgid) == target

    try:
        if use_group:
            os.killpg(target, signal.SIGTERM)
        else:
            os.kill(target, signal.SIGTERM)
    except Exception:
        pass

    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if not _pid_exists(target):
            return
        time.sleep(0.1)

    try:
        if use_group:
            os.killpg(target, signal.SIGKILL)
        else:
            os.kill(target, signal.SIGKILL)
    except Exception:
        pass


def _get_pid_command(pid: int) -> Optional[str]:
    try:
        out = subprocess.check_output(["ps", "-p", str(int(pid)), "-o", "command="], text=True)
    except Exception:
        return None
    cmd = str(out or "").strip()
    return cmd or None


def _parse_cmd_tokens(command: str) -> list[str]:
    cmd = str(command or "").strip()
    if not cmd:
        return []
    try:
        return [str(t) for t in shlex.split(cmd)]
    except Exception:
        return []


def _looks_like_hapa_media_node_serve(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if "serve" not in tokens:
        return False

    if len(tokens) >= 2:
        exe0 = os.path.basename(str(tokens[0] or ""))
        if exe0 in {"hapa-media-node", "hapa_media_node"} and str(tokens[1]) == "serve":
            return True

    for i in range(len(tokens) - 2):
        if str(tokens[i]) == "-m" and str(tokens[i + 1]) == "hapa_media_node" and str(tokens[i + 2]) == "serve":
            return True

    return False


def _extract_flag_value(tokens: list[str], flag: str) -> Optional[str]:
    f = str(flag or "")
    if not f:
        return None
    for i, t in enumerate(tokens):
        tt = str(t)
        if tt == f and i + 1 < len(tokens):
            return str(tokens[i + 1])
        if tt.startswith(f + "="):
            return str(tt.split("=", 1)[1])
    return None


def _redact_tokens(tokens: list[str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = str(tokens[i])
        if t == "--token" and i + 1 < len(tokens):
            out.append(t)
            out.append("***")
            i += 2
            continue
        if t.startswith("--token="):
            out.append("--token=***")
            i += 1
            continue
        out.append(t)
        i += 1
    return " ".join(out)


def _parse_hapa_serve_command(command: str) -> tuple[bool, Optional[str], Optional[int], str]:
    tokens = _parse_cmd_tokens(command)
    if not _looks_like_hapa_media_node_serve(tokens):
        return False, None, None, str(command or "").strip()

    host = _extract_flag_value(tokens, "--host")
    port_raw = _extract_flag_value(tokens, "--port")
    port: Optional[int] = None
    if port_raw:
        try:
            port = int(str(port_raw).strip())
        except Exception:
            port = None

    return True, (str(host).strip() if host else None), port, _redact_tokens(tokens)


def _iso_to_ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return float(dt.timestamp())
    except Exception:
        return None


def _sort_key_asc(iso_value: Any) -> tuple[bool, float]:
    ts = _iso_to_ts(iso_value)
    return (ts is None, float(ts or 0.0))


def _sort_key_desc(iso_value: Any) -> tuple[bool, float]:
    ts = _iso_to_ts(iso_value)
    return (ts is None, -float(ts or 0.0))


def _task_with_node_prefix(node_id: str, task: dict[str, Any]) -> dict[str, Any]:
    out = dict(task)
    out["task_id"] = f"{node_id}:{task.get('task_id')}"

    result = task.get("result")
    if isinstance(result, dict):
        r = dict(result)
        asset_id = r.get("asset_id")
        if asset_id:
            composite_asset_id = f"{node_id}:{asset_id}"
            r["asset_id"] = composite_asset_id
            r["download_url"] = f"/v1/assets/{composite_asset_id}/download"
        out["result"] = r

    return out


def _preset_with_node_prefix(node_id: str, preset: dict[str, Any]) -> dict[str, Any]:
    out = dict(preset)
    out["preset_id"] = f"{node_id}:{preset.get('preset_id')}"

    thumb = preset.get("thumbnail_asset_id")
    if thumb:
        composite_thumb = f"{node_id}:{thumb}"
        out["thumbnail_asset_id"] = composite_thumb
        out["thumbnail_download_url"] = f"/v1/assets/{composite_thumb}/download"
    else:
        out["thumbnail_asset_id"] = None
        out["thumbnail_download_url"] = None

    return out


def _prefix_request_asset_ids(node_id: str, req: Any) -> Any:
    if not isinstance(req, dict):
        return req

    out = dict(req)
    for k in [
        "image_asset_id",
        "masked_image_asset_id",
        "depth_image_asset_id",
        "controlnet_image_asset_id",
    ]:
        v = out.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = f"{node_id}:{v.strip()}"

    redux = out.get("redux_image_asset_ids")
    if isinstance(redux, list):
        out["redux_image_asset_ids"] = [
            (f"{node_id}:{str(v).strip()}" if str(v).strip() else "") for v in redux
        ]

    return out


def _strip_composite_asset_ids(payload: dict[str, Any]) -> Optional[str]:
    node_id: Optional[str] = None

    def _handle_one(value: Any) -> Any:
        nonlocal node_id
        if value is None:
            return value
        s = str(value).strip()
        if not s:
            return ""
        if ":" not in s:
            return s

        nid, inner = _parse_composite_id(s)
        if node_id and nid != node_id:
            raise HTTPException(status_code=400, detail="Mixed node asset ids are not supported")
        node_id = nid
        return inner

    for key in [
        "image_asset_id",
        "masked_image_asset_id",
        "depth_image_asset_id",
        "controlnet_image_asset_id",
    ]:
        if key in payload:
            payload[key] = _handle_one(payload.get(key))

    if "redux_image_asset_ids" in payload and isinstance(payload.get("redux_image_asset_ids"), list):
        payload["redux_image_asset_ids"] = [_handle_one(v) for v in (payload.get("redux_image_asset_ids") or [])]

    return node_id


def _get_node_caps(app: FastAPI, node: HubNode) -> tuple[bool, Optional[dict[str, Any]], Optional[Any]]:
    now = time.time()
    ttl = float(app.state.caps_ttl_seconds)

    with app.state.caps_lock:
        cached = app.state.caps_cache.get(node.node_id)
        if cached is not None:
            age = now - float(cached.get("time") or 0.0)
            if age >= 0.0 and age < ttl:
                return bool(cached.get("ok")), cached.get("capabilities"), cached.get("error")

    try:
        _, caps = _http_json(
            "GET",
            node.base_url + "/capabilities",
            token=node.token,
            body=None,
            timeout_seconds=float(app.state.node_timeout_seconds),
        )
        record: dict[str, Any] = {"time": now, "ok": True, "capabilities": caps, "error": None}
    except HTTPException as exc:
        record = {"time": now, "ok": False, "capabilities": None, "error": exc.detail}

    with app.state.caps_lock:
        app.state.caps_cache[node.node_id] = record

    return bool(record.get("ok")), record.get("capabilities"), record.get("error")


def _mark_node_unhealthy(app: FastAPI, node: HubNode, error: Any) -> None:
    now = time.time()
    record: dict[str, Any] = {"time": now, "ok": False, "capabilities": None, "error": error}
    with app.state.caps_lock:
        app.state.caps_cache[node.node_id] = record


def _eligible_nodes_for_mode(app: FastAPI, mode: str, *, allow_disabled: bool = False) -> list[HubNode]:
    mode = (mode or "txt2img").strip() or "txt2img"

    nodes, disabled = _get_nodes_state(app)

    eligible: list[HubNode] = []
    supported_modes: set[str] = set()

    for node in nodes:
        if not allow_disabled and node.node_id in disabled:
            continue
        ok, caps, _ = _get_node_caps(app, node)
        if not ok or not caps:
            continue
        image = ((caps.get("modalities") or {}).get("image") or {})
        modes = image.get("modes") or []
        if isinstance(modes, list):
            supported_modes.update([str(m) for m in modes])
            if mode in modes:
                eligible.append(node)

    if not eligible:
        if supported_modes and mode not in supported_modes:
            raise HTTPException(status_code=400, detail=f"Unsupported mode: {mode}")
        raise HTTPException(status_code=503, detail="No eligible nodes available")

    return eligible


def _choose_node(app: FastAPI, mode: str) -> HubNode:
    eligible = _eligible_nodes_for_mode(app, mode, allow_disabled=False)

    if len(eligible) == 1:
        return eligible[0]

    with app.state.rr_lock:
        idx = app.state.rr_index % len(eligible)
        app.state.rr_index += 1

    return eligible[idx]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = load_hub_settings()
    app.state.settings = settings
    app.state.rr_index = 0
    app.state.rr_lock = threading.Lock()

    app.state.nodes_lock = threading.Lock()
    app.state.nodes = list(settings.nodes)
    app.state.disabled_node_ids = set()

    app.state.node_timeout_seconds = float(os.environ.get("HAPA_MEDIA_HUB_NODE_TIMEOUT_SECONDS", "30"))
    app.state.caps_ttl_seconds = float(os.environ.get("HAPA_MEDIA_HUB_CAPS_TTL_SECONDS", "5"))
    app.state.caps_cache = {}
    app.state.caps_lock = threading.Lock()

    app.state.managed_nodes_lock = threading.Lock()
    app.state.managed_nodes = {}

    try:
        settings.token_file.parent.mkdir(parents=True, exist_ok=True)
        settings.token_file.write_text(settings.token + "\n", encoding="utf-8")
        try:
            os.chmod(settings.token_file, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[hapa-media-hub] Warning: could not write token file: {settings.token_file} ({e})")

    try:
        yield
    finally:
        procs: list[subprocess.Popen] = []
        leases: list[port_manager.PortLease] = []
        lock = getattr(app.state, "managed_nodes_lock", None)
        if lock is not None:
            with lock:
                for rec in list(getattr(app.state, "managed_nodes", {}).values()):
                    if isinstance(rec, dict):
                        p = rec.get("proc")
                        if p is not None:
                            procs.append(p)
                        lease = rec.get("lease")
                        if isinstance(lease, port_manager.PortLease):
                            leases.append(lease)
                app.state.managed_nodes = {}

        for p in procs:
            try:
                _terminate_process(p)
            except Exception:
                pass

        for lease in leases:
            try:
                lease.release(remove_runtime=True)
            except Exception:
                pass


def create_app() -> FastAPI:
    app = FastAPI(lifespan=_lifespan)

    index_path = Path(__file__).parent / "web" / "index.html"

    @app.get("/")
    def get_index():
        return FileResponse(index_path)

    @app.get("/health")
    def get_health(request: Request):
        settings: HubSettings = request.app.state.settings
        return {
            "ok": True,
            "service": settings.service_name,
            "api_version": settings.api_version,
            "time": utc_now_iso(),
        }

    @app.get("/v1/system", dependencies=[Depends(_require_auth)])
    def get_system(request: Request):
        settings: HubSettings = request.app.state.settings

        pid = int(os.getpid())
        stats = _get_process_tree_stats(pid)
        rss_bytes = stats.get("rss_bytes")
        tree_rss_bytes = stats.get("tree_rss_bytes")
        total_bytes = _get_total_memory_bytes()

        loadavg = None
        try:
            la = os.getloadavg()
            loadavg = {
                "1m": float(la[0]),
                "5m": float(la[1]),
                "15m": float(la[2]),
            }
        except Exception:
            loadavg = None

        nodes: dict[str, Any] = {}
        all_nodes, disabled = _get_nodes_state(request.app)
        for node in all_nodes:
            managed = False
            managed_state: Optional[str] = None
            pid: Optional[int] = None
            lock = getattr(request.app.state, "managed_nodes_lock", None)
            if lock is not None:
                with lock:
                    rec = getattr(request.app.state, "managed_nodes", {}).get(node.node_id)
                    if isinstance(rec, dict):
                        managed = True
                        proc = rec.get("proc")
                        if proc is None:
                            managed_state = "starting"
                        else:
                            try:
                                pid = int(proc.pid)
                            except Exception:
                                pid = None
                            managed_state = "running" if proc.poll() is None else "exited"

            try:
                _, data = _http_json(
                    "GET",
                    node.base_url + "/v1/system",
                    token=node.token,
                    body=None,
                    timeout_seconds=float(request.app.state.node_timeout_seconds),
                )
                if isinstance(data, dict) and isinstance(data.get("memory"), dict):
                    nodes[node.node_id] = {
                        "ok": True,
                        "enabled": node.node_id not in disabled,
                        "base_url": node.base_url,
                        "managed": managed,
                        "managed_state": managed_state,
                        "pid": pid,
                        "service": data.get("service"),
                        "memory": data.get("memory"),
                        "cpu": data.get("cpu"),
                        "workers": data.get("workers"),
                    }
                else:
                    nodes[node.node_id] = {
                        "ok": False,
                        "enabled": node.node_id not in disabled,
                        "base_url": node.base_url,
                        "managed": managed,
                        "managed_state": managed_state,
                        "pid": pid,
                        "error": "Invalid node response",
                    }
            except HTTPException as exc:
                nodes[node.node_id] = {
                    "ok": False,
                    "enabled": node.node_id not in disabled,
                    "base_url": node.base_url,
                    "managed": managed,
                    "managed_state": managed_state,
                    "pid": pid,
                    "error": exc.detail,
                }

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "memory": {
                "pid": pid,
                "rss_bytes": rss_bytes,
                "tree_rss_bytes": tree_rss_bytes,
                "total_bytes": total_bytes,
            },
            "cpu": {
                "pcpu": stats.get("pcpu"),
                "tree_pcpu": stats.get("tree_pcpu"),
                "loadavg": loadavg,
            },
            "nodes": nodes,
        }

    @app.get("/v1/admin/nodes", dependencies=[Depends(_require_admin)])
    def list_nodes(request: Request):
        settings: HubSettings = request.app.state.settings
        nodes, disabled = _get_nodes_state(request.app)
        managed: dict[str, dict[str, Any]] = {}
        lock = getattr(request.app.state, "managed_nodes_lock", None)
        if lock is not None:
            with lock:
                for node_id, rec in getattr(request.app.state, "managed_nodes", {}).items():
                    if not isinstance(rec, dict):
                        continue
                    proc = rec.get("proc")
                    pid: Optional[int] = None
                    state: str = "starting"
                    if proc is not None:
                        try:
                            pid = int(proc.pid)
                        except Exception:
                            pid = None
                        state = "running" if proc.poll() is None else "exited"
                    managed[str(node_id)] = {"pid": pid, "managed_state": state}
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "base_url": n.base_url,
                    "enabled": n.node_id not in disabled,
                    "managed": n.node_id in managed,
                    "managed_state": managed.get(n.node_id, {}).get("managed_state"),
                    "pid": managed.get(n.node_id, {}).get("pid"),
                }
                for n in nodes
            ],
        }

    @app.post("/v1/admin/self-test", dependencies=[Depends(_require_admin)])
    async def run_admin_self_test(body: SelfTestRequest, request: Request):
        settings: HubSettings = request.app.state.settings

        host = str(getattr(settings, "host", "") or "").strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        host_url = host
        if ":" in host_url and not host_url.startswith("["):
            host_url = f"[{host_url}]"
        hub_url = f"http://{host_url}:{int(settings.port)}"

        report = await anyio.to_thread.run_sync(
            lambda: run_self_test(
                hub_url,
                settings.token,
                mode=str(body.mode or "txt2img"),
                model=str(body.model or "schnell"),
                steps=int(body.steps or 2),
                copies=int(body.copies or 2),
                spawn_nodes=int(body.spawn_nodes or 0),
                spawn_max_workers=(
                    int(body.spawn_max_workers) if body.spawn_max_workers is not None else None
                ),
                cleanup=bool(body.cleanup),
                startup_timeout_seconds=float(body.startup_timeout_seconds or 30.0),
                timeout_seconds=float(body.timeout_seconds or 600.0),
                poll_interval_seconds=float(body.poll_interval_seconds or 1.0),
            )
        )

        if not isinstance(report, dict):
            raise HTTPException(status_code=500, detail="Self-test returned invalid response")

        return report

    @app.get("/v1/admin/processes/local", dependencies=[Depends(_require_admin)])
    def list_local_processes(request: Request):
        settings: HubSettings = request.app.state.settings

        nodes, _ = _get_nodes_state(request.app)
        local_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}
        by_base_url: dict[str, str] = {}
        by_port: dict[int, str] = {}
        for n in nodes:
            try:
                parsed = urllib.parse.urlparse(str(n.base_url or ""))
                scheme = parsed.scheme or "http"
                host = str(parsed.hostname or "").strip()
                port = parsed.port
                if port is None:
                    continue
                key = f"{scheme}://{host}:{int(port)}"
                by_base_url[key] = str(n.node_id)
                if host in local_hosts:
                    by_port[int(port)] = str(n.node_id)
            except Exception:
                continue

        managed_by_pid: dict[int, str] = {}
        managed_by_port: dict[int, str] = {}
        lock = getattr(request.app.state, "managed_nodes_lock", None)
        if lock is not None:
            with lock:
                for node_id, rec in getattr(request.app.state, "managed_nodes", {}).items():
                    if not isinstance(rec, dict):
                        continue
                    proc = rec.get("proc")
                    if proc is None:
                        continue
                    try:
                        managed_by_pid[int(proc.pid)] = str(node_id)
                    except Exception:
                        pass
                    if rec.get("port") is not None:
                        try:
                            managed_by_port[int(rec.get("port"))] = str(node_id)
                        except Exception:
                            pass

        try:
            out = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        processes: list[dict[str, Any]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except Exception:
                continue

            cmd = parts[1] if len(parts) > 1 else ""
            is_serve, host, port, redacted = _parse_hapa_serve_command(cmd)
            if not is_serve:
                continue

            base_url = None
            url_host = host
            if url_host in {"0.0.0.0", "::"}:
                url_host = "127.0.0.1"
            if url_host and port is not None:
                base_url = f"http://{url_host}:{int(port)}"

            node_id = None
            managed = False
            if pid in managed_by_pid:
                node_id = managed_by_pid[pid]
                managed = True
            elif port is not None and int(port) in managed_by_port:
                node_id = managed_by_port[int(port)]
                managed = True
            else:
                if base_url and base_url in by_base_url:
                    node_id = by_base_url[base_url]
                elif port is not None and int(port) in by_port:
                    node_id = by_port[int(port)]

            processes.append(
                {
                    "pid": int(pid),
                    "host": host,
                    "port": port,
                    "base_url": base_url,
                    "node_id": node_id,
                    "managed": bool(managed),
                    "command": redacted,
                }
            )

        processes.sort(key=lambda r: (r.get("port") is None, int(r.get("port") or 0), int(r.get("pid") or 0)))

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "processes": processes,
        }

    @app.post("/v1/admin/processes/local/{pid}/terminate", dependencies=[Depends(_require_admin)])
    def terminate_local_process(pid: int, body: LocalProcessTerminateRequest, request: Request):
        settings: HubSettings = request.app.state.settings

        target = int(pid)
        if target < 1:
            raise HTTPException(status_code=400, detail="Invalid pid")
        if not _pid_exists(target):
            raise HTTPException(status_code=404, detail="Process not found")

        cmd = _get_pid_command(target)
        if cmd is None:
            raise HTTPException(status_code=500, detail="Unable to inspect process")

        is_serve, host, port, redacted = _parse_hapa_serve_command(cmd)
        if not is_serve:
            raise HTTPException(status_code=400, detail="Refusing to terminate: not a hapa_media_node serve process")

        url_host = host
        if url_host in {"0.0.0.0", "::"}:
            url_host = "127.0.0.1"
        base_url = f"http://{url_host}:{int(port)}" if url_host and port is not None else None

        node_id: Optional[str] = None
        removed_from_managed = False
        removed_from_nodes = False

        proc: Optional[subprocess.Popen] = None
        lease: Optional[port_manager.PortLease] = None
        lock = getattr(request.app.state, "managed_nodes_lock", None)
        if lock is not None:
            with lock:
                for nid, rec in list(getattr(request.app.state, "managed_nodes", {}).items()):
                    if not isinstance(rec, dict):
                        continue
                    p = rec.get("proc")
                    if p is None:
                        continue
                    try:
                        if int(p.pid) != target:
                            continue
                    except Exception:
                        continue

                    node_id = str(nid)
                    proc = p
                    lp = rec.get("lease")
                    if isinstance(lp, port_manager.PortLease):
                        lease = lp
                    getattr(request.app.state, "managed_nodes", {}).pop(nid, None)
                    removed_from_managed = True
                    break

        if proc is not None:
            try:
                _terminate_process(proc)
            except Exception:
                pass
        else:
            _terminate_pid(target, timeout_seconds=5.0)

        if _pid_exists(target):
            raise HTTPException(status_code=500, detail=f"Failed to terminate pid {target}")

        if lease is not None:
            try:
                lease.release(remove_runtime=True)
            except Exception:
                pass

        if bool(body.remove):
            local_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}
            with request.app.state.nodes_lock:
                nodes = request.app.state.nodes
                idx = None
                if node_id is not None:
                    idx = next((i for i, n in enumerate(nodes) if n.node_id == node_id), None)
                if idx is None and base_url:
                    idx = next(
                        (
                            i
                            for i, n in enumerate(nodes)
                            if str(getattr(n, "base_url", "") or "").strip().rstrip("/")
                            == str(base_url).strip().rstrip("/")
                        ),
                        None,
                    )
                if idx is None and port is not None:
                    candidates = []
                    for i, n in enumerate(nodes):
                        try:
                            parsed = urllib.parse.urlparse(str(n.base_url or ""))
                            nhost = str(parsed.hostname or "").strip()
                            nport = parsed.port
                        except Exception:
                            continue
                        if nport is None:
                            continue
                        if int(nport) != int(port):
                            continue
                        if nhost not in local_hosts:
                            continue
                        candidates.append(i)
                    if len(candidates) == 1:
                        idx = int(candidates[0])

                if idx is not None:
                    removed_node = nodes.pop(int(idx))
                    removed_node_id = str(getattr(removed_node, "node_id", "") or "").strip() or None
                    if node_id is None and removed_node_id is not None:
                        node_id = removed_node_id
                    if node_id is not None:
                        request.app.state.disabled_node_ids.discard(node_id)
                    removed_from_nodes = True

            with request.app.state.caps_lock:
                request.app.state.caps_cache.pop(node_id, None)

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "pid": int(target),
            "host": host,
            "port": port,
            "base_url": base_url,
            "node_id": node_id,
            "managed": bool(removed_from_managed),
            "terminated": True,
            "removed": bool(body.remove) and removed_from_nodes,
            "command": redacted,
        }

    @app.post("/v1/admin/nodes", dependencies=[Depends(_require_admin)], status_code=201)
    def add_node(body: NodeAddRequest, request: Request):
        settings: HubSettings = request.app.state.settings

        node_id = str(body.node_id or "").strip()
        base_url = str(body.base_url or "").strip().rstrip("/")
        token = str(body.token or "").strip()

        if not node_id or ":" in node_id or any(c.isspace() for c in node_id):
            raise HTTPException(status_code=400, detail="Invalid node_id")
        if not base_url:
            raise HTTPException(status_code=400, detail="Missing base_url")
        if not token:
            raise HTTPException(status_code=400, detail="Missing token")

        with request.app.state.nodes_lock:
            nodes = request.app.state.nodes
            if any(n.node_id == node_id for n in nodes):
                raise HTTPException(status_code=409, detail="Node already exists")
            nodes.append(HubNode(node_id=node_id, base_url=base_url, token=token))
            if not bool(body.enabled):
                request.app.state.disabled_node_ids.add(node_id)
            else:
                request.app.state.disabled_node_ids.discard(node_id)

            with request.app.state.caps_lock:
                request.app.state.caps_cache.pop(node_id, None)

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "node": {"node_id": node_id, "base_url": base_url, "enabled": bool(body.enabled)},
        }

    @app.post("/v1/admin/nodes/spawn", dependencies=[Depends(_require_admin)], status_code=201)
    def spawn_node(body: LocalNodeSpawnRequest, request: Request):
        settings: HubSettings = request.app.state.settings

        desired_node_id = str(body.node_id or "").strip() or None

        host = str(body.host or "").strip()
        if not host:
            host = str(getattr(settings, "host", "") or "").strip()
        if not host or host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"

        token_default = str(getattr(settings, "token", "") or "").strip()
        token = str(body.token or "").strip() or token_default
        if not token:
            raise HTTPException(status_code=400, detail="Missing token")

        try:
            base_port = int(body.base_port) if body.base_port is not None else int(
                os.environ.get("HAPA_MEDIA_HUB_LOCAL_NODE_BASE_PORT", "8724")
            )
        except Exception:
            base_port = 8724
        if base_port < 1:
            base_port = 8724

        existing_nodes: list[HubNode] = []
        existing_ids: set[str] = set()
        avoid_ports: set[int] = set()
        avoid_ports.add(int(settings.port))

        with request.app.state.nodes_lock:
            existing_nodes = list(request.app.state.nodes)
            existing_ids = {n.node_id for n in existing_nodes}
            for n in existing_nodes:
                p = _port_from_base_url(n.base_url)
                if p is not None:
                    avoid_ports.add(int(p))

        with request.app.state.managed_nodes_lock:
            for rec in getattr(request.app.state, "managed_nodes", {}).values():
                if isinstance(rec, dict) and rec.get("port") is not None:
                    try:
                        avoid_ports.add(int(rec.get("port")))
                    except Exception:
                        pass

        node_id: str
        if desired_node_id:
            node_id = desired_node_id
        else:
            i = 1
            while True:
                cand = f"node{i}"
                if cand in existing_ids:
                    i += 1
                    continue
                with request.app.state.managed_nodes_lock:
                    if cand in getattr(request.app.state, "managed_nodes", {}):
                        i += 1
                        continue
                node_id = cand
                break

        node_id = str(node_id or "").strip()
        if not node_id or ":" in node_id or any(c.isspace() for c in node_id):
            raise HTTPException(status_code=400, detail="Invalid node_id")

        existing_node = next((n for n in existing_nodes if n.node_id == node_id), None)
        token_final: str
        base_url: str
        port: int
        lease: Optional[port_manager.PortLease] = None

        if existing_node is not None:
            parsed = urllib.parse.urlparse(existing_node.base_url)
            scheme = parsed.scheme or "http"
            existing_host = str(parsed.hostname or "").strip()
            if not existing_host or existing_host in {"0.0.0.0", "::"}:
                existing_host = host
            host = existing_host or host

            port_raw: Optional[int] = parsed.port if parsed.port is not None else None
            if body.port is not None:
                port_raw = int(body.port)
            if port_raw is None:
                raise HTTPException(status_code=400, detail="Node base_url missing port")

            token_final = str(body.token or "").strip() or existing_node.token
            if not token_final:
                raise HTTPException(status_code=400, detail="Missing token")

            try:
                lease = port_manager.acquire_port_lease(
                    service="hapa-media-gen-node",
                    host=host,
                    base_port=int(port_raw),
                    max_scan=1,
                    preferred_port=int(port_raw),
                    instance=node_id,
                    pid=0,
                )
            except Exception:
                raise HTTPException(status_code=409, detail=f"Port unavailable: {port_raw}")

            port = int(lease.port)
            base_url = f"{scheme}://{host}:{int(port)}"
        else:
            token_final = token

            preferred_port: Optional[int] = int(body.port) if body.port is not None else None
            if preferred_port is not None and int(preferred_port) in avoid_ports:
                raise HTTPException(status_code=409, detail=f"Port already in use: {preferred_port}")

            if preferred_port is not None:
                try:
                    lease = port_manager.acquire_port_lease(
                        service="hapa-media-gen-node",
                        host=host,
                        base_port=int(preferred_port),
                        max_scan=1,
                        preferred_port=int(preferred_port),
                        instance=node_id,
                        pid=0,
                    )
                except Exception:
                    raise HTTPException(status_code=409, detail=f"Port unavailable: {preferred_port}")
            else:
                max_scan = 256
                start = max(1, int(base_port))
                for i in range(int(max_scan)):
                    cand = start + i
                    if int(cand) in avoid_ports:
                        continue
                    try:
                        lease = port_manager.acquire_port_lease(
                            service="hapa-media-gen-node",
                            host=host,
                            base_port=int(cand),
                            max_scan=1,
                            preferred_port=int(cand),
                            instance=node_id,
                            pid=0,
                        )
                        break
                    except Exception:
                        lease = None
                        continue

                if lease is None:
                    raise HTTPException(status_code=503, detail="No available ports")

            port = int(lease.port)
            base_url = f"http://{host}:{int(port)}"

        storage_root = os.environ.get("HAPA_MEDIA_HUB_LOCAL_NODE_STORAGE_ROOT")
        if storage_root:
            storage_dir = (Path(storage_root).expanduser().resolve() / node_id).resolve()
        else:
            if node_id == "node1":
                storage_dir = (Path.cwd() / "data").resolve()
            else:
                storage_dir = (Path.cwd() / f"data_{node_id}").resolve()
        try:
            storage_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            try:
                if lease is not None:
                    lease.release(remove_runtime=True)
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=str(exc))

        with request.app.state.managed_nodes_lock:
            if node_id in request.app.state.managed_nodes:
                try:
                    if lease is not None:
                        lease.release(remove_runtime=True)
                except Exception:
                    pass
                raise HTTPException(status_code=409, detail="Node already managed")
            request.app.state.managed_nodes[node_id] = {
                "proc": None,
                "base_url": base_url,
                "host": host,
                "port": int(port),
                "storage_dir": str(storage_dir),
                "lease": lease,
            }

        node_env = os.environ.copy()
        node_env["HAPA_MEDIA_NODE_STORAGE_DIR"] = str(storage_dir)
        if body.max_workers is not None:
            node_env["HAPA_MEDIA_NODE_MAX_WORKERS"] = str(int(body.max_workers))

        token_file = (storage_dir / ".node_token").resolve()
        node_env["HAPA_MEDIA_NODE_TOKEN_FILE"] = str(token_file)
        try:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(str(token_final) + "\n", encoding="utf-8")
            try:
                os.chmod(token_file, 0o600)
            except Exception:
                pass
        except Exception:
            node_env["HAPA_MEDIA_NODE_TOKEN"] = str(token_final)

        cmd = [
            sys.executable,
            "-m",
            "hapa_media_node",
            "serve",
            "--host",
            str(host),
            "--port",
            str(int(port)),
        ]

        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(cmd, env=node_env, start_new_session=True)
            with request.app.state.managed_nodes_lock:
                rec = request.app.state.managed_nodes.get(node_id)
                if isinstance(rec, dict):
                    rec["proc"] = proc
                    rec["token"] = token_final

            if lease is not None and proc is not None:
                try:
                    lease.set_pid(int(proc.pid))
                    lease.write_runtime(
                        base_url=base_url,
                        token_path=token_file,
                        storage_dir=storage_dir,
                        extra={"node_id": str(node_id)},
                    )
                except Exception:
                    pass

            _wait_for_health(base_url, timeout_seconds=float(request.app.state.node_timeout_seconds))
        except HTTPException:
            if proc is not None:
                try:
                    _terminate_process(proc)
                except Exception:
                    pass
            try:
                if lease is not None:
                    lease.release(remove_runtime=True)
            except Exception:
                pass
            with request.app.state.managed_nodes_lock:
                request.app.state.managed_nodes.pop(node_id, None)
            raise
        except Exception as exc:
            if proc is not None:
                try:
                    _terminate_process(proc)
                except Exception:
                    pass
            try:
                if lease is not None:
                    lease.release(remove_runtime=True)
            except Exception:
                pass
            with request.app.state.managed_nodes_lock:
                request.app.state.managed_nodes.pop(node_id, None)
            raise HTTPException(status_code=500, detail=str(exc))

        with request.app.state.nodes_lock:
            nodes_list = request.app.state.nodes
            idx = next((i for i, n in enumerate(nodes_list) if n.node_id == node_id), None)
            if idx is None:
                nodes_list.append(HubNode(node_id=node_id, base_url=base_url, token=token_final))
            else:
                nodes_list[int(idx)] = HubNode(node_id=node_id, base_url=base_url, token=token_final)

            if not bool(body.enabled):
                request.app.state.disabled_node_ids.add(node_id)
            else:
                request.app.state.disabled_node_ids.discard(node_id)

        with request.app.state.caps_lock:
            request.app.state.caps_cache.pop(node_id, None)

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "node": {
                "node_id": node_id,
                "base_url": base_url,
                "enabled": bool(body.enabled),
                "managed": True,
                "managed_state": "running",
                "pid": int(proc.pid) if proc is not None else None,
            },
        }

    @app.post("/v1/admin/nodes/{node_id}/terminate", dependencies=[Depends(_require_admin)])
    def terminate_node(node_id: str, body: LocalNodeTerminateRequest, request: Request):
        settings: HubSettings = request.app.state.settings
        node_id = str(node_id or "").strip()
        proc: Optional[subprocess.Popen] = None
        lease: Optional[port_manager.PortLease] = None
        base_url: Optional[str] = None

        lock = getattr(request.app.state, "managed_nodes_lock", None)
        if lock is None:
            raise HTTPException(status_code=500, detail="Managed nodes unavailable")

        with lock:
            rec = getattr(request.app.state, "managed_nodes", {}).pop(node_id, None)
            if not isinstance(rec, dict):
                raise HTTPException(status_code=404, detail="Node is not managed")
            base_url = rec.get("base_url") if isinstance(rec.get("base_url"), str) else None
            p = rec.get("proc")
            if p is not None:
                proc = p
            lp = rec.get("lease")
            if isinstance(lp, port_manager.PortLease):
                lease = lp

        if proc is not None:
            try:
                _terminate_process(proc)
            except Exception:
                pass

        if lease is not None:
            try:
                lease.release(remove_runtime=True)
            except Exception:
                pass

        removed = False
        if bool(body.remove):
            with request.app.state.nodes_lock:
                nodes = request.app.state.nodes
                idx = next((i for i, n in enumerate(nodes) if n.node_id == node_id), None)
                if idx is not None:
                    nodes.pop(int(idx))
                    removed = True
                request.app.state.disabled_node_ids.discard(node_id)

            with request.app.state.caps_lock:
                request.app.state.caps_cache.pop(node_id, None)

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "node_id": node_id,
            "base_url": base_url,
            "terminated": True,
            "removed": removed,
        }

    @app.post("/v1/admin/nodes/{node_id}/disable", dependencies=[Depends(_require_admin)])
    def disable_node(node_id: str, request: Request):
        settings: HubSettings = request.app.state.settings
        _node_by_id(request.app, node_id)
        with request.app.state.nodes_lock:
            request.app.state.disabled_node_ids.add(str(node_id).strip())
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "node_id": node_id,
            "enabled": False,
        }

    @app.post("/v1/admin/nodes/{node_id}/enable", dependencies=[Depends(_require_admin)])
    def enable_node(node_id: str, request: Request):
        settings: HubSettings = request.app.state.settings
        _node_by_id(request.app, node_id)
        with request.app.state.nodes_lock:
            request.app.state.disabled_node_ids.discard(str(node_id).strip())
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "node_id": node_id,
            "enabled": True,
        }

    @app.delete("/v1/admin/nodes/{node_id}", dependencies=[Depends(_require_admin)])
    def remove_node(node_id: str, request: Request):
        settings: HubSettings = request.app.state.settings
        node_id = str(node_id or "").strip()
        proc: Optional[subprocess.Popen] = None
        lease: Optional[port_manager.PortLease] = None

        with request.app.state.nodes_lock:
            nodes = request.app.state.nodes
            idx = next((i for i, n in enumerate(nodes) if n.node_id == node_id), None)
            if idx is None:
                raise HTTPException(status_code=404, detail="Unknown node")
            nodes.pop(int(idx))
            request.app.state.disabled_node_ids.discard(node_id)

        with request.app.state.caps_lock:
            request.app.state.caps_cache.pop(node_id, None)

        lock = getattr(request.app.state, "managed_nodes_lock", None)
        if lock is not None:
            with lock:
                rec = getattr(request.app.state, "managed_nodes", {}).pop(node_id, None)
                if isinstance(rec, dict):
                    p = rec.get("proc")
                    if p is not None:
                        proc = p
                    lp = rec.get("lease")
                    if isinstance(lp, port_manager.PortLease):
                        lease = lp

        if proc is not None:
            try:
                _terminate_process(proc)
            except Exception:
                pass

        if lease is not None:
            try:
                lease.release(remove_runtime=True)
            except Exception:
                pass

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "node_id": node_id,
        }

    @app.post("/v1/admin/nodes/{node_id}/workers", dependencies=[Depends(_require_admin)])
    def set_node_workers(node_id: str, body: WorkerScaleRequest, request: Request):
        settings: HubSettings = request.app.state.settings
        node = _node_by_id(request.app, node_id)
        forward_body = json.dumps({"max_workers": int(body.max_workers)}).encode("utf-8")
        _, data = _http_json(
            "POST",
            node.base_url + "/v1/admin/workers",
            token=node.token,
            body=forward_body,
            content_type="application/json",
            timeout_seconds=float(request.app.state.node_timeout_seconds),
        )
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "node_id": node.node_id,
            "workers": (data or {}).get("workers") if isinstance(data, dict) else None,
        }

    @app.get("/capabilities", dependencies=[Depends(_require_auth)])
    def get_capabilities(request: Request):
        settings: HubSettings = request.app.state.settings
        engines: set[str] = set()
        models: set[str] = set()
        modes: set[str] = set()
        features: set[str] = set()
        input_fields_base64: set[str] = set()

        nodes: dict[str, Any] = {}
        for node in _enabled_nodes(request.app):
            ok, caps, error = _get_node_caps(request.app, node)
            if ok and caps:
                nodes[node.node_id] = {"ok": True, "capabilities": caps}

                image = ((caps.get("modalities") or {}).get("image") or {})
                if isinstance(image, dict):
                    for v in list(image.get("engines") or []):
                        engines.add(str(v))
                    for v in list(image.get("models") or []):
                        models.add(str(v))
                    for v in list(image.get("modes") or []):
                        modes.add(str(v))
                    for v in list(image.get("features") or []):
                        features.add(str(v))
                    for v in list(image.get("input_fields_base64") or []):
                        input_fields_base64.add(str(v))
            else:
                nodes[node.node_id] = {"ok": False, "error": error}

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "modalities": {
                "image": {
                    "engines": sorted(engines),
                    "models": sorted(models),
                    "modes": sorted(modes),
                    "features": sorted(features),
                    "input_fields_base64": sorted(input_fields_base64),
                }
            },
            "nodes": nodes,
        }

    @app.get("/v1/queue", dependencies=[Depends(_require_auth)])
    def get_queue(
        request: Request,
        queued_limit: int = 50,
        running_limit: int = 10,
        recent_limit: int = 20,
    ):
        settings: HubSettings = request.app.state.settings

        ql = max(0, int(queued_limit))
        rl = max(0, int(running_limit))
        rec_l = max(0, int(recent_limit))

        counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
        running: list[dict[str, Any]] = []
        queued: list[dict[str, Any]] = []
        recent: list[dict[str, Any]] = []

        nodes: dict[str, Any] = {}
        first_error: Optional[HTTPException] = None

        for node in _get_nodes_state(request.app)[0]:
            url = (
                node.base_url
                + "/v1/queue"
                + f"?queued_limit={ql}&running_limit={rl}&recent_limit={rec_l}"
            )
            try:
                _, data = _http_json(
                    "GET",
                    url,
                    token=node.token,
                    body=None,
                    timeout_seconds=float(request.app.state.node_timeout_seconds),
                )
                nodes[node.node_id] = {"ok": True}
            except HTTPException as exc:
                nodes[node.node_id] = {"ok": False, "error": exc.detail}
                if first_error is None:
                    first_error = exc
                continue

            c = data.get("counts") if isinstance(data, dict) else None
            if isinstance(c, dict):
                for k in ["queued", "running", "succeeded", "failed"]:
                    try:
                        counts[k] += int(c.get(k) or 0)
                    except Exception:
                        pass

            r = data.get("running") if isinstance(data, dict) else None
            if isinstance(r, list):
                for t in r:
                    if isinstance(t, dict):
                        running.append(_task_with_node_prefix(node.node_id, t))

            q = data.get("queued") if isinstance(data, dict) else None
            if isinstance(q, list):
                for t in q:
                    if isinstance(t, dict):
                        queued.append(_task_with_node_prefix(node.node_id, t))

            rec = data.get("recent") if isinstance(data, dict) else None
            if isinstance(rec, list):
                for t in rec:
                    if isinstance(t, dict):
                        recent.append(_task_with_node_prefix(node.node_id, t))

        if not any(bool(v.get("ok")) for v in nodes.values()):
            if first_error is not None:
                raise HTTPException(status_code=int(first_error.status_code), detail=first_error.detail)
            raise HTTPException(status_code=503, detail="No nodes available")

        running.sort(key=lambda t: _sort_key_asc(t.get("started_at")))
        queued.sort(key=lambda t: _sort_key_asc(t.get("created_at")))
        recent.sort(key=lambda t: _sort_key_desc(t.get("finished_at")))

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "counts": counts,
            "running": running[:rl],
            "queued": queued[:ql],
            "recent": recent[:rec_l],
            "nodes": nodes,
        }

    @app.get("/v1/presets", dependencies=[Depends(_require_auth)])
    def list_presets(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "DESC",
    ):
        settings: HubSettings = request.app.state.settings

        lim = min(max(0, int(limit)), 200)
        off = max(0, int(offset))

        order_by_v = str(order_by or "created_at").strip() or "created_at"
        order_dir_v = str(order_dir or "DESC").strip().upper() or "DESC"
        if order_dir_v not in {"ASC", "DESC"}:
            raise HTTPException(status_code=400, detail=f"Invalid order_dir: {order_dir}")

        fetch_limit = min(200, lim + off)

        presets: list[dict[str, Any]] = []
        nodes: dict[str, Any] = {}
        first_error: Optional[HTTPException] = None

        for node in _get_nodes_state(request.app)[0]:
            url = (
                node.base_url
                + "/v1/presets"
                + f"?limit={fetch_limit}&offset=0&order_by={order_by_v}&order_dir={order_dir_v}"
            )
            try:
                _, data = _http_json(
                    "GET",
                    url,
                    token=node.token,
                    body=None,
                    timeout_seconds=float(request.app.state.node_timeout_seconds),
                )
                nodes[node.node_id] = {"ok": True}
            except HTTPException as exc:
                nodes[node.node_id] = {"ok": False, "error": exc.detail}
                if first_error is None:
                    first_error = exc
                continue

            items = data.get("presets") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            for p in items:
                if isinstance(p, dict):
                    presets.append(_preset_with_node_prefix(node.node_id, p))

        if not any(bool(v.get("ok")) for v in nodes.values()):
            if first_error is not None:
                raise HTTPException(status_code=int(first_error.status_code), detail=first_error.detail)
            raise HTTPException(status_code=503, detail="No nodes available")

        if order_by_v == "name":
            presets.sort(key=lambda p: str(p.get("name") or "").casefold(), reverse=(order_dir_v == "DESC"))
        elif order_by_v == "updated_at":
            presets.sort(key=lambda p: _sort_key_desc(p.get("updated_at")) if order_dir_v == "DESC" else _sort_key_asc(p.get("updated_at")))
        else:
            presets.sort(key=lambda p: _sort_key_desc(p.get("created_at")) if order_dir_v == "DESC" else _sort_key_asc(p.get("created_at")))

        sliced = presets[off : off + lim]

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "limit": lim,
            "offset": off,
            "presets": sliced,
            "nodes": nodes,
        }

    @app.get("/v1/presets/{preset_id}", dependencies=[Depends(_require_auth)])
    def get_preset(preset_id: str, request: Request):
        settings: HubSettings = request.app.state.settings
        node_id, inner = _parse_composite_id(preset_id)
        node = _node_by_id(request.app, node_id)

        _, data = _http_json(
            "GET",
            node.base_url + f"/v1/presets/{inner}",
            token=node.token,
            body=None,
            timeout_seconds=float(request.app.state.node_timeout_seconds),
        )

        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="Invalid node response")

        data["preset_id"] = f"{node_id}:{data.get('preset_id') or inner}"

        thumb = data.get("thumbnail_asset_id")
        if thumb:
            composite_thumb = f"{node_id}:{thumb}"
            data["thumbnail_asset_id"] = composite_thumb
            data["thumbnail_download_url"] = f"/v1/assets/{composite_thumb}/download"
        else:
            data["thumbnail_asset_id"] = None
            data["thumbnail_download_url"] = None

        req = data.get("request")
        if isinstance(req, dict):
            data["request"] = _prefix_request_asset_ids(node_id, req)

        return data

    @app.post("/v1/presets", dependencies=[Depends(_require_auth)], status_code=201)
    async def create_preset(request: Request):
        raw = await request.body()
        payload: Any = None
        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="Invalid JSON")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        try:
            settings: HubSettings = request.app.state.settings

            req = payload.get("request")
            if not isinstance(req, dict):
                raise HTTPException(status_code=400, detail="Invalid preset request")

            mode = str(req.get("mode") or "txt2img").strip() or "txt2img"

            node_id: Optional[str] = None

            thumb = payload.get("thumbnail_asset_id")
            if isinstance(thumb, str) and ":" in thumb:
                nid, inner_thumb = _parse_composite_id(thumb)
                node_id = nid
                payload["thumbnail_asset_id"] = inner_thumb

            req_node_id = _strip_composite_asset_ids(req)
            if req_node_id:
                if node_id and node_id != req_node_id:
                    raise HTTPException(status_code=400, detail="Mixed node asset ids are not supported")
                node_id = req_node_id

            if node_id:
                node = _node_by_id(request.app, node_id)
                eligible = await anyio.to_thread.run_sync(
                    lambda: _eligible_nodes_for_mode(request.app, mode, allow_disabled=True)
                )
                if not any(n.node_id == node.node_id for n in eligible):
                    raise HTTPException(status_code=400, detail=f"Selected node does not support mode: {mode}")
            else:
                node = await anyio.to_thread.run_sync(_choose_node, request.app, mode)

            forward_body = json.dumps(payload).encode("utf-8")
            _, data = _http_json(
                "POST",
                node.base_url + "/v1/presets",
                token=node.token,
                body=forward_body,
                content_type=request.headers.get("content-type"),
                timeout_seconds=float(request.app.state.node_timeout_seconds),
            )

            if not isinstance(data, dict):
                raise HTTPException(status_code=502, detail="Invalid node response")

            preset_id = data.get("preset_id")
            if not preset_id:
                raise HTTPException(status_code=502, detail="Node response missing preset_id")

            return {
                "api_version": settings.api_version,
                "time": utc_now_iso(),
                "preset_id": f"{node.node_id}:{preset_id}",
            }
        finally:
            del raw

    @app.delete("/v1/presets/{preset_id}", dependencies=[Depends(_require_auth)])
    def delete_preset(preset_id: str, request: Request):
        settings: HubSettings = request.app.state.settings
        node_id, inner = _parse_composite_id(preset_id)
        node = _node_by_id(request.app, node_id)

        _, data = _http_json(
            "DELETE",
            node.base_url + f"/v1/presets/{inner}",
            token=node.token,
            body=None,
            timeout_seconds=float(request.app.state.node_timeout_seconds),
        )

        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="Invalid node response")

        return data

    @app.post("/v1/images/generations", dependencies=[Depends(_require_auth)], status_code=202)
    async def post_image_generation(request: Request):
        raw = await request.body()

        mode = "txt2img"
        payload: Any = None
        target_node_id: Optional[str] = None
        forward_body = raw
        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="Invalid JSON")
            mode = str(payload.get("mode") or "txt2img").strip() or "txt2img"
            target_node_id = _strip_composite_asset_ids(payload)
            if target_node_id:
                forward_body = json.dumps(payload).encode("utf-8")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        finally:
            if isinstance(payload, dict):
                payload.clear()

        try:
            settings: HubSettings = request.app.state.settings
            if target_node_id:
                node = _node_by_id(request.app, target_node_id)
                eligible = await anyio.to_thread.run_sync(
                    lambda: _eligible_nodes_for_mode(request.app, mode, allow_disabled=True)
                )
                if not any(n.node_id == node.node_id for n in eligible):
                    raise HTTPException(status_code=400, detail=f"Selected node does not support mode: {mode}")
                attempt_nodes = [node]
            else:
                eligible = await anyio.to_thread.run_sync(
                    lambda: _eligible_nodes_for_mode(request.app, mode, allow_disabled=False)
                )

                if len(eligible) == 1:
                    attempt_nodes = eligible
                else:
                    with request.app.state.rr_lock:
                        idx = request.app.state.rr_index % len(eligible)
                        request.app.state.rr_index += 1
                    attempt_nodes = eligible[idx:] + eligible[:idx]

            for i, node in enumerate(attempt_nodes):

                def _call_node() -> dict[str, Any]:
                    _, data = _http_json(
                        "POST",
                        node.base_url + "/v1/images/generations",
                        token=node.token,
                        body=forward_body,
                        content_type=request.headers.get("content-type"),
                        timeout_seconds=float(request.app.state.node_timeout_seconds),
                    )
                    return data

                try:
                    data = await anyio.to_thread.run_sync(_call_node)
                    task_id = data.get("task_id")
                    if not task_id:
                        raise HTTPException(status_code=502, detail="Node response missing task_id")

                    return {
                        "api_version": "v1",
                        "time": utc_now_iso(),
                        "task_id": f"{node.node_id}:{task_id}",
                    }
                except HTTPException as exc:
                    retryable = int(exc.status_code) >= 500
                    if not retryable or i == (len(attempt_nodes) - 1):
                        raise
                    await anyio.to_thread.run_sync(_mark_node_unhealthy, request.app, node, exc.detail)
                except Exception as exc:
                    if i == (len(attempt_nodes) - 1):
                        raise HTTPException(status_code=502, detail=str(exc))
                    await anyio.to_thread.run_sync(_mark_node_unhealthy, request.app, node, str(exc))

            raise HTTPException(status_code=503, detail="No eligible nodes available")
        finally:
            del raw

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(_require_auth)])
    def get_task_status(task_id: str, request: Request):
        node_id, inner = _parse_composite_id(task_id)
        node = _node_by_id(request.app, node_id)

        _, data = _http_json(
            "GET",
            node.base_url + f"/v1/tasks/{inner}",
            token=node.token,
            body=None,
            timeout_seconds=float(request.app.state.node_timeout_seconds),
        )

        result = data.get("result")
        if isinstance(result, dict):
            asset_id = result.get("asset_id")
            if asset_id:
                result["asset_id"] = f"{node_id}:{asset_id}"
                result["download_url"] = f"/v1/assets/{node_id}:{asset_id}/download"

        data["task_id"] = f"{node_id}:{data.get('task_id') or inner}"
        return data

    @app.get("/v1/assets/{asset_id}", dependencies=[Depends(_require_auth)])
    def get_asset_metadata(asset_id: str, request: Request):
        node_id, inner = _parse_composite_id(asset_id)
        node = _node_by_id(request.app, node_id)

        _, data = _http_json(
            "GET",
            node.base_url + f"/v1/assets/{inner}",
            token=node.token,
            body=None,
            timeout_seconds=float(request.app.state.node_timeout_seconds),
        )
        data["asset_id"] = f"{node_id}:{data.get('asset_id') or inner}"
        return data

    @app.get("/v1/assets/{asset_id}/download", dependencies=[Depends(_require_auth)])
    def download_asset(asset_id: str, request: Request):
        node_id, inner = _parse_composite_id(asset_id)
        node = _node_by_id(request.app, node_id)

        res, content_type, extra_headers = _http_stream(
            node.base_url + f"/v1/assets/{inner}/download",
            token=node.token,
            timeout_seconds=float(request.app.state.node_timeout_seconds),
        )

        def _iter_chunks():
            try:
                while True:
                    chunk = res.read(1024 * 256)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    res.close()
                except Exception:
                    pass

        return StreamingResponse(_iter_chunks(), media_type=content_type, headers=extra_headers)

    return app
