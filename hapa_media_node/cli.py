from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .config import load_settings
from .hub_config import load_hub_settings
from . import port_manager
from .self_test import run_self_test


def _default_repo_token_file() -> Path:
    return (Path(__file__).resolve().parent.parent / ".node_token").resolve()


def _read_text_file(path: Path) -> Optional[str]:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def _resolve_token(token_arg: Optional[str]) -> Optional[str]:
    token = str(token_arg or "").strip() or None
    if token:
        return token

    token = str(os.environ.get("HAPA_MEDIA_HUB_TOKEN") or "").strip() or None
    if token:
        return token
    token = str(os.environ.get("HAPA_MEDIA_NODE_TOKEN") or "").strip() or None
    if token:
        return token

    candidates: list[Path] = []
    hub_tf = os.environ.get("HAPA_MEDIA_HUB_TOKEN_FILE")
    if hub_tf:
        candidates.append(Path(hub_tf).expanduser())
    node_tf = os.environ.get("HAPA_MEDIA_NODE_TOKEN_FILE")
    if node_tf:
        candidates.append(Path(node_tf).expanduser())
    candidates.append(_default_repo_token_file())

    for p in candidates:
        try:
            path = p
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            else:
                path = path.resolve()
        except Exception:
            continue
        file_tok = _read_text_file(path)
        if file_tok:
            return file_tok

    return None


def _write_private_text_file(path: Path, text: str) -> bool:
    try:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (str(text).strip() + "\n").encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        finally:
            try:
                os.chmod(str(path), 0o600)
            except Exception:
                pass
        return True
    except Exception:
        return False


def _write_private_json(path: Path, value: object) -> bool:
    try:
        data = (json.dumps(value) + "\n").encode("utf-8")
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        finally:
            try:
                os.chmod(str(path), 0o600)
            except Exception:
                pass
        return True
    except Exception:
        return False


def _http_json(
    method: str,
    url: str,
    *,
    token: Optional[str],
    payload: Optional[dict],
    timeout_seconds: Optional[float] = None,
) -> dict:
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


def _file_to_base64(path: str) -> str:
    data = Path(path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    del data
    return encoded


def _http_bytes(method: str, url: str, *, token: Optional[str]) -> bytes:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url=url, method=method, headers=headers)

    try:
        with urllib.request.urlopen(req) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}")


def _split_composite_id(value: str) -> tuple[str, str]:
    value = str(value or "").strip()
    if not value or ":" not in value:
        raise RuntimeError("Invalid id")
    node_id, inner = value.split(":", 1)
    node_id = node_id.strip()
    inner = inner.strip()
    if not node_id or not inner:
        raise RuntimeError("Invalid id")
    return node_id, inner


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _wait_for_health(base_url: str, *, timeout_seconds: float = 30.0) -> None:
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("Missing base_url")

    deadline = time.time() + float(timeout_seconds)
    last_err: Optional[str] = None
    while time.time() < deadline:
        try:
            data = _http_json(
                "GET",
                base_url + "/health",
                token=None,
                payload=None,
                timeout_seconds=2.0,
            )
            if isinstance(data, dict) and data.get("ok") is True:
                return
            last_err = f"Unexpected response: {data}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(0.25)

    raise RuntimeError(f"Timed out waiting for /health at {base_url}: {last_err}")


