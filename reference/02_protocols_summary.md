# Protocols Summary (Hapa AG-aligned)

This repo/workspace should follow the same operating principles captured in `${HAPA_DESKTOP_ROOT}/hapa-og/docs`.

## Priority ordering

- **Integrity (critical)**
  - No crashes, no corrupt artifacts, no misleading states.
- **Flow (high)**
  - Long-running generation should be async and observable (progress/stage).
- **Form (medium)**
  - Keep modules small and explicit; avoid monolith server files.
- **Decoration (low)**
  - UI polish comes after correctness.

## Validation protocol

- Validate from the primary source:
  - official docs
  - source repos
  - current working examples
- Avoid “training-data assumptions” for:
  - model IDs
  - endpoints
  - request/response shapes

## Multi-agent collaboration protocol

- Treat shared/hot files as lockable via a lightweight **claim**.
- Leave trails in a comms folder.
- Prefer additive modules over rewriting shared code.

## Truth / observability discipline

- Make the node’s “belief state” explicit:
  - queue depth
  - task status
  - artifact paths
  - model/config used
- Prefer **SQLite** as the local truth projection for tasks/assets.
- Logs should be structured and bounded (avoid unbounded in-memory logs).
