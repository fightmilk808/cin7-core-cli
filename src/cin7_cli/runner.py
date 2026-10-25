"""Sending one operation, shared by the CLI and the TUI.

Public operations go to the API root with the API key. Internal (web app)
operations go to the web root with the signed-in browser session, and only the
ones the map marks as reads are ever sent: there is no write path for them.
"""

from __future__ import annotations

import os

from . import auth, paging, spec
from .client import Client, Request, base_url, build_url
from .ratelimit import RateLimiter


class NotRunnable(RuntimeError):
    """The operation is listed for reference only."""


def web_base_url() -> str:
    """Web app root, overridable with CIN7_WEB_BASE_URL (tests and mocks)."""
    return os.environ.get("CIN7_WEB_BASE_URL", spec.load()["web_base_url"]).rstrip("/")


def root_for(op: spec.Operation) -> str:
    """Where `op` is sent."""
    return base_url() if op.is_public else web_base_url()


def check_runnable(op: spec.Operation) -> None:
    """Raise NotRunnable for anything internal that isn't a mapped read."""
    if op.is_public:
        return
    if op.source != "web":
        raise NotRunnable(f"{op.full_name} belongs to the {op.group_title}; it is listed for reference only")
    if not op.runnable:
        raise NotRunnable(
            f"{op.full_name} is an internal {op.effect or 'unknown'} endpoint ({op.methods_raw or 'no method'}): "
            "internal endpoints that change things are never sent from this tool"
        )


def credentials_for(op: spec.Operation):
    """API key for public operations, browser session for internal ones."""
    return auth.load() if op.is_public else auth.load_web()


def make_client(op: spec.Operation, rate: float, timeout: float = 60, verbose: bool = False) -> Client:
    return Client(credentials_for(op), RateLimiter(rate), timeout=timeout, verbose=verbose)


def request_for(op: spec.Operation, query: dict, body: object = None) -> Request:
    """The request `op` would send (for --dry-run, confirmation and sending)."""
    if op.read_via_post and body is None:
        body = {}
    return Request(op.method, build_url(op.path, query, root_for(op)), body)


def fetch(
    client: Client,
    op: spec.Operation,
    query: dict,
    body: object = None,
    all_pages: bool = False,
    max_pages: int | None = None,
    progress: bool = True,
    on_page=None,
) -> object:
    """Send `op` once, or walk every page of a paged list."""
    root = root_for(op)
    if op.file:
        return client.send(request_for(op, query, body), raw=True)
    if not all_pages:
        return client.send(request_for(op, query, body))
    if op.is_public:
        limit = int(query.get("Limit") or paging.DEFAULT_LIMIT)
        start = int(query.get("Page") or 1)

        def one(page: int, lim: int) -> object:
            if on_page:
                on_page(page)
            return client.send(Request("GET", build_url(op.path, {**query, "Page": page, "Limit": lim}, root)))

    else:
        # ExtJS grid stores: page, start and limit together; `{root: [...], total}`.
        limit = int(query.get("limit") or paging.DEFAULT_LIMIT)
        start = int(query.get("page") or 1)

        def one(page: int, lim: int) -> object:
            if on_page:
                on_page(page)
            q = {**query, "page": page, "start": (page - 1) * lim, "limit": lim}
            return client.send(request_for(op, q, body))

    return paging.fetch_all(one, op.list_key, limit=limit, start_page=start, max_pages=max_pages, progress=progress)


def save_file(result: tuple, op: spec.Operation, directory) -> str:
    """Write a downloaded file under `directory`; returns its path."""
    import re
    import time
    from pathlib import Path

    data, ctype, disposition = result
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition or "")
    name = Path(m.group(1)).name if m else f"{op.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    if "." not in name:
        name += ".pdf" if "pdf" in ctype else ".csv" if "csv" in ctype else ".xlsx" if "sheet" in ctype else ".bin"
    path = Path(directory) / name
    path.write_bytes(data)
    return str(path)
