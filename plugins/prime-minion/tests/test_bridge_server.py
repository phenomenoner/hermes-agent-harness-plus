from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType

from aiohttp import ClientSession, web

ROOT = Path(__file__).resolve().parents[1]


def load_bridge() -> ModuleType:
    try:
        __import__("agent.credential_pool")
    except ModuleNotFoundError:
        agent_module = ModuleType("agent")
        pool_module = ModuleType("agent.credential_pool")
        setattr(pool_module, "CredentialPool", object)
        setattr(pool_module, "PooledCredential", object)

        def unavailable_pool(*_args, **_kwargs):
            raise AssertionError("credential pool must be supplied by the test source")

        setattr(pool_module, "load_pool", unavailable_pool)
        setattr(agent_module, "credential_pool", pool_module)
        sys.modules["agent"] = agent_module
        sys.modules["agent.credential_pool"] = pool_module

    spec = importlib.util.spec_from_file_location("prime_minion_bridge_test", ROOT / "bridge_server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def start_app(app: web.Application) -> tuple[web.AppRunner, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = int(sock.getsockname()[1])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()
    return runner, f"http://127.0.0.1:{port}"


def test_relay_replaces_credentials_and_streams_sse() -> None:
    bridge = load_bridge()

    async def scenario() -> None:
        observed: dict[str, object] = {}

        async def upstream(request: web.Request) -> web.StreamResponse:
            observed["headers"] = dict(request.headers)
            observed["body"] = await request.text()
            response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n')
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        upstream_app = web.Application()
        upstream_app.router.add_post("/responses", upstream)
        upstream_runner, upstream_url = await start_app(upstream_app)

        credential = bridge._Credential(
            bearer="fixture-token-not-a-secret",
            base_url=upstream_url,
            headers={
                "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent Prime Minion)",
                "originator": "codex_cli_rs",
                "ChatGPT-Account-ID": "real-account",
            },
        )

        class Source:
            def is_authenticated(self) -> bool:
                return True

            def get(self):
                return credential

            def retry(self, failed, status_code):
                return None

        relay_runner, relay_url = await start_app(
            bridge.create_app(Source(), "synthetic-child-token")
        )
        try:
            async with ClientSession() as session:
                async with session.post(
                    relay_url + "/v1/codex/responses",
                    data='{"model":"gpt-5.6-luna","stream":true}',
                    headers={
                        "Authorization": "Bearer synthetic-child-token",
                        "ChatGPT-Account-ID": "placeholder-account",
                        "originator": "pi",
                        "User-Agent": "Prime Agent",
                        "X-Test-Preserved": "yes",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    body = await response.text()
                    assert response.status == 200
                    assert "response.output_text.delta" in body
                    assert "[DONE]" in body

            headers = observed["headers"]
            assert isinstance(headers, dict)
            assert headers["Authorization"] == "Bearer fixture-token-not-a-secret"
            assert headers["ChatGPT-Account-ID"] == "real-account"
            assert headers["originator"] == "codex_cli_rs"
            assert headers["User-Agent"].startswith("codex_cli_rs/")
            assert headers["X-Test-Preserved"] == "yes"
            assert observed["body"] == '{"model":"gpt-5.6-luna","stream":true}'
        finally:
            await relay_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(scenario())


def test_relay_rejects_every_other_path_without_upstream_call() -> None:
    bridge = load_bridge()

    class Source:
        def is_authenticated(self) -> bool:
            return True

        def get(self):
            raise AssertionError("credential resolution must not run for a rejected path")

    async def scenario() -> None:
        runner, url = await start_app(bridge.create_app(Source(), "synthetic-child-token"))
        try:
            async with ClientSession() as session:
                async with session.post(url + "/v1/responses", json={}) as response:
                    payload = await response.json()
                    assert response.status == 404
                    assert payload["error"]["code"] == "path_not_allowed"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_relay_rejects_missing_or_wrong_bearer_before_credential_resolution() -> None:
    bridge = load_bridge()

    class Source:
        def is_authenticated(self) -> bool:
            return True

        def get(self):
            raise AssertionError("credential resolution must not run for invalid relay auth")

    async def scenario() -> None:
        runner, url = await start_app(bridge.create_app(Source(), "expected-synthetic-token"))
        try:
            async with ClientSession() as session:
                for headers in ({}, {"Authorization": "Bearer wrong-synthetic-token"}):
                    async with session.post(
                        url + "/v1/codex/responses",
                        json={},
                        headers=headers,
                    ) as response:
                        payload = await response.json()
                        assert response.status == 401
                        assert payload["error"]["code"] == "invalid_relay_auth"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
