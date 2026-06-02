# Hapa Media Node — Self-Test Protocol (Hub/Node/CLI/UI)

This document defines the **automated self-test harness** for the Hapa Media Hub + Nodes.

## Goals

- Validate hub responsiveness and auth.
- Validate node spawning (local, managed) and cleanup.
- Validate **multi-node routing** (round-robin) by queuing multiple generations.
- Validate **image generation pipeline** end-to-end.
- Validate **asset download** from the hub (composite IDs).

## What “passing” means

A self-test run is considered **PASS** when:

- `/health` returns `{ ok: true }`.
- `/capabilities` returns a dict with `nodes` entries.
- If `spawn_nodes > 0`, at least one node is spawned successfully.
- `copies >= 2` implies:
  - There are at least 2 eligible nodes for the requested `mode`, **and**
  - The queued tasks complete on at least **2 distinct node_ids**.
- All generated tasks reach `status == "succeeded"`.
- Every succeeded task has a `result.asset_id` and the corresponding `/v1/assets/{asset_id}/download` returns non-empty bytes.
- If `cleanup == true`, every spawned node is terminated + removed via admin API.

The self-test produces a **single JSON report** with per-step outcomes and timing.

## Entry points (lockstep)

### 1) CLI

Run against a hub:

- `python -m hapa_media_node self-test --base-url http://127.0.0.1:8723 --token <TOKEN>`

Useful options:

- `--copies 2` (forces multi-node routing checks)
- `--spawn-nodes 1` (default)
- `--no-cleanup` (debugging only)
- `--output self_test_report.json` (save report)

Exit code:

- `0` if `report.ok == true`
- `1` otherwise

### 2) Hub Admin API

- `POST /v1/admin/self-test`

Requires:

- Admin API enabled (not disabled via env vars)
- `Authorization: Bearer <HUB_TOKEN>`

Request body (`SelfTestRequest`):

- `mode` (default `txt2img`)
- `model` (default `schnell`)
- `steps` (default `2`)
- `copies` (default `2`)
- `spawn_nodes` (default `1`)
- `spawn_max_workers` (default `1`)
- `cleanup` (default `true`)
- `startup_timeout_seconds` (default `30`)
- `timeout_seconds` (default `600`)
- `poll_interval_seconds` (default `1`)

Response:

- The JSON report (same shape as CLI output)

### 3) Hub UI

In the **Nodes** panel:

- Click **Run Self-Test**
- The UI:
  - Calls `POST /v1/admin/self-test`
  - Displays PASS/FAIL in the Nodes admin hint line
  - Stores the JSON into the “Self-Test JSON” textarea
  - Allows copying via **Copy Self-Test JSON**

## Observability / Signals

The JSON report is designed to be machine-checked.

Key fields:

- `ok`: overall boolean
- `run_id`: correlation id
- `steps_results[]`: step-by-step results with:
  - `name`
  - `ok`
  - `duration_seconds`
  - optional `error`
  - optional `data`
- `tasks[]`: generated task summary including `task_id`, `node_id`, `asset_id`
- `downloads[]`: asset download probe results

Recommended automated confidence checks:

- **Step integrity**: every entry in `steps_results` has `ok == true`
- **Timing**: alert if `generate.duration_seconds` or `download_assets.duration_seconds` exceeds expected thresholds
- **Routing**: alert if `copies >= 2` and fewer than 2 unique `node_id` values are seen
- **Cleanup**: alert if cleanup step reports errors

## Suggested usage patterns

- Before any manual UI testing:
  - Run CLI self-test once
  - Run UI self-test once
- After changes to hub routing, node spawning, or generation engine:
  - Run CLI self-test with `--copies 2 --spawn-nodes 1`
- For CI:
  - Use dummy generation mode on nodes (if configured in your environment)
  - Run self-test against ephemeral hub/node stack
