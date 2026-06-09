"""Test fixtures.

api.py has no homeassistant imports, so it is loaded directly as a
standalone module ("georide_api"). This keeps the Phase-1 harness free of
the full Home Assistant test stack; package-level imports
(custom_components.georide_trips.*) become available in CI once
pytest-homeassistant-custom-component is in play.

HTTP is tested against a real local aiohttp server (FakeGeoRide) instead of
a mocking library: responses are queued per (method, path) and every call is
recorded, so tests assert on real client/server behavior.
"""

import importlib.util
import sys
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

_API_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "georide_trips"
    / "api.py"
)
_spec = importlib.util.spec_from_file_location("georide_api", _API_PATH)
georide_api = importlib.util.module_from_spec(_spec)
sys.modules["georide_api"] = georide_api
_spec.loader.exec_module(georide_api)


class FakeGeoRide:
    """Scriptable fake GeoRide API server.

    queue() responses per (method, path); they are consumed in order.
    A request with no queued response gets HTTP 599 so the test fails loudly.
    """

    def __init__(self):
        self.responses = {}
        self.calls = []
        self.base_url = None

    def queue(self, method, path, payload=None, status=200):
        self.responses.setdefault((method.upper(), path), []).append((status, payload))

    def call_count(self, method, path):
        return self.calls.count((method.upper(), path))

    async def handler(self, request):
        key = (request.method, request.path)
        self.calls.append(key)
        queued = self.responses.get(key)
        if not queued:
            return web.json_response(
                {"error": f"no queued response for {key}"}, status=599
            )
        status, payload = queued.pop(0)
        if payload is None:
            return web.Response(status=status)
        return web.json_response(payload, status=status)


@pytest_asyncio.fixture
async def fake():
    server_fake = FakeGeoRide()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server_fake.handler)
    server = TestServer(app)
    await server.start_server()
    server_fake.base_url = str(server.make_url("")).rstrip("/")
    yield server_fake
    await server.close()


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
def api(fake, session):
    client = georide_api.GeoRideTripsAPI("user@example.com", "secret", session)
    client.base_url = fake.base_url
    return client
