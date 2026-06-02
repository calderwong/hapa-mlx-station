from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .db import (
    claim_task_for_run,
    create_asset,
    get_task,
    list_task_ids_by_status,
    set_task_fields,
    utc_now_iso,
)
from .mflux_engine import MfluxError, build_mflux_command, run_mflux_generate


DEFAULT_STEPS: dict[str, int] = {
    "schnell": 2,
    "dev": 25,
    "krea-dev": 25,
    "z-image-turbo": 9,
    "fibo": 20,
}

ALLOWED_QUANTIZE: set[int] = {3, 4, 5, 6, 8}

DUMMY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMBA6m5nXQAAAAASUVORK5CYII="


def _resolve_path_under_root(root: Path, stored: str) -> Path:
    p = Path(stored)
    abs_path = p if p.is_absolute() else (root / p)
    abs_path = abs_path.resolve()
    root_abs = root.resolve()

    try:
        abs_path.relative_to(root_abs)
    except ValueError:
        raise ValueError("Invalid path outside storage root")

    return abs_path


def _safe_unlink_tmp(root: Path, stored: str) -> None:
    if not stored:
        return

    root_abs = root.resolve()
    tmp_root = (root_abs / "tmp").resolve()
    try:
        abs_path = _resolve_path_under_root(root_abs, stored)
    except Exception:
        return

    try:
        abs_path.relative_to(tmp_root)
    except ValueError:
        return

    try:
        if abs_path.exists():
            abs_path.unlink()
    except Exception:
        return


