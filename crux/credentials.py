# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The Crux credentials file: a small local secret store for the Slack bot
token, read directly by ``crux/slack.py`` so it works under ANY shell.

The token used to live in an environment variable exported from ``~/.bashrc``,
which only bash users on the same machine ever saw — zsh (the macOS default),
fish, PowerShell, and pushes driven by an IDE/GUI/agent (no shell env loaded)
all missed it. Storing it in a file Crux reads itself removes that shell
dependency entirely.

Location mirrors the global config: ``$XDG_CONFIG_HOME/crux/credentials.json``
(default ``~/.config/crux/credentials.json``) — next to ``crux.toml``, but a
separate file, because config is meant to be readable/shareable and
credentials are not (the file is written 0600).

Shape: ``{"slack_bot_token": "xoxb-..."}``. Unknown keys are preserved across
saves (merge, not overwrite) so the file can grow other secrets later.

Missing, unreadable, or corrupt file => treated as empty. This module must
never raise on read: the Slack path is best-effort, and an absent credentials
file must behave exactly like "no token configured", never a crash.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CREDENTIALS_FILENAME = "credentials.json"


def credentials_path() -> Path:
    """The credentials file path, resolved like the global config dir
    (respects $XDG_CONFIG_HOME)."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "crux" / CREDENTIALS_FILENAME


def load_credentials() -> dict:
    """The parsed credentials file, or {} when missing/unreadable/not a JSON
    object. Never raises."""
    try:
        raw = credentials_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def get_slack_bot_token() -> str:
    """The stored Slack bot token, or '' if absent/unreadable/not a string."""
    token = load_credentials().get("slack_bot_token")
    return token.strip() if isinstance(token, str) else ""


def get_zenhub_api_key() -> str:
    """The stored Zenhub personal API key, or '' if absent/unreadable."""
    key = load_credentials().get("zenhub_api_key")
    return key.strip() if isinstance(key, str) else ""


def _atomic_write(path: Path, data: dict) -> None:
    """Write *data* as JSON, creating parent dirs, replacing the file
    atomically (tempfile + os.replace) and chmod 0600 (a harmless no-op on
    Windows, which has no POSIX mode bits)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
            fh.write("\n")
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows, or a filesystem without mode bits


def save_slack_bot_token(token: str) -> Path:
    """Merge {"slack_bot_token": token} into the credentials file (other keys
    already there are preserved) and return the file's path."""
    data = load_credentials()
    data["slack_bot_token"] = token
    path = credentials_path()
    _atomic_write(path, data)
    return path


def clear_slack_bot_token() -> bool:
    """Remove slack_bot_token from the credentials file, if present. Returns
    whether a token was actually removed (False when there was none)."""
    data = load_credentials()
    if "slack_bot_token" not in data:
        return False
    del data["slack_bot_token"]
    _atomic_write(credentials_path(), data)
    return True


def save_zenhub_api_key(key: str) -> Path:
    """Merge {"zenhub_api_key": key} into the credentials file (D40)."""
    data = load_credentials()
    data["zenhub_api_key"] = key
    path = credentials_path()
    _atomic_write(path, data)
    return path


def clear_zenhub_api_key() -> bool:
    """Remove zenhub_api_key from the credentials file, if present."""
    data = load_credentials()
    if "zenhub_api_key" not in data:
        return False
    del data["zenhub_api_key"]
    _atomic_write(credentials_path(), data)
    return True
