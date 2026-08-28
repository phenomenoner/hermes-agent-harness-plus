#!/usr/bin/env python3
"""Loopback-only OpenAI Codex Responses relay for Prime minions.

The child Prime process receives only a synthetic bearer. This process resolves
Hermes' real OpenAI Codex OAuth credential for each request, replaces all auth
and Codex identity headers, and streams the upstream SSE response unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import logging
import os
import signal
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Any, Optional

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from agent.credential_pool import CredentialPool, PooledCredential, load_pool

_PROVIDER = "openai-codex"
_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
_ALLOWED_PATH = "/v1/codex/responses"
_MAX_REQUEST_BYTES = 10_000_000
_STRIPPED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "authorization",
        "chatgpt-account-id",
        "originator",
        "user-agent",
    }
)
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-encoding",
        "content-length",
    }
)
_LOG = logging.getLogger("prime_minion.bridge")


@dataclass(frozen=True)
class _Credential:
    bearer: str
    base_url: str
    headers: dict[str, str]


def _extract_account_id(access_token: str) -> Optional[str]:
    """Extract the account claim without logging or persisting the token."""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        value = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def _codex_headers(access_token: str) -> dict[str, str]:
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent Prime Minion)",
        "originator": "codex_cli_rs",
    }
    account_id = _extract_account_id(access_token)
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


class _CredentialSource:
    """Resolve and rotate Hermes-managed OpenAI Codex OAuth credentials."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: Optional[CredentialPool] = None

    def is_authenticated(self) -> bool:
        try:
            pool = load_pool(_PROVIDER)
            return bool(pool and pool.has_available())
        except Exception:
            return False

    def get(self) -> _Credential:
        with self._lock:
            pool = load_pool(_PROVIDER)
            if not pool.has_credentials():
                raise RuntimeError(
                    "No Hermes OpenAI Codex OAuth credential. Run "
                    "`hermes auth add openai-codex --type oauth` first."
                )
            entry = pool.select()
            if entry is None:
                raise RuntimeError(
                    "No available Hermes OpenAI Codex OAuth credential; re-authenticate or reset its pool."
                )
            self._pool = pool
            return self._from_entry(entry)

    def retry(self, failed: _Credential, status_code: int) -> Optional[_Credential]:
        if status_code not in {401, 429}:
            return None
        with self._lock:
            pool = self._pool or load_pool(_PROVIDER)
            if status_code == 401:
                entry = pool.try_refresh_current()
                if entry is None:
                    entry = pool.mark_exhausted_and_rotate(status_code=status_code)
            else:
                entry = pool.mark_exhausted_and_rotate(status_code=status_code)
            if entry is None:
                return None
            candidate = self._from_entry(entry)
            if candidate.bearer == failed.bearer:
                return None
            return candidate

    @staticmethod
    def _from_entry(entry: PooledCredential) -> _Credential:
        bearer = str(
            getattr(entry, "runtime_api_key", None)
            or getattr(entry, "access_token", "")
            or ""
        ).strip()
        if not bearer:
            raise RuntimeError("Hermes OpenAI Codex credential has no access token.")
        base_url = str(
            getattr(entry, "runtime_base_url", None)
            or getattr(entry, "base_url", None)
            or _DEFAULT_BASE_URL
        ).strip().rstrip("/")
        if not base_url:
            base_url = _DEFAULT_BASE_URL
        return _Credential(bearer=bearer, base_url=base_url, headers=_codex_headers(bearer))


def _request_headers(request: web.Request, credential: _Credential) -> dict[str, str]:
    result = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _STRIPPED_REQUEST_HEADERS
    }
    result.update(credential.headers)
    result["Authorization"] = f"Bearer {credential.bearer}"
    return result


def _response_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _STRIPPED_RESPONSE_HEADERS
    }


