from __future__ import annotations

import datetime
import errno
import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

API_VERSION = "v1"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def runtime_root() -> Path:
    value = str(os.environ.get("HAPA_RUNTIME_DIR") or "").strip()
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".hapa" / "runtime").resolve()


def _stale_seconds() -> int:
    raw = str(os.environ.get("HAPA_RUNTIME_STALE_SECONDS") or "").strip() or "600"
    try:
        value = int(raw)
    except Exception:
        value = 600
    return int(value) if value >= 1 else 600


def leases_dir(root: Optional[Path] = None) -> Path:
    base = (root or runtime_root()).resolve()
    path = (base / "leases").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtimes_dir(root: Optional[Path] = None) -> Path:
    base = (root or runtime_root()).resolve()
    path = (base / "runtimes").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((str(host), int(port)))
            return True
    except Exception:
        return False


def _safe_component(value: str) -> str:
    text = str(value or "").strip() or "unknown"
    out: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip("._-")
    return cleaned or "unknown"


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")
        try:
            os.chmod(tmp_path, mode)
        except Exception:
            pass
        os.replace(str(tmp_path), str(path))
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _try_create_json_exclusive(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, mode)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
            f.write("\n")
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise

    return True


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _lease_port_from_name(path: Path) -> Optional[int]:
    name = str(Path(path).name)
    if not name.startswith("port-") or not name.endswith(".json"):
        return None
    mid = name[len("port-") : -len(".json")]
    try:
        return int(mid)
    except Exception:
        return None


def _is_stale_record(
    *,
    path: Path,
    pid: Optional[int],
    host: Optional[str],
    port: Optional[int],
    stale_seconds: int,
) -> bool:
    if pid is not None and int(pid) > 0 and not _pid_exists(int(pid)):
        return True

    try:
        age = time.time() - float(Path(path).stat().st_mtime)
    except Exception:
        age = float(stale_seconds) + 1.0

    if age <= float(stale_seconds):
        return False

    check_host = str(host or "").strip() or "127.0.0.1"
    check_port = int(port) if port is not None else None
    if check_port is None:
        return True

    return bool(_is_port_available(check_host, int(check_port)))


def cleanup_stale(*, root: Optional[Path] = None) -> dict[str, int]:
    base = (root or runtime_root()).resolve()
    stale_seconds = _stale_seconds()

    leases = leases_dir(base)
    runtimes = runtimes_dir(base)

    removed_leases = 0
    removed_runtimes = 0

    for lease_path in sorted(leases.glob("port-*.json")):
        data = _read_json(lease_path) or {}

        pid_raw = data.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except Exception:
            pid = None

        host = str(data.get("host") or "").strip() or None

        port_raw = data.get("port")
        try:
            port = int(port_raw) if port_raw is not None else _lease_port_from_name(lease_path)
        except Exception:
            port = _lease_port_from_name(lease_path)

        if not _is_stale_record(
            path=lease_path,
            pid=pid,
            host=host,
            port=port,
            stale_seconds=stale_seconds,
        ):
            continue

        runtime_file = data.get("runtime_file")

        try:
            lease_path.unlink()
            removed_leases += 1
        except Exception:
            pass

        if isinstance(runtime_file, str) and runtime_file:
            try:
                rp = Path(runtime_file).expanduser()
                if not rp.is_absolute():
                    rp = (base / rp).resolve()
                rp.unlink()
                removed_runtimes += 1
            except Exception:
                pass

    for runtime_path in sorted(runtimes.glob("*.json")):
        data = _read_json(runtime_path) or {}

        pid_raw = data.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except Exception:
            pid = None

        host = str(data.get("host") or "").strip() or None

        port_raw = data.get("port")
        try:
            port = int(port_raw) if port_raw is not None else None
        except Exception:
            port = None

        if not _is_stale_record(
            path=runtime_path,
            pid=pid,
            host=host,
            port=port,
            stale_seconds=stale_seconds,
        ):
            continue

        try:
            runtime_path.unlink()
            removed_runtimes += 1
        except Exception:
            pass

    return {"leases_removed": int(removed_leases), "runtimes_removed": int(removed_runtimes)}


