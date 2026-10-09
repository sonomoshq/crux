# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Headless Claude Code invocation (D7).

Runs `claude -p --output-format json --model <cfg.llm_model>` with the prompt on
stdin. The CLI prints a JSON *envelope* such as

    {"type": "result", "result": "<model text>", ...}

The model text is expected to contain exactly one JSON object; we extract the
first balanced ``{...}`` block from it and parse that. On any parse failure we
retry once with "Return ONLY the JSON object." appended to the prompt, then
raise :class:`crux.models.LlmError`. Never a metered API key (D7): the local
`claude` CLI on the author's subscription is the only backend.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from crux.models import Config, LlmError, NotLoggedInError

RETRY_SUFFIX = "\n\nReturn ONLY the JSON object."

# Actionable message shown whenever the local `claude` CLI is not authenticated.
NOT_LOGGED_IN_MSG = (
    "Claude Code is not logged in — run `claude login` (or `/login` inside "
    "Claude Code), then try again. Crux left your commit message unchanged."
)

# Substrings (case-insensitive) that mark a `claude` failure as an auth/login
# problem rather than a transient error. Checked against the CLI's combined
# stdout+stderr so the guidance survives however the CLI phrases it.
_AUTH_SIGNALS = (
    "invalid api key",
    "please run /login",
    "claude login",
    "not logged in",
    "log in",
    "logged out",
    "authenticat",       # authenticate / authentication / unauthenticated
    "unauthorized",
    "oauth token",
    "credit balance",
    "sign in",
)


def _looks_like_auth_error(text: str) -> bool:
    low = text.lower()
    return any(sig in low for sig in _AUTH_SIGNALS)


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the ``}`` closing the ``{`` at *start*, or None.

    Tracks JSON string literals so braces inside strings don't affect depth.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` block in *text* that parses as a
    JSON object, or None. Scans past prose braces (e.g. "in {file} we ...")
    that happen to balance but are not JSON."""
    start = text.find("{")
    while start != -1:
        end = _balanced_end(text, start)
        if end is not None:
            block = text[start:end]
            try:
                if isinstance(json.loads(block), dict):
                    return block
            except ValueError:
                pass
        start = text.find("{", start + 1)
    return None


def _parse_result(stdout: str) -> dict:
    """Envelope stdout -> inner JSON object. Raises ValueError on any failure."""
    try:
        envelope = json.loads(stdout)
    except ValueError as exc:
        raise ValueError(f"claude envelope is not JSON: {exc}") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), str):
        raise ValueError("claude envelope has no string 'result' field")
    block = extract_json_object(envelope["result"])
    if block is None:
        raise ValueError("no JSON object found in claude result text")
    return json.loads(block)


def claude_json(prompt: str, cfg: Config, timeout: int | None = None) -> dict:
    """Run headless claude and return the JSON object it was asked to emit.

    *timeout* (seconds) defaults to ``cfg.llm_timeout`` when not given.
    One retry with RETRY_SUFFIX on parse failure; LlmError after that.
    Missing binary / nonzero exit / timeout raise LlmError immediately
    (retrying cannot fix those).
    """
    exe = shutil.which("claude")
    if exe is None:
        raise LlmError("claude CLI not found on PATH (Crux requires headless Claude Code, D7)")
    limit = cfg.llm_timeout if timeout is None else timeout
    argv = [exe, "-p", "--output-format", "json", "--model", cfg.llm_model]
    parse_errors: list[str] = []
    for attempt_prompt in (prompt, prompt + RETRY_SUFFIX):
        try:
            proc = subprocess.run(
                argv, input=attempt_prompt, capture_output=True,
                encoding="utf-8", errors="replace", timeout=limit,
            )
        except FileNotFoundError as exc:
            raise LlmError(f"failed to execute claude: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LlmError(f"claude timed out after {limit}s") from exc
        if proc.returncode != 0:
            # A not-logged-in `claude` exits nonzero — sometimes with a login
            # message, but often with NOTHING on stderr. Look at stdout too so
            # the failure is never reported as an empty "claude exited 1: ".
            detail = "\n".join(
                part for part in ((proc.stderr or "").strip(),
                                  (proc.stdout or "").strip()) if part
            )
            if _looks_like_auth_error(detail) or not detail:
                # No diagnostic at all is, in practice, the signature of an
                # unauthenticated CLI — point the user at `claude login`.
                raise NotLoggedInError(NOT_LOGGED_IN_MSG)
            raise LlmError(f"claude exited {proc.returncode}: {detail[:400]}")
        try:
            return _parse_result(proc.stdout)
        except ValueError as exc:
            parse_errors.append(str(exc))
    raise LlmError(
        "could not parse JSON from claude output after retry: " + "; ".join(parse_errors)
    )
