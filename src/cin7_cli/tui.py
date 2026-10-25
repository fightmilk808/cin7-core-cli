"""`cin7 tui`: browse and run the read operations in a terminal UI.

The TUI is read-only, full stop: write operations are listed so their shapes can
be read, but there is no way to send one from here. Use the CLI's
`--allow-write` path for that, which asks a person to confirm on the terminal.

Needs the `tui` extra: `pip install 'cin7-core-cli[tui]'`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import webbrowser
from collections import Counter
from pathlib import Path

from rich.markup import escape as rich_escape
from rich.style import Style
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
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from . import auth, output, ratelimit, rowtools, runner, spec
from .cli import TYPE_HINT, TYPES
from .client import ApiError, base_url
from .ratelimit import DEFAULT_PER_MIN

MAX_TABLE_ROWS = 5000

# Public groups pinned to the top, in this order, and the names they go by.
MAIN_GROUPS = ["product", "sale", "stock", "production", "purchase"]
MAIN_TITLES = {
    "product": "Products",
    "sale": "Sales",
    "stock": "Transfers · Stock",
    "production": "Production",
    "purchase": "Purchases",
}
MAIN_WEB = {"inventory", "sale", "production", "purchase", "mrp"}


def first_ok(o: spec.Operation) -> bool:
    """Can this operation be sent from the TUI?"""
    return (o.is_public and not o.is_write) or (not o.is_public and o.runnable)


def leaf_label(o: spec.Operation, style: str = "") -> str:
    """Tree label: ✎ for writes, ⬇ for downloads, ⛔ for internal ones that are never sent."""
    if o.is_public:
        return f"[yellow]✎ {o.name}[/]" if o.is_write else o.name
    if o.source != "web":
        return f"[dim]· {o.name}[/]"
    if not o.runnable:
        return f"[dim strike]⛔ {o.name}[/]"
    mark = "⬇ " if o.file else "↻ " if o.read_via_post else ""
    return f"[{style}]{mark}{o.name}[/]" if style else f"{mark}{o.name}"


CELL_WIDTHS = (24, 60, 160)
STYLE_COLOURS = {"red": "red", "green": "green", "yellow": "yellow"}


def json_label(key: object, value: object) -> str:
    """Tree label for one JSON member, coloured by type."""
    k = f"[bold]{rich_escape(str(key))}[/]" if key is not None else ""
    sep = ": " if key is not None else ""
    if isinstance(value, dict):
        return f"{k} [dim]{{{len(value)}}}[/]"
    if isinstance(value, list):
        return f"{k} [dim]\\[{len(value)}][/]"
    if value is None:
        return f"{k}{sep}[magenta]null[/]"
    if isinstance(value, bool):
        return f"{k}{sep}[magenta]{'true' if value else 'false'}[/]"
    if isinstance(value, (int, float)):
        return f"{k}{sep}[cyan]{value}[/]"
    text = str(value)
    return f"{k}{sep}[green]\"{rich_escape(text[:200])}{'…' if len(text) > 200 else ''}\"[/]"


def fill_json(node, value: object) -> None:
    """Add `value`'s members under `node`; containers fill lazily when opened."""
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for k, v in items:
        label = json_label(k, v)
        if isinstance(v, (dict, list)) and v:
            node.add(label, data=("lazy", v))
        else:
            node.add_leaf(label, data=("leaf", v))


def load_ui_state() -> dict:
    """Sidebar width, compact mode and cell width, remembered between sessions."""
    try:
        return json.loads((auth.config_path().parent / "ui.json").read_text())
    except (OSError, ValueError):
        return {}


def save_ui_state(state: dict) -> None:
    try:
        path = auth.config_path().parent / "ui.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        pass


def history_path() -> Path:
    return ratelimit.state_dir() / "downloads.json"


def load_history() -> list[dict]:
    try:
        return json.loads(history_path().read_text())
    except (OSError, ValueError):
        return []


