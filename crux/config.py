# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Load layered `crux.toml` files into a `crux.models.Config`.

Two layers, applied in order: the machine-global config at
``$XDG_CONFIG_HOME/crux/crux.toml`` (default ``~/.config/crux/crux.toml``,
next to the global git hooks), then the repo's own ``crux.toml``, which
overrides the global file key by key. Set once globally (e.g. the [slack]
channel), it applies to every repo; any repo can override or disable it —
``channel = ""`` turns Slack back off for that repo.

Missing files => pure defaults. Unknown tables/keys are ignored, as are values
whose type does not match the default (so a half-broken config degrades to
defaults instead of crashing the hook).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from crux.models import Config, CruxError

CONFIG_FILENAME = "crux.toml"


class ConfigError(CruxError):
    """Raised when crux.toml exists but cannot be parsed as TOML."""


# (toml table, toml key) -> Config attribute. Anything not listed is ignored.
_MAPPING: dict[str, dict[str, str]] = {
    "scope": {"owners": "scope_owners", "repos": "scope_repos"},
    "gate": {
        "min_behavioral_lines": "gate_min_behavioral_lines",
        "max_blast": "gate_max_blast",
    },
    "sensitivity": {
        "paths": "sensitive_paths",
        "keywords": "sensitive_keywords",
    },
    "history": {
        "window_days": "history_window_days",
        "co_change_threshold": "co_change_threshold",
    },
    "llm": {"model": "llm_model", "timeout": "llm_timeout"},
    "pr": {"default_base": "pr_default_base", "auto_create": "pr_auto_create",
           "push_head": "pr_push_head", "push_remote": "pr_push_remote"},
    "prs": {"jobs": "prs_jobs"},
    "slack": {"channel": "slack_channel", "token_env": "slack_token_env"},
    "standards": {
        "rules": "standards_rules",
        "standards_max": "standards_max",
    },
    "render": {"max_read_lines": "max_read_lines"},
    "commit": {"enrich": "commit_enrich"},
    "memory": {"enabled": "memory_enabled", "max": "memory_max"},
    "super": {
        "home": "super_home",
        "roots": "super_roots",
        "ideas_max": "super_ideas_max",
        "checks_max": "super_checks_max",
        "merge_method": "super_merge_method",
    },
    "serve": {"port": "serve_port"},
    "zenhub": {
        "workspace": "zenhub_workspace",
        "token_env": "zenhub_token_env",
        "close_on_merge": "zenhub_close_on_merge",
        "done_pipeline": "zenhub_done_pipeline",
        "ask": "zenhub_ask",
    },
}


def _coerce(value: object, default: object) -> object | None:
    """Return value adapted to the default's type, or None to ignore it.

    bool is checked first because it subclasses int in Python: a bool-typed
    default only accepts a bool (so `auto_create = true` works), and every
    other type rejects a bool (so `min_behavioral_lines = true` does not
    silently become 1).
    """
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(value, bool):
        return None
    if isinstance(default, float):
        if isinstance(value, (int, float)):
            return float(value)
        return None
    if isinstance(default, int):
        return value if isinstance(value, int) else None
    if isinstance(default, str):
        return value if isinstance(value, str) else None
    if isinstance(default, list):
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return list(value)
        return None
    return None


def global_config_path() -> Path:
    """The machine-wide config file, resolved the same way as the global git
    hooks dir (cli install-hooks): $XDG_CONFIG_HOME/crux/crux.toml, defaulting
    to ~/.config/crux/crux.toml."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "crux" / CONFIG_FILENAME


def _apply_file(cfg: Config, path: Path) -> None:
    """Overlay one crux.toml onto cfg (missing file: no-op)."""
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    for table_name, keys in _MAPPING.items():
        table = data.get(table_name)
        if not isinstance(table, dict):
            continue
        for key, attr in keys.items():
            if key not in table:
                continue
            coerced = _coerce(table[key], getattr(cfg, attr))
            if coerced is not None:
                setattr(cfg, attr, coerced)


def load(repo_root: str | Path) -> Config:
    cfg = Config()
    _apply_file(cfg, global_config_path())
    _apply_file(cfg, Path(repo_root) / CONFIG_FILENAME)
    return cfg
