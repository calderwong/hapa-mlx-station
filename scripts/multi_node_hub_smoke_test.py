from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional


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
        with urllib.request.urlopen(req, timeout=timeout_seconds) as res:
            body = res.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Node unavailable: {exc}")


def _probe_download(url: str, *, token: Optional[str], timeout_seconds: float) -> None:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url=url, method="GET", headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as res:
            chunk = res.read(64)
            if not chunk:
                raise RuntimeError("Empty download")
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


def _wait_for_nodes(
    hub_url: str,
    hub_token: str,
    *,
    expected_node_ids: list[str],
    timeout_seconds: float,
    want_ok: bool,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last: dict[str, Any] = {}

    while True:
        if time.time() >= deadline:
            raise RuntimeError("Timed out waiting for hub node state")

        try:
            caps = _http_json(
                "GET", hub_url + "/capabilities", token=hub_token, payload=None, timeout_seconds=30.0
            )
        except RuntimeError as exc:
            if "Node unavailable" not in str(exc):
                raise
            time.sleep(1)
            continue
        last = caps

        nodes = caps.get("nodes")
        if not isinstance(nodes, dict):
            raise RuntimeError("Hub /capabilities missing nodes")

        ok = True
        for node_id in expected_node_ids:
            entry = nodes.get(node_id)
            if not isinstance(entry, dict):
                ok = False
                break
            if bool(entry.get("ok")) is not want_ok:
                ok = False
                break

        if ok:
            return last

        time.sleep(1)


def _submit_job(
    hub_url: str,
    hub_token: str,
    *,
    prompt: str,
    mode: str,
    model: str,
    steps: int,
    lora_style: Optional[str],
) -> str:
    payload: dict[str, Any] = {
        "mode": mode,
        "prompt": prompt,
        "model": model,
        "steps": steps,
    }
    if lora_style:
        payload["lora_style"] = lora_style

    res = _http_json(
        "POST",
        hub_url + "/v1/images/generations",
        token=hub_token,
        payload=payload,
        timeout_seconds=30.0,
    )

    task_id = res.get("task_id")
    if not task_id:
        raise RuntimeError("Missing task_id in response")

    return str(task_id)


def _wait_for_task(hub_url: str, hub_token: str, *, task_id: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds

    while True:
        if time.time() >= deadline:
            raise RuntimeError("Timed out waiting for task")

        try:
            task = _http_json(
                "GET",
                hub_url + f"/v1/tasks/{task_id}",
                token=hub_token,
                payload=None,
                timeout_seconds=30.0,
            )
        except RuntimeError as exc:
            if "Node unavailable" not in str(exc):
                raise
            time.sleep(1)
            continue

        status = task.get("status")
        stage = task.get("stage")
        progress = task.get("progress")
        pct = int((progress or 0) * 100)
        print(f"[{task_id}] {status} | {stage} | {pct}%")

        if status == "succeeded":
            return task
        if status == "failed":
            raise RuntimeError(task.get("error") or "Task failed")

        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(prog="multi_node_hub_smoke_test")
    parser.add_argument("--hub-url", default="http://127.0.0.1:8726")
    parser.add_argument("--hub-token", required=True)
    parser.add_argument("--expected-nodes", default="a,b")
    parser.add_argument("--mode", default="txt2img")
    parser.add_argument("--model", default="schnell")
    parser.add_argument("--steps", type=int, default=2)
    parser.add_argument("--lora-style", default="storyboard")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--startup-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)

    parser.add_argument("--run-failover", action="store_true")
    parser.add_argument("--failover-node-id", default="b")
    parser.add_argument("--failover-wait-seconds", type=float, default=10.0)

    args = parser.parse_args()

    hub_url = str(args.hub_url).rstrip("/")
    expected = [s.strip() for s in str(args.expected_nodes).split(",") if s.strip()]
    if len(expected) < 1:
        raise SystemExit("--expected-nodes must contain at least 1 node id")
    if int(args.jobs) > 0 and len(expected) < 2:
        raise SystemExit("--expected-nodes must contain at least 2 node ids when --jobs > 0")

    _wait_for_nodes(
        hub_url,
        args.hub_token,
        expected_node_ids=expected,
        timeout_seconds=float(args.startup_timeout_seconds),
        want_ok=True,
    )

    node_ids: list[str] = []
    for i in range(int(args.jobs)):
        task_id = _submit_job(
            hub_url,
            args.hub_token,
            prompt=f"Multi-node smoke test job {i + 1}",
            mode=args.mode,
            model=args.model,
            steps=int(args.steps),
            lora_style=str(args.lora_style) if args.lora_style else None,
        )

        node_id, _ = _parse_composite_id(task_id)
        if node_id not in set(expected):
            raise RuntimeError(f"Unexpected node_id {node_id!r} (expected one of {expected})")

        node_ids.append(node_id)

        task = _wait_for_task(hub_url, args.hub_token, task_id=task_id, timeout_seconds=float(args.timeout_seconds))
        result = task.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Missing result in succeeded task")

        asset_id = result.get("asset_id")
        if not asset_id:
            raise RuntimeError("Missing asset_id in succeeded task")

        asset_node_id, _ = _parse_composite_id(str(asset_id))
        if asset_node_id != node_id:
            raise RuntimeError("asset_id node_id does not match task_id node_id")

        _probe_download(
            hub_url + f"/v1/assets/{asset_id}/download",
            token=args.hub_token,
            timeout_seconds=30.0,
        )

    if len(expected) == 2 and len(node_ids) >= 2:
        if len(set(node_ids)) != 2:
            raise RuntimeError(f"Expected both nodes to be used, got: {node_ids}")
        for idx in range(1, len(node_ids)):
            if node_ids[idx] == node_ids[idx - 1]:
                raise RuntimeError(f"Expected round-robin alternation, got: {node_ids}")

    if args.run_failover:
        failover_node_id = str(args.failover_node_id).strip()
        if not failover_node_id:
            raise RuntimeError("--failover-node-id is required")

        _wait_for_nodes(
            hub_url,
            args.hub_token,
            expected_node_ids=[failover_node_id],
            timeout_seconds=float(args.failover_wait_seconds),
            want_ok=False,
        )

        task_id = _submit_job(
            hub_url,
            args.hub_token,
            prompt="Multi-node failover test job",
            mode=args.mode,
            model=args.model,
            steps=int(args.steps),
            lora_style=str(args.lora_style) if args.lora_style else None,
        )
        node_id, _ = _parse_composite_id(task_id)
        if node_id == failover_node_id:
            raise RuntimeError(f"Failover expected to avoid {failover_node_id!r}, got task_id={task_id!r}")

        _wait_for_task(hub_url, args.hub_token, task_id=task_id, timeout_seconds=float(args.timeout_seconds))

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
