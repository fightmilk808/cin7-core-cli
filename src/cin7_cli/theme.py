"""Colour themes for the TUI.

`CIN7_LIGHT` is the default. `CIN7_DARK` keeps the look the app had before themes
existed (Textual's stock dark palette). `Cin7Tui(themes=[...])` takes any list of
`textual.theme.Theme` objects; the first one is active at start and `ctrl+t`
cycles through the list.
"""

from __future__ import annotations

from collections.abc import Sequence

from textual.app import App
from textual.theme import Theme

BAR_TEXT = "#f3f5f8"

CIN7_LIGHT = Theme(
    name="cin7-light",
    primary="#2f7ff0",
    secondary="#4a6fa5",
    accent="#18b0a8",
    warning="#e0a030",
    error="#e5484d",
    success="#2fa36b",
    foreground="#1d2633",
    background="#f3f5f8",
    surface="#ffffff",
    panel="#1c2b41",
    dark=False,
    variables={
        # The bars sit on the navy $panel, so their text is light, not $foreground.
        "footer-foreground": BAR_TEXT,
        "footer-key-foreground": BAR_TEXT,
        "footer-description-foreground": BAR_TEXT,
        # Marks themes whose sidebar is navy too (see Cin7Tui.CSS, `.-navy-side`).
        "navy-side": "1",
    },
)

# Same values Textual's default dark theme uses, so the app looks as it did before.
CIN7_DARK = Theme(
    name="cin7-dark",
    primary="#0178D4",
    secondary="#004578",
    warning="#ffa62b",
    error="#ba3c5b",
    success="#4EBF71",
    accent="#ffa62b",
    foreground="#e0e0e0",
    dark=True,
)

DEFAULT_THEMES: tuple[Theme, ...] = (CIN7_LIGHT, CIN7_DARK)


# Rich markup tag names used for coloured text on the right-hand side (op title,
# record and JSON trees). `dark` keeps today's tags exactly; `light` is for a light
# content background and every value is >= 4.5:1 on #f3f5f8 (and on white).
TAGS_DARK: dict[str, str] = {
    "dim": "dim",
    "write": "yellow",
    "internal": "#d070ff",
    "print": "#4fd1c5",
    "number": "cyan",
    "string": "green",
    "keyword": "magenta",
}
TAGS_LIGHT: dict[str, str] = {
    "dim": "#566272",
    "write": "#8a5a00",
    "internal": "#7a2fc0",
    "print": "#0e7c74",
    "number": "#0b6a8a",
    "string": "#1b6e35",
    "keyword": "#a3238e",
}


def tags_for(t: Theme) -> dict[str, str]:
    """Markup tag colours that read on the theme's content background."""
    return TAGS_DARK if t.dark else TAGS_LIGHT


def has_navy_side(t: Theme) -> bool:
    """True if the theme paints the sidebar navy with light text."""
    return t.variables.get("navy-side") == "1"


def register(app: App, themes: Sequence[Theme]) -> None:
    """Register every theme with the app so it can be selected by name."""
    for t in themes:
        app.register_theme(t)
