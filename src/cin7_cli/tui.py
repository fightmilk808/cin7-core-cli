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
from textual.screen import ModalScreen
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
    OptionList,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from . import auth, output, printing, ratelimit, rowtools, runner, spec
from .cli import TYPE_HINT, TYPES
from .client import ApiError, base_url
from .safety import PRINT_NOTE
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


# One-key read-only views: (key, label, op group, op name, query params, after-run tweaks).
# Status values come from the Blueprint's value lists; check them against your data.
QUICK_VIEWS = (
    ("1", "Open production orders", "production", "order-list", {"Limit": "100"}, {"hide_done": True, "sort": "RequiredByDate"}),
    ("2", "Sales waiting on stock", "sale", "list",
     {"Limit": "100", "OrderStatus": "AUTHORISED", "CombinedPickStatus": "NOT AVAILABLE"}, {}),
    ("3", "Latest MRP run (internal)", "web-mrp", "mrp-v-mrp", {"limit": "25", "page": "1", "start": "0"},
     {"sort": "date", "desc": True}),
    ("4", "Sales awaiting shipment", "sale", "list",
     {"Limit": "100", "OrderStatus": "AUTHORISED", "CombinedShippingStatus": "NOT SHIPPED"}, {"sort": "ShipBy"}),
)
MAX_RECENT = 8
MAX_HISTORY = 30


def op_by_name(full_name: str) -> spec.Operation | None:
    """`group name` back to its operation."""
    group, _, name = full_name.partition(" ")
    return spec.find(group, name)


