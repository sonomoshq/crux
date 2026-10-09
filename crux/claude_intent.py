# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Claude Code Stop-hook logic (D10): snapshot the authoring session's intent.

Reads the hook JSON from stdin, pulls the LAST assistant text message out of
the .jsonl transcript at `transcript_path`, and writes .crux/intent.json in
the working repo:

    {"ts", "session_id", "summary", "uncertainties": [...]}

summary is capped at 2000 chars; uncertainties are the message lines matching
unsure|uncertain|TODO|not sure. This must never crash or block Claude Code:
`run()` swallows every error and returns 0.

Invoked as the `crux claude-stop-hook` subcommand (registered by
`crux install-hooks`), so it always runs under crux's own interpreter — no
`python3` on PATH required, which is what makes it work on any OS.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

UNCERTAIN_RE = re.compile(r"unsure|uncertain|TODO|not sure", re.IGNORECASE)


def _repo_root(cwd: str) -> str:
    """Repo toplevel for cwd, so intent lands where `crux run` reads it
    (<repo root>/.crux/intent.json) even when the session cwd is a subdir.
    Falls back to cwd outside a repo or when git is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    root = (proc.stdout or "").strip()
    return root if proc.returncode == 0 and root else cwd


def _text_of(message: object) -> str:
    """Concatenated text blocks of one transcript message ('' if none)."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
    return "\n".join(p for p in parts if p)


def _last_assistant_text(transcript_path: str) -> str:
    last = ""
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            # Claude Code transcript lines look like
            #   {"type": "assistant", "message": {"role": "assistant",
            #    "content": [{"type": "text", "text": ...}, ...]}}
            # but tolerate bare {"role": "assistant", "content": ...} too.
            message = rec.get("message")
            if not isinstance(message, dict):
                message = rec
            if rec.get("type") != "assistant" and message.get("role") != "assistant":
                continue
            text = _text_of(message)
            if text:
                last = text
    return last


def _capture(data: dict) -> None:
    transcript_path = data.get("transcript_path") or ""
    text = _last_assistant_text(transcript_path) if transcript_path else ""
    uncertainties = [ln.strip() for ln in text.splitlines()
                     if UNCERTAIN_RE.search(ln)]
    intent = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": data.get("session_id", ""),
        "summary": text[:2000],
        "uncertainties": uncertainties,
    }
    root = _repo_root(data.get("cwd") or os.getcwd())
    crux_dir = os.path.join(root, ".crux")
    os.makedirs(crux_dir, exist_ok=True)
    with open(os.path.join(crux_dir, "intent.json"), "w", encoding="utf-8") as fh:
        json.dump(intent, fh, indent=1)


def run(stream=None) -> int:
    """Read the hook payload from *stream* (default stdin) and capture intent.

    Never raises: any failure is swallowed so the authoring session is never
    blocked or crashed. Always returns 0.
    """
    try:
        data = json.load(stream if stream is not None else sys.stdin)
        if isinstance(data, dict):
            _capture(data)
    except BaseException:
        pass
    return 0
