from __future__ import annotations

import base64
import binascii
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import env_truthy, verify_request_token
from .config import Settings, load_settings
from .db import (
    count_tasks_by_status as db_count_tasks_by_status,
    create_preset as db_create_preset,
    delete_preset as db_delete_preset,
    get_asset,
    get_preset as db_get_preset,
    get_task,
    init_db,
    list_presets as db_list_presets,
    list_tasks as db_list_tasks,
    mark_running_tasks_as_queued,
    utc_now_iso,
)
from .db import create_task as db_create_task
from .worker import ALLOWED_QUANTIZE, DEFAULT_STEPS, TaskWorker


_B64_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_B64_WS = {" ", "\t", "\r", "\n"}


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


class ImageGenerationRequest(BaseModel):
    mode: str = Field(default="txt2img")
    prompt: str
    negative_prompt: Optional[str] = None
    model: str
    base_model: Optional[str] = None
    lora_style: Optional[str] = None
    lora_paths: Optional[list[str]] = None
    lora_scales: Optional[list[float]] = None
    image_base64: Optional[str] = None
    image_asset_id: Optional[str] = None
    masked_image_base64: Optional[str] = None
    masked_image_asset_id: Optional[str] = None
    depth_image_base64: Optional[str] = None
    depth_image_asset_id: Optional[str] = None
    controlnet_image_base64: Optional[str] = None
    controlnet_image_asset_id: Optional[str] = None
    redux_images_base64: Optional[list[str]] = None
    redux_image_asset_ids: Optional[list[str]] = None
    image_strength: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    controlnet_strength: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    redux_image_strengths: Optional[list[float]] = None
    steps: Optional[int] = Field(default=None, ge=1)
    seed: Optional[int] = None
    width: Optional[int] = Field(default=None, ge=256)
    height: Optional[int] = Field(default=None, ge=256)
    quantize: Optional[int] = Field(default=8)
    guidance: Optional[float] = None
    low_ram: bool = False


class PresetCreateRequest(BaseModel):
    name: str
    request: dict
    thumbnail_asset_id: Optional[str] = None


class WorkerScaleRequest(BaseModel):
    max_workers: int = Field(ge=1)


def _require_auth(request: Request) -> None:
    settings: Settings = request.app.state.settings
    allow_query_token = env_truthy(os.environ.get("HAPA_MEDIA_NODE_ALLOW_QUERY_TOKEN")) or env_truthy(
        os.environ.get("HAPA_MEDIA_ALLOW_QUERY_TOKEN")
    )
    verify_request_token(request, settings.token, allow_query_token=allow_query_token)


def _require_admin(request: Request) -> None:
    if env_truthy(os.environ.get("HAPA_MEDIA_NODE_DISABLE_ADMIN_API")) or env_truthy(
        os.environ.get("HAPA_MEDIA_DISABLE_ADMIN_API")
    ):
        raise HTTPException(status_code=404, detail="Not found")
    _require_auth(request)


def _validate_model_request(model: str, base_model: Optional[str]) -> None:
    model = model.strip()
    if model in {"schnell", "dev", "krea-dev", "fibo", "z-image-turbo"}:
        return

    if not base_model:
        raise HTTPException(
            status_code=400,
            detail="base_model is required when using third-party models or local paths",
        )


def _validate_quantize(quantize: Optional[int]) -> None:
    if quantize is None:
        return
    if quantize not in ALLOWED_QUANTIZE:
        raise HTTPException(status_code=400, detail=f"Invalid quantize value: {quantize}")


