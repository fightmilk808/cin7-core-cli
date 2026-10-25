"""Credentials: environment variables, then the config file. Never printed.

Either source may hold the values themselves or 1Password `op://` references.
References are resolved together with one `op inject` call, so a run costs at
most one 1Password approval.

Config file (`$XDG_CONFIG_HOME/cin7-cli/config`, default `~/.config/cin7-cli/config`),
one `KEY=VALUE` per line, written by `cin7 auth setup`:

    CIN7_ACCOUNT_ID=op://Vault/Item/username
    CIN7_APP_KEY=op://Vault/Item/password
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ACCOUNT_ENV = "CIN7_ACCOUNT_ID"
KEY_ENV = "CIN7_APP_KEY"
CONFIG_KEYS = (ACCOUNT_ENV, KEY_ENV, "CIN7_CLI_READ_ONLY", "CIN7_RATE_PER_MIN")


class AuthError(RuntimeError):
    """Credentials are missing or a 1Password reference could not be read."""


@dataclass(frozen=True)
class Credentials:
    """The two Cin7 API headers. `repr` never shows the values."""

    account_id: str
    app_key: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Credentials(account_id=***, app_key=***)"

    def headers(self) -> dict[str, str]:
        """Request headers Cin7 expects on every call."""
        return {"api-auth-accountid": self.account_id, "api-auth-applicationkey": self.app_key}

    def redact(self, text: str) -> str:
        """Replace either secret wherever it appears in `text`."""
        for secret in (self.account_id, self.app_key):
            if secret:
                text = text.replace(secret, "***")
        return text


def config_path() -> Path:
    """Where `cin7 auth setup` writes and `load` reads."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "cin7-cli" / "config"


def read_config(path: Path | None = None) -> dict[str, str]:
    """Parse the KEY=VALUE config file; missing file -> {}."""
    path = path or config_path()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        if k in CONFIG_KEYS:
            out[k] = v.strip().strip('"').strip("'")
    return out


def write_config(values: dict[str, str], path: Path | None = None) -> Path:
    """Write the config file (mode 600). Refuses anything that isn't an op:// reference
    for the two credentials, so a secret value never lands on disk."""
    path = path or config_path()
    for k in (ACCOUNT_ENV, KEY_ENV):
        if k in values and not values[k].startswith("op://"):
            raise AuthError(f"{k} must be an op:// reference; the config file never stores key values")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# cin7-core-cli: 1Password references only, never key values."]
    lines += [f"{k}={values[k]}" for k in CONFIG_KEYS if values.get(k)]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


def setting(name: str, default: str | None = None) -> str | None:
    """A setting from the environment, else the config file."""
    return os.environ.get(name) or read_config().get(name) or default


def _resolve(values: dict[str, str]) -> dict[str, str]:
    refs = {k: v for k, v in values.items() if v.startswith("op://")}
    if not refs:
        return values
    op = shutil.which("op")
    if not op:
        raise AuthError(f"{' and '.join(refs)} are 1Password references but the `op` CLI is not on PATH")
    # One `op inject` for every reference = one approval. A template line per key
    # keeps values apart without them ever touching argv or disk.
    template = "".join(f"{k}={{{{ {v} }}}}\n" for k, v in refs.items())
    try:
        out = subprocess.run([op, "inject"], input=template, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise AuthError("1Password did not answer within 120 s (approve the prompt, or unlock the app)") from exc
    if out.returncode != 0:
        # op's stderr names the reference, not the secret.
        raise AuthError(f"1Password could not resolve {' and '.join(refs)}: {out.stderr.strip()}")
    resolved = dict(values)
    for line in out.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k in refs:
                resolved[k] = v
    missing = [k for k in refs if resolved[k] == refs[k] or not resolved[k]]
    if missing:
        raise AuthError(f"1Password returned nothing for {' and '.join(missing)}")
    return resolved


def source() -> dict[str, str]:
    """Where each credential comes from: 'env', 'config' or 'missing' (no values)."""
    cfg = read_config()
    return {k: "env" if os.environ.get(k) else "config" if cfg.get(k) else "missing" for k in (ACCOUNT_ENV, KEY_ENV)}


def load(account_ref: str | None = None, key_ref: str | None = None) -> Credentials:
    """Read credentials from flags, env vars, then the config file, resolving op:// refs."""
    cfg = read_config()
    values = {
        ACCOUNT_ENV: account_ref or os.environ.get(ACCOUNT_ENV) or cfg.get(ACCOUNT_ENV, ""),
        KEY_ENV: key_ref or os.environ.get(KEY_ENV) or cfg.get(KEY_ENV, ""),
    }
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise AuthError(
            "missing " + " and ".join(missing) + ": run `cin7 auth setup --account-ref op://… --key-ref op://…`"
            " or set them in the environment (see README)"
        )
    resolved = _resolve(values)
    return Credentials(resolved[ACCOUNT_ENV], resolved[KEY_ENV])
