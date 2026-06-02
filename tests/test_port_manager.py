from __future__ import annotations

import multiprocessing
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from hapa_media_node import port_manager


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _find_dead_pid(*, start: int = 999_999) -> int:
    pid = int(start)
    while _pid_exists(pid):
        pid += 1
    return pid


def _pick_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _lease_acquire_worker(
    out_q,
    start_evt,
    release_evt,
    *,
    runtime_root: str,
    host: str,
    base_port: int,
) -> None:
    try:
        if not start_evt.wait(10.0):
            raise RuntimeError("start timeout")

        lease = port_manager.acquire_port_lease(
            service="test-service",
            host=host,
            base_port=int(base_port),
            max_scan=256,
            pid=os.getpid(),
            root=Path(runtime_root),
        )
        out_q.put({"ok": True, "port": int(lease.port)})

        if not release_evt.wait(10.0):
            raise RuntimeError("release timeout")

        lease.release(remove_runtime=True)
    except Exception as exc:
        out_q.put({"ok": False, "error": str(exc)})


class PortManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_stale = os.environ.get("HAPA_RUNTIME_STALE_SECONDS")

    def tearDown(self) -> None:
        if self._old_stale is None:
            os.environ.pop("HAPA_RUNTIME_STALE_SECONDS", None)
        else:
            os.environ["HAPA_RUNTIME_STALE_SECONDS"] = self._old_stale

    def test_concurrent_acquire_unique_ports(self) -> None:
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            base_port = _pick_free_port(host)

            ctx = multiprocessing.get_context("spawn")
            out_q = ctx.Queue()
            start_evt = ctx.Event()
            release_evt = ctx.Event()

            procs = [
                ctx.Process(
                    target=_lease_acquire_worker,
                    args=(out_q, start_evt, release_evt),
                    kwargs={
                        "runtime_root": str(root),
                        "host": host,
                        "base_port": int(base_port),
                    },
                )
                for _ in range(8)
            ]

            for p in procs:
                p.start()

            results: list[dict[str, Any]] = []
            try:
                start_evt.set()
                for _ in procs:
                    results.append(out_q.get(timeout=20.0))

                lease_files = list((root / "leases").glob("port-*.json"))
                self.assertEqual(len(procs), len(lease_files))
            finally:
                release_evt.set()

                for p in procs:
                    p.join(timeout=20.0)
                for p in procs:
                    if p.is_alive():
                        try:
                            p.terminate()
                        except Exception:
                            pass
                for p in procs:
                    p.join(timeout=5.0)

            for p in procs:
                self.assertEqual(p.exitcode, 0)

            errors = [r for r in results if not r.get("ok")]
            self.assertEqual([], errors)

            ports = [int(r["port"]) for r in results if r.get("ok")]
            self.assertEqual(len(procs), len(ports))
            self.assertEqual(len(ports), len(set(ports)))

            self.assertEqual([], list((root / "leases").glob("port-*.json")))
            self.assertEqual([], list((root / "runtimes").glob("*.json")))

    def test_cleanup_stale_dead_pid_removes_lease_and_runtime(self) -> None:
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            base_port = _pick_free_port(host)

            lease = port_manager.acquire_port_lease(
                service="test-service",
                host=host,
                base_port=int(base_port),
                preferred_port=int(base_port),
                pid=0,
                root=root,
            )
            lease.set_pid(_find_dead_pid())

            removed = port_manager.cleanup_stale(root=root)

            self.assertFalse(lease.lease_file.exists())
            self.assertFalse(lease.runtime_file.exists())
            self.assertGreaterEqual(int(removed.get("leases_removed") or 0), 1)
            self.assertGreaterEqual(int(removed.get("runtimes_removed") or 0), 1)

    def test_cleanup_stale_old_lease_free_port_is_removed(self) -> None:
        os.environ["HAPA_RUNTIME_STALE_SECONDS"] = "1"
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            base_port = _pick_free_port(host)

            lease = port_manager.acquire_port_lease(
                service="test-service",
                host=host,
                base_port=int(base_port),
                preferred_port=int(base_port),
                pid=0,
                root=root,
            )

            old = time.time() - 10.0
            os.utime(lease.lease_file, (old, old))

            removed = port_manager.cleanup_stale(root=root)

            self.assertFalse(lease.lease_file.exists())
            self.assertFalse(lease.runtime_file.exists())
            self.assertGreaterEqual(int(removed.get("leases_removed") or 0), 1)

    def test_cleanup_stale_old_lease_in_use_port_is_kept_until_free(self) -> None:
        os.environ["HAPA_RUNTIME_STALE_SECONDS"] = "1"
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            base_port = _pick_free_port(host)

            lease = port_manager.acquire_port_lease(
                service="test-service",
                host=host,
                base_port=int(base_port),
                preferred_port=int(base_port),
                pid=0,
                root=root,
            )

            old = time.time() - 10.0
            os.utime(lease.lease_file, (old, old))

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind((host, int(lease.port)))
                s.listen(1)

                removed1 = port_manager.cleanup_stale(root=root)
                self.assertTrue(lease.lease_file.exists())
                self.assertTrue(lease.runtime_file.exists())
                self.assertEqual(0, int(removed1.get("leases_removed") or 0))
            finally:
                s.close()

            removed2 = port_manager.cleanup_stale(root=root)
            self.assertFalse(lease.lease_file.exists())
            self.assertFalse(lease.runtime_file.exists())
            self.assertGreaterEqual(int(removed2.get("leases_removed") or 0), 1)
