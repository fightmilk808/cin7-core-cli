"""Shared fixtures: a local mock Cin7 server. No test ever reaches the live API."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cin7_cli import ratelimit

ACCOUNT = "test-account-0000"
KEY = "test-key-SECRET-1111"


class MockCin7:
    """Records every request; answers from `routes[(METHOD, path)]`.

    A route value is a callable(request_dict) -> (status, body, headers) or a list
    of such tuples consumed in order.
    """

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict[tuple[str, str], object] = {}
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _handle(self):
                u = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                req = {
                    "method": self.command,
                    "path": u.path.removeprefix("/ExternalApi/v2"),
                    "query": dict(urllib.parse.parse_qsl(u.query)),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": (
                        dict(urllib.parse.parse_qsl(raw.decode()))
                        if raw and "x-www-form-urlencoded" in self.headers.get("Content-Type", "")
                        else json.loads(raw) if raw else None
                    ),
                }
                mock.requests.append(req)
                route = mock.routes.get((self.command, req["path"]))
                if route is None:
                    status, body, headers = 302, None, {"Location": "/Error/NotFound"}
                elif callable(route):
                    status, body, headers = route(req)
                else:
                    status, body, headers = route.pop(0) if isinstance(route, list) else route
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                data = json.dumps(body).encode() if body is not None else b""
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ExternalApi/v2"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Every test: a closed port as the API root, fake creds, its own pacing file, no sleeping."""
    monkeypatch.setenv("CIN7_BASE_URL", "http://127.0.0.1:9/ExternalApi/v2")
    monkeypatch.setenv("CIN7_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("CIN7_APP_KEY", KEY)
    monkeypatch.setenv("CIN7_CLI_STATE_DIR", str(tmp_path / "state"))
    # Never read the developer's real ~/.config/cin7-cli/config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CIN7_CLI_READ_ONLY", raising=False)
    monkeypatch.delenv("CIN7_RATE_PER_MIN", raising=False)
    real_init = ratelimit.RateLimiter.__init__

    def fast_init(self, per_min=ratelimit.DEFAULT_PER_MIN, path=None, sleep=None, clock=None):
        import time

        real_init(self, per_min, path, sleep=sleep or (lambda s: None), clock=clock or time.time)

    monkeypatch.setattr(ratelimit.RateLimiter, "__init__", fast_init)


@pytest.fixture
def mock(monkeypatch):
    m = MockCin7()
    monkeypatch.setenv("CIN7_BASE_URL", m.url)
    yield m
    m.close()
