"""OAuth2 client credentials (local stand-in for API Gateway + Secret Manager), rate limit, fault injection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware

from factory.adapters.ports import Stores
from factory.api_engine.errors import ErrorCode, error, error_body

TOKEN_TTL_S = 3600


class ClientRegistry:
    """One API client per tenant. The secret is stored as a blob (stand-in for Secret Manager);
    callers receive only its reference."""

    def __init__(self, tenant_id: str, seed: int, stores: Stores) -> None:
        self.client_id = f"{tenant_id}-agent"
        self.secret = hashlib.sha256(f"{seed}:{tenant_id}:client".encode()).hexdigest()[:32]
        self.secret_ref = stores.blobs.put(
            "secrets/api-client.json",
            json.dumps({"client_id": self.client_id, "client_secret": self.secret}).encode(),
            "application/json",
        )
        self.tokens: dict[str, tuple[str, float]] = {}

    def issue(self, client_id: str, client_secret: str) -> str:
        """Client-credentials grant: check id and secret (constant time) and issue a random bearer token valid for 1 hour."""
        if not (secrets.compare_digest(client_id, self.client_id) and secrets.compare_digest(client_secret, self.secret)):
            raise error(ErrorCode.INVALID_CLIENT, "Unknown client or wrong secret.")
        token = secrets.token_urlsafe(24)
        self.tokens[token] = (client_id, time.time() + TOKEN_TTL_S)
        return token

    def client_for(self, token: str) -> str:
        """Client id of a valid, unexpired token, else 401. Tokens are held in memory, per process."""
        found = self.tokens.get(token)
        if not found or found[1] < time.time():
            raise error(ErrorCode.INVALID_TOKEN, "Missing, unknown or expired bearer token.")
        return found[0]


class RateLimiter:
    """Sliding one-minute window of request times per client; over the limit -> 429. Per process, like the tokens."""
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, client: str) -> None:
        now, q = time.time(), self.hits[client]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.per_minute:
            raise error(ErrorCode.RATE_LIMITED, f"Limit is {self.per_minute} requests per minute per client.")
        q.append(now)


bearer = HTTPBearer(auto_error=False)


def require_client(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    """FastAPI dependency on every /v1 endpoint: valid bearer token, then the rate limit. Returns the client id."""
    if cred is None:
        raise error(ErrorCode.INVALID_TOKEN, "Missing bearer token.")
    client = request.app.state.clients.client_for(cred.credentials)
    request.app.state.limiter.check(client)
    return client


class FaultInjection(BaseHTTPMiddleware):
    """Seeded error and latency injection on /v1 business endpoints (s8 payload fault_injection)."""

    def __init__(self, app, error_rate_pct: float, latency_p95_ms: int, seed: int, enabled: bool) -> None:
        super().__init__(app)
        self.error_rate = error_rate_pct / 100
        self.latency_s = latency_p95_ms / 1000
        self.rng = random.Random(seed)
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if self.enabled and request.url.path.startswith("/v1/") and request.headers.get("x-fault-injection") != "off":
            if self.rng.random() < self.error_rate:
                return JSONResponse(error_body(ErrorCode.INJECTED_FAULT, "Simulated upstream failure."),
                                    status_code=ErrorCode.INJECTED_FAULT.status)
            if self.rng.random() < 0.05:  # the slow tail: ~5% of calls near the p95 target
                await asyncio.sleep(self.latency_s)
        return await call_next(request)
