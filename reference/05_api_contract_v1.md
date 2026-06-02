#+#+#+#+ # API Contract v1 — Hapa Media Gen Node (+ Hub)

## Auth

- All endpoints except `/health` require:
  - `Authorization: Bearer <token>`
- Optional (disabled by default): `?token=<token>`
  - Enable globally: `HAPA_MEDIA_ALLOW_QUERY_TOKEN=1`
  - Enable node only: `HAPA_MEDIA_NODE_ALLOW_QUERY_TOKEN=1`
  - Enable hub only: `HAPA_MEDIA_HUB_ALLOW_QUERY_TOKEN=1`

## Response envelope

- Responses include:
  - `api_version`
  - `time`
- Some responses also include:
  - `service`

## Endpoints

### `GET /health` (public)

Response:

```json
{ "ok": true, "service": "hapa-media-gen-node", "api_version": "v1", "time": "..." }
```

### `GET /capabilities` (auth)

Response:

```json
{
  "api_version": "v1",
  "time": "...",
  "service": "hapa-media-gen-node",
  "modalities": {
    "image": {
      "engines": ["mflux"],
      "models": ["schnell", "dev", "krea-dev", "z-image-turbo", "fibo"],
      "modes": ["txt2img", "img2img", "fill", "depth", "controlnet", "redux", "upscale"],
      "features": [
        "txt2img",
        "img2img",
        "fill",
        "depth",
        "controlnet",
        "redux",
        "upscale",
        "base64_inputs",
        "negative_prompt",
        "lora",
        "quantize",
        "base_model",
        "third_party_models",
        "guidance",
        "low_ram",
        "metadata"
      ],
      "input_fields_base64": [
        "image_base64",
        "masked_image_base64",
        "depth_image_base64",
        "controlnet_image_base64",
        "redux_images_base64"
      ],
      "mode_inputs_base64": {
        "txt2img": { "required": [], "optional": [] },
        "img2img": { "required": ["image_base64"], "optional": [] },
        "fill": { "required": ["image_base64", "masked_image_base64"], "optional": [] },
        "depth": { "required": ["image_base64"], "optional": ["depth_image_base64"] },
        "controlnet": { "required": ["controlnet_image_base64"], "optional": [] },
        "redux": { "required": ["redux_images_base64"], "optional": [] },
        "upscale": { "required": ["controlnet_image_base64"], "optional": [] }
      },
      "lora_fields": ["lora_style", "lora_paths", "lora_scales"],
      "allowed_quantize": [3, 4, 5, 6, 8],
      "default_steps": {
        "schnell": 2,
        "dev": 25,
        "krea-dev": 25,
        "z-image-turbo": 9,
        "fibo": 20
      },
      "third_party_model_support": { "supported": true, "requires_base_model": true }
    }
  }
}
```

### `POST /v1/images/generations` (auth)

Request (MVP subset):

```json
{
  "mode": "txt2img",
  "prompt": "A photo of a dog",
  "negative_prompt": "...",
  "model": "schnell",
  "base_model": "...",
  "steps": 2,
  "seed": 42,
  "width": 1024,
  "height": 1024,
  "quantize": 8,
  "guidance": 3.5,
  "low_ram": false,
  "lora_style": "storyboard",
  "lora_paths": ["/path/to/lora.safetensors"],
  "lora_scales": [0.8]
}
```

Response:

```json
{ "api_version": "v1", "time": "...", "task_id": "uuid" }
```

### `GET /v1/tasks/{task_id}` (auth)

Response:

```json
{
  "api_version": "v1",
  "time": "...",
  "task_id": "uuid",
  "status": "running",
  "stage": "generating",
  "progress": 0.5,
  "error": null,
  "result": null
}
```

Succeeded example:

```json
{
  "api_version": "v1",
  "time": "...",
  "task_id": "uuid",
  "status": "succeeded",
  "stage": "complete",
  "progress": 1,
  "error": null,
  "result": {
    "asset_id": "uuid",
    "download_url": "/v1/assets/uuid/download"
  }
}
```

### `GET /v1/assets/{asset_id}` (auth)

Response:

```json
{
  "api_version": "v1",
  "time": "...",
  "asset_id": "uuid",
  "type": "image/png",
  "path": "artifacts/uuid.png",
  "created_at": "...",
  "metadata": { "model": "schnell", "steps": 2, "seed": 42 }
}
```

### `GET /v1/assets/{asset_id}/download` (auth)

- Returns raw bytes with appropriate `Content-Type`.

## Hub API (router)

- The hub exposes the same endpoints/shape as a node, but routes requests to configured backend nodes.
- Hub `GET /capabilities` adds a `nodes` section:
  - `nodes.{node_id}.ok: true|false`
  - If `ok: true`, includes `nodes.{node_id}.capabilities` (the node's full `/capabilities` response)
  - If `ok: false`, includes `nodes.{node_id}.error`
- Hub uses **composite IDs** in the form `node_id:inner_id`:
  - `POST /v1/images/generations` returns `task_id: "a:uuid"`
  - `GET /v1/tasks/{task_id}` expects a composite `task_id` and rewrites `result.asset_id` + `download_url` to composite IDs
  - `GET /v1/assets/{asset_id}` and `/download` expect composite `asset_id`
