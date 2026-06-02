# Multi-machine setup (LAN / remote)

This document describes how to run multiple `hapa-media-gen-node` instances on different machines and route requests through a `hapa-media-hub`.

## Network model

- A **node** runs on each machine that can do image generation.
- A **hub** runs on one machine and forwards requests to nodes.
- Clients talk to the hub (recommended) or directly to a node.

All protected endpoints require a token:

- Header: `Authorization: Bearer <token>`
- Optional (disabled by default): query param `?token=<token>`
  - Enable globally: `HAPA_MEDIA_ALLOW_QUERY_TOKEN=1`
  - Enable node only: `HAPA_MEDIA_NODE_ALLOW_QUERY_TOKEN=1`
  - Enable hub only: `HAPA_MEDIA_HUB_ALLOW_QUERY_TOKEN=1`

## Run a node on a LAN address

By default the node binds to `127.0.0.1` (localhost). To make it reachable from other machines on your LAN, bind to `0.0.0.0` (or a specific LAN IP).

Example (Mac/Linux):

```bash
export HAPA_MEDIA_NODE_HOST=0.0.0.0
export HAPA_MEDIA_NODE_PORT=8723
export HAPA_MEDIA_NODE_TOKEN='node_token_a'
export HAPA_MEDIA_NODE_STORAGE_DIR=/absolute/path/to/data

python3 -m hapa_media_node serve
```

Example (Windows PowerShell):

```powershell
$env:HAPA_MEDIA_NODE_HOST = "0.0.0.0"
$env:HAPA_MEDIA_NODE_PORT = "8723"
$env:HAPA_MEDIA_NODE_TOKEN = "node_token_b"
$env:HAPA_MEDIA_NODE_STORAGE_DIR = "C:\path\to\data"

python -m hapa_media_node serve
```

Notes:

- Each node should have its **own** `HAPA_MEDIA_NODE_STORAGE_DIR`.
- Use different `HAPA_MEDIA_NODE_PORT` per node if you run multiple nodes on the same host.

## Run the hub

The hub needs:

- A hub listen host/port/token
- A list of nodes (`HAPA_MEDIA_HUB_NODES` or `HAPA_MEDIA_HUB_NODES_FILE`)

### Option A: inline JSON in `HAPA_MEDIA_HUB_NODES`

```bash
export HAPA_MEDIA_HUB_HOST=0.0.0.0
export HAPA_MEDIA_HUB_PORT=8726
export HAPA_MEDIA_HUB_TOKEN='hub_token'

export HAPA_MEDIA_HUB_NODES='[
  {"id":"a","base_url":"http://192.168.1.10:8723","token":"node_token_a"},
  {"id":"b","base_url":"http://192.168.1.11:8723","token":"node_token_b"}
]'

python3 -m hapa_media_node hub
```

### Option B: JSON file with `HAPA_MEDIA_HUB_NODES_FILE`

Create `hub_nodes.json`:

```json
[
  {"id": "a", "base_url": "http://192.168.1.10:8723", "token": "node_token_a"},
  {"id": "b", "base_url": "http://192.168.1.11:8723", "token": "node_token_b"}
]
```

Run:

```bash
export HAPA_MEDIA_HUB_HOST=0.0.0.0
export HAPA_MEDIA_HUB_PORT=8726
export HAPA_MEDIA_HUB_TOKEN='hub_token'
export HAPA_MEDIA_HUB_NODES_FILE=/absolute/path/to/hub_nodes.json

python3 -m hapa_media_node hub
```

## Validate connectivity

From another machine on the LAN:

- Node health:

```bash
curl -H 'Authorization: Bearer ${HAPA_NODE_TOKEN}' http://192.168.1.10:8723/health
```

- Hub capabilities (includes per-node status under `nodes`):

```bash
curl -H 'Authorization: Bearer ${HAPA_HUB_TOKEN}' http://192.168.1.10:8726/capabilities
```

## Routing + failover behavior

- The hub selects an eligible node via round-robin based on the request `mode`.
- The hub returns **composite IDs**:
  - `task_id`: `<node_id>:<inner_task_id>`
  - `asset_id`: `<node_id>:<inner_asset_id>`

If the chosen node fails during `POST /v1/images/generations` (unavailable or 5xx), the hub will retry the request on another eligible node (if available).

## Basic security recommendations

- Treat tokens like passwords.
- Avoid exposing the node/hub directly to the public internet.
- Prefer one of:
  - Run only on a trusted LAN.
  - Put access behind a VPN.
  - Use an SSH tunnel for ad-hoc access:

```bash
ssh -L 8726:127.0.0.1:8726 user@hub-machine
```

- If you need TLS, terminate TLS in a reverse proxy (e.g. nginx/Caddy) in front of the hub, and restrict direct access to the hub port via firewall rules.
