"""Tree providers: where the TUI sidebar's sections come from.

Each top-level section of the sidebar is one `CollectionProvider`. The TUI ships two,
`QuickViewsProvider` ("Quick views") and `OperationsProvider` ("All operations"); another
app can pass its own to `Cin7Tui(providers=[...])` to add sections of its own, for example
a list of items ranked by how busy they are, with a heat bar in front of each.

A provider returns plain `Node`s; the TUI turns them into tree rows, re-renders them when
the filter text or the theme changes, and calls `provider.run(app, node)` when the user
presses enter on a leaf whose `data` the TUI does not recognise itself.

This module does not import Textual, so a provider can be written and tested without it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from . import printing, spec

if TYPE_CHECKING:  # pragma: no cover
    from .tui import Cin7Tui

HEAT_BARS = "▁▂▃▄▅▆▇█"
READ_ICON = "⚡"
WRITE_ICON = "✍"


@dataclass
class Node:
    """One row of the sidebar tree.

    Attributes:
        id: Stable id among its siblings. The TUI uses the chain of ids (with the section
            title) to keep a row open and under the cursor across rebuilds; never shown.
        label: Row text, shown as plain text: brackets are never read as markup.
        icon: Shown before the label, followed by a space ("" for none). Plain text.
        heat: 0-100, drawn as a bar in front of the row (see `heat_prefix`); None for none.
        children: Child rows. A node with children is a branch, without them a leaf.
        expanded: Whether a branch starts open.
        data: Anything. The TUI acts on `spec.Operation`, `("quick", key)`, `("print", endpoint)`,
            `("group", name)` and `("error",)` itself; enter on any other leaf calls the provider's `run`.
        style: Optional Rich style for the whole label, e.g. "bold cyan". A style that does
            not parse is dropped (the label is shown unstyled).
        markup: True reads `label` as Rich markup (the built-in sections use it for mixed
            colours). Markup that does not parse is shown as plain text instead.
    """

    id: str
    label: str
    icon: str = ""
    heat: int | None = None
    children: list[Node] = field(default_factory=list)
    expanded: bool = False
    data: object = None
    style: str = ""
    markup: bool = False


@runtime_checkable
class CollectionProvider(Protocol):
    """A top-level sidebar section.

    Required: `title`, `nodes`, `run`. Optional, read with getattr:
        expanded (bool, default True): whether the section starts open.
        attach(app) -> None: called once with the `Cin7Tui` before the first `nodes` call.

    The tree is rebuilt often (every filter keystroke, every theme change), so `nodes`
    should be quick and should not call the network itself; fetch elsewhere and return
    what you have. When that fetch brings new data, call `app.refresh_sidebar()` (on the
    app's thread, e.g. from a worker via `app.call_from_thread`) to redraw: open branches
    and the cursor row stay where they were, matched by `title` and the chain of `Node.id`s,
    so keep ids stable between calls. If `nodes` raises, or a row cannot be drawn, the
    section shows one "error" leaf and the rest of the TUI keeps working.
    """

    title: str

    def nodes(self, filter_text: str) -> list[Node]:
        """Rows for this section. `filter_text` is the sidebar filter box, stripped ("" for none);
        match it case-insensitively. Return [] to hide the section."""
        ...

    def run(self, app: Cin7Tui, node: Node) -> None:
        """Enter on one of this section's leaves whose `data` the TUI does not handle itself."""
        ...


def heat_prefix(heat: int | None) -> str:
    """A bar for 0-100 followed by a space: "▁ " for 0 up to "█ " for 100; "" for None."""
    if heat is None:
        return ""
    return HEAT_BARS[max(0, min(7, int(heat) * 8 // 100))] + " "


def _markup_ok(markup: str) -> bool:
    """True if `markup` parses and every style in it is one Rich can draw."""
    from rich.style import Style
    from rich.text import Text

    try:
        for span in Text.from_markup(markup).spans:
            if isinstance(span.style, str):
                Style.parse(span.style)
    except Exception:  # noqa: BLE001 - MarkupError, StyleSyntaxError, ColorParseError...
        return False
    return True


def _style_ok(style: str) -> bool:
    from rich.style import Style

    if "[" in style or "]" in style:
        return False
    try:
        Style.parse(style)
    except Exception:  # noqa: BLE001
        return False
    return True


def render_label(node: Node, recolor: Callable[[str], str] | None = None) -> str:
    """Markup for a row that always parses: heat bar, icon, then the (styled) label.

    The icon and label are escaped, so they show exactly as given; with `node.markup` the
    label is kept as markup if it parses. `recolor` rewrites markup tags (the TUI uses it to
    lighten colours on a dark sidebar panel); it is applied to markup labels and to `style`.
    A style that does not parse is dropped. Raises TypeError if `label` is not a str.
    """
    from rich.markup import escape

    if not isinstance(node.label, str):
        raise TypeError(f"Node {node.id!r}: label must be a str, not {type(node.label).__name__}")
    recolor = recolor or (lambda s: s)
    icon = f"{node.icon} " if node.icon else ""
    body = escape(node.label)
    if node.markup:
        tinted = recolor(node.label)
        if _markup_ok(tinted):
            body = tinted
    if node.style:
        style = recolor(f"[{node.style}]")[1:-1]
        if _style_ok(style):
            body = f"[{style}]{body}[/]"
    return f"{escape(heat_prefix(node.heat) + icon)}{body}"


# --- operations ---------------------------------------------------------------------

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


def op_by_name(full_name: str) -> spec.Operation | None:
    """`group name` back to its operation."""
    group, _, name = full_name.partition(" ")
    return spec.find(group, name)


def first_ok(o: spec.Operation) -> bool:
    """Can this operation be sent from the TUI?"""
    return (o.is_public and not o.is_write) or (not o.is_public and o.runnable)


def is_list(o: spec.Operation) -> bool:
    """A list view (as opposed to one record or an action): these are bold in the sidebar."""
    return o.name == "list" or o.name.endswith("-list") or o.paged


def op_icon(o: spec.Operation) -> str:
    """✍ for writes, ⚡ for reads the TUI can send, nothing for ones it never sends."""
    if o.is_write:
        return WRITE_ICON
    return READ_ICON if first_ok(o) else ""


def leaf_label(o: spec.Operation, style: str = "") -> str:
    """Tree label (the icon comes separately, see `op_icon`): bold for lists, amber for writes,
    ⬇ for downloads, ⛔ for internal ones that are never sent."""
    if o.is_public:
        if o.is_write:
            return f"[yellow]{o.name}[/]"
        return f"[bold]{o.name}[/]" if is_list(o) else o.name
    if o.source != "web":
        return f"[dim]· {o.name}[/]"
    if not o.runnable:
        return f"[dim strike]⛔ {o.name}[/]"
    mark = "⬇ " if o.file else "↻ " if o.read_via_post else ""
    weight = "bold " if is_list(o) else ""
    return f"[{weight}{style}]{mark}{o.name}[/]" if (style or weight) else f"{mark}{o.name}"


class QuickViewsProvider:
    """"Quick views": one leaf per `QUICK_VIEWS` entry. Enter runs the same view as its key (1-4)."""

    title = "Quick views"
    expanded = True

    def nodes(self, filter_text: str) -> list[Node]:
        needle = filter_text.lower()
        return [
            Node(id=f"quick-{key}", label=f"{label} [dim]{key}[/]", icon=READ_ICON, data=("quick", key), markup=True)
            for key, label, group, name, _, _ in QUICK_VIEWS
            if not needle or needle in f"{label} {group} {name}".lower()
        ]

    def run(self, app: Cin7Tui, node: Node) -> None:  # pragma: no cover - the TUI handles ("quick", key)
        app.action_quick(node.data[1])  # type: ignore[index]


class OperationsProvider:
    """"All operations": MAIN, PUBLIC API, INTERNAL, PRINT and REFERENCE, grouped as the API is.

    Reads `show_writes` and `compact` from the app it is attached to (both False without one).
    """

    title = "All operations"
    expanded = True

    def __init__(self) -> None:
        self.app: object = None

    def attach(self, app: object) -> None:
        self.app = app

    def run(self, app: Cin7Tui, node: Node) -> None:  # pragma: no cover - the TUI handles its own data
        pass

    def nodes(self, filter_text: str) -> list[Node]:
        show_writes = bool(getattr(self.app, "show_writes", False))
        compact = bool(getattr(self.app, "compact", False))
        needle = filter_text.lower()
        by_group = spec.groups()
        out: list[Node] = []

        def matches(ops: list[spec.Operation]) -> list[spec.Operation]:
            return [
                o
                for o in ops
                if (show_writes or first_ok(o))
                and (not needle or needle in f"{o.group} {o.name} {o.path} {o.resource} {o.title}".lower())
            ]

        def section(sid: str, label: str, groups: list[str], style: str, leaf_style: str = "") -> None:
            found = [(g, matches(by_group[g])) for g in groups if g in by_group]
            found = [(g, m) for g, m in found if m]
            if not found:
                return
            kids = []
            for g, ops in found:
                sendable = sum(first_ok(o) for o in ops)
                title = MAIN_TITLES.get(g) or ops[0].group_title.removeprefix("Web · ")
                counts = "" if compact else f" [dim]{sendable}/{len(ops)}[/]" if show_writes else f" [dim]{len(ops)}[/]"
                leaves = [Node(id=o.full_name, label=leaf_label(o, leaf_style), icon=op_icon(o), data=o, markup=True) for o in ops]
                kids.append(Node(id=g, label=f"[{style}]{title}[/]{counts}", children=leaves, markup=True,
                                 expanded=bool(needle), data=("group", g)))
            out.append(Node(id=sid, label=label, children=kids, expanded=True, markup=True))

        public = [g for g, ops in by_group.items() if ops[0].is_public]
        section("main", "[bold]★ MAIN[/]", [g for g in MAIN_GROUPS if g in public], "bold black on #ffb454")
        section("public", "[bold cyan]◇ PUBLIC API[/] [dim]· API key[/]", [g for g in public if g not in MAIN_GROUPS], "cyan")
        web = [g for g, ops in by_group.items() if ops[0].source == "web"]
        web.sort(key=lambda g: (g.removeprefix("web-") not in MAIN_WEB, g))
        section("internal", "[bold #d070ff]⬢ INTERNAL[/] [dim]· your login · reads only[/]", web, "#d070ff", "#b58ad8")
        prints = [p for p in printing.endpoints() if not needle or needle in f"{p.path} {p.where}".lower()]
        if prints:
            leaves = [
                Node(id=pe.path, label=f"[#4fd1c5]{pe.path}[/] [dim]{'bulk' if pe.bulk else 'one'}[/]"
                     + ("" if pe.sendable else " [dim](not mapped)[/]"), data=("print", pe), markup=True)
                for pe in prints
            ]
            out.append(Node(id="print", markup=True, label="[bold #4fd1c5]⎙ PRINT[/] [dim]· where printing lives[/]",
                            children=leaves, expanded=bool(needle)))
        ref = [g for g, ops in by_group.items() if ops[0].source in ("wms", "mes", "report-service")]
        section("reference", "[bold dim]▫ REFERENCE[/] [dim]· never sent[/]", ref, "dim")
        return out


def default_providers() -> list[CollectionProvider]:
    """What the TUI shows when it is given no providers: Quick views, then All operations."""
    return [QuickViewsProvider(), OperationsProvider()]
