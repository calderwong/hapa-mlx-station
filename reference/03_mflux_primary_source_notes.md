# mflux — Primary Source Notes (GitHub: filipstrand/mflux)

Source: https://github.com/filipstrand/mflux

## What it is

- MLX port of diffusion image models:
  - FLUX
  - Qwen Image
  - Bria / FIBO
  - Z-Image-Turbo
- Minimal / explicit implementation: architectures are hardcoded; tokenizers via HF Transformers.

## Installation (primary source)

- Via `uv` tool:
  - `uv tool install --upgrade mflux --prerelease=allow`
- Or via pip:
  - `pip install -U mflux`

## CLI entry points (examples)

- Generate (Flux):
  - `mflux-generate --model schnell --prompt "..." --steps 2 --seed 2 -q 8`
  - `mflux-generate --model dev --prompt "..." --steps 25 --seed 2 -q 8`
- Generate (Qwen):
  - `mflux-generate-qwen --prompt "..." --steps 20 --seed 2 -q 6`

## Cache locations

- mflux cache (default): `~/Library/Caches/mflux/`
- Hugging Face model cache (separate): `~/.cache/huggingface/`

Config via env vars:

- `MFLUX_CACHE_DIR` — set mflux cache dir
- `HF_HOME` — set Hugging Face cache dir

## Quantization

- `--quantize` / `-q` supports: 3, 4, 5, 6, 8-bit
- `--low-ram` option reduces memory usage on constrained machines
- `mflux-save` can save quantized weights to disk for reuse

## Third-party model support

`--model` can accept:

- predefined names (`dev`, `schnell`, `fibo`, `z-image-turbo`, ...)
- Hugging Face repos (example: `Freepik/flux.1-lite-8B`)
- local paths (example: `/path/to/models/my-model`)

## Python usage

The README shows Flux python usage via `Flux1.from_name(...)` and `generate_image(...)`.

## Z-Image (important)

- mflux supports Z-Image-Turbo and provides a dedicated CLI.
- Z-Image is fast (~9 steps) but model weights are large; quantization is important.

## Implications for Hapa Media Gen Node

- The node should expose:
  - model selection
  - quantization controls
  - cache directory controls (so we can place caches on external storage)
- The node should default to safe, small-footprint configs:
  - `schnell` + low steps + quantization
  - or pre-quantized weights when available
