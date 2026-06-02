# Design Plan — Hapa Media Gen Node (Mac Studio / Silicon Nexus)

## Goal

Build a **siloed, contained** “Generative AI Media” capability on the Mac Studio using **`mflux` as the base layer** and expose it via:

1. **CLI** for local operator usage
2. **Web UI** (“Hapa Gen Media Server” UI)
3. **LAN HTTP API** for other Hapa Nodes to request work + retrieve artifacts

## MVP scope (Phase 0)

- **txt2img** image generation via mflux
- Async **job queue** with explicit task status
- Artifact storage on disk + metadata in SQLite
- Authenticated API (bearer token)
- Minimal web UI to submit prompts and download results

Non-goals for MVP:

- video generation
- multi-worker concurrency
- mDNS discovery

## Architecture

### Process model

- Single Python service:
  - FastAPI web server (API + static UI)
  - Background worker loop (single GPU/MLX worker)
  - SQLite metadata store
  - Filesystem artifact store

### Components

- **API Layer** (FastAPI)
  - versioned routes
  - auth middleware
  - request validation

- **Job/Task Orchestrator**
  - accepts job requests
  - persists tasks to SQLite
  - processes tasks sequentially
  - updates task stage/progress/status

- **mflux Engine Adapter**
  - wraps mflux Python API or CLI
  - enforces allowed models + sane defaults
  - controls cache dirs via env vars

- **Asset Store**
  - writes images to a deterministic path
  - records provenance metadata:
    - prompt
    - model
    - steps/seed/quantization
    - timestamp

- **Web UI**
  - basic form: prompt + model + steps + seed
  - shows task progress/status
  - renders completed image

- **CLI**
  - convenience wrapper to:
    - start server
    - submit jobs
    - wait for completion
    - download artifacts

## Data model (minimal)

### Task

- `task_id` (uuid)
- `type`: `image.generate`
- `status`: `queued | running | succeeded | failed | canceled`
- `stage`: freeform string (e.g. `queued`, `loading_model`, `generating`, `saving`)
- `progress`: float `0..1` (best-effort)
- `request_json` (prompt/options)
- `result_asset_id` (nullable)
- timestamps

### Asset

- `asset_id` (uuid)
- `type`: `image/png`
- `path` (absolute or storage-root relative)
- `metadata_json`
- timestamps

## API surface (MVP)

- `GET /health` (no auth)
- `GET /capabilities` (auth)
- `POST /v1/images/generations` (auth) -> `202 { task_id }`
- `GET /v1/tasks/{task_id}` (auth)
- `GET /v1/assets/{asset_id}` (auth)
- `GET /v1/assets/{asset_id}/download` (auth)

## Security

- Bind to loopback by default for local dev.
- Require `Authorization: Bearer <token>` for all non-health endpoints.
- Do not expose this API publicly.

## Integration posture (future)

- Windows Hapa Node can add a provider family like:
  - `Home Nexus (Mac)`
  - with health/capabilities/jobs/assets
- Later: add mDNS discovery + TXT record `api_version`.

## Operational principles

- **Do not lie** about status: explicit stage + progress.
- Prefer a **single worker** by default (memory safety), then scale cautiously.
- Keep logs structured; do not store unbounded logs in memory.