@dataclass
class PortLease:
    service: str
    host: str
    port: int
    pid: int
    started_at: str
    lease_file: Path
    runtime_file: Path

    def to_lease_dict(self) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "service": str(self.service),
            "host": str(self.host),
            "port": int(self.port),
            "pid": int(self.pid),
            "started_at": str(self.started_at),
            "runtime_file": str(self.runtime_file),
        }

    def write_lease(self) -> None:
        _atomic_write_json(self.lease_file, self.to_lease_dict(), mode=0o600)

    def write_runtime(
        self,
        *,
        base_url: Optional[str] = None,
        token_path: Optional[Path] = None,
        storage_dir: Optional[Path] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        if base_url is None:
            base_url = f"http://{self.host}:{int(self.port)}"

        data: dict[str, Any] = {
            "api_version": API_VERSION,
            "service": str(self.service),
            "base_url": str(base_url),
            "host": str(self.host),
            "port": int(self.port),
            "pid": int(self.pid),
            "started_at": str(self.started_at),
            "updated_at": _utc_now_iso(),
            "lease_file": str(self.lease_file),
        }

        if token_path is not None:
            data["token_path"] = str(Path(token_path).expanduser().resolve())
        if storage_dir is not None:
            data["storage_dir"] = str(Path(storage_dir).expanduser().resolve())
        if isinstance(extra, dict) and extra:
            for k, v in extra.items():
                key = str(k)
                if key in data:
                    continue
                data[key] = v

        _atomic_write_json(self.runtime_file, data, mode=0o600)

    def set_pid(self, pid: int) -> None:
        self.pid = int(pid)
        self.write_lease()

    def release(self, *, remove_runtime: bool = True) -> None:
        try:
            self.lease_file.unlink()
        except Exception:
            pass
        if remove_runtime:
            try:
                self.runtime_file.unlink()
            except Exception:
                pass


def acquire_port_lease(
    *,
    service: str,
    host: str,
    base_port: int,
    max_scan: int = 256,
    preferred_port: Optional[int] = None,
    instance: Optional[str] = None,
    pid: Optional[int] = None,
    root: Optional[Path] = None,
) -> PortLease:
    base = (root or runtime_root()).resolve()
    cleanup_stale(root=base)

    leases = leases_dir(base)
    runtimes = runtimes_dir(base)

    safe_service = _safe_component(service)
    started_at = _utc_now_iso()

    pid_value = int(pid) if pid is not None else 0

    candidates: list[int] = []
    if preferred_port is not None:
        try:
            candidates.append(int(preferred_port))
        except Exception:
            candidates = []

    start = max(1, int(base_port))
    for i in range(int(max_scan)):
        p = start + i
        if p not in candidates:
            candidates.append(p)

    for port in candidates:
        lease_file = (leases / f"port-{int(port)}.json").resolve()
        instance_final = _safe_component(instance) if instance else str(int(port))
        runtime_file = (runtimes / f"{safe_service}-{instance_final}.json").resolve()

        lease = PortLease(
            service=str(service),
            host=str(host),
            port=int(port),
            pid=int(pid_value),
            started_at=str(started_at),
            lease_file=lease_file,
            runtime_file=runtime_file,
        )

        if not _try_create_json_exclusive(lease_file, lease.to_lease_dict(), mode=0o600):
            continue

        if not _is_port_available(host, int(port)):
            try:
                lease_file.unlink()
            except Exception:
                pass
            continue

        try:
            lease.write_runtime()
        except Exception:
            try:
                lease_file.unlink()
            except Exception:
                pass
            raise

        return lease

    raise RuntimeError("No available ports")


def list_runtimes(*, root: Optional[Path] = None, service: Optional[str] = None) -> list[dict[str, Any]]:
    base = (root or runtime_root()).resolve()
    cleanup_stale(root=base)

    out: list[dict[str, Any]] = []
    for p in sorted(runtimes_dir(base).glob("*.json")):
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        if service is not None and str(data.get("service") or "") != str(service):
            continue
        out.append(data)
    return out