def add_history(entry: dict) -> None:
    try:
        history_path().write_text(json.dumps((load_history() + [entry])[-200:]))
    except OSError:
        pass


class ParamInput(Input):
    """An Input bound to one query parameter."""

    def __init__(self, param: spec.Param):
        hint = TYPE_HINT.get(param.type, "TEXT")
        super().__init__(placeholder=f"{hint}{'  (required)' if param.required else ''}", id=f"p-{param.flag}")
        self.param = param


HELP = """[bold]Sidebar[/]   ctrl+b hide/show · [ ] narrower/wider · c compact (no counts) · / filter · m show/hide writes (never sent)
[bold]Run[/]       ctrl+r run · All pages checkbox · ctrl+e CSV · ctrl+j JSON (saved to the folder you started in)
[bold]Table[/]     arrows move · Enter opens the record · click a header or press s to sort (asc, desc, off)
           h hide column · H show all · w cell width · ctrl+f search results
[bold]Rows[/]      y copy cell · Y copy row as JSON · i copy id · u copy link · o open in Cin7 (browser)
[bold]Colours[/]   red backordered/void/cancelled/overdue · yellow in progress · green done
[bold]Tabs[/]      Table · Summary (count by a column, totals) · Record · JSON (both collapsible) · Shape · Downloads · Log
[bold]Safety[/]    reads only. Writes and internal changes are never sent from here."""