def _normalize_loras(
    lora_paths: Optional[list[str]],
    lora_scales: Optional[list[float]],
) -> tuple[Optional[list[str]], Optional[list[float]]]:
    if lora_paths is None:
        if lora_scales is not None:
            raise HTTPException(status_code=400, detail="lora_paths is required when lora_scales is provided")
        return None, None

    lora_paths = [p.strip() for p in lora_paths if str(p).strip()]
    if not lora_paths:
        return None, None

    if lora_scales is None:
        return lora_paths, None

    if not lora_scales:
        return lora_paths, None

    if len(lora_scales) == 1 and len(lora_paths) > 1:
        lora_scales = [float(lora_scales[0])] * len(lora_paths)
    elif len(lora_scales) != len(lora_paths):
        raise HTTPException(status_code=400, detail="lora_scales must have length 1 or match lora_paths length")

    return lora_paths, [float(s) for s in lora_scales]


def _guess_image_ext(mime: Optional[str], payload: bytes) -> str:
    if mime == "image/png":
        return ".png"
    if mime in {"image/jpg", "image/jpeg"}:
        return ".jpg"
    if mime == "image/webp":
        return ".webp"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8"):
        return ".jpg"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    return ".img"


def _write_base64_image_to_tmp(storage_dir: Path, value: str, *, prefix: str) -> str:
    value = value or ""
    if not value:
        raise HTTPException(status_code=400, detail=f"Missing {prefix} image")

    mime: Optional[str] = None
    if value.startswith("data:"):
        header, b64 = value.split(",", 1)
        if header.startswith("data:"):
            mime = header[5:].split(";", 1)[0]
        value = b64

    for ch in value:
        if ch in _B64_ALLOWED:
            continue
        if ch in _B64_WS:
            continue
        raise HTTPException(status_code=400, detail=f"Invalid base64 for {prefix} image")

    try:
        payload = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid base64 for {prefix} image")

    if not payload:
        raise HTTPException(status_code=400, detail=f"Invalid base64 for {prefix} image")

    ext = _guess_image_ext(mime, payload)

    tmp_dir = (storage_dir / "tmp" / "inputs").resolve()
    tmp_dir.mkdir(parents=True, exist_ok=True)

    out_abs = (tmp_dir / f"{prefix}_{uuid.uuid4().hex}{ext}").resolve()
    storage_root = storage_dir.resolve()
    try:
        out_abs.relative_to(storage_root)
    except ValueError:
        raise HTTPException(status_code=500, detail="Invalid temp path")

    out_abs.write_bytes(payload)
    del payload

    return str(out_abs.relative_to(storage_root))


def _asset_id_to_stored_path(settings: Settings, asset_id: str) -> str:
    asset_id = (asset_id or "").strip()
    if not asset_id:
        raise HTTPException(status_code=400, detail="Asset id is required")

    asset = get_asset(settings.db_path, asset_id)
    if not asset:
        raise HTTPException(status_code=400, detail=f"Asset not found: {asset_id}")

    asset_type = str(asset.get("type") or "")
    if not asset_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"Asset is not an image: {asset_id}")

    stored = str(asset.get("path") or "").strip()
    if not stored:
        raise HTTPException(status_code=500, detail="Invalid asset path")

    p = Path(stored)
    abs_path = p if p.is_absolute() else (settings.storage_dir / p)
    abs_path = abs_path.resolve()

    storage_root = settings.storage_dir.resolve()
    try:
        abs_path.relative_to(storage_root)
    except ValueError:
        raise HTTPException(status_code=500, detail="Invalid asset path")

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="Asset file missing")

    return str(abs_path.relative_to(storage_root))