def create_app(source: _CredentialSource, synthetic_bearer: str) -> web.Application:
    if not synthetic_bearer:
        raise ValueError("synthetic_bearer must not be empty")
    app = web.Application(client_max_size=_MAX_REQUEST_BYTES)

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "provider": _PROVIDER,
                "authenticated": source.is_authenticated(),
            }
        )

    async def relay(request: web.Request) -> web.StreamResponse:
        if request.path != _ALLOWED_PATH or request.method != "POST":
            return web.json_response(
                {
                    "error": {
                        "message": f"Only POST {_ALLOWED_PATH} is allowed.",
                        "type": "path_not_allowed",
                        "code": "path_not_allowed",
                    }
                },
                status=404,
            )
        supplied_auth = request.headers.get("Authorization", "")
        expected_auth = f"Bearer {synthetic_bearer}"
        if not hmac.compare_digest(supplied_auth, expected_auth):
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid relay authorization.",
                        "type": "invalid_relay_auth",
                        "code": "invalid_relay_auth",
                    }
                },
                status=401,
            )
        try:
            credential = source.get()
        except Exception as exc:
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "upstream_auth_failed",
                        "code": "upstream_auth_failed",
                    }
                },
                status=401,
            )

        body = await request.read()
        timeout = ClientTimeout(total=None, sock_connect=15, sock_read=900)

        async def send(active: _Credential):
            upstream_url = f"{active.base_url}/responses"
            session = ClientSession(timeout=timeout)
            try:
                response = await session.request(
                    "POST",
                    upstream_url,
                    data=body,
                    headers=_request_headers(request, active),
                    allow_redirects=False,
                )
            except Exception:
                await session.close()
                raise
            return session, response

        try:
            session, upstream = await send(credential)
        except (ClientError, asyncio.TimeoutError) as exc:
            return web.json_response(
                {
                    "error": {
                        "message": f"Codex upstream unavailable: {type(exc).__name__}",
                        "type": "upstream_unreachable",
                        "code": "upstream_unreachable",
                    }
                },
                status=502,
            )

        if upstream.status in {401, 429}:
            try:
                retry_credential = source.retry(credential, upstream.status)
            except Exception:
                retry_credential = None
            if retry_credential is not None:
                upstream.release()
                await session.close()
                try:
                    session, upstream = await send(retry_credential)
                except (ClientError, asyncio.TimeoutError) as exc:
                    return web.json_response(
                        {
                            "error": {
                                "message": f"Codex upstream unavailable after retry: {type(exc).__name__}",
                                "type": "upstream_unreachable",
                                "code": "upstream_unreachable",
                            }
                        },
                        status=502,
                    )

        response = web.StreamResponse(
            status=upstream.status,
            headers=_response_headers(upstream.headers),
        )
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                if chunk:
                    await response.write(chunk)
        except (ClientError, ConnectionError, asyncio.CancelledError):
            raise
        finally:
            upstream.release()
            await session.close()
        await response.write_eof()
        return response

    app.router.add_get("/health", health)
    app.router.add_route("*", "/{tail:.*}", relay)
    return app


async def _watch_parent(parent_pid: int, stop: asyncio.Event) -> None:
    if parent_pid <= 1:
        return
    while not stop.is_set():
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            stop.set()
            return
        except PermissionError:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def _run(parent_pid: int, synthetic_bearer: str) -> None:
    source = _CredentialSource()
    if not source.is_authenticated():
        print(
            json.dumps(
                {
                    "ready": False,
                    "error": "Hermes OpenAI Codex OAuth is not authenticated.",
                }
            ),
            flush=True,
        )
        raise SystemExit(2)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = int(sock.getsockname()[1])

    runner = web.AppRunner(create_app(source, synthetic_bearer), access_log=None)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    print(json.dumps({"ready": True, "host": "127.0.0.1", "port": port}), flush=True)
    watcher = asyncio.create_task(_watch_parent(parent_pid, stop))
    try:
        await stop.wait()
    finally:
        watcher.cancel()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    synthetic_bearer = sys.stdin.readline().strip()
    if not synthetic_bearer:
        print(
            json.dumps({"ready": False, "error": "Missing relay authentication handoff."}),
            flush=True,
        )
        raise SystemExit(2)
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_run(args.parent_pid, synthetic_bearer))


if __name__ == "__main__":
    main()
