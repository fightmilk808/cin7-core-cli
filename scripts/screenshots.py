"""Render the README screenshots: Textual SVG exports from a headless run.

Data is invented and served by the same mock server the tests use, so nothing here
reaches Cin7. Run from the repo root:

    pip install -e '.[dev]' && python scripts/screenshots.py

Writes docs/tui-light.svg (default theme), docs/tui.svg (dark theme) and docs/splash.svg.
Afterwards run `pytest tests/test_scrub.py`: no real order number may reach an SVG.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SIZE = (140, 40)

# Invented sales: (customer reference, status, amount, customer).
SALES = [
    ("#10001", "BACKORDERED", 1840.00, "Birchwood Supply"),
    ("#10002", "SHIPPED", 312.50, "Harbor Test Co"),
    ("#10003", "ORDERED", 96.00, "Juniper Goods"),
    ("#10004", "IN PROGRESS", 2275.25, "Larkspur Outfitters"),
    ("#10005", "SHIPPED", 148.75, "Birchwood Supply"),
    ("#10006", "BACKORDERED", 560.00, "Maple Row Trading"),
    ("#10007", "ORDERED", 33.20, "Harbor Test Co"),
    ("#10008", "SHIPPED", 904.10, "Juniper Goods"),
    ("#10009", "IN PROGRESS", 77.00, "Larkspur Outfitters"),
    ("#10010", "ORDERED", 1210.00, "Maple Row Trading"),
]


def _rows() -> list[dict]:
    return [
        {
            "SaleID": f"00000000-0000-0000-0000-{i:012d}",
            "CustomerReference": ref,
            "OrderNumber": f"SO-{i + 1:05d}",
            "Status": status,
            "InvoiceAmount": amount,
            "Customer": customer,
        }
        for i, (ref, status, amount, customer) in enumerate(SALES)
    ]


async def render() -> None:
    sys.path.insert(0, str(ROOT))  # the tests' mock server
    from tests.conftest import MockCin7

    from cin7_cli import spec
    from cin7_cli.splash import CIN7_SPLASH
    from cin7_cli.theme import CIN7_DARK, CIN7_LIGHT
    from cin7_cli.tui import Cin7Tui

    mock = MockCin7()
    mock.routes[("GET", "/saleList")] = (200, {"Total": len(SALES), "Page": 1, "SaleList": _rows()}, {})
    os.environ["CIN7_BASE_URL"] = mock.url

    # The status line shows how long the call took; pin it so a re-render gives the same SVG.
    real_show = Cin7Tui._show
    Cin7Tui._show = lambda self, op, data, seconds: real_show(self, op, data, 0.0)  # type: ignore[method-assign]

    async def settle(app, pilot) -> None:
        await app.workers.wait_for_complete()
        await pilot.pause()

    try:
        # The splash, held on screen at full strength (the real one fades in).
        still = dataclasses.replace(CIN7_SPLASH, effect="none", seconds=30)
        app = Cin7Tui(export_dir=Path(tempfile.mkdtemp()), splash=still, show_splash=True)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            app.save_screenshot(str(DOCS / "splash.svg"))

        # The tree, with a sale list run: light (the default), then dark.
        for themes, name in (((CIN7_LIGHT, CIN7_DARK), "tui-light.svg"), ((CIN7_DARK, CIN7_LIGHT), "tui.svg")):
            app = Cin7Tui(export_dir=Path(tempfile.mkdtemp()), themes=themes, show_splash=False)
            async with app.run_test(size=SIZE) as pilot:
                app.select(spec.find("sale", "list"))
                await pilot.pause()
                app.action_run()
                await settle(app, pilot)
                app.save_screenshot(str(DOCS / name))
    finally:
        mock.close()


def main() -> None:
    state = tempfile.mkdtemp()
    os.environ.update(
        CIN7_ACCOUNT_ID="demo-account-0000",
        CIN7_APP_KEY="demo-key-0000",
        CIN7_CLI_STATE_DIR=state,
        XDG_CONFIG_HOME=state,
        CIN7_TUI_SPLASH="1",
    )
    os.environ.pop("CIN7_CLI_READ_ONLY", None)
    sys.path.insert(0, str(ROOT / "src"))
    asyncio.run(render())
    for name in ("tui-light.svg", "tui.svg", "splash.svg"):
        print(DOCS / name, (DOCS / name).stat().st_size, "bytes")


if __name__ == "__main__":
    main()