def _iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _seconds_between(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[float]:
    start = _iso_to_dt(start_iso)
    end = _iso_to_dt(end_iso)
    if not start or not end:
        return None
    try:
        return float((end - start).total_seconds())
    except Exception:
        return None


def _task_to_public(task: dict, *, settings: Settings) -> dict:
    result = None
    asset_id = task.get("result_asset_id")
    if asset_id:
        result = {
            "asset_id": asset_id,
            "download_url": f"/v1/assets/{asset_id}/download",
        }

    created_at = task.get("created_at")
    started_at = task.get("started_at")
    finished_at = task.get("finished_at")

    return {
        "task_id": task.get("task_id"),
        "type": task.get("type"),
        "status": task.get("status"),
        "stage": task.get("stage"),
        "progress": task.get("progress"),
        "error": task.get("error"),
        "created_at": created_at,
        "updated_at": task.get("updated_at"),
        "started_at": started_at,
        "finished_at": finished_at,
        "queue_wait_seconds": _seconds_between(created_at, started_at),
        "run_seconds": _seconds_between(started_at, finished_at),
        "total_seconds": _seconds_between(created_at, finished_at),
        "request": task.get("request"),
        "result": result,
    }


def _sanitize_preset_request(value: dict) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Invalid preset request")

    drop_keys = {
        "image_base64",
        "masked_image_base64",
        "depth_image_base64",
        "controlnet_image_base64",
        "redux_images_base64",
        "image_path",
        "masked_image_path",
        "depth_image_path",
        "controlnet_image_path",
        "redux_image_paths",
    }

    cleaned: dict = {}
    for k, v in value.items():
        if str(k) in drop_keys:
            continue
        cleaned[k] = v
    return cleaned


def _safe_unlink_tmp(storage_dir: Path, rel_path: str) -> None:
    if not rel_path:
        return

    storage_root = storage_dir.resolve()
    tmp_root = (storage_root / "tmp").resolve()
    p = Path(rel_path)
    abs_path = p if p.is_absolute() else (storage_root / p)
    abs_path = abs_path.resolve()

    try:
        abs_path.relative_to(tmp_root)
    except ValueError:
        return

    try:
        if abs_path.exists():
            abs_path.unlink()
    except Exception:
        return


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = load_settings()

    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)

    requeued = mark_running_tasks_as_queued(settings.db_path)

    app.state.settings = settings

    try:
        max_workers = int(os.environ.get("HAPA_MEDIA_NODE_MAX_WORKERS", "1"))
    except Exception:
        max_workers = 1
    max_workers = max(1, max_workers)

    app.state.worker = TaskWorker(settings, max_workers=max_workers)
    app.state.worker.start()

    try:
        settings.token_file.parent.mkdir(parents=True, exist_ok=True)
        settings.token_file.write_text(settings.token + "\n", encoding="utf-8")
        try:
            os.chmod(settings.token_file, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[hapa-media-node] Warning: could not write token file: {settings.token_file} ({e})")
    if requeued:
        print(f"[hapa-media-node] Re-queued running tasks after restart: {requeued}")

    yield

    app.state.worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=_lifespan)

    index_path = Path(__file__).parent / "web" / "index.html"

    @app.get("/")
    def get_index():
        return FileResponse(index_path)

    @app.get("/health")
    def get_health(request: Request):
        settings: Settings = request.app.state.settings
        return {
            "ok": True,
            "service": settings.service_name,
            "api_version": settings.api_version,
            "time": utc_now_iso(),
        }

    @app.get("/v1/system", dependencies=[Depends(_require_auth)])
    def get_system(request: Request):
        settings: Settings = request.app.state.settings
        pid = int(os.getpid())
        stats = _get_process_tree_stats(pid)
        rss_bytes = stats.get("rss_bytes")
        tree_rss_bytes = stats.get("tree_rss_bytes")
        total_bytes = _get_total_memory_bytes()

        worker = getattr(request.app.state, "worker", None)
        worker_stats = worker.get_worker_stats() if worker else None

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
            "workers": worker_stats,
            "processes": stats.get("processes"),
        }

    @app.post("/v1/admin/workers", dependencies=[Depends(_require_admin)])
    def set_workers(body: WorkerScaleRequest, request: Request):
        settings: Settings = request.app.state.settings
        worker = getattr(request.app.state, "worker", None)
        if not worker:
            raise HTTPException(status_code=500, detail="Worker unavailable")

        stats = worker.set_max_workers(body.max_workers)
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "workers": stats,
        }

    @app.get("/capabilities", dependencies=[Depends(_require_auth)])
    def get_capabilities(request: Request):
        settings: Settings = request.app.state.settings
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "service": settings.service_name,
            "modalities": {
                "image": {
                    "engines": ["mflux"],
                    "models": ["schnell", "dev", "krea-dev", "z-image-turbo", "fibo"],
                    "modes": ["txt2img", "img2img", "fill", "depth", "controlnet", "redux", "upscale"],
                    "features": [
                        "txt2img",
                        "img2img",
                        "fill",
                        "depth",
                        "controlnet",
                        "redux",
                        "upscale",
                        "base64_inputs",
                        "negative_prompt",
                        "lora",
                        "quantize",
                        "base_model",
                        "third_party_models",
                        "guidance",
                        "low_ram",
                        "metadata",
                    ],
                    "input_fields_base64": [
                        "image_base64",
                        "masked_image_base64",
                        "depth_image_base64",
                        "controlnet_image_base64",
                        "redux_images_base64",
                    ],
                    "mode_inputs_base64": {
                        "txt2img": {"required": [], "optional": []},
                        "img2img": {"required": ["image_base64"], "optional": []},
                        "fill": {
                            "required": ["image_base64", "masked_image_base64"],
                            "optional": [],
                        },
                        "depth": {"required": ["image_base64"], "optional": ["depth_image_base64"]},
                        "controlnet": {"required": ["controlnet_image_base64"], "optional": []},
                        "redux": {"required": ["redux_images_base64"], "optional": []},
                        "upscale": {"required": ["controlnet_image_base64"], "optional": []},
                    },
                    "lora_fields": ["lora_style", "lora_paths", "lora_scales"],
                    "allowed_quantize": sorted(ALLOWED_QUANTIZE),
                    "default_steps": DEFAULT_STEPS,
                    "third_party_model_support": {
                        "supported": True,
                        "requires_base_model": True,
                    },
                }
            },
        }

    @app.post("/v1/images/generations", dependencies=[Depends(_require_auth)], status_code=202)
    def post_image_generation(body: ImageGenerationRequest, request: Request):
        settings: Settings = request.app.state.settings

        mode = (body.mode or "txt2img").strip()
        if mode not in {"txt2img", "img2img", "fill", "depth", "controlnet", "redux", "upscale"}:
            raise HTTPException(status_code=400, detail=f"Unsupported mode: {mode}")

        prompt = body.prompt.strip()
        negative_prompt = body.negative_prompt.strip() if body.negative_prompt else None
        model = body.model.strip()
        base_model = body.base_model.strip() if body.base_model else None

        lora_style = body.lora_style.strip() if body.lora_style else None
        lora_paths, lora_scales = _normalize_loras(body.lora_paths, body.lora_scales)

        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt is required")
        if not model:
            raise HTTPException(status_code=400, detail="Model is required")

        _validate_model_request(model, base_model)
        _validate_quantize(body.quantize)

        steps = body.steps if body.steps is not None else DEFAULT_STEPS.get(model, 4)

        width = body.width
        height = body.height
        if mode == "txt2img":
            width = 1024 if width is None else width
            height = 1024 if height is None else height

        tmp_paths: list[str] = []

        req = {
            "mode": mode,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "model": model,
            "base_model": base_model,
            "lora_style": lora_style,
            "lora_paths": lora_paths,
            "lora_scales": lora_scales,
            "steps": steps,
            "seed": body.seed,
            "quantize": body.quantize,
            "guidance": body.guidance,
            "low_ram": body.low_ram,
        }

        if width is not None:
            req["width"] = width
        if height is not None:
            req["height"] = height

        try:
            if mode == "img2img":
                if body.image_base64:
                    rel = _write_base64_image_to_tmp(settings.storage_dir, body.image_base64, prefix="image")
                    tmp_paths.append(rel)
                    req["image_path"] = rel
                    body.image_base64 = None
                elif body.image_asset_id:
                    rel = _asset_id_to_stored_path(settings, str(body.image_asset_id))
                    req["image_path"] = rel
                    req["image_asset_id"] = str(body.image_asset_id).strip()
                    body.image_asset_id = None
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="image_base64 or image_asset_id is required for img2img",
                    )
                if body.image_strength is not None:
                    req["image_strength"] = body.image_strength

            if mode == "fill":
                image_rel: Optional[str] = None
                mask_rel: Optional[str] = None

                if body.image_base64:
                    image_rel = _write_base64_image_to_tmp(settings.storage_dir, body.image_base64, prefix="image")
                    tmp_paths.append(image_rel)
                    body.image_base64 = None
                elif body.image_asset_id:
                    image_rel = _asset_id_to_stored_path(settings, str(body.image_asset_id))
                    req["image_asset_id"] = str(body.image_asset_id).strip()
                    body.image_asset_id = None
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="image_base64 or image_asset_id is required for fill",
                    )

                if body.masked_image_base64:
                    mask_rel = _write_base64_image_to_tmp(
                        settings.storage_dir,
                        body.masked_image_base64,
                        prefix="mask",
                    )
                    tmp_paths.append(mask_rel)
                    body.masked_image_base64 = None
                elif body.masked_image_asset_id:
                    mask_rel = _asset_id_to_stored_path(settings, str(body.masked_image_asset_id))
                    req["masked_image_asset_id"] = str(body.masked_image_asset_id).strip()
                    body.masked_image_asset_id = None
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="masked_image_base64 or masked_image_asset_id is required for fill",
                    )

                req["image_path"] = image_rel
                req["masked_image_path"] = mask_rel

            if mode == "depth":
                if body.image_base64:
                    image_rel = _write_base64_image_to_tmp(settings.storage_dir, body.image_base64, prefix="image")
                    tmp_paths.append(image_rel)
                    req["image_path"] = image_rel
                    body.image_base64 = None
                elif body.image_asset_id:
                    image_rel = _asset_id_to_stored_path(settings, str(body.image_asset_id))
                    req["image_path"] = image_rel
                    req["image_asset_id"] = str(body.image_asset_id).strip()
                    body.image_asset_id = None
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="image_base64 or image_asset_id is required for depth",
                    )
                if body.depth_image_base64:
                    depth_rel = _write_base64_image_to_tmp(settings.storage_dir, body.depth_image_base64, prefix="depth")
                    tmp_paths.append(depth_rel)
                    req["depth_image_path"] = depth_rel
                    body.depth_image_base64 = None
                elif body.depth_image_asset_id:
                    depth_rel = _asset_id_to_stored_path(settings, str(body.depth_image_asset_id))
                    req["depth_image_path"] = depth_rel
                    req["depth_image_asset_id"] = str(body.depth_image_asset_id).strip()
                    body.depth_image_asset_id = None

            if mode in {"controlnet", "upscale"}:
                if body.controlnet_image_base64:
                    ctrl_rel = _write_base64_image_to_tmp(
                        settings.storage_dir,
                        body.controlnet_image_base64,
                        prefix="control",
                    )
                    tmp_paths.append(ctrl_rel)
                    req["controlnet_image_path"] = ctrl_rel
                    body.controlnet_image_base64 = None
                elif body.controlnet_image_asset_id:
                    ctrl_rel = _asset_id_to_stored_path(settings, str(body.controlnet_image_asset_id))
                    req["controlnet_image_path"] = ctrl_rel
                    req["controlnet_image_asset_id"] = str(body.controlnet_image_asset_id).strip()
                    body.controlnet_image_asset_id = None
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="controlnet_image_base64 or controlnet_image_asset_id is required",
                    )
                if body.controlnet_strength is not None:
                    req["controlnet_strength"] = body.controlnet_strength

            if mode == "redux":
                images = body.redux_images_base64 or []
                asset_ids = body.redux_image_asset_ids or []
                asset_ids = [str(v).strip() for v in asset_ids if str(v).strip()]

                if not images and not asset_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="redux_images_base64 or redux_image_asset_ids is required",
                    )

                redux_paths: list[str] = []
                if images:
                    for idx, img in enumerate(images):
                        rel = _write_base64_image_to_tmp(settings.storage_dir, img, prefix=f"redux_{idx}")
                        tmp_paths.append(rel)
                        redux_paths.append(rel)
                    body.redux_images_base64 = None
                else:
                    for idx, aid in enumerate(asset_ids):
                        rel = _asset_id_to_stored_path(settings, aid)
                        redux_paths.append(rel)
                    req["redux_image_asset_ids"] = asset_ids
                    body.redux_image_asset_ids = None

                req["redux_image_paths"] = redux_paths
                if body.redux_image_strengths is not None:
                    req["redux_image_strengths"] = [float(s) for s in body.redux_image_strengths]
        except HTTPException:
            for rel in tmp_paths:
                _safe_unlink_tmp(settings.storage_dir, rel)
            raise

        task_id = str(uuid.uuid4())
        try:
            db_create_task(settings.db_path, task_id=task_id, task_type="image.generate", request=req)
        except Exception as exc:
            for rel in tmp_paths:
                _safe_unlink_tmp(settings.storage_dir, rel)
            raise HTTPException(status_code=500, detail="Failed to enqueue task") from exc

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "task_id": task_id,
        }

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(_require_auth)])
    def get_task_status(task_id: str, request: Request):
        settings: Settings = request.app.state.settings
        task = get_task(settings.db_path, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        result = None
        asset_id = task.get("result_asset_id")
        if asset_id:
            result = {
                "asset_id": asset_id,
                "download_url": f"/v1/assets/{asset_id}/download",
            }

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "task_id": task["task_id"],
            "status": task["status"],
            "stage": task["stage"],
            "progress": task["progress"],
            "error": task["error"],
            "result": result,
        }

    @app.get("/v1/tasks", dependencies=[Depends(_require_auth)])
    def list_tasks(
        request: Request,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "ASC",
    ):
        settings: Settings = request.app.state.settings

        statuses = None
        if status is not None:
            raw = str(status)
            parts = [s.strip() for s in raw.split(",") if s.strip()]
            statuses = parts or None

        try:
            tasks = db_list_tasks(
                settings.db_path,
                statuses=statuses,
                limit=limit,
                offset=offset,
                order_by=order_by,
                order_dir=order_dir,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "limit": min(int(limit), 200),
            "offset": max(0, int(offset)),
            "tasks": [_task_to_public(t, settings=settings) for t in tasks],
        }

    @app.get("/v1/queue", dependencies=[Depends(_require_auth)])
    def get_queue(
        request: Request,
        queued_limit: int = 50,
        running_limit: int = 10,
        recent_limit: int = 20,
    ):
        settings: Settings = request.app.state.settings

        try:
            running = db_list_tasks(
                settings.db_path,
                statuses=["running"],
                limit=running_limit,
                offset=0,
                order_by="started_at",
                order_dir="ASC",
            )
            queued = db_list_tasks(
                settings.db_path,
                statuses=["queued"],
                limit=queued_limit,
                offset=0,
                order_by="created_at",
                order_dir="ASC",
            )
            recent = db_list_tasks(
                settings.db_path,
                statuses=["succeeded", "failed"],
                limit=recent_limit,
                offset=0,
                order_by="finished_at",
                order_dir="DESC",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        counts = {
            "queued": db_count_tasks_by_status(settings.db_path, ["queued"]),
            "running": db_count_tasks_by_status(settings.db_path, ["running"]),
            "succeeded": db_count_tasks_by_status(settings.db_path, ["succeeded"]),
            "failed": db_count_tasks_by_status(settings.db_path, ["failed"]),
        }

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "counts": counts,
            "running": [_task_to_public(t, settings=settings) for t in running],
            "queued": [_task_to_public(t, settings=settings) for t in queued],
            "recent": [_task_to_public(t, settings=settings) for t in recent],
        }

    @app.post("/v1/presets", dependencies=[Depends(_require_auth)], status_code=201)
    def create_preset(body: PresetCreateRequest, request: Request):
        settings: Settings = request.app.state.settings

        name = str(body.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Preset name is required")

        preset_request = _sanitize_preset_request(body.request or {})

        thumb = (str(body.thumbnail_asset_id).strip() if body.thumbnail_asset_id else None) or None
        if thumb:
            _asset_id_to_stored_path(settings, thumb)

        preset_id = str(uuid.uuid4())
        try:
            db_create_preset(
                settings.db_path,
                preset_id=preset_id,
                name=name,
                request=preset_request,
                thumbnail_asset_id=thumb,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to create preset") from exc

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "preset_id": preset_id,
        }

    @app.get("/v1/presets", dependencies=[Depends(_require_auth)])
    def list_presets(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "DESC",
    ):
        settings: Settings = request.app.state.settings

        try:
            presets = db_list_presets(
                settings.db_path,
                limit=limit,
                offset=offset,
                order_by=order_by,
                order_dir=order_dir,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        out = []
        for p in presets:
            thumb = p.get("thumbnail_asset_id")
            out.append(
                {
                    "preset_id": p.get("preset_id"),
                    "name": p.get("name"),
                    "thumbnail_asset_id": thumb,
                    "thumbnail_download_url": (f"/v1/assets/{thumb}/download" if thumb else None),
                    "created_at": p.get("created_at"),
                    "updated_at": p.get("updated_at"),
                }
            )

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "limit": min(int(limit), 200),
            "offset": max(0, int(offset)),
            "presets": out,
        }

    @app.get("/v1/presets/{preset_id}", dependencies=[Depends(_require_auth)])
    def get_preset(preset_id: str, request: Request):
        settings: Settings = request.app.state.settings
        preset = db_get_preset(settings.db_path, preset_id)
        if not preset:
            raise HTTPException(status_code=404, detail="Preset not found")

        thumb = preset.get("thumbnail_asset_id")
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "preset_id": preset.get("preset_id"),
            "name": preset.get("name"),
            "thumbnail_asset_id": thumb,
            "thumbnail_download_url": (f"/v1/assets/{thumb}/download" if thumb else None),
            "created_at": preset.get("created_at"),
            "updated_at": preset.get("updated_at"),
            "request": preset.get("request"),
        }

    @app.delete("/v1/presets/{preset_id}", dependencies=[Depends(_require_auth)])
    def delete_preset(preset_id: str, request: Request):
        settings: Settings = request.app.state.settings
        ok = db_delete_preset(settings.db_path, preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Preset not found")
        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "deleted": True,
        }

    @app.get("/v1/assets/{asset_id}", dependencies=[Depends(_require_auth)])
    def get_asset_metadata(asset_id: str, request: Request):
        settings: Settings = request.app.state.settings
        asset = get_asset(settings.db_path, asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")

        return {
            "api_version": settings.api_version,
            "time": utc_now_iso(),
            "asset_id": asset["asset_id"],
            "type": asset["type"],
            "path": asset["path"],
            "created_at": asset["created_at"],
            "metadata": asset["metadata"],
        }

    @app.get("/v1/assets/{asset_id}/download", dependencies=[Depends(_require_auth)])
    def download_asset(asset_id: str, request: Request):
        settings: Settings = request.app.state.settings
        asset = get_asset(settings.db_path, asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")

        stored = Path(asset["path"])
        abs_path = stored if stored.is_absolute() else (settings.storage_dir / stored)
        abs_path = abs_path.resolve()

        storage_root = settings.storage_dir.resolve()
        try:
            abs_path.relative_to(storage_root)
        except ValueError:
            raise HTTPException(status_code=500, detail="Invalid asset path")

        if not abs_path.exists():
            raise HTTPException(status_code=404, detail="Asset file missing")

        return FileResponse(path=abs_path, media_type=asset["type"], filename=abs_path.name)

    return app
