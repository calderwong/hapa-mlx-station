from __future__ import annotations

import datetime
import json
import secrets
import time
import urllib.error
import urllib.request
from typing import Any, Optional


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _http_json(
    method: str,
    url: str,
    *,
    token: Optional[str],
    payload: Optional[dict[str, Any]],
    timeout_seconds: float,
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
        with urllib.request.urlopen(req, timeout=float(timeout_seconds)) as res:
            body = res.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Node unavailable: {exc}")


def _probe_download(url: str, *, token: Optional[str], timeout_seconds: float) -> int:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url=url, method="GET", headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=float(timeout_seconds)) as res:
            chunk = res.read(64)
            if not chunk:
                raise RuntimeError("Empty download")
            return int(len(chunk))
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Node unavailable: {exc}")


def _parse_composite_id(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if not value or ":" not in value:
        raise RuntimeError(f"Invalid composite id: {value!r}")

    node_id, inner = value.split(":", 1)
    node_id = node_id.strip()
    inner = inner.strip()
    if not node_id or not inner:
        raise RuntimeError(f"Invalid composite id: {value!r}")

    return node_id, inner


def _eligible_nodes_for_mode(caps: dict[str, Any], mode: str) -> list[str]:
    mode_v = str(mode or "txt2img").strip() or "txt2img"
    nodes = caps.get("nodes")
    if not isinstance(nodes, dict):
        return []

    eligible: list[str] = []
    for node_id, entry in nodes.items():
        if not isinstance(entry, dict) or not bool(entry.get("ok")):
            continue
        node_caps = entry.get("capabilities")
        if not isinstance(node_caps, dict):
            continue
        image = ((node_caps.get("modalities") or {}).get("image") or {})
        if not isinstance(image, dict):
            continue
        modes = image.get("modes") or []
        if isinstance(modes, list) and mode_v in [str(m) for m in modes]:
            eligible.append(str(node_id))

    return eligible


def _wait_for_tasks_done(
    hub_url: str,
    hub_token: str,
    *,
    task_ids: list[str],
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, dict[str, Any]]:
    deadline = time.time() + float(timeout_seconds)
    remaining = set(str(t) for t in task_ids)
    done: dict[str, dict[str, Any]] = {}

    while remaining:
        if time.time() >= deadline:
            raise RuntimeError(f"Timed out waiting for tasks ({len(remaining)} remaining)")

        for task_id in list(remaining):
            task = _http_json(
                "GET",
                hub_url + f"/v1/tasks/{task_id}",
                token=hub_token,
                payload=None,
                timeout_seconds=15.0,
            )

            status = task.get("status") if isinstance(task, dict) else None
            if status in {"succeeded", "failed"}:
                done[str(task_id)] = task
                remaining.discard(str(task_id))

        if remaining:
            time.sleep(max(0.05, float(poll_interval_seconds)))

    return done


def run_self_test(
    hub_url: str,
    hub_token: str,
    *,
    mode: str = "txt2img",
    model: str = "schnell",
    steps: int = 2,
    copies: int = 2,
    spawn_nodes: int = 1,
    spawn_max_workers: Optional[int] = 1,
    cleanup: bool = True,
    startup_timeout_seconds: float = 30.0,
    timeout_seconds: float = 600.0,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    hub_url = str(hub_url or "").strip().rstrip("/")
    if not hub_url:
        raise RuntimeError("Missing hub_url")

    hub_token = str(hub_token or "").strip()
    if not hub_token:
        raise RuntimeError("Missing hub_token")

    mode_v = str(mode or "txt2img").strip() or "txt2img"
    model_v = str(model or "schnell").strip() or "schnell"

    copies_v = int(copies)
    if copies_v < 1:
        raise RuntimeError("copies must be >= 1")

    steps_v = int(steps)
    if steps_v < 1:
        raise RuntimeError("steps must be >= 1")

    spawn_nodes_v = int(spawn_nodes)
    if spawn_nodes_v < 0:
        raise RuntimeError("spawn_nodes must be >= 0")

    run_id = secrets.token_hex(4)

    result: dict[str, Any] = {
        "api_version": "v1",
        "time": _utc_now_iso(),
        "ok": False,
        "run_id": run_id,
        "hub_url": hub_url,
        "mode": mode_v,
        "model": model_v,
        "steps": steps_v,
        "copies": copies_v,
        "steps_results": [],
        "spawned_nodes": [],
        "tasks": [],
        "downloads": [],
    }

    spawned_node_ids: list[str] = []

    def _record_step(
        name: str,
        *,
        ok: bool,
        started_at: str,
        finished_at: str,
        duration_seconds: float,
        error: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        skipped: bool = False,
    ) -> None:
        rec: dict[str, Any] = {
            "name": str(name),
            "ok": bool(ok),
            "started_at": str(started_at),
            "finished_at": str(finished_at),
            "duration_seconds": float(duration_seconds),
        }
        if skipped:
            rec["skipped"] = True
        if error:
            rec["error"] = str(error)
        if data is not None:
            rec["data"] = data
        result["steps_results"].append(rec)

    try:
        step_start = time.time()
        started_at = _utc_now_iso()
        try:
            health = _http_json(
                "GET",
                hub_url + "/health",
                token=None,
                payload=None,
                timeout_seconds=10.0,
            )
            if not isinstance(health, dict) or health.get("ok") is not True:
                raise RuntimeError(f"Unexpected /health response: {health}")
            _record_step(
                "hub_health",
                ok=True,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            _record_step(
                "hub_health",
                ok=False,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                error=str(exc),
            )

        step_start = time.time()
        started_at = _utc_now_iso()
        eligible_before: list[str] = []
        caps: dict[str, Any] = {}
        try:
            caps = _http_json(
                "GET",
                hub_url + "/capabilities",
                token=hub_token,
                payload=None,
                timeout_seconds=30.0,
            )
            if not isinstance(caps, dict):
                raise RuntimeError("Invalid /capabilities response")
            eligible_before = _eligible_nodes_for_mode(caps, mode_v)
            _record_step(
                "hub_capabilities",
                ok=True,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                data={"eligible_nodes": eligible_before},
            )
        except Exception as exc:
            _record_step(
                "hub_capabilities",
                ok=False,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                error=str(exc),
            )

        eligible_after: list[str] = list(eligible_before)
        if spawn_nodes_v <= 0:
            _record_step(
                "spawn_nodes",
                ok=True,
                skipped=True,
                started_at=_utc_now_iso(),
                finished_at=_utc_now_iso(),
                duration_seconds=0.0,
                data={"eligible_nodes_before": eligible_before, "eligible_nodes_after": eligible_after},
            )
        else:
            step_start = time.time()
            started_at = _utc_now_iso()
            spawned: list[dict[str, Any]] = []
            errors: list[str] = []
            try:
                for i in range(spawn_nodes_v):
                    node_id = f"selftest-{run_id}-{i + 1}"
                    payload: dict[str, Any] = {"enabled": True, "node_id": node_id}
                    if spawn_max_workers is not None:
                        payload["max_workers"] = int(spawn_max_workers)

                    resp = _http_json(
                        "POST",
                        hub_url + "/v1/admin/nodes/spawn",
                        token=hub_token,
                        payload=payload,
                        timeout_seconds=float(startup_timeout_seconds),
                    )
                    node = (resp.get("node") if isinstance(resp, dict) else None) or {}
                    node_id_final = str(node.get("node_id") or node_id)
                    spawned.append(
                        {
                            "node_id": node_id_final,
                            "base_url": node.get("base_url"),
                            "pid": node.get("pid"),
                        }
                    )
                    spawned_node_ids.append(node_id_final)

                result["spawned_nodes"] = spawned

                caps2 = _http_json(
                    "GET",
                    hub_url + "/capabilities",
                    token=hub_token,
                    payload=None,
                    timeout_seconds=30.0,
                )
                if isinstance(caps2, dict):
                    eligible_after = _eligible_nodes_for_mode(caps2, mode_v)

                _record_step(
                    "spawn_nodes",
                    ok=True,
                    started_at=started_at,
                    finished_at=_utc_now_iso(),
                    duration_seconds=time.time() - step_start,
                    data={
                        "eligible_nodes_before": eligible_before,
                        "eligible_nodes_after": eligible_after,
                        "spawned": spawned,
                        "errors": errors,
                    },
                )
            except Exception as exc:
                errors.append(str(exc))
                _record_step(
                    "spawn_nodes",
                    ok=False,
                    started_at=started_at,
                    finished_at=_utc_now_iso(),
                    duration_seconds=time.time() - step_start,
                    error=str(exc),
                    data={"errors": errors},
                )

        step_start = time.time()
        started_at = _utc_now_iso()
        task_ids: list[str] = []
        try:
            for i in range(copies_v):
                payload = {
                    "mode": mode_v,
                    "prompt": f"Self-test {run_id} job {i + 1}",
                    "model": model_v,
                    "steps": int(steps_v),
                }
                submit = _http_json(
                    "POST",
                    hub_url + "/v1/images/generations",
                    token=hub_token,
                    payload=payload,
                    timeout_seconds=30.0,
                )
                task_id = submit.get("task_id") if isinstance(submit, dict) else None
                if not task_id:
                    raise RuntimeError("Missing task_id in response")
                task_ids.append(str(task_id))

            tasks = _wait_for_tasks_done(
                hub_url,
                hub_token,
                task_ids=task_ids,
                timeout_seconds=float(timeout_seconds),
                poll_interval_seconds=float(poll_interval_seconds),
            )

            task_recs: list[dict[str, Any]] = []
            node_ids_seen: list[str] = []
            for tid in task_ids:
                task = tasks.get(str(tid)) or {}
                status = task.get("status") if isinstance(task, dict) else None
                rec: dict[str, Any] = {"task_id": str(tid), "status": status}
                try:
                    node_id, _ = _parse_composite_id(str(tid))
                    rec["node_id"] = node_id
                    node_ids_seen.append(node_id)
                except Exception:
                    pass

                result_obj = task.get("result") if isinstance(task, dict) else None
                if isinstance(result_obj, dict):
                    asset_id = result_obj.get("asset_id")
                    if asset_id:
                        rec["asset_id"] = str(asset_id)
                        rec["download_url"] = result_obj.get("download_url")

                err = task.get("error") if isinstance(task, dict) else None
                if err:
                    rec["error"] = err

                task_recs.append(rec)

            result["tasks"] = task_recs

            unique_nodes = sorted(set(node_ids_seen))
            if copies_v >= 2 and len(eligible_after) < 2:
                raise RuntimeError(f"Need >=2 eligible nodes for multi-node routing, got {len(eligible_after)}")
            if copies_v >= 2 and len(unique_nodes) < 2:
                raise RuntimeError(f"Expected tasks to route to >=2 nodes, saw {unique_nodes}")

            ok = all(t.get("status") == "succeeded" for t in task_recs)
            _record_step(
                "generate",
                ok=ok,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                data={"tasks": task_recs, "eligible_nodes": eligible_after, "unique_nodes": unique_nodes},
            )
        except Exception as exc:
            _record_step(
                "generate",
                ok=False,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                error=str(exc),
                data={"task_ids": task_ids, "eligible_nodes": eligible_after},
            )

        step_start = time.time()
        started_at = _utc_now_iso()
        try:
            downloads: list[dict[str, Any]] = []
            for task in result.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                asset_id = task.get("asset_id")
                if not asset_id:
                    continue
                url = hub_url + f"/v1/assets/{asset_id}/download"
                n = _probe_download(url, token=hub_token, timeout_seconds=30.0)
                downloads.append({"asset_id": str(asset_id), "bytes_read": int(n)})

            result["downloads"] = downloads

            ok = bool(downloads) and all(int(d.get("bytes_read") or 0) > 0 for d in downloads)
            _record_step(
                "download_assets",
                ok=ok,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                data={"downloads": downloads},
            )
        except Exception as exc:
            _record_step(
                "download_assets",
                ok=False,
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                error=str(exc),
            )

        if not cleanup:
            _record_step(
                "cleanup",
                ok=True,
                skipped=True,
                started_at=_utc_now_iso(),
                finished_at=_utc_now_iso(),
                duration_seconds=0.0,
            )
        else:
            step_start = time.time()
            started_at = _utc_now_iso()
            errors: list[str] = []
            for node_id in list(spawned_node_ids):
                try:
                    _http_json(
                        "POST",
                        hub_url + f"/v1/admin/nodes/{node_id}/terminate",
                        token=hub_token,
                        payload={"remove": True},
                        timeout_seconds=30.0,
                    )
                except Exception as exc:
                    errors.append(f"{node_id}: {exc}")

            _record_step(
                "cleanup",
                ok=not bool(errors),
                started_at=started_at,
                finished_at=_utc_now_iso(),
                duration_seconds=time.time() - step_start,
                data={"terminated": list(spawned_node_ids), "errors": errors},
            )

        result["ok"] = all(bool(s.get("ok")) for s in (result.get("steps_results") or []))
        result["time"] = _utc_now_iso()
        return result
    finally:
        spawned_node_ids.clear()
