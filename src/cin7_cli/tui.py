"""`cin7 tui`: browse and run the read operations in a terminal UI.

The TUI is read-only, full stop: write operations are listed so their shapes can
be read, but there is no way to send one from here. Use the CLI's
`--allow-write` path for that, which asks a person to confirm on the terminal.

Needs the `tui` extra: `pip install 'cin7-core-cli[tui]'`.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from . import auth, output, paging, spec
from .cli import TYPE_HINT, TYPES
from .client import ApiError, Client, Request, base_url, build_url
from .ratelimit import DEFAULT_PER_MIN, RateLimiter

MAX_TABLE_ROWS = 5000


class ParamInput(Input):
    """An Input bound to one query parameter."""

    def __init__(self, param: spec.Param):
        hint = TYPE_HINT.get(param.type, "TEXT")
        super().__init__(placeholder=f"{hint}{'  (required)' if param.required else ''}", id=f"p-{param.flag}")
        self.param = param


class Cin7Tui(App):
    """Operation browser on the left, form and results on the right."""

    TITLE = "cin7"
    SUB_TITLE = "Cin7 Core API v2 · read-only"
    CSS = """
    #left { width: 38; border-right: solid $panel; }
    #filter { margin: 0 1; }
    #ops { height: 1fr; }
    #right { width: 1fr; }
    #op-title { padding: 0 1; text-style: bold; }
    #op-meta { padding: 0 1; color: $text-muted; }
    #form { height: auto; max-height: 45%; padding: 0 1; }
    .row { height: 1; margin-bottom: 1; }
    .row Label { width: 34; }
    .row Input { width: 1fr; height: 1; border: none; padding: 0 1; background: $boost; }
    .row Input:focus { background: $primary 30%; }
    #actions { height: auto; padding: 0 1; }
    #actions Button { margin-right: 1; }
    #status { padding: 0 1; color: $text-muted; height: 1; }
    #results { height: 1fr; }
    """
    BINDINGS = [
        Binding("ctrl+r", "run", "Run"),
        Binding("ctrl+e", "export", "Export CSV"),
        Binding("ctrl+j", "export_json", "Export JSON"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, rate: float = DEFAULT_PER_MIN, export_dir: Path | None = None):
        super().__init__()
        self.rate = rate
        self.export_dir = export_dir or Path.cwd()
        self.op: spec.Operation | None = None
        self.data: object = None
        self.rows: list[dict] = []
        self.client: Client | None = None
        self.running = False

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Input(placeholder="/ filter operations", id="filter")
                yield Tree("Cin7", id="ops")
            with Vertical(id="right"):
                yield Static("Pick an operation on the left", id="op-title")
                yield Static("", id="op-meta")
                yield VerticalScroll(id="form")
                with Horizontal(id="actions"):
                    yield Checkbox("All pages", id="all-pages", disabled=True)
                    yield Button("Run  ^R", id="run", variant="primary", disabled=True)
                    yield Button("Export CSV  ^E", id="export", disabled=True)
                yield Static("", id="status")
                with TabbedContent(id="results"):
                    with TabPane("Table", id="tab-table"):
                        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                    with TabPane("JSON", id="tab-json"):
                        yield TextArea("", id="json", language="json", read_only=True)
                    with TabPane("Record", id="tab-record"):
                        yield TextArea("", id="record", language="json", read_only=True)
                    with TabPane("Shape", id="tab-shape"):
                        yield TextArea("", id="shape", language="json", read_only=True)
                    with TabPane("Log", id="tab-log"):
                        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._fill_tree("")
        self.query_one("#ops", Tree).focus()
        self.log_line(f"API root {base_url()} · {self.rate:g} calls/min shared with every cin7 process")

    def _fill_tree(self, text: str) -> None:
        tree = self.query_one("#ops", Tree)
        tree.clear()
        tree.root.expand()
        needle = text.lower().strip()
        for group, ops in spec.groups().items():
            matches = [
                o
                for o in ops
                if not needle or needle in f"{o.group} {o.name} {o.path} {o.resource} {o.title}".lower()
            ]
            if not matches:
                continue
            reads = sum(not o.is_write for o in matches)
            node = tree.root.add(f"{group} [dim]{reads}/{len(matches)}[/]", data=None, expand=bool(needle))
            for o in matches:
                label = o.name if not o.is_write else f"[yellow]✎ {o.name}[/]"
                node.add_leaf(label, data=o)

    # --- events -------------------------------------------------------------

    @on(Input.Changed, "#filter")
    def _filter(self, event: Input.Changed) -> None:
        self._fill_tree(event.value)

    @on(Input.Submitted, "#filter")
    def _filter_done(self) -> None:
        self.query_one("#ops", Tree).focus()

    @on(Tree.NodeSelected, "#ops")
    def _picked(self, event: Tree.NodeSelected) -> None:
        if isinstance(event.node.data, spec.Operation):
            self.select(event.node.data)

    @on(Input.Submitted, "#form Input")
    def _submit(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#run")
    def _run_pressed(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#export")
    def _export_pressed(self) -> None:
        self.action_export()

    @on(DataTable.RowSelected, "#table")
    def _row(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self.rows):
            self.query_one("#record", TextArea).text = json.dumps(self.rows[idx], indent=2, default=str)
            self.query_one("#results", TabbedContent).active = "tab-record"

    # --- actions ------------------------------------------------------------

    def select(self, op: spec.Operation) -> None:
        """Show `op`'s form and shape."""
        self.op = op
        kind = "[$warning]WRITE · not runnable here[/]" if op.is_write else "read"
        self.query_one("#op-title", Static).update(f"cin7 {op.full_name}  [dim]{kind}[/]")
        alias = f" · alias {', '.join(op.aliases)}" if op.aliases else ""
        self.query_one("#op-meta", Static).update(f"{op.method} {op.path} · {op.resource} / {op.title}{alias}")
        form = self.query_one("#form", VerticalScroll)
        form.remove_children()
        rows = []
        for p in op.params:
            star = "*" if p.required else ""
            rows.append(Horizontal(Label(f"{p.name}{star} [dim]{p.type}[/]"), ParamInput(p), classes="row"))
        if not rows:
            rows.append(Label("[dim]No query parameters[/]"))
        form.mount_all(rows)
        pages = self.query_one("#all-pages", Checkbox)
        pages.disabled = not op.paged or op.is_write
        pages.value = False
        self.query_one("#run", Button).disabled = op.is_write
        shape = {"params": [p.__dict__ for p in op.params], "request": op.request, "response": op.response}
        self.query_one("#shape", TextArea).text = json.dumps(shape, indent=2)
        if op.is_write:
            self.query_one("#results", TabbedContent).active = "tab-shape"
            self.set_status("Writes can't be sent from the TUI. Use the CLI with --allow-write.")
        else:
            self.set_status("Fill in parameters, then ^R")

    def collect_query(self) -> dict[str, object] | None:
        """Typed query from the form, or None (with a notification) if a value is bad."""
        query: dict[str, object] = {}
        for inp in self.query(ParamInput):
            raw = inp.value.strip()
            p = inp.param
            if not raw:
                if p.required:
                    self.notify(f"{p.name} is required", severity="error")
                    inp.focus()
                    return None
                continue
            try:
                query[p.name] = TYPES.get(p.type, str)(raw)
            except (ValueError, argparse.ArgumentTypeError) as exc:
                self.notify(f"{p.name}: {exc}", severity="error")
                inp.focus()
                return None
        return query

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_run(self) -> None:
        op = self.op
        if op is None or self.running:
            return
        if op.is_write:  # belt and braces: the button is disabled too
            self.notify("Read-only: writes can't be sent from the TUI", severity="warning")
            return
        query = self.collect_query()
        if query is None:
            return
        all_pages = op.paged and self.query_one("#all-pages", Checkbox).value
        self.running = True
        self.query_one("#run", Button).disabled = True
        self.set_status("Running…")
        self._call(op, query, all_pages)

    @work(thread=True, exclusive=True)
    def _call(self, op: spec.Operation, query: dict, all_pages: bool) -> None:
        started = time.monotonic()
        try:
            client = self._client()
            root = base_url()
            if all_pages:
                limit = int(query.get("Limit") or paging.DEFAULT_LIMIT)

                def fetch(page: int, lim: int) -> object:
                    self.call_from_thread(self.set_status, f"Fetching page {page}…")
                    return client.send(Request("GET", build_url(op.path, {**query, "Page": page, "Limit": lim}, root)))

                data = paging.fetch_all(fetch, op.list_key, limit=limit, start_page=int(query.get("Page") or 1), progress=False)
            else:
                url = build_url(op.path, query, root)
                self.call_from_thread(self.log_line, f"GET {url}")
                data = client.send(Request("GET", url))
        except (ApiError, auth.AuthError, ValueError) as exc:
            self.call_from_thread(self._failed, str(exc))
            return
        self.call_from_thread(self._show, op, data, time.monotonic() - started)

    def _client(self) -> Client:
        if self.client is None:
            self.client = Client(auth.load(), RateLimiter(self.rate))
        return self.client

    def _failed(self, message: str) -> None:
        self.running = False
        self.query_one("#run", Button).disabled = False
        self.set_status(f"[$error]{escape(message)}[/]")
        self.log_line(f"[red]{escape(message)}[/]")
        self.notify(message, severity="error", timeout=8)

    def _show(self, op: spec.Operation, data: object, seconds: float) -> None:
        self.running = False
        self.query_one("#run", Button).disabled = False
        self.data = data
        self.rows = output.rows_of(data, op.list_key)
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        cols = output.columns(self.rows)
        if cols:
            table.add_columns(*cols)
            for r in self.rows[:MAX_TABLE_ROWS]:
                table.add_row(*[Text(output.cell(r.get(c))[:80]) for c in cols])
        self.query_one("#json", TextArea).text = json.dumps(data, indent=2, default=str)
        self.query_one("#export", Button).disabled = not self.rows
        total = data.get("Total") if isinstance(data, dict) else None
        more = f" of {total}" if total is not None else ""
        clipped = f" (table shows first {MAX_TABLE_ROWS})" if len(self.rows) > MAX_TABLE_ROWS else ""
        calls = self.client.calls if self.client else 0
        self.set_status(f"{len(self.rows)} rows{more}{clipped} · {seconds:.1f}s · {calls} calls this session")
        self.log_line(f"{op.full_name}: {len(self.rows)} rows{more}")
        self.query_one("#results", TabbedContent).active = "tab-table"
        table.focus()

    def _export(self, fmt: str) -> None:
        if self.op is None or self.data is None:
            self.notify("Run something first", severity="warning")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.export_dir / f"cin7-{self.op.group}-{self.op.name}-{stamp}.{fmt}"
        path.write_text(output.render(self.data, fmt, self.op.list_key) + "\n")
        self.log_line(f"saved {path}")
        self.notify(f"Saved {path.name}")

    def action_export(self) -> None:
        self._export("csv")

    def action_export_json(self) -> None:
        self._export("json")

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)


def run(rate: float = DEFAULT_PER_MIN) -> int:
    """Start the TUI. Returns an exit code."""
    Cin7Tui(rate=rate).run()
    return 0
