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

    def nodes(self, filter_text: str) -> list[Node]:
        raise RuntimeError("boom")

    def run(self, app, node: Node) -> None:  # pragma: no cover - never reached
        pass


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
