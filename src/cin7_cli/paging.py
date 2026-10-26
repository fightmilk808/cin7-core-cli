"""`--all-pages`: walk a `{Total, Page, <Key>: [...]}` list until it is exhausted."""

from __future__ import annotations

import sys
from collections.abc import Callable

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# How many pages a walk reads when nobody says otherwise, and the most anyone may ask
# for. The API key is shared with the live freight pipeline and its limits are counted
# per key: a walk of a big list (product availability is 170+ pages) spends minutes of
# that budget. A whole table belongs to the warehouse (cin7_raw), not to the API.
DEFAULT_MAX_PAGES = 20
HARD_MAX_PAGES = 50
WAREHOUSE_HINT = "the API key is shared with freight; read whole tables from the warehouse (cin7_raw)"


def page_cap(max_pages: int | None) -> int:
    """The pages a walk may read: `max_pages`, or DEFAULT_MAX_PAGES when none is given.
    More than HARD_MAX_PAGES is refused before anything is sent."""
    if max_pages is None:
        return DEFAULT_MAX_PAGES
    if max_pages < 1:
        raise ValueError("--max-pages must be 1 or more")
    if max_pages > HARD_MAX_PAGES:
        raise ValueError(f"--max-pages {max_pages} is over the limit of {HARD_MAX_PAGES}; "
                         f"{WAREHOUSE_HINT}")
    return max_pages


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
    or after `max_pages` (DEFAULT_MAX_PAGES when none is given, never more than
    HARD_MAX_PAGES). A walk the cap cut short says so: `Truncated` is true and
    `Pages` how many were read. Cin7 has no stable sort, so rows that move between
    pages mid-walk can repeat or be missed; dedupe downstream if that matters.
    """
    cap = page_cap(max_pages)
    rows: list = []
    total = None
    page = start_page
    pages = 0
    truncated = False
    while True:
        body = fetch(page, limit)
        pages += 1
        if not isinstance(body, dict):
            raise ValueError(f"page {page} was not a JSON object")
        batch = body.get(key) or []
        total = body.get("Total", body.get("total", total))
        rows.extend(batch)
        if progress:
            of = f"/{total}" if total is not None else ""
            print(f"cin7: page {page}: {len(rows)}{of} rows", file=sys.stderr)
        if len(batch) < limit or (total is not None and len(rows) >= int(total)):
            break
        if pages >= cap:
            truncated = True
            print(f"cin7: stopped after {cap} pages ({len(rows)} rows"
                  f"{f' of {total}' if total is not None else ''}); {WAREHOUSE_HINT}",
                  file=sys.stderr)
            break
        page += 1
    return {"Total": total if total is not None else len(rows), "Page": None, "Pages": pages,
            "Truncated": truncated, key: rows}
