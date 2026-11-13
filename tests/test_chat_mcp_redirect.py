"""Native MCP client redirect policy.

Same-origin 308 path normalization is followed. A cross-origin 308 is refused
and the second endpoint records zero requests. Stdlib HTTP server plus the
stock legacy ``Client``. No application transport and no mocks.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from django.test import SimpleTestCase
from fastmcp import Client
from mcp import MCPError

PROBE_TOOL = "probe"
ENTRY = "/mcp"
CANONICAL = "/mcp/"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, redirect_all: str | None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.redirect_all = redirect_all
        self.requests: list[tuple[str, str, str | None]] = []
        self.locations: list[str] = []
        self._lock = threading.Lock()
        self.ready = threading.Event()

    def serve_forever(self, poll_interval: float = 0.05) -> None:
        self.ready.set()
        super().serve_forever(poll_interval=poll_interval)

    def record(self, method: str, path: str, rpc: str | None) -> None:
        with self._lock:
            self.requests.append((method, path.split("?", 1)[0], rpc))

    def note_location(self, location: str) -> None:
        with self._lock:
            self.locations.append(location)

    def snapshot(
        self,
    ) -> tuple[list[tuple[str, str, str | None]], list[str]]:
        with self._lock:
            return list(self.requests), list(self.locations)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _peer(self) -> _Server:
        server = self.server
        if not isinstance(server, _Server):
            raise RuntimeError("redirect handler is not bound to its peer")
        return server

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length") or "0"
        length = int(raw)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _rpc(self, body: bytes) -> tuple[dict[str, Any] | None, str | None]:
        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, None
        if not isinstance(message, dict):
            return None, None
        method = message.get("method")
        return message, method if isinstance(method, str) else None

    def do_POST(self) -> None:
        body = self._read_body()
        message, rpc = self._rpc(body)
        peer = self._peer()
        path = self.path.split("?", 1)[0]
        peer.record("POST", path, rpc)
        if peer.redirect_all is not None:
            self._redirect(peer.redirect_all)
            return
        if path == ENTRY:
            self._redirect(CANONICAL)
            return
        if path == CANONICAL and message is not None:
            self._mcp(message)
            return
        self._empty(404)

    def do_GET(self) -> None:
        self._reject()

    def do_DELETE(self) -> None:
        self._reject()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._reject
        raise AttributeError(name)

    def _reject(self) -> None:
        self._read_body()
        self._peer().record(self.command, self.path, None)
        self._empty(404)

    def _redirect(self, location: str) -> None:
        self._peer().note_location(location)
        self.send_response(308)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _mcp(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "notifications/initialized":
            self._empty(202)
            return
        request_id = message.get("id")
        if method == "initialize":
            params = message.get("params")
            requested = (
                params.get("protocolVersion") if isinstance(params, dict) else None
            )
            version = requested if isinstance(requested, str) else "2025-11-25"
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "redirect-probe", "version": "0"},
                    },
                }
            )
            return
        if method == "tools/list":
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": PROBE_TOOL,
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {},
                                },
                            }
                        ]
                    },
                }
            )
            return
        self._json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )


class _Peer:
    def __init__(self, redirect_all: str | None = None) -> None:
        self._redirect_all = redirect_all
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Peer:
        server = _Server(self._redirect_all)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()
        if not server.ready.wait(5):
            self.__exit__(None, None, None)
            raise RuntimeError("MCP redirect peer did not start")
        return self

    def __exit__(self, *exc: object) -> None:
        server = self._server
        thread = self._thread
        if (
            server is not None
            and thread is not None
            and thread.is_alive()
            and server.ready.is_set()
        ):
            server.shutdown()
        if server is not None:
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    @property
    def _bound(self) -> _Server:
        if self._server is None:
            raise RuntimeError("MCP redirect peer is not running")
        return self._server

    @property
    def port(self) -> int:
        return int(self._bound.server_address[1])

    @property
    def url(self) -> str:
        host, port = self._bound.server_address[:2]
        return f"http://{host}:{port}/mcp"

    @property
    def requests(self) -> list[tuple[str, str, str | None]]:
        requests, _locations = self._bound.snapshot()
        return requests

    @property
    def locations(self) -> list[str]:
        _requests, locations = self._bound.snapshot()
        return locations


def _open(url: str) -> Client:
    return Client(url, mode="legacy", init_timeout=10, timeout=15)


class NativeClientRedirectTests(SimpleTestCase):
    def test_same_origin_path_normalization_succeeds(self) -> None:
        async def listed(url: str) -> list[str]:
            async with _open(url) as client:
                tools = await client.list_tools()
            return [tool.name for tool in tools]

        with _Peer() as peer:
            names = asyncio.run(listed(peer.url))
            requests = peer.requests
            locations = peer.locations

        self.assertEqual(names, [PROBE_TOOL])
        self.assertTrue(locations)
        self.assertTrue(all(location == CANONICAL for location in locations))
        self.assertEqual(requests[0], ("POST", ENTRY, "initialize"))
        self.assertIn(("POST", CANONICAL, "initialize"), requests)
        self.assertIn(("POST", CANONICAL, "tools/list"), requests)
        self.assertTrue(all(method == "POST" for method, _path, _rpc in requests))
        self.assertTrue(
            all(path in {ENTRY, CANONICAL} for _method, path, _rpc in requests)
        )

    def test_cross_origin_redirect_never_reaches_the_second_endpoint(self) -> None:
        async def connect(url: str) -> None:
            async with _open(url) as client:
                await client.list_tools()

        with _Peer() as second, _Peer(redirect_all=second.url) as first:
            self.assertNotEqual(first.port, second.port)
            with self.assertRaises(MCPError) as caught:
                asyncio.run(connect(first.url))
            message = str(caught.exception)
            first_locations = first.locations
            second_requests = second.requests

        self.assertIn("not followed", message)
        self.assertIn(str(second.port), message)
        self.assertTrue(first_locations)
        self.assertTrue(all(location == second.url for location in first_locations))
        self.assertEqual(second_requests, [])
