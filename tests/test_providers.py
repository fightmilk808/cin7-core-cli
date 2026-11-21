"""Tree providers: the sidebar's sections come from `CollectionProvider`s."""

from __future__ import annotations

import pytest

from cin7_cli import providers
from cin7_cli.providers import Node, heat_prefix

pytest.importorskip("textual")

from textual.widgets import Input, Tree  # noqa: E402

from cin7_cli import spec  # noqa: E402
from cin7_cli.tui import Cin7Tui  # noqa: E402


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


class FakeProvider:
    title = "Hot items"

    def __init__(self):
        self.calls: list[str] = []
        self.ran: list[Node] = []

    def nodes(self, filter_text: str) -> list[Node]:
        self.calls.append(filter_text)
        return [
            Node(id="a", label="Alpha", icon="●", heat=100, data={"k": "a"}),
            Node(id="b", label="Beta", icon="●", heat=0, data={"k": "b"}),
        ]

    def run(self, app, node: Node) -> None:
        self.ran.append(node)


class BrokenProvider:
    title = "Broken"

    def __init__(self):
        self.ran: list[Node] = []

    def nodes(self, filter_text: str) -> list[Node]:
        raise RuntimeError("boom")

    def run(self, app, node: Node) -> None:
        self.ran.append(node)


def _heads(tree):
    return [str(h.label) for h in tree.root.children]


def _section(tree, title):
    return next(h for h in tree.root.children if title in str(h.label))


# --- heat_prefix ---------------------------------------------------------------------


def test_heat_prefix():
    assert heat_prefix(None) == ""
    assert heat_prefix(0) == "▁ "
    assert heat_prefix(12) == "▁ "
    assert heat_prefix(13) == "▂ "
    assert heat_prefix(50) == "▅ "
    assert heat_prefix(99) == "█ "
    assert heat_prefix(100) == "█ "


def test_node_defaults():
    n = Node(id="x", label="X")
    assert n.icon == "" and n.heat is None and n.children == [] and not n.expanded
    assert n.data is None and n.style == ""
    assert Node(id="y", label="Y").children is not n.children


def test_default_providers_need_no_textual():
    q = providers.QuickViewsProvider()
    assert q.title == "Quick views"
    leaves = q.nodes("")
    assert [n.icon for n in leaves] == ["⚡"] * len(leaves) and leaves[0].data == ("quick", "1")
    ops = providers.OperationsProvider()
    assert ops.title == "All operations" and ops.expanded


# --- in the app ----------------------------------------------------------------------


