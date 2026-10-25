"""HTTP client for the Cin7 Core public API v2 (stdlib only)."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .auth import Credentials
from .ratelimit import RateLimiter

LIVE_BASE_URL = "https://inventory.dearsystems.com/ExternalApi/v2"
BACKOFF = (5, 15, 30, 60)  # seconds before each retry after a 429 or 5xx


class ApiError(RuntimeError):
    """A non-2xx answer from Cin7, with the status and (redacted) body."""

    def __init__(self, status: int, message: str, body: object = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Cin7 answers a wrong path or casing with 302 -> /Error/NotFound. Following it
    # returns an HTML page with 200, which hides the mistake.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Request:
    """A prepared call, printable without secrets."""

    method: str
    url: str
    body: object = None
    form: dict | None = None  # application/x-www-form-urlencoded fields (print actions)

    def describe(self) -> str:
        """Human-readable request line plus body, for --dry-run and write confirmation."""
        text = f"{self.method} {self.url}"
        if self.form is not None:
            text += "\n" + urllib.parse.urlencode(self.form)
        elif self.body is not None:
            text += "\n" + json.dumps(self.body, indent=2)
        return text


def base_url() -> str:
    """API root, overridable with CIN7_BASE_URL (used by tests and mocks)."""
    return os.environ.get("CIN7_BASE_URL", LIVE_BASE_URL).rstrip("/")


def build_url(path: str, query: dict[str, object], root: str | None = None) -> str:
    """Join root + path + encoded query, dropping None values."""
    q = {k: _fmt(v) for k, v in query.items() if v is not None}
    url = (root or base_url()) + path
    return url + ("?" + urllib.parse.urlencode(q) if q else "")


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class Client:
    """Makes paced, retried calls. Read/write policy is enforced by the CLI layer."""

    def __init__(self, creds: Credentials, limiter: RateLimiter, timeout: float = 60, sleep=time.sleep, verbose=False):
        self.creds = creds
        self.limiter = limiter
        self.timeout = timeout
        self._sleep = sleep
        self.verbose = verbose
        self._opener = urllib.request.build_opener(_NoRedirect)
        self.calls = 0

    def send(self, req: Request, raw: bool = False) -> object:
        """Send `req`, retrying 429 (any method) and 5xx (GET only).

        Returns parsed JSON, or the response bytes with their Content-Type when
        `raw` (file downloads).
        """
        if req.form is not None:
            data = urllib.parse.urlencode(req.form).encode()
        else:
            data = json.dumps(req.body).encode() if req.body is not None else None
        attempt = 0
        while True:
            self.limiter.wait()
            self.calls += 1
            if self.verbose:
                print(f"> {req.method} {req.url}", file=sys.stderr)
            http_req = urllib.request.Request(req.url, data=data, method=req.method)
            for k, v in self.creds.headers().items():
                http_req.add_header(k, v)
            http_req.add_header("Accept", "application/json")
            if data is not None:
                http_req.add_header(
                    "Content-Type", "application/x-www-form-urlencoded" if req.form is not None else "application/json"
                )
            try:
                with self._opener.open(http_req, timeout=self.timeout) as resp:
                    if raw:
                        return resp.read(), resp.headers.get("Content-Type", ""), resp.headers.get("Content-Disposition", "")
                    return _parse(resp.read())
            except urllib.error.HTTPError as err:
                status = err.code
                raw = err.read()
                retry_after = err.headers.get("Retry-After") if err.headers else None
                retryable = status == 429 or (status >= 500 and req.method == "GET")
                if retryable and attempt < len(BACKOFF):
                    wait = _retry_after(retry_after) or BACKOFF[attempt]
                    attempt += 1
                    print(f"cin7: HTTP {status}, waiting {wait:g}s before retry {attempt}", file=sys.stderr)
                    self.limiter.penalise(wait)
                    continue
                raise self._error(status, raw, err.headers) from None
            except urllib.error.URLError as err:
                raise ApiError(0, self.creds.redact(f"could not reach {req.url}: {err.reason}")) from None

    def _error(self, status: int, raw: bytes, headers) -> ApiError:
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("Location", "") if headers else ""
            if "login" in loc.lower():
                return ApiError(status, "the Cin7 web session has expired: save a fresh Cookie header (see README)")
            return ApiError(status, f"redirected to {loc or '?'}: Cin7 does this for a wrong path or casing")
        body = _parse(raw)
        msg = {
            400: "bad request (Cin7 also uses 400 for a missing record)",
            403: "forbidden: the account id or application key is wrong or revoked",
            404: "not found",
            429: "rate limited and retries exhausted",
        }.get(status, "request failed")
        if isinstance(body, list) and body and isinstance(body[0], dict) and "Exception" in body[0]:
            msg = str(body[0]["Exception"])
        return ApiError(status, self.creds.redact(msg), body)


def _retry_after(value: str | None) -> float | None:
    try:
        return max(1.0, float(value)) if value else None
    except ValueError:
        return None


def _parse(raw: bytes) -> object:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