def _tail(text: Optional[str], max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[-max_len:]


class TaskWorker:
    def __init__(self, settings: Settings, *, max_workers: int = 1):
        self._settings = settings
        self._stop_event = threading.Event()
        try:
            max_workers = int(max_workers)
        except Exception:
            max_workers = 1
        self._max_workers = max(1, max_workers)

        self._pick_lock = threading.Lock()
        self._scale_lock = threading.Lock()
        self._started = False
        self._threads: list[tuple[threading.Thread, threading.Event]] = []
        for i in range(self._max_workers):
            stop_event = threading.Event()
            t = threading.Thread(
                target=self._run_loop,
                args=(stop_event,),
                name=f"hapa-media-node-worker-{i+1}",
                daemon=True,
            )
            self._threads.append((t, stop_event))

    def start(self) -> None:
        with self._scale_lock:
            if self._started:
                return
            self._started = True
            threads = list(self._threads)

        for t, _ in threads:
            t.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        with self._scale_lock:
            for _, stop_event in self._threads:
                stop_event.set()
            threads = list(self._threads)

        for t, _ in threads:
            if t.ident is None:
                continue
            t.join(timeout=timeout_seconds)

    @property
    def max_workers(self) -> int:
        with self._scale_lock:
            return int(self._max_workers)

    def get_worker_stats(self) -> dict[str, Any]:
        with self._scale_lock:
            active = 0
            retiring = 0
            alive_total = 0
            for t, stop_event in self._threads:
                if t.ident is not None and not t.is_alive():
                    continue
                if t.ident is None:
                    active += 1
                    alive_total += 1
                    continue
                alive_total += 1
                if stop_event.is_set():
                    retiring += 1
                else:
                    active += 1

            return {
                "max_workers": int(self._max_workers),
                "active_workers": int(active),
                "retiring_workers": int(retiring),
                "alive_workers": int(alive_total),
            }

    def set_max_workers(self, max_workers: int) -> dict[str, Any]:
        try:
            max_workers = int(max_workers)
        except Exception:
            max_workers = 1
        max_workers = max(1, max_workers)

        with self._scale_lock:
            self._max_workers = max_workers

            next_index = 1
            for t, _ in self._threads:
                name = str(getattr(t, "name", ""))
                if name.startswith("hapa-media-node-worker-"):
                    try:
                        idx = int(name.rsplit("-", 1)[-1])
                    except Exception:
                        idx = 0
                    next_index = max(next_index, idx + 1)
            active_threads = [
                (t, e) for (t, e) in self._threads if (not e.is_set()) and (t.ident is None or t.is_alive())
            ]
            active = len(active_threads)

            if max_workers > active:
                to_add = max_workers - active
                for _ in range(to_add):
                    stop_event = threading.Event()
                    t = threading.Thread(
                        target=self._run_loop,
                        args=(stop_event,),
                        name=f"hapa-media-node-worker-{next_index}",
                        daemon=True,
                    )
                    next_index += 1
                    self._threads.append((t, stop_event))
                    if self._started:
                        t.start()
            elif max_workers < active:
                to_retire = active - max_workers
                for t, stop_event in reversed(self._threads):
                    if to_retire <= 0:
                        break
                    if stop_event.is_set():
                        continue
                    stop_event.set()
                    to_retire -= 1

        return self.get_worker_stats()

    def _run_loop(self, thread_stop_event: threading.Event) -> None:
        while not self._stop_event.is_set() and not thread_stop_event.is_set():
            task_id = self._pick_next_task_id()
            if not task_id:
                self._stop_event.wait(0.5)
                continue

            try:
                self._process_task(task_id)
            except Exception as exc:
                set_task_fields(
                    self._settings.db_path,
                    task_id,
                    {
                        "status": "failed",
                        "stage": "worker_exception",
                        "error": str(exc),
                        "progress": 1.0,
                        "finished_at": utc_now_iso(),
                    },
                )

    def _pick_next_task_id(self) -> Optional[str]:
        with self._pick_lock:
            task_ids = list_task_ids_by_status(self._settings.db_path, ["queued"])
            for task_id in task_ids:
                if claim_task_for_run(self._settings.db_path, task_id):
                    return task_id
            return None

    def _process_task(self, task_id: str) -> None:
        task = get_task(self._settings.db_path, task_id)
        if not task:
            return

        req = dict(task["request"])

        mode = str(req.get("mode") or "txt2img").strip()
        if mode not in {"txt2img", "img2img", "fill", "depth", "controlnet", "redux", "upscale"}:
            raise ValueError(f"Unsupported mode: {mode}")

        prompt = str(req.get("prompt") or "").strip()
        negative_prompt = (str(req.get("negative_prompt")).strip() if req.get("negative_prompt") else None) or None
        model = str(req.get("model") or "").strip()
        base_model = (str(req.get("base_model")).strip() if req.get("base_model") else None) or None

        lora_style = (str(req.get("lora_style")).strip() if req.get("lora_style") else None) or None
        lora_paths = req.get("lora_paths")
        if lora_paths is not None:
            lora_paths = [str(p).strip() for p in list(lora_paths) if str(p).strip()]
            if not lora_paths:
                lora_paths = None

        lora_scales = req.get("lora_scales")
        if lora_scales is not None:
            lora_scales = [float(s) for s in list(lora_scales)]
            if not lora_scales:
                lora_scales = None

        if not prompt:
            raise ValueError("Missing prompt")
        if not model:
            raise ValueError("Missing model")

        steps = int(req["steps"]) if req.get("steps") is not None else DEFAULT_STEPS.get(model, 4)
        seed = int(req["seed"]) if req.get("seed") is not None else None
        width = int(req["width"]) if req.get("width") is not None else None
        height = int(req["height"]) if req.get("height") is not None else None

        quantize = req.get("quantize")
        if quantize is not None:
            quantize = int(quantize)
            if quantize not in ALLOWED_QUANTIZE:
                raise ValueError(f"Invalid quantize value: {quantize}")

        guidance = req.get("guidance")
        if guidance is not None:
            guidance = float(guidance)

        low_ram = bool(req.get("low_ram") or False)

        image_path_rel = (str(req.get("image_path")).strip() if req.get("image_path") else None) or None
        masked_image_path_rel = (str(req.get("masked_image_path")).strip() if req.get("masked_image_path") else None) or None
        depth_image_path_rel = (str(req.get("depth_image_path")).strip() if req.get("depth_image_path") else None) or None
        controlnet_image_path_rel = (str(req.get("controlnet_image_path")).strip() if req.get("controlnet_image_path") else None) or None

        redux_image_paths_rel = req.get("redux_image_paths")
        if redux_image_paths_rel is not None:
            redux_image_paths_rel = [str(p).strip() for p in list(redux_image_paths_rel) if str(p).strip()]
            if not redux_image_paths_rel:
                redux_image_paths_rel = None

        image_strength = req.get("image_strength")
        if image_strength is not None:
            image_strength = float(image_strength)

        controlnet_strength = req.get("controlnet_strength")
        if controlnet_strength is not None:
            controlnet_strength = float(controlnet_strength)

        redux_image_strengths = req.get("redux_image_strengths")
        if redux_image_strengths is not None:
            redux_image_strengths = [float(s) for s in list(redux_image_strengths)]
            if not redux_image_strengths:
                redux_image_strengths = None

        input_tmp_paths: list[str] = []
        if image_path_rel:
            input_tmp_paths.append(image_path_rel)
        if masked_image_path_rel:
            input_tmp_paths.append(masked_image_path_rel)
        if depth_image_path_rel:
            input_tmp_paths.append(depth_image_path_rel)
        if controlnet_image_path_rel:
            input_tmp_paths.append(controlnet_image_path_rel)
        if redux_image_paths_rel:
            input_tmp_paths.extend(redux_image_paths_rel)

        asset_id = str(uuid.uuid4())
        output_abs = (self._settings.artifacts_dir / f"{asset_id}.png").resolve()
        output_abs.parent.mkdir(parents=True, exist_ok=True)

        dummy_generation = (
            str(os.environ.get("HAPA_MEDIA_NODE_DUMMY_GENERATION") or "").strip().lower() in {"1", "true", "yes", "on"}
        )
        engine_name = "dummy" if dummy_generation else "mflux"

        set_task_fields(
            self._settings.db_path,
            task_id,
            {
                "stage": "generating",
                "progress": 0.6,
            },
        )

        try:
            if dummy_generation:
                output_abs.write_bytes(base64.b64decode(DUMMY_PNG_BASE64))
                proc_result = {"stdout": "", "stderr": ""}
            else:
                image_abs = _resolve_path_under_root(self._settings.storage_dir, image_path_rel) if image_path_rel else None
                masked_abs = (
                    _resolve_path_under_root(self._settings.storage_dir, masked_image_path_rel)
                    if masked_image_path_rel
                    else None
                )
                depth_abs = (
                    _resolve_path_under_root(self._settings.storage_dir, depth_image_path_rel)
                    if depth_image_path_rel
                    else None
                )
                control_abs = (
                    _resolve_path_under_root(self._settings.storage_dir, controlnet_image_path_rel)
                    if controlnet_image_path_rel
                    else None
                )
                redux_abs = (
                    [_resolve_path_under_root(self._settings.storage_dir, p) for p in redux_image_paths_rel]
                    if redux_image_paths_rel
                    else None
                )

                cmd = build_mflux_command(
                    self._settings,
                    mode=mode,
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    model=model,
                    base_model=base_model,
                    lora_style=lora_style,
                    lora_paths=lora_paths,
                    lora_scales=lora_scales,
                    steps=steps,
                    seed=seed,
                    width=width,
                    height=height,
                    quantize=quantize,
                    guidance=guidance,
                    low_ram=low_ram,
                    output_path=output_abs,
                    image_path=image_abs,
                    masked_image_path=masked_abs,
                    depth_image_path=depth_abs,
                    controlnet_image_path=control_abs,
                    redux_image_paths=redux_abs,
                    image_strength=image_strength,
                    controlnet_strength=controlnet_strength,
                    redux_image_strengths=redux_image_strengths,
                )

                proc_result = run_mflux_generate(cmd)
        except MfluxError as exc:
            set_task_fields(
                self._settings.db_path,
                task_id,
                {
                    "status": "failed",
                    "stage": "mflux_failed",
                    "error": str(exc),
                    "progress": 1.0,
                    "finished_at": utc_now_iso(),
                },
            )
            return
        finally:
            for rel in input_tmp_paths:
                _safe_unlink_tmp(self._settings.storage_dir, rel)

        if not output_abs.exists():
            set_task_fields(
                self._settings.db_path,
                task_id,
                {
                    "status": "failed",
                    "stage": "missing_output",
                    "error": f"Expected output file not found: {output_abs}",
                    "progress": 1.0,
                    "finished_at": utc_now_iso(),
                },
            )
            return

        set_task_fields(
            self._settings.db_path,
            task_id,
            {
                "stage": "saving",
                "progress": 0.9,
            },
        )

        mflux_metadata = None
        for candidate in [Path(str(output_abs) + ".json"), output_abs.with_suffix(".json")]:
            if not candidate.exists():
                continue
            try:
                mflux_metadata = json.loads(candidate.read_text())
                break
            except Exception:
                mflux_metadata = None

        output_rel = str(output_abs.relative_to(self._settings.storage_dir))

        asset_metadata: dict[str, Any] = {
            "engine": engine_name,
            "mode": mode,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "model": model,
            "base_model": base_model,
            "lora_style": lora_style,
            "lora_paths": lora_paths,
            "lora_scales": lora_scales,
            "image_strength": image_strength,
            "controlnet_strength": controlnet_strength,
            "redux_image_strengths": redux_image_strengths,
            "steps": steps,
            "seed": seed,
            "width": width,
            "height": height,
            "quantize": quantize,
            "guidance": guidance,
            "low_ram": low_ram,
            "mflux_stdout_tail": _tail(proc_result.get("stdout"), 2000),
            "mflux_stderr_tail": _tail(proc_result.get("stderr"), 2000),
        }

        if mflux_metadata is not None:
            asset_metadata["mflux_metadata"] = mflux_metadata

        create_asset(
            self._settings.db_path,
            asset_id=asset_id,
            asset_type="image/png",
            path=output_rel,
            metadata=asset_metadata,
        )

        set_task_fields(
            self._settings.db_path,
            task_id,
            {
                "status": "succeeded",
                "stage": "complete",
                "progress": 1.0,
                "result_asset_id": asset_id,
                "finished_at": utc_now_iso(),
            },
        )
