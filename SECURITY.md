# Hapa MLX Station Security

## Sensitive Data

This repo can touch model credentials, provider API keys, local bearer tokens, prompt text, source images, generated media, model paths, task SQLite rows, and multi-node hub routing data. Treat real tokens, generated artifacts, and runtime databases as private operational data unless the operator explicitly marks them publishable.

## Never Commit

- `.env`, `.env.*`, `.node_token`, provider keys, Hugging Face auth material, cookies, or credential JSON.
- `data/`, generated SQLite files, WAL/SHM sidecars, task stores, media artifacts, and smoke-test output images.
- Model weights, LoRA files, downloaded checkpoints, generated previews, benchmark captures, and private source images.
- Logs that include prompts, bearer tokens, local paths to private source material, or provider responses.

## Runtime Boundaries

- Keep media node, hub, and keys surfaces on `127.0.0.1` by default.
- Query-token auth should remain disabled unless intentionally enabled for a local test.
- Admin APIs should be disabled when not actively operating the station.
- Do not expose generated-asset downloads on a public network without a reviewed auth and retention policy.
- Store portable artifact metadata in vault manifests; store the heavy binaries in `hapa-vault` or a Hypercore transfer batch.

## Pre-Publication Gate

```bash
git status --short
git ls-files | grep -Ei '(^|/)(\\.env($|\\.)|\\.node_token$|.*\\.pem$|.*\\.key$|id_rsa|id_ed25519|.*credentials\\.(json|yaml|yml|toml|ini)|.*secrets\\.(json|yaml|yml|toml|ini))'
find . -maxdepth 3 -type f \\( -name '*.sqlite*' -o -name '*.db*' -o -name '*.png' -o -name '*.jpg' -o -name '*.jpeg' -o -name '*.webp' -o -name '*.safetensors' -o -name '*.ckpt' \\)
python3 -m compileall -q hapa_media_node hapa_keys_node tests
```

If a credential or private generated asset was ever committed, rotate the credential, remove the asset, and rewrite history before public release.
