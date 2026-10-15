"""OAuth2 client credentials (local stand-in for API Gateway + Secret Manager), rate limit, fault injection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware

from factory.adapters.ports import Stores
from factory.contracts import Blueprint

TOKEN_TTL_S = 3600


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message, "is_synthetic": True})


class ClientRegistry:
    """One API client per tenant. The secret is stored as a blob (stand-in for Secret Manager);
    callers receive only its reference."""

    def __init__(self, bp: Blueprint, stores: Stores) -> None:
        self.client_id = f"{bp.tenant.id}-agent"
        self.secret = hashlib.sha256(f"{bp.repeatability.root_seed}:{bp.tenant.id}:client".encode()).hexdigest()[:32]
        self.secret_ref = stores.blobs.put(
            "secrets/api-client.json",
            json.dumps({"client_id": self.client_id, "client_secret": self.secret}).encode(),
            "application/json",
        )
        self.tokens: dict[str, tuple[str, float]] = {}

    def issue(self, client_id: str, client_secret: str) -> str:
        if not (secrets.compare_digest(client_id, self.client_id) and secrets.compare_digest(client_secret, self.secret)):
            raise error(401, "INVALID_CLIENT", "Unknown client or wrong secret.")
        token = secrets.token_urlsafe(24)
        self.tokens[token] = (client_id, time.time() + TOKEN_TTL_S)
        return token

    def client_for(self, token: str) -> str:
        found = self.tokens.get(token)
        if not found or found[1] < time.time():
            raise error(401, "INVALID_TOKEN", "Missing, unknown or expired bearer token.")
        return found[0]


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, client: str) -> None:
        now, q = time.time(), self.hits[client]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.per_minute:
            raise error(429, "RATE_LIMITED", f"Limit is {self.per_minute} requests per minute per client.")
        q.append(now)


bearer = HTTPBearer(auto_error=False)


def require_client(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if cred is None:
        raise error(401, "INVALID_TOKEN", "Missing bearer token.")
    client = request.app.state.clients.client_for(cred.credentials)
    request.app.state.limiter.check(client)
    return client


class FaultInjection(BaseHTTPMiddleware):
    """Seeded error and latency injection on /v1 business endpoints (blueprint api_spec.fault_injection)."""

    def __init__(self, app, error_rate_pct: float, latency_p95_ms: int, seed: int, enabled: bool) -> None:
        super().__init__(app)
        self.error_rate = error_rate_pct / 100
        self.latency_s = latency_p95_ms / 1000
        self.rng = random.Random(seed)
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if self.enabled and request.url.path.startswith("/v1/") and request.headers.get("x-fault-injection") != "off":
            if self.rng.random() < self.error_rate:
                return JSONResponse({"error": "INJECTED_FAULT", "message": "Simulated upstream failure.", "is_synthetic": True},
                                    status_code=503)
            if self.rng.random() < 0.05:  # the slow tail: ~5% of calls near the p95 target
                await asyncio.sleep(self.latency_s)
        return await call_next(request)
