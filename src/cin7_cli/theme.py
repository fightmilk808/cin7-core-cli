"""Colour themes for the TUI.

`CIN7_LIGHT` is the default. `CIN7_DARK` keeps the look the app had before themes
existed (Textual's stock dark palette). `Cin7Tui(themes=[...])` takes any list of
`textual.theme.Theme` objects; the first one is active at start and `ctrl+t`
cycles through the list.

A theme tunes the TUI through `Theme.variables` (all optional; see docs/GUIDE.md):

- `"sidebar-on-panel": "1"` paints the sidebar in `$panel` with `$footer-foreground` text, and
  the `sidebar-*` variables below colour its input, guides, cursor and scrollbar.
- `light-*` variables colour the cursor, primary button and table header on light themes.
- `tag-<name>` overrides one colour of `TAGS_DARK`/`TAGS_LIGHT`, `row-<name>` one of
  `ROW_DARK`/`ROW_LIGHT` (`row-amber` is accepted for `row-yellow`).

Every `sidebar-*` and `light-*` variable a theme leaves out is derived from its own palette
(`derived_variables`), so no theme inherits Cin7's colours by accident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from textual.app import App
from textual.color import Color
from textual.theme import Theme

BAR_TEXT = "#f3f5f8"

SIDEBAR_ON_PANEL = "sidebar-on-panel"

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
        # The sidebar sits on the navy $panel too (see Cin7Tui.CSS, `.-sidebar-on-panel`).
        SIDEBAR_ON_PANEL: "1",
        "sidebar-input-background": "#2a3d58",
        "sidebar-placeholder": "#bdcadd",
        "sidebar-guides": "#5d7391",
        "sidebar-cursor-background": "#2f7ff0",
        "sidebar-cursor-foreground": "#ffffff",
        "sidebar-scrollbar": "#97b8e7",
        "sidebar-scrollbar-hover": "#b9d0f0",
        "sidebar-scrollbar-active": "#dbe7f8",
        # Light content: cursor, primary button and table header.
        "light-cursor-background": "#1f63c7",
        "light-cursor-foreground": "#ffffff",
        "light-button-hover": "#2a6fd8",
        "light-header-background": "#dde5f0",
        "light-header-hover": "#c9d6e8",
        "light-header-foreground": "#1d2633",
        "light-toggle": "#9fb2cc",
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
    variables={
        # Sidebar scrollbar thumb, >= 3:1 against Textual's dark track.
        "sidebar-scrollbar": "#4f7fae",
        "sidebar-scrollbar-hover": "#6b97c4",
        "sidebar-scrollbar-active": "#8fb2d6",
    },
)

DEFAULT_THEMES: tuple[Theme, ...] = (CIN7_LIGHT, CIN7_DARK)


def derived_variables(css: Mapping[str, str]) -> dict[str, str]:
    """Defaults for every `sidebar-*` and `light-*` variable, from a theme's own CSS variables.

    `css` is what Textual generated for the theme (`$panel`, `$primary`, `$footer-foreground`...).
    The TUI uses these only for the variables the theme does not set itself.
    """
    panel = Color.parse(css["panel"])
    bar = Color.parse(css["footer-foreground"])
    primary = Color.parse(css["primary"])
    background = Color.parse(css["background"])
    foreground = Color.parse(css["foreground"])
    cursor = primary.darken(0.1)
    return {
        "sidebar-input-background": panel.blend(bar, 0.08).hex,
        "sidebar-placeholder": panel.blend(bar, 0.75).hex,
        "sidebar-guides": panel.blend(bar, 0.35).hex,
        "sidebar-cursor-background": primary.hex,
        "sidebar-cursor-foreground": primary.get_contrast_text(1.0).hex,
        "sidebar-scrollbar": panel.blend(bar, 0.55).hex,
        "sidebar-scrollbar-hover": panel.blend(bar, 0.7).hex,
        "sidebar-scrollbar-active": panel.blend(bar, 0.85).hex,
        "light-cursor-background": cursor.hex,
        "light-cursor-foreground": cursor.get_contrast_text(1.0).hex,
        "light-button-hover": cursor.lighten(0.05).hex,
        "light-header-background": background.blend(primary, 0.12).hex,
        "light-header-hover": background.blend(primary, 0.2).hex,
        "light-header-foreground": foreground.hex,
        "light-toggle": background.blend(foreground, 0.35).hex,
    }


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
    """Markup tag colours that read on the theme's content background.

    `TAGS_DARK` or `TAGS_LIGHT` by `t.dark`, each overridable with a `tag-<name>` variable."""
    base = TAGS_DARK if t.dark else TAGS_LIGHT
    return {k: t.variables.get(f"tag-{k}", v) for k, v in base.items()}


# Whole-row status colours (`rowtools.row_style` returns the keys). Dark keeps the ANSI names
# the app always used; light swaps them for darker values that are >= 4.5:1 on white, the
# #f3f4f4 table body and the shaded locked columns (#c0ccdf).
ROW_DARK: dict[str, str] = {"red": "red", "green": "green", "yellow": "yellow"}
ROW_LIGHT: dict[str, str] = {"red": "#a01e14", "green": "#185a2b", "yellow": "#704a00"}
ROW_ALIASES = {"yellow": "amber"}


def row_colours_for(t: Theme) -> dict[str, str]:
    """Colours for `rowtools.row_style` results that read on the theme's table background.

    `ROW_DARK` or `ROW_LIGHT` by `t.dark`, each overridable with a `row-<name>` variable."""
    base = ROW_DARK if t.dark else ROW_LIGHT
    out = {}
    for k, v in base.items():
        alias = ROW_ALIASES.get(k)
        out[k] = t.variables.get(f"row-{k}") or (t.variables.get(f"row-{alias}") if alias else None) or v
    return out


def has_sidebar_on_panel(t: Theme) -> bool:
    """True if the theme paints the sidebar in its `$panel` colour with `$footer-foreground` text."""
    return t.variables.get(SIDEBAR_ON_PANEL) == "1"


def register(app: App, themes: Sequence[Theme]) -> None:
    """Register every theme with the app so it can be selected by name."""
    for t in themes:
        app.register_theme(t)
