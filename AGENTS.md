# Hapa MLX Station Agent Guide

## Node Role

`hapa-mlx-station` is Hapa's Apple Silicon media-generation and local model operations station. It owns the FastAPI media node/hub, media task queue, local artifact registry, MLX/mflux integration, and an embedded keys-node surface for provider-backed calls.

## Source Of Truth

- `README.md` defines the verified node role, ports, auth, API, and verification commands.
- `hapa_media_node/` owns the media node, hub, queue, storage, worker, and browser UI.
- `hapa_keys_node/` owns the local secret/key proxy surface included in this station.
- `reference/` contains API contracts, multi-machine setup notes, and design context.
- `docs/PUBLICATION_BOUNDARY.md` defines what belongs in the public source repo versus `hapa-vault`.
- `tests/` and root verification scripts capture current smoke/self-test behavior.
- `SELF_TEST_PROTOCOL.md`, when present, is the local protocol note for station self-tests.

## Safe Edit Boundaries

- Keep node, hub, and keys surfaces loopback-first unless the operator explicitly approves broader network exposure.
- Do not commit tokens, `.env`, `.node_token`, generated SQLite files, media artifacts, model weights, LoRA files, Hugging Face credentials, generated images, or real smoke outputs.
- Treat root-level ad hoc test files as pre-publication review targets; inspect them for embedded token strings before publishing.
- Preserve task IDs, asset IDs, provenance, prompt metadata, model names, seeds, and generation parameters when moving artifacts into `hapa-vault`.
- Keep public docs honest about runtime generation: real MLX/mflux success requires local dependencies, model access, and configured tokens.

## Hapa Connectivity

- Reads prompts, source images, presets, local model settings, and optional provider keys.
- Produces task records, media artifacts, thumbnails, generation metadata, and hub routing telemetry.
- Related nodes: `hapa-keys-node`, `hapa-telemetry-node`, `hapa-lance-node`, `hapa_second_brain`, `hapa-song-registry`, Hapa wiki, and Overwatch operations.
- Source code belongs in `hapa-system`; generated assets, model/runtime outputs, DB snapshots, and artifact manifests belong in `hapa-vault`.

## Verification

```bash
python3 -m compileall -q hapa_media_node hapa_keys_node tests test_connection.py test_new_features.py test_preview.py verify_preview_features.py scripts/multi_node_hub_smoke_test.py
python3 -m unittest discover -s tests
python3 -m hapa_media_node --help
```

Runtime media checks require configured dependencies and tokens. Prefer self-test or dry-run evidence before changing publication status.
