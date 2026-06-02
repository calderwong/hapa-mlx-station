# Hapa.ai Context Summary (from `hapa-og/docs`)

## What Hapa AG is (high-level)

Hapa AG is an **Electron desktop app** that is:

- **Local-first** (storage and media live on the operator machine)
- **Provider-agnostic** (Gemini / Vertex / OpenAI / local llama.cpp)
- **Card-centric** (everything becomes reusable cards; lineage/provenance matters)
- **Multimedia capable** (image/video/audio, wormhole-derived artifacts)

Key implemented areas:

- **Card Library** (core UX)
- **Hell Week Pipeline** (document -> cards -> media)
- **Wormhole processing** (summaries, key terms, wiki, transcripts)
- **Local AI** for chat (llama.cpp) and planned local vision via a Python Bridge

## What matters for the Silicon Nexus / Mac-side node

### Intent (from workstreams)

Mac Studio “Silicon Nexus” is treated as a **compute lighthouse**:

- Headless or semi-headless service
- Heavy local inference workloads
- Exposes a stable **LAN API contract** to other nodes

### Boundary contract (minimum recommended)

A stable, versioned bridge between:

- Windows Hapa Node (Electron) and
- Mac Silicon Nexus server (HTTP+JSON over LAN)

Minimum endpoints implied by `WORKSTREAMS.md`:

- `GET /health`
- `GET /capabilities`
- Async jobs:
  - `POST /generate/video` -> `202 { task_id }`
  - `GET /tasks/{task_id}` -> status/progress/result
- Asset retrieval:
  - `GET /assets/{asset_id}`
  - `GET /assets/{asset_id}/download`

### Protocol alignment

Operational invariants we must preserve:

- **Integrity > Flow > Form > Decoration**
- **Never assume. Validate from primary sources**
- **Truth hierarchy**:
  - Hypercore/P2P = network truth
  - SQLite = fast local query truth (but never pretend it’s complete when partial)
- **UI / APIs must not lie**:
  - task status must be explicit and observable
  - no “phantom completion” or hidden partial results