class NameScreen(ModalScreen):
    """Ask for one line of text. Dismisses with it, or None on Escape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    NameScreen { align: center middle; }
    NameScreen > Vertical { width: 60; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield Input(id="name")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    @on(Input.Submitted)
    def _done(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PickScreen(ModalScreen):
    """Choose one of several saved items; `delete` removes the highlighted one."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("delete", "remove", "Delete")]
    DEFAULT_CSS = """
    PickScreen { align: center middle; }
    PickScreen > Vertical { width: 70; height: auto; max-height: 24; border: round $accent; padding: 1 2; background: $surface; }
    """

    def __init__(self, title: str, items: dict[str, str]):
        super().__init__()
        self.title_text = title
        self.items = items

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[bold]{self.title_text}[/]  [dim]Enter loads · Delete removes · Esc closes[/]")
            yield OptionList(*[f"{k}  [dim]{v}[/]" for k, v in self.items.items()], id="pick")

    def on_mount(self) -> None:
        self.query_one("#pick", OptionList).focus()

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(("load", list(self.items)[event.option_index]))

    def action_remove(self) -> None:
        ol = self.query_one("#pick", OptionList)
        if ol.highlighted is not None:
            self.dismiss(("delete", list(self.items)[ol.highlighted]))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen):
    """Every key, in one overlay."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", "Close"), Binding("q", "close", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > VerticalScroll { width: 110; height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("[bold #ffb454]Cin7 TUI keys[/]  [dim]Esc, ? or q closes[/]\n")
            yield Static(help_table())

    def action_close(self) -> None:
        self.dismiss(None)


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


def json_label(key: object, value: object) -> str:
    """Tree label for one JSON member, colored by type."""
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


class ParamSelect(Select):
    """A dropdown bound to one query parameter that has a fixed set of values (blank = leave out)."""

    def __init__(self, param: spec.Param):
        super().__init__([(c, c) for c in param.choices], allow_blank=True, prompt="any" if not param.required else "choose",
                         id=f"p-{param.flag}")
        self.param = param


def raw_value(widget) -> str:
    """The text a parameter widget holds: blank when a dropdown is unset."""
    if isinstance(widget, Select):
        # Unset is a NoSelection sentinel (Select.NULL / BLANK, by Textual version), never a str.
        return widget.value if isinstance(widget.value, str) else ""
    return widget.value.strip()


class ParamInput(Input):
    """An Input bound to one query parameter."""

    def __init__(self, param: spec.Param):
        hint = TYPE_HINT.get(param.type, "TEXT")
        super().__init__(placeholder=f"{hint}{'  (required)' if param.required else ''}", id=f"p-{param.flag}")
        self.param = param


HELP_ROWS = (
    ("Run", "ctrl+r", "run the selected operation (Enter in any field does too)"),
    ("", "f", "show or hide the filter form (it hides itself after a run)"),
    ("", "ctrl+e · ctrl+j", "export the result as CSV · JSON (listed on the Downloads tab)"),
    ("Quick views", "1  2  3  4", "open production orders · sales waiting on stock · latest MRP run · sales awaiting shipment"),
    ("Pinned · Saved", "p", "pin or unpin the selected operation (pinned and recent sit at the top of the sidebar)"),
    ("", "ctrl+s · ctrl+o", "save the form under a name · load or delete a saved filter"),
    ("History", "<  >", "back and forward through earlier results"),
    ("", "k", "freeze the current table in a right-hand pane to compare"),
    ("Sidebar", "ctrl+b", "hide or show"),
    ("", "[  ]", "narrower · wider"),
    ("", "c · m · /", "compact (no counts) · show or hide writes (never sent) · filter operations"),
    ("Table", "s  or click header", "sort: ascending, descending, off"),
    ("", "h · H · w", "hide column · show all columns · cell width"),
    ("", "ctrl+f", "search the results as you type"),
    ("", "Enter", "open the row in the Record tab"),
    ("Rows", "y · Y · i · u", "copy cell · row as JSON · id · link"),
    ("", "o", "open the record in Cin7 (browser)"),
    ("Print", "Space · ctrl+a", "mark a row · mark all"),
    ("", "P", "print the marked rows: pick a template, then type a confirmation"),
    ("Colors", "", "red: backordered, void, cancelled or overdue · yellow: in progress · green: done"),
    ("Safety", "", "reads only. Writes and internal changes are never sent. The bar at the top names the credential in use."),
)


def help_table():
    """The key list as an aligned two-column table."""
    from rich.table import Table

    t = Table.grid(padding=(0, 2), expand=True)
    t.add_column(style="bold #ffb454", no_wrap=True)
    t.add_column(style="bold", no_wrap=True)
    t.add_column(ratio=1)
    last = None
    for section, keys, what in HELP_ROWS:
        if section and last is not None:
            t.add_row("", "", "")
        last = section or last
        t.add_row(section, keys, what)
    return t


class Cin7Tui(App):
    """Operation browser on the left, form and results on the right."""

    TITLE = "cin7"
    SUB_TITLE = ""
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
    #form { height: auto; max-height: 14; padding: 0 1; layout: grid; grid-size: 2; grid-gutter: 0 3; }
    #form.collapsed { display: none; }
    #form > Static, #form > Label { column-span: 2; }
    .row { height: 1; }
    .row Label { width: 24; color: $text-muted; }
    .row Input { width: 1fr; height: 1; border: none; padding: 0 1; background: $boost; }
    .row Input:focus { background: $primary 30%; }
    .row Select { width: 1fr; height: 1; }
    .row Select > SelectCurrent { border: none; height: 1; padding: 0 1; background: $boost; }
    .row Select:focus > SelectCurrent { background: $primary 30%; }
    #actions { height: 1; padding: 0 1; margin: 1 0 0 0; }
    #actions Button { height: 1; min-width: 12; border: none; margin-right: 2; padding: 0 2; }
    #actions Checkbox { height: 1; border: none; padding: 0; margin-right: 2; background: transparent; }
    #status { padding: 0 1; color: $text-muted; height: 1; }
    #results { height: 1fr; }
    #search { height: 1; border: none; padding: 0 1; margin: 0 0 0 0; background: $boost; }
    #search:focus { background: $primary 30%; }
    #group-by { width: 40; margin: 0 1; }
    #summary { height: 1fr; }
    #tables { height: 1fr; }
    #table { height: 1fr; width: 1fr; }
    #side { display: none; width: 1fr; border-left: solid $panel; }
    #side.on { display: block; }
    #side-title { color: $text-muted; padding: 0 1; }
    #table2 { height: 1fr; }
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
        Binding("f", "toggle_form", "Filters"),
        Binding("p", "pin", "Pin", show=False),
        Binding("1", "quick('1')", "Prod orders", show=False),
        Binding("2", "quick('2')", "Waiting on stock", show=False),
        Binding("3", "quick('3')", "MRP run", show=False),
        Binding("4", "quick('4')", "To ship", show=False),
        Binding("ctrl+s", "save_filter", "Save filter", show=False),
        Binding("ctrl+o", "load_filter", "Saved", show=False),
        Binding("less_than_sign", "history_back", "Back", show=False),
        Binding("greater_than_sign", "history_forward", "Fwd", show=False),
        Binding("k", "pin_result", "Pin result", show=False),
        Binding("space", "mark_row", "Mark", show=False),
        Binding("ctrl+a", "mark_all", "Mark all", show=False),
        Binding("P", "print_rows", "Print", show=True),
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
        self.pins: list[str] = list(ui.get("pins", []))
        self.recent: list[str] = list(ui.get("recent", []))
        self.saved: dict[str, dict] = dict(ui.get("saved", {}))
        self.history: list[dict] = []
        self.hpos = -1
        self.after: dict = {}
        self.side_open = False
        self.marked: set[str] = set()
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
                        with Horizontal(id="tables"):
                            yield DataTable(id="table", cursor_type="cell", zebra_stripes=False)
                            with Vertical(id="side"):
                                yield Static("", id="side-title")
                                yield DataTable(id="table2", cursor_type="row")
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
                "pins": self.pins,
                "recent": self.recent,
                "saved": self.saved,
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
        tree.show_root = False
        tree.guide_depth = 3
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
                title = MAIN_TITLES.get(g) or ops[0].group_title.removeprefix("Web · ")
                counts = "" if self.compact else f" [dim]{sendable}/{len(ops)}[/]" if self.show_writes else f" [dim]{len(ops)}[/]"
                node = head.add(f"[{style}]{title}[/]{counts}", data=("group", g), expand=bool(needle))
                for o in ops:
                    node.add_leaf(leaf_label(o, leaf_style), data=o)

        def quick_section(label: str, names: list[str]) -> None:
            ops = [o for o in (op_by_name(n) for n in names) if o is not None and (not needle or needle in o.full_name)]
            if not ops:
                return
            head = tree.root.add(label, data=None, expand=True)
            for o in ops:
                head.add_leaf(f"{o.full_name}", data=o)

        quick_section("[bold]◆ PINNED[/]", self.pins)
        quick_section("[bold]↺ RECENT[/]", [n for n in self.recent if n not in self.pins][:MAX_RECENT])
        public = [g for g, ops in by_group.items() if ops[0].is_public]
        section("[bold]★ MAIN[/]", [g for g in MAIN_GROUPS if g in public], "bold black on #ffb454")
        section("[bold cyan]◇ PUBLIC API[/] [dim]· API key[/]", [g for g in public if g not in MAIN_GROUPS], "cyan")
        web = [g for g, ops in by_group.items() if ops[0].source == "web"]
        web.sort(key=lambda g: (g.removeprefix("web-") not in MAIN_WEB, g))
        section("[bold #d070ff]⬢ INTERNAL[/] [dim]· your login · reads only[/]", web, "#d070ff", "#b58ad8")
        prints = [p for p in printing.endpoints() if not needle or needle in f"{p.path} {p.where}".lower()]
        if prints:
            head = tree.root.add("[bold #4fd1c5]⎙ PRINT[/] [dim]· where printing lives[/]", data=None, expand=bool(needle))
            for pe in prints:
                mark = "" if pe.sendable else " [dim](not mapped)[/]"
                head.add_leaf(f"[#4fd1c5]{pe.path}[/] [dim]{'bulk' if pe.bulk else 'one'}[/]{mark}", data=("print", pe))
        ref = [g for g, ops in by_group.items() if ops[0].source in ("wms", "mes", "report-service")]
        section("[bold dim]▫ REFERENCE[/] [dim]· never sent[/]", ref, "dim")

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
        data = event.node.data
        if isinstance(data, spec.Operation):
            self.select(data)
        elif isinstance(data, tuple) and data and data[0] == "print":
            self._show_print_info(data[1])
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
            widget = ParamSelect(p) if p.choices else ParamInput(p)
            rows.append(Horizontal(Label(f"{p.name}{star}"), widget, classes="row"))
        if not rows:
            rows.append(Label("[dim]No query parameters[/]"))
        form.remove_class("collapsed")
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
        for inp in self._param_widgets():
            raw = raw_value(inp)
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

    def _param_widgets(self) -> list:
        """Every parameter widget in the form, inputs and dropdowns, in form order."""
        return [w for w in self.query("#form *") if isinstance(w, (ParamInput, ParamSelect))]

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
        after, self.after = self.after, {}
        if after.get("hide_done"):
            self.rows = [r for r in self.rows if rowtools.row_style(r) != "green"]
        self.cols = output.columns(self.rows)
        self.hidden = set()
        self.sort_col, self.sort_desc, self.search_text = None, False, ""
        want = after.get("sort")
        if want:
            low = want.lower()
            self.sort_col = next((c for c in self.cols if c == want), None) or next(
                (c for c in self.cols if low in c.lower()), None
            )
            self.sort_desc = bool(after.get("desc"))
        self.query_one("#search", Input).value = ""
        self._touch_recent(op)
        self._push_history(op)
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
        self.set_status(f"{len(self.rows)} rows{more}{clipped} · {seconds:.1f}s · {calls} calls · f edit filters · ? help")
        self.log_line(f"{op.full_name}: {len(self.rows)} rows{more}")
        self.query_one("#results", TabbedContent).active = "tab-table"
        self.query_one("#form").add_class("collapsed")
        self.query_one("#table", DataTable).focus()

    def action_toggle_form(self) -> None:
        form = self.query_one("#form")
        form.toggle_class("collapsed")
        if not form.has_class("collapsed"):
            first = next(iter(self._param_widgets()), None)
            if first:
                first.focus()

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
            color = rowtools.row_style(r)
            link = rowtools.record_url(self.op, r) if self.op is not None else None
            cells = []
            rid = rowtools.record_id(r)
            for n, c in enumerate(cols):
                text = output.cell(r.get(c))
                if n == 0 and rid and rid in self.marked:
                    text = "● " + text
                text = text if len(text) <= self.cell_width else text[: self.cell_width - 1] + "…"
                is_url = text.startswith(("http://", "https://"))
                drill = bool(n == 0 and link) or is_url
                style = Style(color=color, link=(link if n == 0 and link else text if is_url else None),
                              underline=drill, bold=drill)
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

    # --- pinned, recent, quick views, saved filters, history ------------------------

    def _touch_recent(self, op: spec.Operation) -> None:
        self.recent = [op.full_name] + [n for n in self.recent if n != op.full_name]
        self.recent = self.recent[: MAX_RECENT * 2]
        self._remember()
        self._fill_tree(self.query_one("#filter", Input).value)

    def action_pin(self) -> None:
        if self.op is None:
            return
        name = self.op.full_name
        if name in self.pins:
            self.pins.remove(name)
            self.notify(f"Unpinned {name}", timeout=2)
        else:
            self.pins.insert(0, name)
            self.notify(f"Pinned {name}", timeout=2)
        self._remember()
        self._fill_tree(self.query_one("#filter", Input).value)

    def _fill_form(self, params: dict[str, str]) -> None:
        by_name = {w.param.name.lower(): w for w in self._param_widgets()}
        for k, v in params.items():
            w = by_name.get(k.lower())
            if w is None:
                continue
            if isinstance(w, Select):
                match = next((c for c in w.param.choices if c.lower() == str(v).lower()), None)
                if match:
                    w.value = match
            else:
                w.value = str(v)

    def _run_when_ready(self, after: dict | None = None) -> None:
        """The form is mounted asynchronously; run once it exists."""
        self.after = after or {}
        self.call_after_refresh(self.action_run)

    def action_quick(self, key: str) -> None:
        view = next((v for v in QUICK_VIEWS if v[0] == key), None)
        if view is None:
            return
        _, label, group, name, params, after = view
        op = spec.find(group, name)
        if op is None or not first_ok(op):
            self.notify(f"{label}: operation not available", severity="warning")
            return
        self.select(op)
        self.set_status(f"{label} …")
        self.call_after_refresh(lambda: (self._fill_form(params), self._run_when_ready(after)))

    def action_save_filter(self) -> None:
        if self.op is None:
            self.notify("Pick an operation first", severity="warning")
            return

        def done(name: str | None) -> None:
            if not name or self.op is None:
                return
            params = {w.param.name: raw_value(w) for w in self._param_widgets() if raw_value(w)}
            self.saved[name] = {"op": self.op.full_name, "params": params,
                                "all_pages": self.query_one("#all-pages", Checkbox).value}
            self._remember()
            self.notify(f"Saved filter “{name}”", timeout=3)

        self.push_screen(NameScreen(f"Save {self.op.full_name} with these values as:"), done)

    def action_load_filter(self) -> None:
        if not self.saved:
            self.notify("No saved filters yet: ctrl+s saves the current form", severity="warning")
            return

        def done(choice: tuple | None) -> None:
            if not choice:
                return
            kind, name = choice
            if kind == "delete":
                self.saved.pop(name, None)
                self._remember()
                self.notify(f"Deleted “{name}”", timeout=2)
                return
            item = self.saved.get(name)
            op = op_by_name(item["op"]) if item else None
            if op is None or not first_ok(op):
                self.notify(f"“{name}”: operation not available", severity="warning")
                return
            self.select(op)

            def go() -> None:
                self._fill_form(item.get("params", {}))
                if item.get("all_pages") and op.paged:
                    self.query_one("#all-pages", Checkbox).value = True
                self._run_when_ready()

            self.call_after_refresh(go)

        self.push_screen(PickScreen("Saved filters", {n: v["op"] for n, v in self.saved.items()}), done)

    def _push_history(self, op: spec.Operation) -> None:
        snap = {"op": op, "data": self.data, "rows": self.rows, "cols": self.cols, "when": time.strftime("%H:%M:%S"),
                "query": {w.param.name: raw_value(w) for w in self._param_widgets() if raw_value(w)}}
        del self.history[self.hpos + 1 :]
        self.history.append(snap)
        self.history = self.history[-MAX_HISTORY:]
        self.hpos = len(self.history) - 1

    def _restore(self, snap: dict) -> None:
        self.op = snap["op"]
        self._set_mode(self.op)
        self.data, self.rows, self.cols = snap["data"], snap["rows"], snap["cols"]
        self.hidden, self.sort_col, self.sort_desc, self.search_text = set(), None, False, ""
        self.query_one("#search", Input).value = ""
        self.query_one("#op-title", Static).update(f"cin7 {self.op.full_name}  [dim]history {self.hpos + 1}/{len(self.history)}[/]")
        self._rebuild_view()
        self._fill_group_choices()
        jt = self.query_one("#json-tree", Tree)
        jt.clear()
        jt.root.set_label(f"[bold]{self.op.full_name}[/]")
        fill_json(jt.root, self.data)
        jt.root.expand()
        self.set_status(f"{len(self.rows)} rows · from {snap['when']} · history {self.hpos + 1}/{len(self.history)} (< >)")

    def action_history_back(self) -> None:
        if self.hpos > 0:
            self.hpos -= 1
            self._restore(self.history[self.hpos])
        else:
            self.notify("No earlier result", timeout=2)

    def action_history_forward(self) -> None:
        if self.hpos < len(self.history) - 1:
            self.hpos += 1
            self._restore(self.history[self.hpos])
        else:
            self.notify("No later result", timeout=2)

    def action_pin_result(self) -> None:
        """Show a frozen copy of the current table in a right-hand pane, to compare with the next result."""
        side = self.query_one("#side")
        if self.side_open:
            self.side_open = False
            side.remove_class("on")
            return
        if not self.view or self.op is None:
            self.notify("Run something first", severity="warning")
            return
        self.side_open = True
        side.add_class("on")
        t2 = self.query_one("#table2", DataTable)
        t2.clear(columns=True)
        cols = self._shown_cols()
        t2.add_columns(*cols)
        for r in self.view[:MAX_TABLE_ROWS]:
            t2.add_row(*[Text(output.cell(r.get(c))[: self.cell_width], style=Style(color=rowtools.row_style(r))) for c in cols])
        self.query_one("#side-title", Static).update(
            f"pinned · {self.op.full_name} · {len(self.view)} rows · {time.strftime('%H:%M:%S')}  (k closes)"
        )

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # --- printing ---------------------------------------------------------------

    def _show_print_info(self, pe: printing.PrintEndpoint) -> None:
        self.op = None
        self._set_mode(None)
        self.query_one("#op-title", Static).update(f"[bold #4fd1c5]⎙ {pe.path}[/]  [dim]{'bulk' if pe.bulk else 'one record'}[/]")
        self.query_one("#op-meta", Static).update(f"Lives on: {pe.where or 'unknown'}")
        form = self.query_one("#form", VerticalScroll)
        form.remove_children()
        how = (
            "Open a list (Sales, Purchases, Products, Customers, Production orders), mark rows with Space "
            "(ctrl+a marks all), then press [bold]P[/]. You pick a template, then type a confirmation."
            if pe.sendable and pe.bulk
            else "Prints one record. From the CLI: [bold]cin7 print send[/]."
            if pe.sendable
            else "The map doesn't say what this endpoint takes, so it can't be sent from here."
        )
        form.mount(Static(how + f"\n[dim]{PRINT_NOTE}[/]"))
        self.query_one("#run", Button).disabled = True
        self.query_one("#all-pages", Checkbox).disabled = True
        tree = self.query_one("#shape-tree", Tree)
        tree.clear()
        tree.root.set_label(f"[bold]{pe.path}[/]")
        fill_json(tree.root, {"method": pe.method, "params": list(pe.params), "effect": pe.effect, "screens": list(pe.pages),
                              "menu": list(pe.labels), "sendable": pe.sendable})
        tree.root.expand()
        self.query_one("#results", TabbedContent).active = "tab-shape"
        self.set_status("Print actions are never sent from this view.")

    def action_mark_row(self) -> None:
        row, _ = self._cursor()
        rid = rowtools.record_id(row) if row else None
        if not rid:
            return
        self.marked ^= {rid}
        table = self.query_one("#table", DataTable)
        at = (table.cursor_row, table.cursor_column)
        self._draw_table()
        table.move_cursor(row=at[0], column=at[1])
        self.set_status(f"{len(self.marked)} marked · P prints them")

    def action_mark_all(self) -> None:
        ids = {rid for r in self.view if (rid := rowtools.record_id(r))}
        self.marked = set() if ids <= self.marked else ids
        self._draw_table()
        self.set_status(f"{len(self.marked)} marked · P prints them")

    def action_print_rows(self) -> None:
        """Bulk print: marked rows (or the cursor row) of the current list, through your Cin7 session."""
        if self.op is None:
            return
        endpoints = printing.for_list(self.op)
        if not endpoints:
            self.notify("No list print for this view. The Print section in the sidebar lists where printing lives.", severity="warning")
            return
        row, _ = self._cursor()
        ids = [rid for r in self.view if (rid := rowtools.record_id(r)) and rid in self.marked]
        if not ids and row is not None and (one := rowtools.record_id(row)):
            ids = [one]
        if not ids:
            self.notify("Mark rows with Space first", severity="warning")
            return

        def with_endpoint(path: str | None) -> None:
            pe = printing.find(path) if path else None
            if pe is None:
                return
            if pe.template_param:
                self._pick_template(pe, ids)
            else:
                self._confirm_print(pe, ids, None, "")

        if len(endpoints) == 1:
            with_endpoint(endpoints[0].path)
        else:
            items = {p.path: ("combined into one PDF" if "Combined" in p.path else "one document each") for p in endpoints}
            self.push_screen(PickScreen("Print as", items), lambda c: with_endpoint(c[1] if c else None))

    @work(thread=True, exclusive=True, group="templates")
    def _pick_template(self, pe: printing.PrintEndpoint, ids: list[str]) -> None:
        try:
            top = spec.find("templates", "list")
            client = self._client(top)
            data = runner.fetch(client, top, {}, all_pages=True, progress=False)
        except (ApiError, auth.AuthError, ValueError) as exc:
            self.call_from_thread(self.notify, f"Could not load templates: {exc}", severity="error")
            return
        templates = {t["TemplateID"]: f"{t.get('Type', '')} · {t.get('Name', '')}" for t in data.get("Templates", [])}
        if not templates:
            self.call_from_thread(self.notify, "Cin7 returned no templates", severity="warning")
            return
        items = {f"{v}": k for k, v in sorted(templates.items(), key=lambda kv: kv[1])}

        def chosen(choice: tuple | None) -> None:
            if choice:
                tid = items[choice[1]]
                self._confirm_print(pe, ids, tid, choice[1])

        self.call_from_thread(self.push_screen, PickScreen(f"Template for {pe.path}", {k: "" for k in items}), chosen)

    def _confirm_print(self, pe: printing.PrintEndpoint, ids: list[str], template_id: str | None, template_name: str) -> None:
        try:
            req = printing.build_request(pe, ids, template_id)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        want = f"print {len(ids)}"

        def typed(answer: str | None) -> None:
            if answer != want:
                self.notify("Print cancelled", timeout=3)
                return
            self._do_print(req, len(ids))

        self.push_screen(
            NameScreen(
                f"PRINT {len(ids)} document(s) · {pe.path}" + (f" · {template_name}" if template_name else "")
                + f"\n[dim]{PRINT_NOTE}[/]\nType [bold]{want}[/] to print:"
            ),
            typed,
        )

    @work(thread=True, exclusive=True, group="print")
    def _do_print(self, req, count: int) -> None:
        try:
            client = runner.make_client(spec.find("web-mrp", "mrp-v-mrp"), self.rate)  # a web-session client
            result = client.send(req, raw=True)
            fake = spec.Operation(group="print", group_title="Print", name=req.url.rsplit("/", 2)[-2].lower(), resource=req.url,
                                  title="print", method=req.method, path=req.url, params=())
            path = runner.save_file(result, fake, self.export_dir)
        except (ApiError, auth.AuthError, ValueError) as exc:
            self.call_from_thread(self.notify, f"Print failed: {exc}", severity="error", timeout=8)
            return
        self.call_from_thread(self._record_download, f"print ×{count} {req.url.split('?')[0].rsplit('/', 2)[-2]}", path)
        self.call_from_thread(self.notify, f"Printed {count} → {Path(path).name}", timeout=6)

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
