"""Boot splash for the TUI: a wordmark that fades in, then gives way to the operation tree.

`Cin7Tui(splash=...)` takes a `Splash`. `CIN7_SPLASH` is the default. A host app can pass
its own: a different `art` and `gradient`, an async `effect` that draws its own frames
(see `SplashScreen.draw`), and an async `status_provider` whose text appears under the art.

Any key or click skips the splash, and that key is consumed: it never reaches the tree
behind. Text is always drawn in a readable colour for the active theme: gradient colours
that would be too faint on the theme's background are darkened (or lightened) just enough.

`CIN7_TUI_SPLASH=0` in the environment, or `cin7 tui --no-splash`, turns it off.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Literal

from rich.cells import set_cell_size
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

MIN_CONTRAST = 4.5  # WCAG AA for normal text
FADE_SHARE = 0.6  # the fade takes this share of `seconds`; the rest holds the full wordmark

Effect = Literal["fade", "none"] | Callable[["SplashScreen"], Awaitable[None]]


@dataclass
class Splash:
    """What the boot splash shows and how long it stays.

    `effect` is "fade", "none", or an async callable taking the `SplashScreen`. A callable
    drives its own frames (via `SplashScreen.draw`); the splash ends when it returns, or
    after `seconds` at the latest. `status_provider` is awaited once; its text goes on the
    status line, "?" if it raises, nothing if it is still running when the splash ends.
    """

    art: str
    gradient: Sequence[str]  # hex colours, top line to bottom line
    effect: Effect = "fade"
    seconds: float = 0.75
    tagline: str = ""
    status_provider: Callable[[], Awaitable[str]] | None = None


def enabled(flag_no_splash: bool) -> bool:
    """False if `--no-splash` was given, else on unless CIN7_TUI_SPLASH=0."""
    if flag_no_splash:
        return False
    return os.environ.get("CIN7_TUI_SPLASH", "1") != "0"


def load_art(name: str) -> str:
    """Wordmark text shipped in `cin7_cli/art/<name>.txt`, without the trailing newline."""
    return (resources.files("cin7_cli") / "art" / f"{name}.txt").read_text(encoding="utf-8").rstrip("\n")


CIN7_SPLASH = Splash(art=load_art("cin7"), gradient=["#2f7ff0", "#18b0a8"], effect="fade", seconds=0.75)


# --- colour helpers ---------------------------------------------------------------


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{round(min(255, max(0, v))):02x}" for v in rgb)


def _luminance(rgb: Sequence[float]) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two hex colours (1 to 21)."""
    hi, lo = sorted((_luminance(_rgb(a)), _luminance(_rgb(b))), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def readable(fg: str, bg: str, minimum: float = MIN_CONTRAST) -> str:
    """`fg` itself if it reads on `bg`, else `fg` moved toward black or white until it does."""
    if contrast(fg, bg) >= minimum:
        return fg
    start = _rgb(fg)
    # Move away from the background: toward black on a light one, white on a dark one.
    target = (0, 0, 0) if _luminance(_rgb(bg)) > 0.18 else (255, 255, 255)
    for step in range(1, 101):
        t = step / 100
        out = _hex([s + (e - s) * t for s, e in zip(start, target)])
        if contrast(out, bg) >= minimum:
            return out
    return _hex(target)


def _lerp(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex([x + (y - x) * t for x, y in zip(ra, rb)])


def gradient_colour(gradient: Sequence[str], index: int, count: int) -> str | None:
    """Colour for line `index` of `count`, spread evenly over the gradient stops."""
    if not gradient:
        return None
    if len(gradient) == 1 or count <= 1:
        return gradient[0]
    pos = index / (count - 1) * (len(gradient) - 1)
    i = min(int(pos), len(gradient) - 2)
    return _lerp(gradient[i], gradient[i + 1], pos - i)


# --- screen -----------------------------------------------------------------------


class SplashScreen(Screen[None]):
    """Art, tagline and status line on the theme background; ends itself, or on any key or click."""

    DEFAULT_CSS = """
    SplashScreen {
        align: center middle;
        background: $background;
        overflow: hidden;
    }
    SplashScreen #splash-body {
        width: auto;
        max-width: 100%;
        height: auto;
        align-horizontal: center;
    }
    SplashScreen #splash-body Static {
        width: auto;
        max-width: 100%;
        height: auto;
        background: transparent;
    }
    SplashScreen #splash-body #splash-tagline, SplashScreen #splash-body #splash-status {
        width: 100%;
        margin-top: 1;
        text-align: center;
    }
    SplashScreen #splash-tagline.-empty, SplashScreen #splash-status.-empty { display: none; }
    """

    def __init__(self, splash: Splash):
        super().__init__()
        self.splash = splash
        self._done = False
        self._tasks: list[asyncio.Task] = []
        self._art: str | Text = splash.art
        self._status_text = ""

    # --- colours (resolved from the active theme when drawn) ---

    def _theme_colours(self) -> tuple[str, str]:
        css = self.app.get_css_variables()
        return css["background"][:7], css["foreground"][:7]

    def _ink(self) -> str:
        bg, fg = self._theme_colours()
        return readable(fg, bg)

    def _width(self) -> int:
        return max(self.app.size.width, 1)

    def _crop(self, line: str) -> str:
        """`line` cut to the terminal width (by cells); a narrow terminal crops, it never wraps."""
        return set_cell_size(line, self._width()).rstrip() if line else line

    def _styled(self, art: str) -> Text:
        """The art, one readable gradient colour per line, cropped to the width."""
        bg, _ = self._theme_colours()
        lines = art.split("\n")
        text = Text(no_wrap=True, overflow="crop", end="")
        for i, line in enumerate(lines):
            colour = gradient_colour(self.splash.gradient, i, len(lines)) or self._ink()
            text.append(self._crop(line), style=readable(colour, bg))
            if i < len(lines) - 1:
                text.append("\n")
        return text

    def _plain(self, line: str) -> Text:
        return Text(self._crop(line), style=self._ink(), justify="center", no_wrap=True, overflow="crop", end="")

    # --- widgets ---

    def compose(self) -> ComposeResult:
        s = self.splash
        with Vertical(id="splash-body"):
            yield Static(self._styled(s.art), id="splash-art")
            yield Static(self._plain(s.tagline), id="splash-tagline", classes="" if s.tagline else "-empty")
            yield Static("", id="splash-status", classes="-empty")

    def draw(self, art: str | Text) -> None:
        """Replace the art. A custom `effect` calls this once per frame."""
        if self._done:
            return
        self._art = art
        self.query_one("#splash-art", Static).update(self._styled(art) if isinstance(art, str) else art)

    def set_status(self, text: str) -> None:
        """Set the line under the tagline ("" hides it)."""
        if self._done:
            return
        self._status_text = text
        status = self.query_one("#splash-status", Static)
        status.update(self._plain(text))
        status.set_class(not text, "-empty")

    # --- life cycle ---

    def on_mount(self) -> None:
        s = self.splash
        body = self.query_one("#splash-body")
        if s.effect == "fade":
            body.styles.opacity = 0.0
            body.styles.animate("opacity", 1.0, duration=max(s.seconds * FADE_SHARE, 0.01))
        if callable(s.effect):
            self._tasks.append(asyncio.create_task(self._drive(s.effect)))
        else:
            self.set_timer(max(s.seconds, 0.0), self._finish)
        if s.status_provider is not None:
            self._tasks.append(asyncio.create_task(self._status(s.status_provider)))

    async def _drive(self, effect: Callable[[SplashScreen], Awaitable[None]]) -> None:
        try:
            await asyncio.wait_for(effect(self), timeout=self.splash.seconds)
        except Exception:  # noqa: BLE001 - a broken effect (or the timeout) must not trap the user here
            pass
        self._finish()

    async def _status(self, provider: Callable[[], Awaitable[str]]) -> None:
        try:
            text = await asyncio.wait_for(provider(), timeout=self.splash.seconds)
        except asyncio.TimeoutError:
            return
        except Exception:  # noqa: BLE001
            text = "?"
        self.set_status(str(text))

    def on_resize(self) -> None:
        """Re-crop everything to the new width."""
        if self._done or not self.is_mounted:
            return
        self.draw(self._art)
        self.query_one("#splash-tagline", Static).update(self._plain(self.splash.tagline))
        self.query_one("#splash-status", Static).update(self._plain(self._status_text))

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        me = asyncio.current_task()
        for task in self._tasks:
            if task is not me:
                task.cancel()
        self.dismiss()

    def on_unmount(self) -> None:
        self._done = True
        for task in self._tasks:
            task.cancel()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self._finish()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._finish()
