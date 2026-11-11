"""Make a super checkout build as ONE tree across repos.

A Rust repo depends on another Sonomos repo by a git `rev` pin, so two PRs in
one repo that each bump that pin conflict, and — worse — the build ignores the
sibling checkout entirely: it fetches whatever rev is pinned. Testing a bundle
then quietly tests the pinned commits, not the bundle.

Two pieces fix that:

* `take_pin_side` resolves a merge conflict that is ONLY a rev pin to a bundle
  repo (plus `Cargo.lock`) by taking either side — the pin no longer decides
  what gets built;
* `apply` writes an untracked `.cargo/config.toml` that `[patch]`es every
  crate of every bundle repo to its local checkout, so the build uses the
  combined tree. `drop` takes it back out (and restores `Cargo.lock`, which the
  patched build rewrites) before the next press.

Nothing here is committed; the config file is listed in `.git/info/exclude`.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib

log = logging.getLogger("crux.cargopatch")

MARKER = "# crux super checkout — local patch, removed on the next press"
CONFIG = os.path.join(".cargo", "config.toml")

_URL = re.compile(r'(?:ssh://git@|https://|git@)github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$')
_LOCK_SOURCE = re.compile(r'^source = "git\+([^"?#]+)', re.M)
_PIN = re.compile(r'\b(rev|tag|branch)\s*=\s*"[^"]*"')


def slug_of(url: str) -> str:
    """`ssh://git@github.com/sonomoshq/Service-Mesh.git` → `sonomoshq/service-mesh`."""
    m = _URL.match(url.strip())
    return m.group(1).lower() if m else ""


def _git(args: list[str], cwd: str) -> str:
    import crux.gitio as gitio
    try:
        return gitio.run_git(args, cwd=cwd, timeout=120)
    except Exception as exc:  # noqa: BLE001 — GitError and friends, as one type here
        raise RuntimeError(str(exc)) from exc


def _manifests(root: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", ".git", "node_modules")]
        if "Cargo.toml" in filenames:
            found.append(os.path.join(dirpath, "Cargo.toml"))
    return found


def _load(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def packages(root: str) -> dict[str, str]:
    """Every crate in the checkout at *root*: package name → its directory."""
    out: dict[str, str] = {}
    for manifest in _manifests(root):
        name = _load(manifest).get("package", {}).get("name")
        if isinstance(name, str):
            out.setdefault(name, os.path.dirname(manifest))
    return out


def git_urls(root: str) -> set[str]:
    """Every git dependency URL, spelled exactly as the repo spells it."""
    urls: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            git = node.get("git")
            if isinstance(git, str):
                urls.add(git)
            for value in node.values():
                walk(value)

    for manifest in _manifests(root):
        walk(_load(manifest))
    try:
        with open(os.path.join(root, "Cargo.lock"), encoding="utf-8") as f:
            urls.update(_LOCK_SOURCE.findall(f.read()))
    except OSError:
        pass
    return urls


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^<<<<<<< [^\n]*\n(.*?)^(?:\|\|\|\|\|\|\| [^\n]*\n.*?)?=======\n(.*?)^>>>>>>> [^\n]*\n",
                   re.S | re.M)


def _pin_only(ours: str, theirs: str, bundle: set[str]) -> bool:
    a, b = ours.splitlines(), theirs.splitlines()
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        url = re.search(r'git\s*=\s*"([^"]+)"', x)
        if not url or slug_of(url.group(1)) not in bundle:
            return False
        if _PIN.sub("", x) != _PIN.sub("", y):
            return False
    return True


def take_pin_side(path: str, bundle: set[str]) -> list[str]:
    """Resolve the conflicted files in *path* that only differ in a git pin to
    a repo in *bundle* (lower-case slugs); `Cargo.lock` goes with them. Takes
    our side and `git add`s it. Returns the files resolved."""
    files = [f for f in _git(["diff", "--name-only", "--diff-filter=U"], path).splitlines() if f]
    done: list[str] = []
    manifests = [f for f in files if os.path.basename(f) == "Cargo.toml"]
    for name in manifests:
        full = os.path.join(path, name)
        with open(full, encoding="utf-8") as f:
            text = f.read()
        hunks = list(_HUNK.finditer(text))
        if not hunks or not all(_pin_only(h.group(1), h.group(2), bundle) for h in hunks):
            continue
        with open(full, "w", encoding="utf-8") as f:
            f.write(_HUNK.sub(lambda h: h.group(1), text))
        _git(["add", "--", name], path)
        done.append(name)
    if done or not manifests:
        for name in files:
            if os.path.basename(name) == "Cargo.lock":
                _git(["checkout", "--ours", "--", name], path)
                _git(["add", "--", name], path)
                done.append(name)
    return done


# ---------------------------------------------------------------------------
# The local patch
# ---------------------------------------------------------------------------

def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(urls: dict[str, dict[str, str]], number: int) -> str:
    lines = [MARKER, f"# super/{number}: builds the bundle's own checkouts, not the pinned revs", ""]
    for url in sorted(urls):
        lines.append(f"[patch.{_quote(url)}]")
        for name, where in sorted(urls[url].items()):
            lines.append(f"{name} = {{ path = {_quote(where)} }}")
        lines.append("")
    return "\n".join(lines)


def apply(path: str, siblings: dict[str, str], number: int) -> str:
    """Patch *path*'s git deps on any repo in *siblings* (slug → checkout) to
    that checkout. Returns a short note for the report, or "" if nothing to do."""
    if not os.path.exists(os.path.join(path, "Cargo.toml")):
        return ""
    config = os.path.join(path, CONFIG)
    if os.path.exists(config) and not _ours(config):
        try:
            tracked = _git(["ls-files", "--", CONFIG], path).strip()
        except RuntimeError:
            tracked = "?"
        return (f"has its own {CONFIG}{' (tracked)' if tracked else ''} — not patched; "
                f"it builds the pinned revs of its bundle deps")
    me = os.path.realpath(path)
    table: dict[str, dict[str, str]] = {}
    for url in git_urls(path):
        sibling = siblings.get(slug_of(url))
        if not sibling or os.path.realpath(sibling) == me:
            continue
        crates = packages(sibling)
        if crates:
            table[url] = crates
    if not table:
        return ""
    os.makedirs(os.path.dirname(config), exist_ok=True)
    with open(config, "w", encoding="utf-8") as f:
        f.write(render(table, number))
    _exclude(path)
    names = sorted({os.path.basename(os.path.normpath(siblings[slug_of(u)])) for u in table},
                   key=str.lower)
    log.info("patched %s to build against local %s", path, ", ".join(names))
    return f"builds against your local {', '.join(names)} ({CONFIG}, untracked)"


def _ours(config: str) -> bool:
    try:
        with open(config, encoding="utf-8") as f:
            return f.readline().rstrip("\n") == MARKER
    except OSError:
        return False


def _exclude(path: str) -> None:
    try:
        exclude = os.path.join(_git(["rev-parse", "--git-path", "info/exclude"], path).strip())
        exclude = exclude if os.path.isabs(exclude) else os.path.join(path, exclude)
        line = "/" + CONFIG.replace(os.sep, "/")
        existing = open(exclude, encoding="utf-8").read() if os.path.exists(exclude) else ""
        if line not in existing.splitlines():
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + line + "\n")
    except (OSError, RuntimeError):
        pass


def drop(path: str) -> bool:
    """Take a previous press's patch back out of *path*, restoring the
    `Cargo.lock` files the patched build rewrote. True when there was one."""
    config = os.path.join(path, CONFIG)
    if not path or not _ours(config):
        return False
    os.remove(config)
    try:
        os.rmdir(os.path.dirname(config))
    except OSError:
        pass
    try:
        changed = [f for f in _git(["diff", "--name-only"], path).splitlines()
                   if os.path.basename(f) == "Cargo.lock"]
        if changed:
            _git(["checkout", "--", *changed], path)
    except RuntimeError as exc:
        log.warning("could not restore Cargo.lock in %s: %s", path, exc)
    return True