async def test_default_sections(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        heads = _heads(tree)
        assert "Quick views" in heads[0] and "All operations" in heads[1]
        allops = _section(tree, "All operations")
        assert allops.is_expanded
        kids = [str(k.label) for k in allops.children]
        assert "MAIN" in kids[0] and "PUBLIC API" in kids[1] and "INTERNAL" in kids[2]


async def test_quick_view_leaf_runs(mock, tmp_path):
    mock.routes[("GET", "/production/orderList")] = (200, {"ProductionOrderListItems": [
        {"ProductionOrderID": "a", "OrderNumber": "MO-1", "Status": "RELEASED", "RequiredByDate": "2026-11-01T00:00:00"},
    ]}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        leaf = _section(tree, "Quick views").children[0]
        assert "Open production orders" in str(leaf.label)
        tree.focus()
        tree.move_cursor(leaf)
        await pilot.press("enter")
        await pilot.pause()
        await _settle(app, pilot)
        await pilot.pause()
        assert mock.requests and mock.requests[0]["path"] == "/production/orderList"
        assert mock.requests[0]["query"] == {"Limit": "100"}


def _leaves(node):
    for k in node.children:
        if k.children:
            yield from _leaves(k)
        else:
            yield k


async def test_icons(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    app.show_writes = True
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        leaves = [n for n in _leaves(_section(tree, "All operations")) if isinstance(n.data, spec.Operation)]
        read = next(n for n in leaves if n.data.is_public and not n.data.is_write)
        write = next(n for n in leaves if n.data.is_write)
        assert str(read.label).startswith("⚡ "), str(read.label)
        assert str(write.label).startswith("✍ "), str(write.label)


async def test_custom_provider_section(tmp_path):
    fake = FakeProvider()
    app = Cin7Tui(export_dir=tmp_path, providers=[providers.QuickViewsProvider(), providers.OperationsProvider(), fake])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        heads = _heads(tree)
        assert "Quick views" in heads[0] and "All operations" in heads[1] and "Hot items" in heads[2]
        labels = [str(n.label) for n in _section(tree, "Hot items").children]
        assert labels == ["█ ● Alpha", "▁ ● Beta"]
        # enter on a leaf whose data the app does not know goes to the provider
        leaf = _section(tree, "Hot items").children[1]
        tree.focus()
        tree.move_cursor(leaf)
        await pilot.press("enter")
        await pilot.pause()
        assert [n.id for n in fake.ran] == ["b"]


async def test_providers_argument_replaces_defaults(tmp_path):
    app = Cin7Tui(export_dir=tmp_path, providers=[FakeProvider()])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert _heads(app.query_one("#ops", Tree)) == ["Hot items"]


async def test_provider_error_is_a_leaf(tmp_path):
    app = Cin7Tui(export_dir=tmp_path, providers=[BrokenProvider(), FakeProvider()])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        broken = _section(tree, "Broken")
        assert len(broken.children) == 1 and "error" in str(broken.children[0].label)
        assert "Hot items" in _heads(tree)[1], "later providers still render"
        app.query_one("#filter", Input).value = "x"
        await pilot.pause()
        assert app.is_running


async def test_enter_on_error_leaf_does_nothing(tmp_path):
    broken = BrokenProvider()
    app = Cin7Tui(export_dir=tmp_path, providers=[broken])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        leaf = _section(tree, "Broken").children[0]
        tree.focus()
        tree.move_cursor(leaf)
        await pilot.press("enter")
        await pilot.pause()
        assert broken.ran == [], "the broken provider's run is not called with the error row"
        assert app.is_running and app.op is None


async def test_filter_reaches_providers(tmp_path):
    fake = FakeProvider()
    app = Cin7Tui(export_dir=tmp_path, providers=[fake])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.query_one("#filter", Input).focus()
        await pilot.press("s", "a", "l", "e")
        await pilot.pause()
        assert fake.calls[-1] == "sale"


async def test_provider_sections_survive_ctrl_t(tmp_path):
    from cin7_cli import theme

    fake = FakeProvider()
    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT, theme.CIN7_DARK],
                  providers=[providers.QuickViewsProvider(), providers.OperationsProvider(), fake])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        n = len(fake.calls)
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "cin7-dark" and len(fake.calls) > n, "re-rendered for the new theme"
        labels = [str(x.label) for x in _section(app.query_one("#ops", Tree), "Hot items").children]
        assert labels == ["█ ● Alpha", "▁ ● Beta"]


# --- provider text is plain text: stray brackets and bad styles never crash ----------


class ListProvider:
    """A provider that returns whatever nodes it is given, under any title."""

    def __init__(self, title: str, nodes: list[Node]):
        self.title = title
        self._nodes = nodes
        self.ran: list[Node] = []

    def nodes(self, filter_text: str) -> list[Node]:
        return list(self._nodes)

    def run(self, app, node: Node) -> None:
        self.ran.append(node)


async def _type_filter(app, pilot, text: str) -> None:
    app.query_one("#filter", Input).focus()
    for ch in text:
        await pilot.press(ch)
    await pilot.pause()
    app.export_screenshot()  # render every row now: a bad style would raise here


async def test_label_with_markup_characters_is_plain_text(tmp_path):
    odd = ListProvider("Odd", [Node(id="x", label="Close [/] bracket"), Node(id="y", label="[bold]not bold[/]")])
    app = Cin7Tui(export_dir=tmp_path, providers=[odd, FakeProvider()])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.export_screenshot()
        tree = app.query_one("#ops", Tree)
        labels = [str(n.label) for n in _section(tree, "Odd").children]
        assert labels == ["Close [/] bracket", "[bold]not bold[/]"]
        assert "Hot items" in _heads(tree)[1], "later providers still render"
        await _type_filter(app, pilot, "cl")
        assert app.is_running


async def test_title_with_markup_characters_is_plain_text(tmp_path):
    odd = ListProvider("Title [/x]", [Node(id="x", label="row")])
    app = Cin7Tui(export_dir=tmp_path, providers=[odd, FakeProvider()])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.export_screenshot()
        tree = app.query_one("#ops", Tree)
        assert _heads(tree)[0] == "Title [/x]"
        assert [str(n.label) for n in tree.root.children[0].children] == ["row"]
        assert "Hot items" in _heads(tree)[1]
        await _type_filter(app, pilot, "ro")
        assert app.is_running


async def test_bad_style_falls_back_to_plain(tmp_path):
    odd = ListProvider("Odd", [Node(id="x", label="styled", style="not-a-colour [", heat=50, icon="●"),
                               Node(id="y", label="ok", style="bold")])
    app = Cin7Tui(export_dir=tmp_path, providers=[odd, FakeProvider()])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.export_screenshot()
        tree = app.query_one("#ops", Tree)
        rows = _section(tree, "Odd").children
        assert [str(n.label) for n in rows] == ["▅ ● styled", "ok"]
        assert any(span.style for span in rows[1].label.spans), "a good style is still applied"
        await _type_filter(app, pilot, "st")
        assert app.is_running


async def test_node_that_cannot_be_drawn_is_an_error_leaf(tmp_path):
    bad = ListProvider("Bad", [Node(id="ok", label="fine"), Node(id="x", label=None)])  # type: ignore[arg-type]
    app = Cin7Tui(export_dir=tmp_path, providers=[bad, FakeProvider()])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.export_screenshot()
        tree = app.query_one("#ops", Tree)
        section = _section(tree, "Bad")
        assert len(section.children) == 1 and "error" in str(section.children[0].label)
        assert "Hot items" in _heads(tree)[1], "later providers still render"
        await _type_filter(app, pilot, "f")
        assert app.is_running


def test_render_label_is_plain_and_survives_a_bad_style():
    from rich.text import Text

    from cin7_cli.providers import render_label

    assert Text.from_markup(render_label(Node(id="a", label="a [/] b", icon="[x]"))).plain == "[x] a [/] b"
    shown = Text.from_markup(render_label(Node(id="a", label="txt", style="bogus colour [", heat=100)))
    assert shown.plain == "█ txt" and not shown.spans
    good = Text.from_markup(render_label(Node(id="a", label="txt", style="bold cyan")))
    assert good.plain == "txt" and good.spans


# --- the sidebar keeps its place across rebuilds ---------------------------------------


def _walk(node):
    for k in node.children:
        yield k
        yield from _walk(k)


def _find(tree, pred):
    return next(n for n in _walk(tree.root) if pred(n))


def _sales_group(tree):
    return _find(tree, lambda n: n.data == ("group", "sale"))


def _is_op(name):
    return lambda n: isinstance(n.data, spec.Operation) and n.data.full_name == name


class Changing:
    """A provider whose rows change between calls, as a usage feed refreshes."""

    title = "Usage"

    def __init__(self):
        self.extra: list[Node] = []
        self.keep_b = True

    def nodes(self, filter_text: str) -> list[Node]:
        leaves = [Node(id="a", label="Alpha")] + ([Node(id="b", label="Beta")] if self.keep_b else [])
        return [Node(id="grp", label="Group", children=leaves + self.extra)]  # starts collapsed

    def run(self, app, node: Node) -> None:
        pass


async def test_ctrl_t_keeps_an_open_group_and_the_cursor(tmp_path):
    from cin7_cli import theme

    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT, theme.CIN7_DARK])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        _sales_group(tree).expand()
        quick = _section(tree, "Quick views")
        quick.collapse()  # open by default: closing it must stick too
        await pilot.pause()
        tree.focus()
        tree.move_cursor(_find(tree, _is_op("sale quote-get")))
        await pilot.pause()
        for theme_name in ("cin7-dark", "cin7-light"):
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.theme == theme_name
            assert _sales_group(tree).is_expanded, theme_name
            assert not _section(tree, "Quick views").is_expanded, theme_name
            assert _is_op("sale quote-get")(tree.cursor_node), (theme_name, tree.cursor_node)