class Cin7Tui(App):
    """Operation browser on the left, form and results on the right."""

    TITLE = "cin7"
    SUB_TITLE = "Cin7 Core · public API + internal · read-only"
    CSS = """
    #modebar { height: 1; padding: 0 1; text-style: bold; }
    #modebar.idle { background: $success 25%; }
    #modebar.public { background: $success 35%; }
    #modebar.internal { background: $error 55%; color: white; }
    #modebar.reference { background: $warning 40%; }
    #left { width: 44; border-right: solid $panel; }
    #left.hidden { display: none; }
    #filter { margin: 0 1; }
    #ops { height: 1fr; }
    #right { width: 1fr; }
    #op-title { padding: 0 1; text-style: bold; }
    #op-meta { padding: 0 1; color: $text-muted; }
    #form { height: auto; max-height: 40%; padding: 0 1; }
    .row { height: 1; margin-bottom: 1; }
    .row Label { width: 34; }
    .row Input { width: 1fr; height: 1; border: none; padding: 0 1; background: $boost; }
    .row Input:focus { background: $primary 30%; }
    #actions { height: auto; padding: 0 1; }
    #actions Button { margin-right: 1; }
    #status { padding: 0 1; color: $text-muted; height: 1; }
    #results { height: 1fr; }
    #search { height: 1; border: none; padding: 0 1; margin: 0 0 0 0; background: $boost; }
    #search:focus { background: $primary 30%; }
    #group-by { width: 40; margin: 0 1; }
    #summary { height: 1fr; }
    #table { height: 1fr; }
    """
    BINDINGS = [
        Binding("ctrl+r", "run", "Run"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+f", "focus_search", "Search"),
        Binding("ctrl+e", "export", "CSV"),
        Binding("ctrl+j", "export_json", "JSON", show=False),
        Binding("slash", "focus_filter", "Ops", show=False),
        Binding("left_square_bracket", "sidebar_narrower", "◀", show=False),
        Binding("right_square_bracket", "sidebar_wider", "▶", show=False),
        Binding("c", "toggle_compact", "Compact", show=False),
        Binding("m", "toggle_writes", "Show writes", show=False),
        Binding("s", "sort", "Sort"),
        Binding("h", "hide_column", "Hide col", show=False),
        Binding("H", "show_columns", "Show cols", show=False),
        Binding("w", "cycle_width", "Width", show=False),
        Binding("y", "copy_cell", "Copy", show=True),
        Binding("Y", "copy_row", "Row", show=False),
        Binding("i", "copy_id", "ID", show=False),
        Binding("u", "copy_url", "Link", show=False),
        Binding("o", "open_record", "Open in Cin7"),
        Binding("question_mark", "help", "Help"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, rate: float = DEFAULT_PER_MIN, export_dir: Path | None = None):
        super().__init__()
        self.rate = rate
        self.export_dir = export_dir or Path.cwd()
        self.op: spec.Operation | None = None
        self.data: object = None
        self.rows: list[dict] = []  # everything the call returned
        self.view: list[dict] = []  # after search and sort; what the table shows
        self.cols: list[str] = []
        self.hidden: set[str] = set()
        self.sort_col: str | None = None
        self.sort_desc = False
        self.search_text = ""
        self.group_col: str | None = None
        self.client = None
        self.clients: dict = {}
        self.running = False
        ui = load_ui_state()
        self.sidebar_width = int(ui.get("sidebar_width", 44))
        self.compact = bool(ui.get("compact", False))
        self.show_writes = bool(ui.get("show_writes", False))
        self.cell_width = int(ui.get("cell_width", CELL_WIDTHS[1]))
        self.sidebar_shown = True

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="modebar")
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
                        yield Input(placeholder="ctrl+f  search results…", id="search")
                        yield DataTable(id="table", cursor_type="cell", zebra_stripes=False)
                    with TabPane("Summary", id="tab-summary"):
                        yield Select([("— none —", "")], id="group-by", allow_blank=False, prompt="Group by")
                        yield DataTable(id="summary", cursor_type="row")
                    with TabPane("Record", id="tab-record"):
                        yield Tree("pick a row", id="record-tree")
                    with TabPane("JSON", id="tab-json"):
                        yield Tree("response", id="json-tree")
                    with TabPane("Shape", id="tab-shape"):
                        yield Tree("shape", id="shape-tree")
                    with TabPane("Downloads", id="tab-downloads"):
                        yield DataTable(id="downloads", cursor_type="row")
                    with TabPane("Log", id="tab-log"):
                        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._set_mode(None)
        self._apply_sidebar()
        self._fill_tree("")
        self.query_one("#ops", Tree).focus()
        self.query_one("#table", DataTable).show_cursor = True
        dl = self.query_one("#downloads", DataTable)
        dl.add_columns("When", "What", "Saved to")
        self._fill_downloads()
        self.log_line(f"API root {base_url()} · {self.rate:g} calls/min shared with every cin7 process")

    # --- sidebar -------------------------------------------------------------

    def _apply_sidebar(self) -> None:
        left = self.query_one("#left")
        left.styles.width = self.sidebar_width
        left.set_class(not self.sidebar_shown, "hidden")

    def _remember(self) -> None:
        save_ui_state(
            {
                "sidebar_width": self.sidebar_width,
                "compact": self.compact,
                "cell_width": self.cell_width,
                "show_writes": self.show_writes,
            }
        )

    def action_toggle_sidebar(self) -> None:
        self.sidebar_shown = not self.sidebar_shown
        self._apply_sidebar()
        if not self.sidebar_shown:
            self.query_one("#table", DataTable).focus()

    def action_sidebar_narrower(self) -> None:
        self.sidebar_width = max(24, self.sidebar_width - 4)
        self._apply_sidebar()
        self._remember()

    def action_sidebar_wider(self) -> None:
        self.sidebar_width = min(90, self.sidebar_width + 4)
        self._apply_sidebar()
        self._remember()

    def action_toggle_compact(self) -> None:
        self.compact = not self.compact
        self._fill_tree(self.query_one("#filter", Input).value)
        self._remember()

    def action_toggle_writes(self) -> None:
        """Show or hide writes and never-sent endpoints in the sidebar. Viewing never sends anything."""
        self.show_writes = not self.show_writes
        self._fill_tree(self.query_one("#filter", Input).value)
        self._set_mode(self.op)
        self._remember()
        self.notify("Writes visible in the sidebar (still never sent)" if self.show_writes else "Writes hidden", timeout=3)

    def _set_mode(self, op: spec.Operation | None) -> None:
        """The always-on bar: which credential the selected operation would use, and that writes are off."""
        bar = self.query_one("#modebar", Static)
        seen = "writes VISIBLE (m)" if self.show_writes else "writes hidden (m)"
        if op is None or op.is_public:
            cls = "public" if op else "idle"
            bar.update(f"🔒 WRITES OFF · reads only · API key · {seen}")
        elif op.source == "web":
            cls = "internal"
            bar.update(f"⚠ INTERNAL · signed-in browser session, acting as you in Cin7 · reads only · {seen}")
        else:
            cls = "reference"
            bar.update(f"📖 REFERENCE ONLY · never sent · {seen}")
        for c in ("idle", "public", "internal", "reference"):
            bar.set_class(c == cls, c)

    def _fill_tree(self, text: str) -> None:
        tree = self.query_one("#ops", Tree)
        tree.clear()
        tree.root.expand()
        needle = text.lower().strip()
        by_group = spec.groups()

        def matches(ops: list[spec.Operation]) -> list[spec.Operation]:
            return [
                o
                for o in ops
                if (self.show_writes or first_ok(o))
                and (not needle or needle in f"{o.group} {o.name} {o.path} {o.resource} {o.title}".lower())
            ]

        def section(label: str, groups: list[str], style: str, leaf_style: str = "") -> None:
            found = [(g, matches(by_group[g])) for g in groups if g in by_group]
            found = [(g, m) for g, m in found if m]
            if not found:
                return
            head = tree.root.add(label, data=None, expand=True)
            for g, ops in found:
                sendable = sum(first_ok(o) for o in ops)
                title = MAIN_TITLES.get(g) or ops[0].group_title
                counts = "" if self.compact else f" [dim]{sendable}/{len(ops)}[/]" if self.show_writes else f" [dim]{len(ops)}[/]"
                node = head.add(f"[{style}]{title}[/]{counts}", data=("group", g), expand=bool(needle))
                for o in ops:
                    node.add_leaf(leaf_label(o, leaf_style), data=o)

        public = [g for g, ops in by_group.items() if ops[0].is_public]
        section("[bold]★ MAIN[/]", [g for g in MAIN_GROUPS if g in public], "bold black on #ffb454")
        section("[bold]Public API[/] [dim]· API key[/]", [g for g in public if g not in MAIN_GROUPS], "cyan")
        web = [g for g, ops in by_group.items() if ops[0].source == "web"]
        web.sort(key=lambda g: (g.removeprefix("web-") not in MAIN_WEB, g))
        section("[bold #d070ff]⬢ INTERNAL[/] [dim]· web session, reads only[/]", web, "#d070ff", "#b58ad8")
        ref = [g for g, ops in by_group.items() if ops[0].source in ("wms", "mes", "report-service")]
        section("[bold dim]Reference only[/] [dim]· never sent[/]", ref, "dim")

    # --- events -------------------------------------------------------------

    @on(Input.Changed, "#filter")
    def _filter(self, event: Input.Changed) -> None:
        self._fill_tree(event.value)

    @on(Input.Submitted, "#filter")
    def _filter_done(self) -> None:
        self.query_one("#ops", Tree).focus()

    @on(Input.Changed, "#search")
    def _search(self, event: Input.Changed) -> None:
        self.search_text = event.value
        self._rebuild_view()

    @on(Input.Submitted, "#search")
    def _search_done(self) -> None:
        self.query_one("#table", DataTable).focus()

    @on(Tree.NodeSelected, "#ops")
    def _picked(self, event: Tree.NodeSelected) -> None:
        if isinstance(event.node.data, spec.Operation):
            self.select(event.node.data)
        else:
            event.node.toggle()

    @on(Tree.NodeExpanded)
    def _lazy(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        if isinstance(node.data, tuple) and node.data and node.data[0] == "lazy":
            fill_json(node, node.data[1])
            node.data = ("done", node.data[1])

    @on(Input.Submitted, "#form Input")
    def _submit(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#run")
    def _run_pressed(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#export")
    def _export_pressed(self) -> None:
        self.action_export()

    @on(DataTable.CellSelected, "#table")
    def _cell(self, event: DataTable.CellSelected) -> None:
        idx = event.coordinate.row
        if 0 <= idx < len(self.view):
            self._show_record(self.view[idx])

    @on(DataTable.HeaderSelected, "#table")
    def _header(self, event: DataTable.HeaderSelected) -> None:
        shown = self._shown_cols()
        if 0 <= event.column_index < len(shown):
            self._toggle_sort(shown[event.column_index])

    @on(DataTable.RowSelected, "#downloads")
    def _download_row(self, event: DataTable.RowSelected) -> None:
        path = str(event.row_key.value)
        if Path(path).exists():
            self._open_path(path)

    @on(Select.Changed, "#group-by")
    def _group_changed(self, event: Select.Changed) -> None:
        self.group_col = str(event.value) or None
        self._fill_summary()

    def _show_record(self, row: dict) -> None:
        tree = self.query_one("#record-tree", Tree)
        tree.clear()
        label = row.get("CustomerReference") or row.get("OrderNumber") or row.get("Name") or row.get("SKU") or "record"
        tree.root.set_label(f"[bold]{rich_escape(str(label))}[/]")
        fill_json(tree.root, row)
        tree.root.expand()
        self.query_one("#results", TabbedContent).active = "tab-record"
        tree.focus()

    # --- actions ------------------------------------------------------------

    def select(self, op: spec.Operation) -> None:
        """Show `op`'s form and shape."""
        self.op = op
        self._set_mode(op)
        if op.is_public:
            kind = "[yellow]WRITE · not runnable here[/]" if op.is_write else "read"
        elif op.runnable:
            kind = "[#d070ff]INTERNAL read · browser session[/]" + (" · download" if op.file else "")
        else:
            kind = "[#d070ff]INTERNAL · never sent[/]" if op.source == "web" else "[dim]reference only[/]"
        self.query_one("#op-title", Static).update(f"cin7 {op.full_name}  [dim]{kind}[/]")
        alias = f" · alias {', '.join(op.aliases)}" if op.aliases else ""
        pages = f" · used on {', '.join(op.pages[:4])}" if op.pages else ""
        self.query_one("#op-meta", Static).update(f"{op.method} {op.path} · {op.title}{alias}{pages}")
        form = self.query_one("#form", VerticalScroll)
        form.remove_children()
        rows = []
        for p in op.params:
            star = "*" if p.required else ""
            rows.append(Horizontal(Label(f"{p.name}{star} [dim]{p.type}[/]"), ParamInput(p), classes="row"))
        if not rows:
            rows.append(Label("[dim]No query parameters[/]"))
        form.mount_all(rows)
        if op.read_via_post:
            form.mount(Horizontal(Label("[dim]JSON query body[/]"), Input(placeholder="{}", id="post-body"), classes="row"))
        sendable = first_ok(op)
        box = self.query_one("#all-pages", Checkbox)
        box.disabled = not op.paged or not sendable
        box.value = False
        self.query_one("#run", Button).disabled = not sendable
        shape = {"params": [p.__dict__ for p in op.params], "request": op.request, "response": op.response}
        tree = self.query_one("#shape-tree", Tree)
        tree.clear()
        tree.root.set_label(f"[bold]{op.full_name}[/]")
        fill_json(tree.root, shape)
        tree.root.expand()
        if not sendable:
            self.query_one("#results", TabbedContent).active = "tab-shape"
            self.set_status(
                "Writes can't be sent from the TUI. Use the CLI with --allow-write."
                if op.is_public
                else "Not sent from here: internal endpoints that change things stay unreachable."
            )
        else:
            self.set_status("Fill in parameters, then ^R" + (" (saves a file)" if op.file else ""))

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
        self.sidebar_shown = True
        self._apply_sidebar()
        self.query_one("#filter", Input).focus()

    def action_focus_search(self) -> None:
        self.query_one("#results", TabbedContent).active = "tab-table"
        self.query_one("#search", Input).focus()

    def action_run(self) -> None:
        op = self.op
        if op is None or self.running:
            return
        try:
            runner.check_runnable(op)
        except runner.NotRunnable as exc:
            self.notify(str(exc), severity="warning")
            return
        if op.is_public and op.is_write:  # belt and braces: the button is disabled too
            self.notify("Read-only: writes can't be sent from the TUI", severity="warning")
            return
        query = self.collect_query()
        if query is None:
            return
        body = None
        if op.read_via_post:
            raw = self.query_one("#post-body", Input).value.strip()
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                self.notify(f"body is not JSON: {exc}", severity="error")
                return
        all_pages = op.paged and self.query_one("#all-pages", Checkbox).value
        self.running = True
        self.query_one("#run", Button).disabled = True
        self.set_status("Running…")
        self._call(op, query, body, all_pages)

    @work(thread=True, exclusive=True)
    def _call(self, op: spec.Operation, query: dict, body: object, all_pages: bool) -> None:
        started = time.monotonic()
        try:
            client = self._client(op)
            self.call_from_thread(self.log_line, f"{op.method} {runner.request_for(op, query, body).url}")
            data = runner.fetch(
                client, op, query, body, all_pages=all_pages, progress=False,
                on_page=lambda p: self.call_from_thread(self.set_status, f"Fetching page {p}…"),
            )
            if op.file:
                path = runner.save_file(data, op, self.export_dir)
                data = {"saved": path}
                self.call_from_thread(self._record_download, op.full_name, path)
        except (ApiError, auth.AuthError, ValueError, runner.NotRunnable) as exc:
            self.call_from_thread(self._failed, str(exc))
            return
        self.call_from_thread(self._show, op, data, time.monotonic() - started)

    def _client(self, op: spec.Operation):
        key = "public" if op.is_public else "web"
        if key not in self.clients:
            self.clients[key] = runner.make_client(op, self.rate)
        self.client = self.clients[key]
        return self.client

    def _failed(self, message: str) -> None:
        self.running = False
        self.query_one("#run", Button).disabled = False
        self.set_status(f"[red]{escape(message)}[/]")
        self.log_line(f"[red]{escape(message)}[/]")
        self.notify(message, severity="error", timeout=8)

    # --- results ----------------------------------------------------------------

    def _show(self, op: spec.Operation, data: object, seconds: float) -> None:
        self.running = False
        self.query_one("#run", Button).disabled = False
        self.data = data
        self.rows = output.rows_of(data, op.list_key)
        self.cols = output.columns(self.rows)
        self.hidden = set()
        self.sort_col, self.sort_desc, self.search_text = None, False, ""
        self.query_one("#search", Input).value = ""
        self._rebuild_view()
        self._fill_group_choices()
        jt = self.query_one("#json-tree", Tree)
        jt.clear()
        jt.root.set_label(f"[bold]{op.full_name}[/]")
        fill_json(jt.root, data)
        jt.root.expand()
        self.query_one("#export", Button).disabled = not self.rows
        total = data.get("Total", data.get("total")) if isinstance(data, dict) else None
        more = f" of {total}" if total is not None else ""
        clipped = f" (table shows first {MAX_TABLE_ROWS})" if len(self.rows) > MAX_TABLE_ROWS else ""
        calls = self.client.calls if self.client else 0
        self.set_status(f"{len(self.rows)} rows{more}{clipped} · {seconds:.1f}s · {calls} calls this session · ? help")
        self.log_line(f"{op.full_name}: {len(self.rows)} rows{more}")
        self.query_one("#results", TabbedContent).active = "tab-table"
        self.query_one("#table", DataTable).focus()

    def _shown_cols(self) -> list[str]:
        return [c for c in self.cols if c not in self.hidden]

    def _rebuild_view(self) -> None:
        """Search, sort, then draw the table."""
        needle = self.search_text.lower().strip()
        rows = self.rows
        if needle:
            shown = self._shown_cols()
            rows = [r for r in rows if any(needle in output.cell(r.get(c)).lower() for c in shown)]
        self.view = rowtools.sort_rows(rows, self.sort_col, self.sort_desc)
        self._draw_table()
        self._fill_summary()

    def _draw_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        cols = self._shown_cols()
        if not cols:
            return
        arrow = {False: " ▲", True: " ▼"}
        for c in cols:
            table.add_column(c + (arrow[self.sort_desc] if c == self.sort_col else ""), key=c)
        table.fixed_columns = rowtools.frozen_columns(cols)
        for i, r in enumerate(self.view[:MAX_TABLE_ROWS]):
            colour = rowtools.row_style(r)
            link = rowtools.record_url(self.op, r) if self.op is not None else None
            cells = []
            for n, c in enumerate(cols):
                text = output.cell(r.get(c))
                text = text if len(text) <= self.cell_width else text[: self.cell_width - 1] + "…"
                style = Style(color=colour, link=link if n == 0 and link else None, underline=bool(n == 0 and link))
                cells.append(Text(text, style=style))
            table.add_row(*cells, key=str(i))

    def _fill_group_choices(self) -> None:
        sel = self.query_one("#group-by", Select)
        options = [("— none —", "")] + [(c, c) for c in self.cols if self._groupable(c)]
        sel.set_options(options)
        pick = next((c for c in ("Status", "OrderStatus", "Type", "Supplier", "Customer", "Location") if c in self.cols), "")
        sel.value = pick
        self.group_col = pick or None

    def _groupable(self, col: str) -> bool:
        vals = [r.get(col) for r in self.rows[:2000]]
        scalar = [v for v in vals if v is not None and not isinstance(v, (list, dict))]
        return bool(scalar) and len({str(v) for v in scalar}) <= max(50, len(scalar) // 3)

    def _fill_summary(self) -> None:
        table = self.query_one("#summary", DataTable)
        table.clear(columns=True)
        rows = self.view
        if not self.group_col or not rows:
            return
        numeric = [
            c for c in self.cols
            if c != self.group_col
            and any(isinstance(r.get(c), (int, float)) and not isinstance(r.get(c), bool) for r in rows)
        ][:6]
        table.add_columns(self.group_col, "Count", *[f"Σ {c}" for c in numeric])
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(output.cell(r.get(self.group_col)) or "(blank)", []).append(r)

        def total(rs: list[dict], c: str) -> str:
            t = sum(v for v in (r.get(c) for r in rs) if isinstance(v, (int, float)) and not isinstance(v, bool))
            return f"{t:,.2f}".rstrip("0").rstrip(".")

        for key, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            table.add_row(key, str(len(rs)), *[total(rs, c) for c in numeric])
        table.add_row("[Total]", str(len(rows)), *[total(rows, c) for c in numeric])

    # --- table keys -------------------------------------------------------------

    def _cursor(self) -> tuple[dict | None, str | None]:
        """Row and column under the table cursor."""
        table = self.query_one("#table", DataTable)
        shown = self._shown_cols()
        r, c = table.cursor_row, table.cursor_column
        row = self.view[r] if 0 <= r < len(self.view) else None
        return row, shown[c] if 0 <= c < len(shown) else None

    def _toggle_sort(self, col: str) -> None:
        if self.sort_col != col:
            self.sort_col, self.sort_desc = col, False
        elif not self.sort_desc:
            self.sort_desc = True
        else:
            self.sort_col, self.sort_desc = None, False
        self._rebuild_view()
        self.set_status(f"sorted by {self.sort_col} {'▼' if self.sort_desc else '▲'}" if self.sort_col else "sort off")

    def action_sort(self) -> None:
        _, col = self._cursor()
        if col:
            self._toggle_sort(col)

    def action_hide_column(self) -> None:
        _, col = self._cursor()
        if col and len(self._shown_cols()) > 1:
            self.hidden.add(col)
            if self.sort_col == col:
                self.sort_col = None
            self._rebuild_view()
            self.set_status(f"hid {col} · H shows all columns")

    def action_show_columns(self) -> None:
        self.hidden = set()
        self._rebuild_view()

    def action_cycle_width(self) -> None:
        i = CELL_WIDTHS.index(self.cell_width) if self.cell_width in CELL_WIDTHS else 0
        self.cell_width = CELL_WIDTHS[(i + 1) % len(CELL_WIDTHS)]
        self._draw_table()
        self._remember()
        self.set_status(f"cell width {self.cell_width}")

    # --- copy and open -----------------------------------------------------------

    def _copy(self, text: str, what: str) -> None:
        if shutil.which("pbcopy"):
            try:
                subprocess.run(["pbcopy"], input=text.encode(), timeout=5, check=True)
            except (OSError, subprocess.SubprocessError):
                self.copy_to_clipboard(text)
        else:
            self.copy_to_clipboard(text)
        self.notify(f"Copied {what}", timeout=2)

    def action_copy_cell(self) -> None:
        row, col = self._cursor()
        if row is not None and col:
            v = row.get(col)
            self._copy(json.dumps(v) if isinstance(v, (dict, list)) else "" if v is None else str(v), col)

    def action_copy_row(self) -> None:
        row, _ = self._cursor()
        if row is not None:
            self._copy(json.dumps(row, indent=2, default=str), "row as JSON")

    def action_copy_id(self) -> None:
        row, _ = self._cursor()
        rid = rowtools.record_id(row) if row else None
        if rid:
            self._copy(rid, "id")
        else:
            self.notify("This row has no id", severity="warning")

    def _url(self) -> str | None:
        row, _ = self._cursor()
        return rowtools.record_url(self.op, row) if row and self.op else None

    def action_copy_url(self) -> None:
        url = self._url()
        if url:
            self._copy(url, "Cin7 link")
        else:
            self.notify("No Cin7 page known for this row", severity="warning")

    def _open_url(self, url: str) -> None:
        webbrowser.open(url)

    def action_open_record(self) -> None:
        url = self._url()
        if url:
            self._open_url(url)
            self.notify(f"Opening {url}", timeout=3)
        else:
            self.notify("No Cin7 page known for this row", severity="warning")

    def _open_path(self, path: str) -> None:
        if shutil.which("open"):
            subprocess.Popen(["open", path])

    def action_help(self) -> None:
        self.notify(HELP, title="Keys", timeout=30)

    # --- exports and downloads ------------------------------------------------------

    def _record_download(self, what: str, path: str) -> None:
        add_history({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "what": what, "path": path})
        self._fill_downloads()
        self.log_line(f"saved {path}")

    def _fill_downloads(self) -> None:
        table = self.query_one("#downloads", DataTable)
        table.clear()
        for e in reversed(load_history()):
            table.add_row(e["when"], e["what"], e["path"], key=e["path"])

    def _export(self, fmt: str) -> None:
        if self.op is None or self.data is None:
            self.notify("Run something first", severity="warning")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.export_dir / f"cin7-{self.op.group}-{self.op.name}-{stamp}.{fmt}"
        path.write_text(output.render(self.data, fmt, self.op.list_key) + "\n")
        self._record_download(f"{self.op.full_name} ({fmt})", str(path))
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
