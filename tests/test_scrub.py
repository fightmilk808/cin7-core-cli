"""Guard: the public repo must never carry real Cin7 order numbers.

Example ids are invented (#10001 and up). Any tracked text file that contains a
`#2xxxx` token fails this test, so a real customer reference cannot slip in.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_ORDER = re.compile(r"#2[0-9]{4}\b")
THIS_FILE = "tests/test_scrub.py"


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line and line != THIS_FILE]


def test_no_real_order_numbers():
    hits = []
    for name in _tracked_files():
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in REAL_ORDER.finditer(line):
                hits.append(f"{name}:{lineno}: {match.group(0)}")
    assert not hits, "real-looking order numbers in tracked files:\n" + "\n".join(hits)
