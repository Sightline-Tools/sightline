from __future__ import annotations

import hmac
import ipaddress
import os
import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response


COOKIE_NAME = "sightline_session"
PUBLIC_API_PATHS = {"/api/health"}


def create_session_secret() -> str:
    return os.environ.get("SIGHTLINE_SESSION_SECRET") or secrets.token_urlsafe(32)


def valid_secret(candidate: str | None, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(str(candidate), expected)


class LocalAPISecurityMiddleware(BaseHTTPMiddleware):
    """Require a launcher-established cookie for the loopback API."""

    def __init__(self, app, *, session_secret: str, allow_unauthenticated_loopback: bool = False) -> None:
        super().__init__(app)
        self._session_secret = session_secret
        self._allow_unauthenticated_loopback = allow_unauthenticated_loopback

    @staticmethod
    def _is_loopback_request(request: Request) -> bool:
        if request.client is None:
            return False
        try:
            return ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            return request.client.host.lower() == "localhost"

    @staticmethod
    def _is_loopback_host(request: Request) -> bool:
        """Return whether the Host header names this computer's loopback interface."""
        if not request.headers.get("host"):
            return False
        try:
            hostname = request.url.hostname
        except ValueError:
            return False
        if hostname is None:
            return False
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return hostname.lower() == "localhost"

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            return await call_next(request)

        unauthenticated_loopback = (
            self._allow_unauthenticated_loopback
            or os.environ.get("SIGHTLINE_DISABLE_AUTH") == "1"
        )
        if unauthenticated_loopback and (
            not self._is_loopback_request(request) or not self._is_loopback_host(request)
        ):
            return JSONResponse(
                {"detail": "Unauthenticated source mode is available only from this computer."},
                status_code=403,
            )

        if not unauthenticated_loopback and not valid_secret(request.cookies.get(COOKIE_NAME), self._session_secret):
            return JSONResponse({"detail": "Open Sightline from its launcher or tray icon."}, status_code=401)

        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if origin and (not host or origin.rstrip("/") != f"http://{host}"):
            return JSONResponse({"detail": "Cross-origin API requests are not allowed."}, status_code=403)
        return await call_next(request)

