# Hapa MLX Station Publication Boundary

This repo is the source package for Hapa's Apple Silicon media-generation station. It should be publishable as code, docs, protocol, and local-first node scaffolding without carrying private runtime state.

## Keep In Source

- `hapa_media_node/`: FastAPI media node, hub/router, queue, SQLite schema code, worker, web UI, CLI, port manager, and self-test harness.
- `hapa_keys_node/`: loopback-first key vault/proxy service, CLI, web UI, and self-test harness.
- `reference/`: source-level API contracts, multi-machine setup notes, and design notes that do not expose private paths or real tokens.
- `tests/`: unit and smoke tests that use temp dirs, env tokens, or placeholders.
- `docs/`: connectivity notes, screenshots intended for docs, security guidance, and this boundary contract.
- `.env.example`, `hub_nodes.example.json`, and other placeholder config examples.

## Keep Out Of Source

- `.env`, `.node_token`, bearer tokens, provider API keys, Hugging Face auth, cookies, and credential JSON.
- `data/`, `data_node*/`, `data_selftest-*/`, SQLite databases, WAL/SHM sidecars, task stores, queues, and local token files.
- Generated images, smoke-test PNGs, task JSON, private source images, model weights, LoRA files, checkpoints, benchmark captures, and downloaded model artifacts.
- Root-level ad hoc operator scripts that contain real local bearer tokens or live-machine instructions.

## Vault And Transfer Shape

- Heavy media artifacts and DB snapshots belong in `hapa-vault`, not git.
- Portable manifests should preserve task IDs, asset IDs, prompts where approved, model names, seeds, dimensions, generation parameters, timestamps, source node IDs, and destination wiki/card references.
- Hypercore transfer batches should move vault manifests and binaries together, with repo commits referencing only stable manifest IDs or vault-relative paths.

## Pre-Publication Checklist

```bash
git status --short
git ls-files | grep -Ei '(^|/)(\.env($|\.)|\.node_token$|.*\.pem$|.*\.key$|id_rsa|id_ed25519|.*credentials\.(json|yaml|yml|toml|ini)|.*secrets\.(json|yaml|yml|toml|ini))'
python3 -m compileall -q hapa_media_node hapa_keys_node tests
python3 -m unittest discover -s tests
```

Runtime self-tests are useful evidence, but they should write reports into ignored runtime locations or `hapa-vault`, not into the source repo.