def _terminate_children(children: list[tuple[str, subprocess.Popen]]) -> None:
    for _, p in reversed(children):
        if p.poll() is None:
            try:
                os.killpg(int(p.pid), signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if all(p.poll() is not None for _, p in children):
            break
        time.sleep(0.1)

    for _, p in reversed(children):
        if p.poll() is None:
            try:
                os.killpg(int(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    for _, p in children:
        try:
            p.wait(timeout=1)
        except Exception:
            pass


def _redact_payload_for_log(payload: dict) -> dict:
    cleaned: dict = {}
    for k, v in payload.items():
        key = str(k)
        if key.endswith("_base64") or key == "redux_images_base64":
            continue
        cleaned[key] = v
    return cleaned


def _build_generation_payload_from_args(args: argparse.Namespace) -> dict:
    if args.request_file:
        raw = Path(str(args.request_file)).read_text(encoding="utf-8")
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise RuntimeError("request_file must contain a JSON object")
        return data

    if not args.prompt:
        raise RuntimeError("--prompt is required when --request-file is not provided")

    payload: dict = {
        "mode": args.mode,
        "prompt": args.prompt,
        "negative_prompt": args.negative_prompt,
        "model": args.model,
        "base_model": args.base_model,
        "lora_style": args.lora_style,
        "lora_paths": args.lora_paths,
        "lora_scales": args.lora_scales,
        "image_strength": args.image_strength,
        "controlnet_strength": args.controlnet_strength,
        "redux_image_strengths": args.redux_image_strengths,
        "steps": args.steps,
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "quantize": args.quantize,
        "guidance": args.guidance,
        "low_ram": bool(args.low_ram),
    }

    if args.image_file:
        payload["image_base64"] = _file_to_base64(args.image_file)
    if args.masked_image_file:
        payload["masked_image_base64"] = _file_to_base64(args.masked_image_file)
    if args.depth_image_file:
        payload["depth_image_base64"] = _file_to_base64(args.depth_image_file)
    if args.controlnet_image_file:
        payload["controlnet_image_base64"] = _file_to_base64(args.controlnet_image_file)
    if args.redux_image_files:
        payload["redux_images_base64"] = [_file_to_base64(p) for p in args.redux_image_files]

    return {k: v for (k, v) in payload.items() if v is not None}


def _submit_task(base_url: str, *, token: str, payload: dict) -> str:
    submit = _http_json(
        "POST",
        str(base_url).rstrip("/") + "/v1/images/generations",
        token=token,
        payload=payload,
        timeout_seconds=30.0,
    )
    task_id = submit.get("task_id") if isinstance(submit, dict) else None
    if not task_id:
        raise RuntimeError("Missing task_id in response")
    return str(task_id)


def _poll_tasks_done(
    base_url: str,
    *,
    token: str,
    task_ids: list[str],
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, dict]:
    deadline = time.time() + float(timeout_seconds)
    remaining = set(str(t) for t in task_ids)
    done: dict[str, dict] = {}

    while remaining:
        if time.time() >= deadline:
            raise RuntimeError(f"Timed out waiting for tasks ({len(remaining)} remaining)")

        for task_id in list(remaining):
            try:
                task = _http_json(
                    "GET",
                    str(base_url).rstrip("/") + f"/v1/tasks/{task_id}",
                    token=token,
                    payload=None,
                    timeout_seconds=15.0,
                )
            except Exception:
                continue

            status = task.get("status") if isinstance(task, dict) else None
            if status in {"succeeded", "failed"}:
                done[task_id] = task
                remaining.discard(task_id)

        if remaining:
            time.sleep(max(0.05, float(poll_interval_seconds)))

    return done


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / float(len(values)))


def _pct(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    p = max(0.0, min(100.0, float(pct))) / 100.0
    idx = int(round(p * (len(s) - 1)))
    idx = max(0, min(len(s) - 1, idx))
    return float(s[idx])


def cmd_serve(args: argparse.Namespace) -> int:
    if args.host:
        os.environ["HAPA_MEDIA_NODE_HOST"] = args.host
    if args.port:
        os.environ["HAPA_MEDIA_NODE_PORT"] = str(args.port)
    if args.token:
        os.environ["HAPA_MEDIA_NODE_TOKEN"] = args.token

    settings = load_settings()

    base_url = f"http://{settings.host}:{settings.port}"
    print(f"[hapa-media-node] baseUrl={base_url}")
    print(f"[hapa-media-node] tokenFile={settings.token_file}")

    import uvicorn

    uvicorn.run(
        "hapa_media_node.app:create_app",
        host=settings.host,
        port=settings.port,
        factory=True,
        reload=bool(args.reload),
    )
    return 0


def cmd_hub(args: argparse.Namespace) -> int:
    if args.host:
        os.environ["HAPA_MEDIA_HUB_HOST"] = args.host
    if args.port:
        os.environ["HAPA_MEDIA_HUB_PORT"] = str(args.port)
    if args.token:
        os.environ["HAPA_MEDIA_HUB_TOKEN"] = args.token
    if args.nodes:
        os.environ["HAPA_MEDIA_HUB_NODES"] = args.nodes
    if args.nodes_file:
        os.environ["HAPA_MEDIA_HUB_NODES_FILE"] = args.nodes_file

    settings = load_hub_settings()

    base_url = f"http://{settings.host}:{settings.port}"
    print(f"[hapa-media-hub] baseUrl={base_url}")
    print(f"[hapa-media-hub] tokenFile={settings.token_file}")

    import uvicorn

    uvicorn.run(
        "hapa_media_node.hub_app:create_app",
        host=settings.host,
        port=settings.port,
        factory=True,
        reload=bool(args.reload),
    )
    return 0


def cmd_stack(args: argparse.Namespace) -> int:
    host = str(args.host or "127.0.0.1")
    hub_port = int(args.hub_port or 8723)
    node_base_port = int(args.node_base_port or 8724)
    num_nodes = int(args.num_nodes or 1)
    reload_flag = bool(args.reload)

    token = str(args.token or "").strip()
    if not token:
        token = secrets.token_hex(16)

    node_max_workers: Optional[int] = None
    if args.node_max_workers is not None:
        node_max_workers = int(args.node_max_workers)
        if node_max_workers < 1:
            raise RuntimeError("node_max_workers must be >= 1")

    storage_root: Optional[Path] = None
    if args.storage_root:
        storage_root = Path(args.storage_root).expanduser().resolve()

    if num_nodes < 1:
        raise RuntimeError("num_nodes must be >= 1")

    children: list[tuple[str, subprocess.Popen]] = []
    nodes: list[dict] = []
    leases: list[port_manager.PortLease] = []

    def _node_storage_dir(node_index: int, node_id: str) -> Path:
        if storage_root is not None:
            return (storage_root / node_id).resolve()
        if node_index == 0:
            return (Path.cwd() / "data").resolve()
        return (Path.cwd() / f"data_{node_id}").resolve()

    def _terminate_children() -> None:
        for name, p in reversed(children):
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if all(p.poll() is not None for _, p in children):
                break
            time.sleep(0.1)

        for name, p in reversed(children):
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass

        for _, p in children:
            try:
                p.wait(timeout=1)
            except Exception:
                pass

    try:
        for i in range(num_nodes):
            node_id = f"node{i + 1}"
            lease = port_manager.acquire_port_lease(
                service="hapa-media-gen-node",
                host=host,
                base_port=int(node_base_port) + i,
                preferred_port=int(node_base_port) + i,
                pid=0,
            )
            leases.append(lease)
            node_port = int(lease.port)
            node_base_url = f"http://{host}:{node_port}"

            storage_dir = _node_storage_dir(i, node_id)
            storage_dir.mkdir(parents=True, exist_ok=True)
            token_file = (storage_dir / ".node_token").resolve()

            node_env = os.environ.copy()
            node_env["HAPA_MEDIA_NODE_STORAGE_DIR"] = str(storage_dir)
            node_env["HAPA_MEDIA_NODE_TOKEN_FILE"] = str(token_file)
            if node_max_workers is not None:
                node_env["HAPA_MEDIA_NODE_MAX_WORKERS"] = str(node_max_workers)

            if not _write_private_text_file(token_file, token):
                node_env["HAPA_MEDIA_NODE_TOKEN"] = token

            cmd = [
                sys.executable,
                "-m",
                "hapa_media_node",
                "serve",
                "--host",
                host,
                "--port",
                str(node_port),
            ]
            if reload_flag:
                cmd.append("--reload")

            p = subprocess.Popen(cmd, env=node_env)
            children.append((f"node:{node_id}", p))
            try:
                lease.set_pid(int(p.pid))
                lease.write_runtime(
                    base_url=node_base_url,
                    token_path=token_file,
                    storage_dir=storage_dir,
                    extra={"node_id": node_id},
                )
            except Exception:
                pass

            nodes.append({"node_id": node_id, "base_url": node_base_url, "token": token})

        time.sleep(0.5)

        hub_lease = port_manager.acquire_port_lease(
            service="hapa-media-hub",
            host=host,
            base_port=int(hub_port),
            preferred_port=int(hub_port),
            pid=0,
        )
        leases.append(hub_lease)
        hub_port_final = int(hub_lease.port)
        hub_url = f"http://{host}:{hub_port_final}"

        if storage_root is not None:
            hub_storage_dir = (storage_root / "hub").resolve()
        else:
            hub_storage_dir = (Path.cwd() / "data_hub").resolve()
        hub_storage_dir.mkdir(parents=True, exist_ok=True)
        hub_token_file = (hub_storage_dir / ".node_token").resolve()
        hub_nodes_file = (hub_storage_dir / "hub_nodes.json").resolve()

        hub_env = os.environ.copy()
        hub_env["HAPA_MEDIA_HUB_TOKEN_FILE"] = str(hub_token_file)
        if not _write_private_text_file(hub_token_file, token):
            hub_env["HAPA_MEDIA_HUB_TOKEN"] = token

        wrote_nodes_file = _write_private_json(hub_nodes_file, nodes)
        if not wrote_nodes_file:
            hub_env["HAPA_MEDIA_HUB_NODES"] = json.dumps(nodes)

        hub_cmd = [
            sys.executable,
            "-m",
            "hapa_media_node",
            "hub",
            "--host",
            host,
            "--port",
            str(hub_port_final),
        ]
        if wrote_nodes_file:
            hub_cmd.extend(["--nodes-file", str(hub_nodes_file)])
        if reload_flag:
            hub_cmd.append("--reload")

        hub_proc = subprocess.Popen(hub_cmd, env=hub_env)
        children.append(("hub", hub_proc))

        try:
            hub_lease.set_pid(int(hub_proc.pid))
            hub_lease.write_runtime(
                base_url=hub_url,
                token_path=hub_token_file,
                storage_dir=hub_storage_dir,
                extra={"role": "hub"},
            )
        except Exception:
            pass

        print(f"[hapa-media-stack] hubUrl={hub_url}")
        print(f"[hapa-media-stack] hubTokenFile={hub_token_file}")
        if wrote_nodes_file:
            print(f"[hapa-media-stack] hubNodesFile={hub_nodes_file}")

        while True:
            for name, p in children:
                rc = p.poll()
                if rc is not None:
                    raise RuntimeError(f"{name} exited with code {rc}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        _terminate_children()
        for lease in leases:
            try:
                lease.release(remove_runtime=True)
            except Exception:
                pass


def cmd_capabilities(args: argparse.Namespace) -> int:
    token = _resolve_token(args.token)
    data = _http_json("GET", args.base_url + "/capabilities", token=token, payload=None)
    print(json.dumps(data, indent=2))
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    token = _resolve_token(args.token)
    if not token:
        raise RuntimeError("Token required")

    payload: dict = {
        "mode": args.mode,
        "prompt": args.prompt,
        "negative_prompt": args.negative_prompt,
        "model": args.model,
        "base_model": args.base_model,
        "lora_style": args.lora_style,
        "lora_paths": args.lora_paths,
        "lora_scales": args.lora_scales,
        "image_strength": args.image_strength,
        "controlnet_strength": args.controlnet_strength,
        "redux_image_strengths": args.redux_image_strengths,
        "steps": args.steps,
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "quantize": args.quantize,
        "guidance": args.guidance,
        "low_ram": bool(args.low_ram),
    }

    if args.image_file:
        payload["image_base64"] = _file_to_base64(args.image_file)
    if args.masked_image_file:
        payload["masked_image_base64"] = _file_to_base64(args.masked_image_file)
    if args.depth_image_file:
        payload["depth_image_base64"] = _file_to_base64(args.depth_image_file)
    if args.controlnet_image_file:
        payload["controlnet_image_base64"] = _file_to_base64(args.controlnet_image_file)
    if args.redux_image_files:
        payload["redux_images_base64"] = [_file_to_base64(p) for p in args.redux_image_files]

    payload = {k: v for (k, v) in payload.items() if v is not None}

    submit = _http_json(
        "POST",
        args.base_url + "/v1/images/generations",
        token=token,
        payload=payload,
    )

    payload.clear()

    task_id = submit.get("task_id")
    if not task_id:
        raise RuntimeError("Missing task_id in response")

    deadline = None
    if args.timeout_seconds:
        deadline = time.time() + float(args.timeout_seconds)

    while True:
        if deadline is not None and time.time() >= deadline:
            raise RuntimeError("Timed out waiting for task")

        task = _http_json(
            "GET",
            args.base_url + f"/v1/tasks/{task_id}",
            token=token,
            payload=None,
        )

        status = task.get("status")
        stage = task.get("stage")
        progress = task.get("progress")
        pct = int((progress or 0) * 100)
        print(f"[{task_id}] {status} | {stage} | {pct}%")

        if status == "succeeded":
            result = task.get("result") or {}
            asset_id = result.get("asset_id")
            if not asset_id:
                raise RuntimeError("Missing asset_id in succeeded task")

            out_path = args.output or f"{asset_id}.png"
            data = _http_bytes(
                "GET",
                args.base_url + f"/v1/assets/{asset_id}/download",
                token=token,
            )

            with open(out_path, "wb") as f:
                f.write(data)

            print(f"Saved: {out_path}")
            return 0

        if status == "failed":
            raise RuntimeError(task.get("error") or "Task failed")

        time.sleep(1)


def cmd_self_test(args: argparse.Namespace) -> int:
    base_url = str(args.base_url or "").strip().rstrip("/")
    token = str(_resolve_token(args.token) or "").strip()
    if not token:
        raise RuntimeError("Token required")

    report = run_self_test(
        base_url,
        token,
        mode=str(args.mode or "txt2img"),
        model=str(args.model or "schnell"),
        steps=int(args.steps or 2),
        copies=int(args.copies or 2),
        spawn_nodes=int(args.spawn_nodes or 0),
        spawn_max_workers=(int(args.spawn_max_workers) if args.spawn_max_workers is not None else 1),
        cleanup=not bool(args.no_cleanup),
        startup_timeout_seconds=float(args.startup_timeout_seconds or 30.0),
        timeout_seconds=float(args.timeout_seconds or 600.0),
        poll_interval_seconds=float(args.poll_interval_seconds or 1.0),
    )

    text = json.dumps(report, indent=2)
    print(text)

    out = str(args.output or "").strip()
    if out:
        path = Path(out).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")

    return 0 if bool(report.get("ok")) else 1


def cmd_bench(args: argparse.Namespace) -> int:
    host = str(args.host or "127.0.0.1")
    hub_port = int(args.hub_port or 8723)
    node_base_port = int(args.node_base_port or 8724)
    reload_flag = bool(args.reload)

    token = str(args.token or "").strip()
    if not token:
        token = secrets.token_hex(16)

    nodes_list = [int(v) for v in list(args.nodes or [])]
    workers_list = [int(v) for v in list(args.workers or [])]
    if not nodes_list:
        nodes_list = [1]
    if not workers_list:
        workers_list = [1]
    for v in nodes_list:
        if v < 1:
            raise RuntimeError("--nodes must be >= 1")
    for v in workers_list:
        if v < 1:
            raise RuntimeError("--workers must be >= 1")

    num_images = int(args.images or 0)
    if num_images < 1:
        raise RuntimeError("--images must be >= 1")

    warmup = int(args.warmup or 0)
    if warmup < 0:
        raise RuntimeError("--warmup must be >= 0")

    poll_interval_seconds = float(args.poll_interval_seconds or 1.0)
    timeout_seconds = float(args.timeout_seconds or 1800.0)
    if poll_interval_seconds <= 0:
        raise RuntimeError("--poll-interval-seconds must be > 0")
    if timeout_seconds <= 0:
        raise RuntimeError("--timeout-seconds must be > 0")

    tag = str(args.tag or "").strip() or None

    storage_root = Path(str(args.storage_root or (Path.cwd() / "bench_data"))).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)

    log_path = Path(str(args.log_path or "benchmarks.jsonl")).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    payload_base = _build_generation_payload_from_args(args)
    payload_for_log = _redact_payload_for_log(payload_base)

    combos = [(n, w) for n in nodes_list for w in workers_list]
    results: list[dict] = []

    print(
        "nodes\tworkers\ttotal\timages\tok\tfail\tduration_s\timg_per_min\tavg_queue_s\tavg_run_s\tp90_total_s"
    )

    for num_nodes, workers_per_node in combos:
        run_id = _utc_now_iso().replace(":", "").replace("+", "").replace("-", "")
        run_storage_root = (storage_root / f"{run_id}_n{num_nodes}_w{workers_per_node}").resolve()
        run_storage_root.mkdir(parents=True, exist_ok=True)

        children: list[tuple[str, subprocess.Popen]] = []
        nodes: list[dict] = []
        hub_url = f"http://{host}:{hub_port}"
        leases: list[port_manager.PortLease] = []
        hub_port_final = int(hub_port)

        try:
            for i in range(int(num_nodes)):
                node_id = f"node{i + 1}"
                lease = port_manager.acquire_port_lease(
                    service="hapa-media-gen-node",
                    host=host,
                    base_port=int(node_base_port) + i,
                    preferred_port=int(node_base_port) + i,
                    pid=0,
                )
                leases.append(lease)
                node_port = int(lease.port)
                node_base_url = f"http://{host}:{node_port}"

                node_storage = (run_storage_root / node_id).resolve()
                node_storage.mkdir(parents=True, exist_ok=True)
                token_file = (node_storage / ".node_token").resolve()

                node_env = os.environ.copy()
                node_env["HAPA_MEDIA_NODE_STORAGE_DIR"] = str(node_storage)
                node_env["HAPA_MEDIA_NODE_MAX_WORKERS"] = str(int(workers_per_node))
                node_env["HAPA_MEDIA_NODE_TOKEN_FILE"] = str(token_file)

                if not _write_private_text_file(token_file, token):
                    node_env["HAPA_MEDIA_NODE_TOKEN"] = token

                cmd = [
                    sys.executable,
                    "-m",
                    "hapa_media_node",
                    "serve",
                    "--host",
                    host,
                    "--port",
                    str(node_port),
                ]
                if reload_flag:
                    cmd.append("--reload")

                p = subprocess.Popen(cmd, env=node_env, start_new_session=True)
                children.append((f"node:{node_id}", p))
                try:
                    lease.set_pid(int(p.pid))
                    lease.write_runtime(
                        base_url=node_base_url,
                        token_path=token_file,
                        storage_dir=node_storage,
                        extra={"node_id": node_id},
                    )
                except Exception:
                    pass
                nodes.append({"node_id": node_id, "base_url": node_base_url, "token": token})

            time.sleep(0.5)

            hub_lease = port_manager.acquire_port_lease(
                service="hapa-media-hub",
                host=host,
                base_port=int(hub_port),
                preferred_port=int(hub_port),
                pid=0,
            )
            leases.append(hub_lease)
            hub_port_final = int(hub_lease.port)
            hub_url = f"http://{host}:{hub_port_final}"

            hub_storage = (run_storage_root / "hub").resolve()
            hub_storage.mkdir(parents=True, exist_ok=True)
            hub_token_file = (hub_storage / ".node_token").resolve()
            hub_nodes_file = (hub_storage / "hub_nodes.json").resolve()

            hub_env = os.environ.copy()
            hub_env["HAPA_MEDIA_HUB_TOKEN_FILE"] = str(hub_token_file)
            if not _write_private_text_file(hub_token_file, token):
                hub_env["HAPA_MEDIA_HUB_TOKEN"] = token

            wrote_nodes_file = _write_private_json(hub_nodes_file, nodes)
            if not wrote_nodes_file:
                hub_env["HAPA_MEDIA_HUB_NODES"] = json.dumps(nodes)

            hub_cmd = [
                sys.executable,
                "-m",
                "hapa_media_node",
                "hub",
                "--host",
                host,
                "--port",
                str(hub_port_final),
            ]
            if wrote_nodes_file:
                hub_cmd.extend(["--nodes-file", str(hub_nodes_file)])
            if reload_flag:
                hub_cmd.append("--reload")

            hub_proc = subprocess.Popen(hub_cmd, env=hub_env, start_new_session=True)
            children.append(("hub", hub_proc))

            try:
                hub_lease.set_pid(int(hub_proc.pid))
                hub_lease.write_runtime(
                    base_url=hub_url,
                    token_path=hub_token_file,
                    storage_dir=hub_storage,
                    extra={"role": "hub"},
                )
            except Exception:
                pass

            for node in nodes:
                _wait_for_health(str(node.get("base_url")), timeout_seconds=60.0)
            _wait_for_health(hub_url, timeout_seconds=60.0)

            if warmup:
                warmup_ids: list[str] = []
                for i in range(int(warmup)):
                    pld = dict(payload_base)
                    if args.seed_base is not None:
                        pld["seed"] = int(args.seed_base) + i
                    warmup_ids.append(_submit_task(hub_url, token=token, payload=pld))
                _poll_tasks_done(
                    hub_url,
                    token=token,
                    task_ids=warmup_ids,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )

            system_before = _http_json(
                "GET",
                hub_url + "/v1/system",
                token=token,
                payload=None,
                timeout_seconds=15.0,
            )

            task_ids: list[str] = []
            t0 = time.time()
            for i in range(int(num_images)):
                pld = dict(payload_base)
                if args.seed_base is not None:
                    pld["seed"] = int(args.seed_base) + int(warmup) + i
                task_ids.append(_submit_task(hub_url, token=token, payload=pld))

            done = _poll_tasks_done(
                hub_url,
                token=token,
                task_ids=task_ids,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            t1 = time.time()

            system_after = _http_json(
                "GET",
                hub_url + "/v1/system",
                token=token,
                payload=None,
                timeout_seconds=15.0,
            )

            ok_count = sum(1 for t in done.values() if isinstance(t, dict) and t.get("status") == "succeeded")
            fail_count = sum(1 for t in done.values() if isinstance(t, dict) and t.get("status") == "failed")

            tasks: list[dict] = []
            queue_waits: list[float] = []
            run_times: list[float] = []
            total_times: list[float] = []

            for task_id in task_ids:
                t = done.get(task_id) or {}
                if isinstance(t, dict):
                    nid = None
                    try:
                        nid, _ = _split_composite_id(task_id)
                    except Exception:
                        nid = None

                    row = {
                        "task_id": task_id,
                        "node_id": nid,
                        "status": t.get("status"),
                        "queue_wait_seconds": t.get("queue_wait_seconds"),
                        "run_seconds": t.get("run_seconds"),
                        "total_seconds": t.get("total_seconds"),
                        "created_at": t.get("created_at"),
                        "started_at": t.get("started_at"),
                        "finished_at": t.get("finished_at"),
                        "result": t.get("result"),
                        "error": t.get("error"),
                    }
                    tasks.append(row)

                    qws = t.get("queue_wait_seconds")
                    rs = t.get("run_seconds")
                    ts = t.get("total_seconds")
                    if isinstance(qws, (int, float)):
                        queue_waits.append(float(qws))
                    if isinstance(rs, (int, float)):
                        run_times.append(float(rs))
                    if isinstance(ts, (int, float)):
                        total_times.append(float(ts))

            duration = max(0.0001, float(t1 - t0))
            img_per_min = (float(ok_count) / duration) * 60.0

            record: dict = {
                "time": _utc_now_iso(),
                "tag": tag,
                "host": host,
                "hub_url": hub_url,
                "hub_port": hub_port_final,
                "node_base_port": node_base_port,
                "nodes": int(num_nodes),
                "workers_per_node": int(workers_per_node),
                "total_workers": int(num_nodes) * int(workers_per_node),
                "images": int(num_images),
                "warmup": int(warmup),
                "payload": payload_for_log,
                "duration_seconds": duration,
                "images_per_min": img_per_min,
                "ok": ok_count,
                "failed": fail_count,
                "stats": {
                    "avg_queue_wait_seconds": _mean(queue_waits),
                    "avg_run_seconds": _mean(run_times),
                    "avg_total_seconds": _mean(total_times),
                    "p50_total_seconds": _pct(total_times, 50.0),
                    "p90_total_seconds": _pct(total_times, 90.0),
                    "p95_total_seconds": _pct(total_times, 95.0),
                },
                "system_before": system_before,
                "system_after": system_after,
                "tasks": tasks,
            }
            results.append(record)

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            avg_q = record["stats"].get("avg_queue_wait_seconds")
            avg_r = record["stats"].get("avg_run_seconds")
            p90 = record["stats"].get("p90_total_seconds")

            print(
                f"{num_nodes}\t{workers_per_node}\t{int(num_nodes) * int(workers_per_node)}\t{num_images}"
                f"\t{ok_count}\t{fail_count}\t{duration:.2f}\t{img_per_min:.2f}\t{avg_q}\t{avg_r}\t{p90}"
            )

        except Exception as exc:
            if not bool(args.continue_on_error):
                raise
            err_rec = {
                "time": _utc_now_iso(),
                "tag": tag,
                "nodes": int(num_nodes),
                "workers_per_node": int(workers_per_node),
                "error": str(exc),
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(err_rec) + "\n")
            print(
                f"{num_nodes}\t{workers_per_node}\t{int(num_nodes) * int(workers_per_node)}\t{num_images}"
                f"\t0\t0\t-\t-\t-\t-\t-\t(error)"
            )
        finally:
            _terminate_children(children)
            for lease in leases:
                try:
                    lease.release(remove_runtime=True)
                except Exception:
                    pass
            time.sleep(0.5)

    if results:
        best = max(results, key=lambda r: float(r.get("images_per_min") or 0.0))
        print(
            f"best\t{best.get('nodes')}\t{best.get('workers_per_node')}\t{best.get('total_workers')}\t{best.get('images')}"
            f"\t{best.get('ok')}\t{best.get('failed')}\t-\t{float(best.get('images_per_min') or 0.0):.2f}"
        )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hapa-media-node")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--token")
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    hub = sub.add_parser("hub")
    hub.add_argument("--host")
    hub.add_argument("--port", type=int)
    hub.add_argument("--token")
    hub.add_argument("--nodes")
    hub.add_argument("--nodes-file", dest="nodes_file")
    hub.add_argument("--reload", action="store_true")
    hub.set_defaults(func=cmd_hub)

    stack = sub.add_parser("stack")
    stack.add_argument("--host")
    stack.add_argument("--hub-port", dest="hub_port", type=int, default=8723)
    stack.add_argument("--node-base-port", dest="node_base_port", type=int, default=8724)
    stack.add_argument("--num-nodes", dest="num_nodes", type=int, default=1)
    stack.add_argument("--node-max-workers", dest="node_max_workers", type=int)
    stack.add_argument("--token")
    stack.add_argument("--storage-root", dest="storage_root")
    stack.add_argument("--reload", action="store_true")
    stack.set_defaults(func=cmd_stack)

    bench = sub.add_parser("bench")
    bench.add_argument("--host")
    bench.add_argument("--hub-port", dest="hub_port", type=int, default=8723)
    bench.add_argument("--node-base-port", dest="node_base_port", type=int, default=8724)
    bench.add_argument("--token")
    bench.add_argument("--storage-root", dest="storage_root")
    bench.add_argument("--reload", action="store_true")
    bench.add_argument("--nodes", nargs="+", type=int, default=[1])
    bench.add_argument("--workers", nargs="+", type=int, default=[1])
    bench.add_argument("--images", type=int, default=9)
    bench.add_argument("--warmup", type=int, default=1)
    bench.add_argument("--poll-interval-seconds", dest="poll_interval_seconds", type=float, default=1.0)
    bench.add_argument("--timeout-seconds", dest="timeout_seconds", type=float, default=1800.0)
    bench.add_argument("--log-path", dest="log_path", default="benchmarks.jsonl")
    bench.add_argument("--tag")
    bench.add_argument("--continue-on-error", dest="continue_on_error", action="store_true")
    bench.add_argument("--request-file", dest="request_file")
    bench.add_argument("--seed-base", dest="seed_base", type=int)
    bench.add_argument(
        "--mode",
        default="txt2img",
        choices=["txt2img", "img2img", "fill", "depth", "controlnet", "redux", "upscale"],
    )
    bench.add_argument("--prompt")
    bench.add_argument("--negative-prompt", dest="negative_prompt")
    bench.add_argument("--model", default="schnell")
    bench.add_argument("--base-model", dest="base_model")
    bench.add_argument("--lora-style", dest="lora_style")
    bench.add_argument("--lora-paths", dest="lora_paths", nargs="+")
    bench.add_argument("--lora-scales", dest="lora_scales", type=float, nargs="+")
    bench.add_argument("--image-file", dest="image_file")
    bench.add_argument("--masked-image-file", dest="masked_image_file")
    bench.add_argument("--depth-image-file", dest="depth_image_file")
    bench.add_argument("--controlnet-image-file", dest="controlnet_image_file")
    bench.add_argument("--redux-image-files", dest="redux_image_files", nargs="+")
    bench.add_argument("--image-strength", dest="image_strength", type=float)
    bench.add_argument("--controlnet-strength", dest="controlnet_strength", type=float)
    bench.add_argument("--redux-image-strengths", dest="redux_image_strengths", type=float, nargs="+")
    bench.add_argument("--steps", type=int)
    bench.add_argument("--seed", type=int)
    bench.add_argument("--width", type=int)
    bench.add_argument("--height", type=int)
    bench.add_argument("--quantize", type=int)
    bench.add_argument("--guidance", type=float)
    bench.add_argument("--low-ram", action="store_true")
    bench.set_defaults(func=cmd_bench)

    caps = sub.add_parser("capabilities")
    caps.add_argument(
        "--base-url",
        default=(
            os.environ.get("HAPA_MEDIA_HUB_BASE_URL")
            or os.environ.get("HAPA_MEDIA_NODE_BASE_URL")
            or "http://127.0.0.1:8723"
        ),
    )
    caps.add_argument(
        "--token",
    )
    caps.set_defaults(func=cmd_capabilities)

    gen = sub.add_parser("generate")
    gen.add_argument(
        "--base-url",
        default=(
            os.environ.get("HAPA_MEDIA_HUB_BASE_URL")
            or os.environ.get("HAPA_MEDIA_NODE_BASE_URL")
            or "http://127.0.0.1:8723"
        ),
    )
    gen.add_argument(
        "--token",
    )
    gen.add_argument(
        "--mode",
        default="txt2img",
        choices=["txt2img", "img2img", "fill", "depth", "controlnet", "redux", "upscale"],
    )
    gen.add_argument("--prompt", required=True)
    gen.add_argument("--negative-prompt", dest="negative_prompt")
    gen.add_argument("--model", default="schnell")
    gen.add_argument("--base-model", dest="base_model")
    gen.add_argument("--lora-style", dest="lora_style")
    gen.add_argument("--lora-paths", dest="lora_paths", nargs="+")
    gen.add_argument("--lora-scales", dest="lora_scales", type=float, nargs="+")
    gen.add_argument("--image-file", dest="image_file")
    gen.add_argument("--masked-image-file", dest="masked_image_file")
    gen.add_argument("--depth-image-file", dest="depth_image_file")
    gen.add_argument("--controlnet-image-file", dest="controlnet_image_file")
    gen.add_argument("--redux-image-files", dest="redux_image_files", nargs="+")
    gen.add_argument("--image-strength", dest="image_strength", type=float)
    gen.add_argument("--controlnet-strength", dest="controlnet_strength", type=float)
    gen.add_argument("--redux-image-strengths", dest="redux_image_strengths", type=float, nargs="+")
    gen.add_argument("--steps", type=int)
    gen.add_argument("--seed", type=int)
    gen.add_argument("--width", type=int)
    gen.add_argument("--height", type=int)
    gen.add_argument("--quantize", type=int)
    gen.add_argument("--guidance", type=float)
    gen.add_argument("--low-ram", action="store_true")
    gen.add_argument("--output")
    gen.add_argument("--timeout-seconds", type=float)
    gen.set_defaults(func=cmd_generate)

    st = sub.add_parser("self-test")
    st.add_argument(
        "--base-url",
        default=(
            os.environ.get("HAPA_MEDIA_HUB_BASE_URL")
            or os.environ.get("HAPA_MEDIA_NODE_BASE_URL")
            or "http://127.0.0.1:8723"
        ),
    )
    st.add_argument(
        "--token",
    )
    st.add_argument(
        "--mode",
        default="txt2img",
        choices=["txt2img", "img2img", "fill", "depth", "controlnet", "redux", "upscale"],
    )
    st.add_argument("--model", default="schnell")
    st.add_argument("--steps", type=int, default=2)
    st.add_argument("--copies", type=int, default=2)
    st.add_argument("--spawn-nodes", dest="spawn_nodes", type=int, default=1)
    st.add_argument("--spawn-max-workers", dest="spawn_max_workers", type=int)
    st.add_argument("--no-cleanup", dest="no_cleanup", action="store_true")
    st.add_argument("--startup-timeout-seconds", dest="startup_timeout_seconds", type=float, default=30.0)
    st.add_argument("--timeout-seconds", dest="timeout_seconds", type=float, default=600.0)
    st.add_argument("--poll-interval-seconds", dest="poll_interval_seconds", type=float, default=1.0)
    st.add_argument("--output")
    st.set_defaults(func=cmd_self_test)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
