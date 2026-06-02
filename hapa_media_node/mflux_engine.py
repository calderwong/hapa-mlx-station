from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from .config import Settings


class MfluxError(RuntimeError):
    pass


def _resolve_mflux_tool(settings: Settings, tool_name: str) -> str:
    base = Path(settings.mflux_bin)
    if base.is_absolute() or len(base.parts) > 1:
        candidate = base.with_name(tool_name)
        if candidate.exists():
            return str(candidate)
    return tool_name


def _append_common_args(
    cmd: list[str],
    *,
    prompt: str,
    negative_prompt: Optional[str],
    model: str,
    base_model: Optional[str],
    lora_style: Optional[str],
    lora_paths: Optional[list[str]],
    lora_scales: Optional[list[float]],
    steps: int,
    seed: Optional[int],
    width: Optional[int],
    height: Optional[int],
    quantize: Optional[int],
    guidance: Optional[float],
    low_ram: bool,
    output_path: Path,
) -> None:
    cmd.extend(["--model", model])

    if base_model:
        cmd.extend(["--base-model", base_model])

    if lora_style:
        cmd.extend(["--lora-style", lora_style])

    if lora_paths:
        cmd.append("--lora-paths")
        cmd.extend([str(p) for p in lora_paths])

    if lora_scales:
        cmd.append("--lora-scales")
        cmd.extend([str(s) for s in lora_scales])

    cmd.extend(["--prompt", prompt])

    if negative_prompt:
        cmd.extend(["--negative-prompt", negative_prompt])

    cmd.extend(["--steps", str(steps)])

    if width is not None:
        cmd.extend(["--width", str(width)])

    if height is not None:
        cmd.extend(["--height", str(height)])

    if seed is not None:
        cmd.extend(["--seed", str(seed)])

    if quantize is not None:
        cmd.extend(["--quantize", str(quantize)])

    if guidance is not None:
        cmd.extend(["--guidance", str(guidance)])

    if low_ram:
        cmd.append("--low-ram")

    cmd.extend(["--output", str(output_path)])
    cmd.append("--metadata")


def build_mflux_command(
    settings: Settings,
    *,
    mode: str,
    prompt: str,
    negative_prompt: Optional[str],
    model: str,
    base_model: Optional[str],
    lora_style: Optional[str],
    lora_paths: Optional[list[str]],
    lora_scales: Optional[list[float]],
    steps: int,
    seed: Optional[int],
    width: Optional[int],
    height: Optional[int],
    quantize: Optional[int],
    guidance: Optional[float],
    low_ram: bool,
    output_path: Path,
    image_path: Optional[Path] = None,
    masked_image_path: Optional[Path] = None,
    depth_image_path: Optional[Path] = None,
    controlnet_image_path: Optional[Path] = None,
    redux_image_paths: Optional[list[Path]] = None,
    image_strength: Optional[float] = None,
    controlnet_strength: Optional[float] = None,
    redux_image_strengths: Optional[list[float]] = None,
) -> list[str]:
    mode = (mode or "txt2img").strip()

    model = (model or "").strip()

    if mode in {"txt2img", "img2img"}:
        tool = settings.mflux_bin
        if model == "z-image-turbo":
            tool = _resolve_mflux_tool(settings, "mflux-generate-z-image-turbo")
        elif model == "fibo":
            tool = _resolve_mflux_tool(settings, "mflux-generate-fibo")
        cmd: list[str] = [tool]
    elif mode == "fill":
        cmd = [_resolve_mflux_tool(settings, "mflux-generate-fill")]
    elif mode == "depth":
        cmd = [_resolve_mflux_tool(settings, "mflux-generate-depth")]
    elif mode == "controlnet":
        cmd = [_resolve_mflux_tool(settings, "mflux-generate-controlnet")]
    elif mode == "redux":
        cmd = [_resolve_mflux_tool(settings, "mflux-generate-redux")]
    elif mode == "upscale":
        cmd = [_resolve_mflux_tool(settings, "mflux-upscale")]
    else:
        raise ValueError(f"Unsupported mflux mode: {mode}")

    _append_common_args(
        cmd,
        prompt=prompt,
        negative_prompt=negative_prompt,
        model=model,
        base_model=base_model,
        lora_style=lora_style,
        lora_paths=lora_paths,
        lora_scales=lora_scales,
        steps=steps,
        seed=seed,
        width=width,
        height=height,
        quantize=quantize,
        guidance=guidance,
        low_ram=low_ram,
        output_path=output_path,
    )

    if mode == "img2img":
        if image_path is None:
            raise ValueError("image_path is required for img2img")
        cmd.extend(["--image-path", str(image_path)])
        if image_strength is not None:
            cmd.extend(["--image-strength", str(image_strength)])

    if mode == "fill":
        if image_path is None:
            raise ValueError("image_path is required for fill")
        if masked_image_path is None:
            raise ValueError("masked_image_path is required for fill")
        cmd.extend(["--image-path", str(image_path)])
        cmd.extend(["--masked-image-path", str(masked_image_path)])

    if mode == "depth":
        if image_path is None:
            raise ValueError("image_path is required for depth")
        cmd.extend(["--image-path", str(image_path)])
        if depth_image_path is not None:
            cmd.extend(["--depth-image-path", str(depth_image_path)])

    if mode in {"controlnet", "upscale"}:
        if controlnet_image_path is None:
            raise ValueError("controlnet_image_path is required")
        cmd.extend(["--controlnet-image-path", str(controlnet_image_path)])
        if controlnet_strength is not None:
            cmd.extend(["--controlnet-strength", str(controlnet_strength)])

    if mode == "redux":
        if not redux_image_paths:
            raise ValueError("redux_image_paths is required")
        cmd.append("--redux-image-paths")
        cmd.extend([str(p) for p in redux_image_paths])
        if redux_image_strengths:
            cmd.append("--redux-image-strengths")
            cmd.extend([str(s) for s in redux_image_strengths])

    return cmd


def run_mflux_generate(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "mflux failed").strip()
        raise MfluxError(msg)

    return {"stdout": proc.stdout, "stderr": proc.stderr}
