"""`--all-pages`: walk a `{Total, Page, <Key>: [...]}` list until it is exhausted."""

from __future__ import annotations

import sys
from collections.abc import Callable

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def fetch_all(
    fetch: Callable[[int, int], object],
    key: str,
    limit: int = DEFAULT_LIMIT,
    start_page: int = 1,
    max_pages: int | None = None,
    progress: bool = True,
) -> dict:
    """Call `fetch(page, limit)` until every row is in. Returns one merged envelope.

    Stops when a page comes back short or empty, when `Total` rows are collected,
    or after `max_pages`. Cin7 has no stable sort, so rows that move between pages
    mid-walk can repeat or be missed; dedupe downstream if that matters.
    """
    rows: list = []
    total = None
    page = start_page
    pages = 0
    while True:
        body = fetch(page, limit)
        pages += 1
        if not isinstance(body, dict):
            raise ValueError(f"page {page} was not a JSON object")
        batch = body.get(key) or []
        total = body.get("Total", total)
        rows.extend(batch)
        if progress:
            of = f"/{total}" if total is not None else ""
            print(f"cin7: page {page}: {len(rows)}{of} rows", file=sys.stderr)
        if len(batch) < limit or (total is not None and len(rows) >= int(total)):
            break
        if max_pages is not None and pages >= max_pages:
            print(f"cin7: stopped after --max-pages {max_pages}", file=sys.stderr)
            break
        page += 1
    return {"Total": total if total is not None else len(rows), "Page": None, "Pages": pages, key: rows}
