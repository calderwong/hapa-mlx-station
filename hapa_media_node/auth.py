from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request


def verify_request_token(request: Request, expected_token: str, *, allow_query_token: bool = False) -> None:
    token = None

    auth = request.headers.get("authorization")
    if auth:
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]

    if token is None and allow_query_token:
        token = request.query_params.get("token")

    if not token or token != expected_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


def env_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
