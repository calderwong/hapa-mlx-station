# CAMPFIRE — Hapa MLX Station

## Node intent

Hapa MLX Station is the local Apple Silicon media forge for Hapa.ai. It gives Hapa agents and apps a loopback-first way to turn prompts and image inputs into local image artifacts, while preserving an inspectable node contract: health, capabilities, bearer-token auth, task state, SQLite truth, and artifact downloads.

## Verified surfaces in this repository

- Media node: `hapa_media_node/app.py`
- Media hub/router: `hapa_media_node/hub_app.py`
- Media CLI: `hapa_media_node/cli.py`
- Keys/proxy node: `hapa_keys_node/server.py`
- API contract notes: `reference/05_api_contract_v1.md`
- Multi-machine notes: `reference/06_multi_machine_setup.md`
- Test/smoke surfaces: `tests/`, `scripts/multi_node_hub_smoke_test.py`, `SELF_TEST_PROTOCOL.md`

## Operating boundary

- Default media node port: `8723`.
- Common hub port when avoiding local node conflict: `8726`.
- Local stack defaults: hub `8723`, nodes from `8724`.
- Keys node port: `8733`.
- Public: `/health` and UI `/` where present.
- Authenticated: `/capabilities` and `/v1/*`.
- Tokens are bearer tokens. Generated local token files and runtime data are operational secrets/state, not source artifacts.

## Hapa links

- Global wiki: `[[Nodes/Existing/hapa-mlx-station]]`
- Node index: `[[Nodes/Index]]`
- Overwatch registry: `[[Operations/Overwatch Node Registry and Status Board]]`
- Related capability domains: `[[Capabilities/Index]]`, `[[Systems/Taxonomy v2]]`

## Current truth vs inference

Verified by source inspection: FastAPI services, CLI commands, endpoint names, SQLite/artifact storage, hub composite-ID routing, auth toggles, and test commands.

Inferred ecosystem role: this repo is Hapa's media embodiment station and local compute forge. That role is consistent with the repository name, README/reference docs, Overwatch runbooks, and API surfaces, but production status should be updated only after fresh health/capabilities/self-test evidence.

## Fast checks

```bash
python3 -m compileall -q hapa_media_node hapa_keys_node tests test_connection.py test_new_features.py test_preview.py verify_preview_features.py scripts/multi_node_hub_smoke_test.py
python3 -m unittest discover -s tests
python3 -m hapa_media_node --help
```

## Licensing note

Project code is MIT licensed under Hapa.ai / Calder Wong. Contributors may opt into Bananas work-contribution tracking for attribution/accounting. Preserve all third-party notices and dependency/model license terms.

## Open risks

- Real media generation requires local mflux/model access and should be validated separately from cheap syntax/unit checks.
- Ad hoc root-level scripts contain local token literals and need review before any public distribution.
- Existing active code changes in this repo are broader than this docs/licensing sweep; keep docs grounded in verified commands and sources.
