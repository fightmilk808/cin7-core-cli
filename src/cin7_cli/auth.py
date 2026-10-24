"""Credentials: environment variables or 1Password `op://` references. Never printed."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

ACCOUNT_ENV = "CIN7_ACCOUNT_ID"
KEY_ENV = "CIN7_APP_KEY"


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


def _resolve(value: str, env_name: str) -> str:
    if not value.startswith("op://"):
        return value
    op = shutil.which("op")
    if not op:
        raise AuthError(f"{env_name} is a 1Password reference but the `op` CLI is not on PATH")
    try:
        out = subprocess.run([op, "read", "--no-newline", value], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise AuthError(f"`op read` timed out resolving {env_name}") from exc
    if out.returncode != 0:
        # stderr from op names the reference, not the secret.
        raise AuthError(f"`op read` failed for {env_name}: {out.stderr.strip()}")
    return out.stdout.strip()


def load(account_ref: str | None = None, key_ref: str | None = None) -> Credentials:
    """Read credentials from flags, then env vars. Values may be `op://` references."""
    account = account_ref or os.environ.get(ACCOUNT_ENV, "")
    key = key_ref or os.environ.get(KEY_ENV, "")
    missing = [n for n, v in ((ACCOUNT_ENV, account), (KEY_ENV, key)) if not v]
    if missing:
        raise AuthError(
            "missing " + " and ".join(missing) + ": set them to the values or to op:// references (see README)"
        )
    return Credentials(_resolve(account, ACCOUNT_ENV), _resolve(key, KEY_ENV))