async def test_custom_provider_node_keeps_its_place_across_ctrl_t_and_new_data(tmp_path):
    from cin7_cli import theme

    feed = Changing()
    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT, theme.CIN7_DARK],
                  providers=[providers.QuickViewsProvider(), feed, providers.OperationsProvider()])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ops", Tree)
        group = _find(tree, lambda n: str(n.label) == "Group")
        assert not group.is_expanded
        group.expand()
        await pilot.pause()
        tree.focus()
        tree.move_cursor(_find(tree, lambda n: str(n.label) == "Beta"))
        await pilot.pause()

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert _find(tree, lambda n: str(n.label) == "Group").is_expanded
        assert str(tree.cursor_node.label) == "Beta"

        feed.extra = [Node(id="c", label="Gamma")]
        feed.extra.insert(0, Node(id="z", label="Zeta"))
        app.refresh_sidebar()
        await pilot.pause()
        kids = [str(n.label) for n in _find(tree, lambda n: str(n.label) == "Group").children]
        assert kids == ["Alpha", "Beta", "Zeta", "Gamma"], "refresh_sidebar shows the new rows"
        assert _find(tree, lambda n: str(n.label) == "Group").is_expanded
        assert str(tree.cursor_node.label) == "Beta", "cursor stays on the same row"

        feed.keep_b = False  # the row under the cursor goes away: no crash, cursor somewhere sensible
        app.refresh_sidebar()
        await pilot.pause()
        assert tree.cursor_node is not None and app.is_running


async def test_refresh_sidebar_keeps_the_filter(tmp_path):
    fake = FakeProvider()
    app = Cin7Tui(export_dir=tmp_path, providers=[fake])
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.query_one("#filter", Input).value = "alp"
        await pilot.pause()
        app.refresh_sidebar()
        await pilot.pause()
        assert fake.calls[-1] == "alp"
