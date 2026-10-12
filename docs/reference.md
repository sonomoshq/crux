# Crux reference guide

[← Back to Crux](../README.md) · [Quick start](../README.md#quick-start)

Full technical detail behind the short README. Start with [how the review works](#how-it-works) and the [complete command reference](#command-reference), or jump to a setup or integration guide below.

| Get set up | Work with your team | Troubleshoot |
| --- | --- | --- |
| [Guided & manual installation](#install) | [Super PRs](#super-prs-optional) | [Native Windows](#windows) |
| [Git hooks & commit enrichment](#hook-up-git-once-per-machine) | [Slack](#slack-notifications-optional) | [Troubleshooting](#troubleshooting) |
| [Config & scope](#configure) | [Zenhub](#zenhub-optional) | [Developing Crux](#developing-crux) |
| [Claude Code & cloud sessions](#claude-code-plugin) | [Repository memory](#memory) | [Architecture](../DESIGN.md) |

---

## How it works

1. **Collect context locally.** Git and ripgrep provide the diff, structural
   changes, references, history, sensitive-path signals, and nearby tests.
2. **Decide whether a model review is useful.** By default, Crux skips the AI
   review only when there are fewer than 50 behavioral changed lines, no
   sensitive-path or added-keyword matches, no dependency-file changes, and
   a maximum reference-based impact score below 5. These settings are configurable.
3. **Explain the change.** Claude Code receives relevant code excerpts and repo
   context. Crux groups and orders related changes, then renders an overview,
   focused reading lists, and links to the code. Unchanged annotations can be
   reused on later pushes.
4. **Keep the review current.** Published runs update a single review card on the
   PR. When appropriate, Crux can also post a separate verification walkthrough.
   Optional integrations carry updates into the team's existing workflow.

The card is guidance for a reviewer. Its classifications, including the rendered
"Safe to skip" label, are heuristic judgments; they do not establish that a change
is correct. A successful Crux status means the review ran, not that tests passed
or that a person approved the code. A gate-skipped published run can still update
PR metadata/status and notify a configured Slack channel.

### What runs where

**Crux runs on your machine; the AI review uses your Claude Code CLI.** Relevant
patch excerpts and repository context, which can include commit messages, a PR
description, `CLAUDE.md`, captured assistant intent, and remembered repo facts,
are passed to that CLI. It is not an offline model bundled with Crux.

Published runs write to GitHub. Slack and Zenhub are optional connections.
`crux preview` avoids publishing the review, but may still read GitHub context
or fetch a missing base reference. `--no-llm` disables the Claude analysis for
that run. Review [the configuration example](../crux.toml.example) to control scope,
integrations, memory, and model selection.

## Command reference

| Command | What it does |
| --- | --- |
| `crux preview` | Analyze committed changes and print the card without publishing it. |
| `crux run` | Find or create a PR, update its metadata, and publish or refresh the review. |
| `crux prs` | List open PRs across your configured owners' repos; requires `gh`. |
| `crux super new` | Bundle related PRs into one combined review brief. |
| `crux super refresh 3` | Refresh bundle 3 after its PRs change. |
| `crux merge` | Confirm, approve as the authenticated reviewer, and attempt to merge this PR. |
| `crux serve` | Run the local service behind card and brief action links. |
| `crux memory` | List, add, forget, or clear remembered repository facts. |
| `crux zenhub` | Set up ticket links, inspect them, or sync merged work. |
| `crux install-hooks` | Install review, branch-tracking, commit-enrichment, and Claude Code hooks. |

Use `crux <command> --help` for options. Ordinary merge actions cannot approve
your own PR; the separately documented admin path records an override.

## Install

<a id="quick-setup"></a>

### Guided setup

```sh
git clone https://github.com/sonomoshq/crux.git
cd crux
python3 install.py
```

`install.py` (stdlib only, no dependencies) walks through the setup: it checks the
prerequisites below and offers to install any that are missing, installs the
`crux` CLI with pipx, hooks up git (global or per-repo), and scaffolds config
(global or per-repo). It can also configure Slack, guiding you through creating
the app if you don't have one, writing the channel to your config, and saving
the bot token to the crux credentials file. It is designed to be re-run and
detects existing configuration before changes.
Review the [hook behavior](#hook-up-git-once-per-machine) before enabling it.

Prefer to do it by hand, or need to understand a step? The manual instructions
below cover exactly what the script automates.

### Manual installation

Crux supports a command-line workflow on macOS, Linux, and Windows. The current
automatic delayed-review path has a native Windows limitation, so Windows users
should start with the [manual commands and setup notes](#windows). Git hook
entry points are thin `sh` shims that hand off to Python; Git for Windows supplies
the shell for those entry points.

Requirements:

- Python ≥ 3.11
- `git`: 2.38 or newer for [super PRs](#super-prs-optional) (`git merge-tree
  --write-tree`); everything else runs on older gits. Ubuntu and Pop!_OS 22.04
  ship 2.34: `sudo add-apt-repository ppa:git-core/ppa && sudo apt update &&
  sudo apt install git`. `python install.py` warns when yours is too old.
- ripgrep (`rg`)
- GitHub CLI (`gh`), authenticated: run `gh auth login` if you haven't.
  Generic hosts without `gh` (CI runners, plain containers) can post with a
  `GH_TOKEN`/`GITHUB_TOKEN` env var instead. In the cloud environment documented
  in [Cloud sessions](#cloud-sessions), the session must publish the card using
  its GitHub tools. Preview does not require GitHub write authentication.
- Claude Code (`claude`) on your PATH and authenticated for model analysis

Install the `crux` CLI from a clone of this repo (no third-party Python runtime dependencies).
**`pipx` is recommended**: it installs into an isolated environment, puts `crux` on your
PATH, and works cleanly on distros with an *externally-managed*
system Python (Arch/Manjaro, Debian/Ubuntu, newer Fedora), where a plain `pip install`
is blocked by [PEP 668](https://peps.python.org/pep-0668/):

```sh
git clone https://github.com/sonomoshq/crux.git
cd crux
pipx install .
```

Install `pipx` first if you don't have it, then make sure its bin dir is on PATH:

```sh
sudo pacman -S python-pipx      # Arch / Manjaro   (dnf install pipx / apt install pipx elsewhere)
pipx ensurepath                 # add ~/.local/bin to PATH, then restart your shell
```

The runtime tools also come from your package manager, e.g. on Arch/Manjaro:
`sudo pacman -S ripgrep github-cli` (`rg` + `gh`); install Claude Code (`claude`) separately.

Alternatives:

- **Editable / dev install:** `pipx install --editable .`
- **A virtualenv:** `python -m venv .venv && . .venv/bin/activate && pip install .`
- **Plain `pip`** works only where Python isn't externally managed (e.g. some Fedora setups):
  `pip install .` (or `pip install -e .`). On Arch/Debian, `pip install --break-system-packages .`
  overrides the PEP 668 guard but fights your package manager: prefer `pipx`.

Verify it works (and that the pre-push hook will find it):

```sh
crux --help
crux --version   # confirms the installed CLI resolves from this terminal
```

> **Open a new terminal first.** `pipx install` puts `crux` and the private
> `_crux-hook` on PATH only for shells started *after* it runs. Restart the
> shell you just installed from, too. The hooks are deliberately silent when
> `_crux-hook` isn't found (`command -v _crux-hook || exit 0`), so a commit or
> push from a stale-PATH terminal simply does nothing rather than erroring. If
> hooks seem to no-op, this is almost always why. Restart your terminal and
> confirm `command -v _crux-hook` resolves.

### Hook up git (once per machine)

```sh
crux install-hooks
```

On native Windows, read the [automatic-review limitation](#windows) first.
This installation is global: run it once, not per repo. It installs the pre-push hook into
`~/.config/crux/git-hooks` and points `git config --global core.hooksPath` at it,
so pushes can trigger a background `crux run`. Automatic review and commit
enrichment require an allowlisted origin owner and, if configured, repository.
There is no default owner: `install.py` asks, or see `[scope]` in
`crux.toml.example`. Other installed hooks are described below.

Because a global `core.hooksPath` would otherwise disable per-repo hooks, the
directory also contains pass-through shims: every repo's own `.git/hooks` (husky,
pre-commit, etc.) keeps running, and a failing local pre-push still aborts the push.

The command also installs a `post-checkout` hook to track the base of new
branches and a `post-commit` hook for commit-message enrichment. **Enrichment
is enabled by default:** it can expand a human-written commit message from the
diff and amend the commit before it is pushed. A push may stop with an instruction
to push again if enrichment changed its commit IDs. To keep commit messages
unchanged, set `[commit] enrich = false` in your configuration before enabling
hooks.

For Claude Code, the command merges `Stop` (intent capture) and `SessionStart`
(start the local action service) hooks into `~/.claude/settings.json`, preserving
existing entries. The Stop hook stores the last assistant message in the working
repo's `.crux/intent.json`; the review allowlist does not gate that capture. If
the settings file cannot be parsed, it is left untouched and manual merge
instructions are printed instead.

- Already have a global `core.hooksPath`? Crux uses that existing
  directory and leaves the configured path in place. If a hook file conflicts
  with a non-Crux hook it cannot safely reuse, installation refuses that overwrite
  and explains what to do.
- Prefer per-repo? `crux install-hooks --local` scopes hook installation to the current
  repo: the git hooks go into `.git/hooks`, and the Claude Code hooks go into the
  repo's `.claude/settings.json`.

<a id="configure-optional"></a>

### Configure

Config is layered, like the hooks: a machine-global file applies to every repo,
and a repo's own file overrides it key by key.

- **Global (recommended for machine-wide things like Slack):** copy
  `crux.toml.example` to `~/.config/crux/crux.toml` (or
  `$XDG_CONFIG_HOME/crux/crux.toml`), in the same directory as the global git hooks.
- **Per repo:** copy it to `crux.toml` at the repo root. Only the keys you set
  there override the global file; everything else is inherited.

Most keys have defaults, but **`[scope] owners` must name the GitHub owners you
want `run` and `preview` to review**. There is no default owner; use the owner's
exact spelling from the `origin` remote. The optional `repos` list narrows
automatic review and commit-enrichment triggers and selects the super-PR picker's
repositories. Direct `run` and `preview` check only `owners`.

```toml
[scope]
owners = ["your-org"]
# repos = ["your-org/your-repo"]  # optional filter for automatic triggers

[commit]
enrich = false                 # leave commit messages alone when hooks run
```

The full [crux.toml.example](../crux.toml.example) documents the gate, sensitivity
rules, model, PR behavior, integrations, and other settings. Merge the sections
you need into an existing file instead of replacing your configuration.

## Claude Code plugin

Crux is also installable as a Claude Code plugin. This repo is its own
marketplace:

```
/plugin marketplace add sonomoshq/crux
/plugin install crux@crux
```

The plugin adds `/crux:preview`, `/crux:run`, `/crux:prs`, `/crux:setup`,
`/crux:super`, and `/crux:zenhub`. It also installs three kinds of hooks:

- **Stop:** capture the last assistant message as context for a later review.
- **SessionStart:** start `crux serve` if it is down, so card action links can
  reach the local service after a reboot.
- **PostToolUse:** watch `git push`, `gh pr create`, and supported GitHub MCP
  push/PR tools, then trigger a detached review.

Automatic review triggers respect `[scope] owners` and `[scope] repos`. If the
machine also has Crux's git pre-push hook, plain pushes are left to that hook to
avoid a duplicate review. The Stop and SessionStart hooks are separate from that
review scope. Publication depends on the host's GitHub access; cloud sessions can
need the manual posting step below. Native Windows users should also read the
[delayed-review limitation](#windows).

### Cloud sessions

The repository's cloud integration notes describe environments where the automatic
trigger cannot publish the card. Use `/crux:run` and the session's GitHub tools for
that step. Host capabilities can vary; the details below document that limitation
and the supported setup routes.

For claude.ai/code cloud sessions, the supported setup avoids relying on an
interactive plugin install.
The repository documents two complementary installation routes:

1. **Commit this to the target repo's `.claude/settings.json`** (this repo's
   own copy is the working example) so Claude Code offers/loads the plugin
   wherever repo-settings plugin installs are supported:

   ```json
   {
     "extraKnownMarketplaces": {
       "crux": {"source": {"source": "github", "repo": "sonomoshq/crux"}}
     },
     "enabledPlugins": {"crux@crux": true}
   }
   ```

2. **Add two lines to the claude.ai/code environment's setup script**: the
   reliable path. Cloud sessions have been observed starting with the repo
   settings above present but the plugin not installed; the setup script runs
   before Claude Code launches, so the plugin and its hooks are in place from
   the first turn:

   ```sh
   claude plugin marketplace add sonomoshq/crux || true
   claude plugin install crux@crux || true
   ```

In a cloud session the plugin also takes care of the CLI itself. A SessionStart
hook pip-installs `crux` from the plugin's own checkout whenever it's missing.
This applies only to cloud containers; on a laptop the hook never touches pip,
so install the CLI using [Install](#install). Three cloud-specific notes:

- **Analysis prerequisites.** `crux preview` and the plugin's hooks
  need `git`, ripgrep, Python ≥ 3.11, and the `claude` CLI, which are expected in
  the documented cloud environment. Check that they exist, and configure `[scope] owners` before
  running a preview.
- **Posting goes through the session's GitHub tools, and that step is
  manual.** In the cloud environment documented here, containers have no `gh` and the
  egress proxy answers GitHub API reads with its own credential while blocking
  raw writes. The PostToolUse hook still detaches a background `crux run`, and
  what that run does depends on whether a token is set. Neither outcome ends
  with a card:

  - **No token (the default state).** Crux's REST fallback refuses to send a
    request with no token at all, so the run fails at its *first* step, PR
    discovery, and stops **before any analysis**. `crux.log` gets
    `crux run failed: gh not installed and no GitHub token found …`.
  - **A token set.** PR reads can succeed through the proxy, but writes are
    refused. Metadata and status updates log warnings and continue. For a
    nontrivial default run, publishing the progress card fails before the
    Claude review begins. A trivial change or `--no-llm` follows a different
    path, but neither bypasses the restriction on publishing a review card.

  Either way the failure only reaches `~/.cache/crux/crux.log`; nothing
  routes it back into the session. To get the card up, ask for `/crux:run`
  (or run `crux preview` yourself). That command tells the session to take
  the rendered card and create/update the single `<!-- crux:card -->`
  comment with the GitHub MCP tools. On *other* gh-less hosts (generic CI,
  containers with open egress), no such step is needed: Crux posts directly
  through the GitHub REST API using `GH_TOKEN`/`GITHUB_TOKEN`.
- **Two things still need `gh` on every host**, with no REST fallback:
  `crux prs`, and the repo clone inside `crux super`. Install `gh` on a host that supports it to use those paths.

The plugin wraps, but does not replace, the `crux` CLI. Outside cloud sessions,
follow [Install](#install).

## Super PRs (optional)

When one feature spans several repos, reviewing its PRs one at a time hides
what they add up to and what breaks in the gaps between them. `crux super`
bundles them into a single brief and merges them with one command.

```toml
[super]
home  = "your-org/pr-bundles"   # existing repo; the brief is filed there as an issue
roots = ["~/src"]       # where local clones are found (optional)
```

```
crux super new                  # pick from local branches + open PRs, then brief them
crux super add 3                # a repo you did not expect? add it, keeping the number
crux super remove 3             # detach one: the PR itself is untouched
crux super refresh 3            # re-brief after the PRs move
crux super merge 3              # land them all, reporting whatever blocked
crux super order 3              # show the landing order and merge method
crux super order 3 web#12 api#34 worker#56 --method merge
                                # pin them: re-briefs keep it, Merge follows it
crux super list                  # list bundles
crux super show 3                # show bundle 3
```

`python install.py` prompts for `home` and adds this block to an existing
config. There is nothing else to declare. The picker offers the repos already
in `[scope]`: your `repos` allowlist if you set one, otherwise every repo of
your `owners`. The PRs you pick form the super PR. The picker lists branches
in your local clones alongside their open PRs, newest first. Picking a branch
with no PR opens one; Crux names every repo it is about to open a PR in and
asks once (`--yes` skips the ask). Name the bundle with `--name` if the default
(the first selected branch) is not what you want.

`--dry-run` on `new`, `add` or `remove` prints the proposed brief without
saving the bundle, pushing branches, or opening PRs. A picked branch that has
no PR yet is named and left out of the preview. With no one at the keyboard
(stdin closed or piped), the pickers and the merge confirmation cancel
instead of crashing. They explain how to pass the answer on the command line
(the picker numbers, or `--yes`).

A super PR is one change all the way through: one combined review pass over
collected evidence, one set of prompts, one Slack message. The pushes it makes
stand the pre-push hook down, so bundling five branches does not also produce five
single-repo review cards and five announcements. Later pushes to a member
branch re-brief the bundle instead of posting a per-PR card, and the brief
carries a short "how to verify this" walkthrough that crosses the repos.

### Landing order and merge method

The review pass proposes a landing order. By default, every re-brief proposes
it again, including the automatic one after a push to a member branch.
When the order is yours to decide (one PR needs another's API, or the
branches are stacked), pin it:

```
crux super order 3 your-org/web#12 api#34 worker#56
```

List every member that has not landed yet, in order, as `owner/repo#N` or
just `repo#N` when the repo name is unique in the bundle. A partial list is
refused rather than guessed at: the error prints the full command, ready to
edit. From then on, re-briefs keep your order (the brief still shows the review
pass's suggestion and its reasoning when it disagrees). A member added later
goes last, and a removed one drops out. `--unpin` hands the order back to the
review pass. `crux super order 3` on its own shows the current order and
method.

`--method merge|squash|rebase` sets how this bundle merges. Squash is the
default, and it is wrong for PRs built on each other's commits. Once the first
is squashed, the later ones conflict with the very commits they contain. Use
`--method merge` there. The method is stored on the bundle and in its brief,
so the brief's Merge button uses it on whoever's machine it is pressed. The
order of precedence is `crux super merge --method` (one run) > the bundle's
method > `[super] merge_method` in `crux.toml` > squash; `--method default`
clears the bundle's own.

Either change rewrites the brief's "Landing order" section in place, without
a new review pass or changes to the rest of the brief. The brief always shows
the method next to the order. `--no-brief` saves the change without touching the brief.

### Merge and Close, from the brief

```
crux serve                      # optional: the service also starts itself
```

You do not normally need to run the service by hand. Crux starts it as a detached
process during its push workflow and when it publishes a card or brief, if it is
not already listening. It does not require systemd, launchd, or Task Scheduler.
The Claude Code SessionStart hook can also bring it back after a reboot. Starting
the service does not resolve the separate [Windows delayed-review limitation](#windows).

The brief carries three links to `http://127.0.0.1:8787`. The same link works for
everyone reading it: loopback resolves on the machine of whoever clicks, so
the request reaches *their* Crux and `gh` approves in *their* name. Merge
approves every member PR as you and lands them in the brief's order, with its
merge method. **Set up to test** puts every member repo on its PR branch by
fetching, switching, and fast-forwarding. It never merges or stashes; repos with
uncommitted work are reported and left alone. The page then shows the verification
steps. Close retires the bundle,
with closing the member PRs offered as a separate, deliberate second button.

You cannot approve your own work. Wrote every PR in the bundle? The page
refuses and offers to ask in Slack for someone else. Wrote one of them? The
rest is approved and landed, and yours is skipped with a reason naming it. The
card can't hide the button from you because GitHub renders one issue body for
everyone. The rule is enforced when you open the link, and
`crux super merge` follows the same one.

When the approval path can't be satisfied, **Admin merge** lands the bundle on
admin rights without approving anything. It needs admin on every member repo,
and it records who used the override and that nothing was approved in the
merge report and the Slack reply. Ordinary single-PR cards carry the same pair of
buttons.

If `[slack] channel` is configured, publishing a bundle announces it there too,
and every later refresh plus the final merge report replies in that same
thread: one bundle, one conversation.

Crux combines the member PRs into **one review workflow** using per-repo evidence
summaries. It uses `git merge-tree` to compute combined changes without switching
your working tree; fetching references, caching clones, and creating Git objects
can still happen. The model may retry, so one combined review does not mean a
fixed price or exactly one model call.

Briefs deliberately cap ideas and review checks, while keeping merge conflicts
and the member landing order visible. Their length can therefore grow with the
bundle. Cross-repo merges **are not atomic**: some PRs can land while others are
blocked. Crux reports each outcome so you can resolve the blockers and retry.

## Slack notifications (optional)

Crux can announce each PR to a Slack channel and thread later updates under the
same message. It's off until you set both a channel and a bot token. Configure
it once in the global `~/.config/crux/crux.toml` and it works for every repo;
a repo can pick a different channel in its own `crux.toml`, or opt out with
`channel = ""`.

1. **Create a Slack app** at <https://api.slack.com/apps> → *Create New App* →
   *From scratch*, and pick your workspace.
2. **Add bot scopes** under *OAuth & Permissions* → *Bot Token Scopes*:
   - `chat:write`: post messages
   - `channels:history`: check whether a PR is already linked (add
     `groups:history` too for private channels)
   - `channels:read`: resolve a channel name to its ID (add `groups:read`
     for private channels; not needed if you configure a channel **ID**)
3. **Install the app** to your workspace (*Install App*) and copy the
   **Bot User OAuth Token** (`xoxb-…`).
4. **Invite the bot** to the channel: in Slack, `/invite @YourApp` in
   `#pull-requests`.
5. **Point Crux at it**: in `~/.config/crux/crux.toml` (all repos) or a
   repo's `crux.toml` (that repo only):

   ```toml
   [slack]
   channel = "pull-requests"    # channel name or ID (C0123…)
   # token_env = "SLACK_BOT_TOKEN"   # env var Crux reads the token from
   ```

6. **Give Crux the token.** `python3 install.py`'s Slack step saves it to the
   crux credentials file (`~/.config/crux/credentials.json`, mode `0600`),
   which Crux reads directly, so it works under any shell (bash/zsh/fish/
   PowerShell) and even when a push comes from an IDE, GUI client, or agent
   with no shell environment. To set it by hand:

   ```sh
   mkdir -p ~/.config/crux
   printf '{"slack_bot_token": "xoxb-…"}\n' > ~/.config/crux/credentials.json
   chmod 600 ~/.config/crux/credentials.json
   ```

   Prefer an environment variable instead? Crux still honors one: export
   `SLACK_BOT_TOKEN` (or the name set by `token_env`) and it takes precedence
   over the file. The credentials file is the shell-independent default.

   > **On Windows the `0600` mode is a no-op.** NTFS doesn't honor POSIX
   > permission bits, so `credentials.json` gets no owner-only protection there:
   > it's readable by anything running as your user. Treat it like any other
   > secret on that machine (rely on your account's file ACLs), or use the
   > `SLACK_BOT_TOKEN` env var if your setup manages secrets that way.

On the first push Crux checks the channel for the PR link; if it's absent it
posts a new message, and every later push replies in that message's thread. It's
best-effort: if the token or scopes are missing, Crux logs a warning and the
review proceeds normally.

## Zenhub (optional)

A ticket in Zenhub and the pull request that implements it are two records of
one piece of work. Crux already knows when a PR lands because it is the thing
that lands it. It can close the ticket too, keeping the board aligned with
what actually shipped.

**Off unless you turn it on.** While `[zenhub] workspace` is empty, Crux makes
no Zenhub calls and asks no questions.

```bash
crux zenhub setup      # stores an API key in ~/.config/crux/credentials.json
```

Then name your workspace in `~/.config/crux/crux.toml`:

```toml
[zenhub]
workspace = "Engineering"       # a name, or the ID from the app URL
done_pipeline = "Closed"     # optional: where a closed card lands
```

Keys are made at **app.zenhub.com/settings/tokens** (Zenhub shows one once;
copy it before closing the dialog). `crux zenhub status` shows what is
configured; `crux zenhub doctor` checks Crux's queries against the live Zenhub
schema, which is where to look first if something silently does nothing.

### Linking

On `git push`, Crux offers a ranked list of tickets your branch is probably
about, showing each one's number, title and description. You pick:

```
🎫 Zenhub: which ticket does example-org/Widget#42 close?

   1  example-org/Widget#29 · Photo uploads fail on slow networks  [In Progress]  ← named by this branch
      The uploader gives up after three attempts even when the backend is healthy.
   2  example-org/Widget#31 · Rename the dashboard tabs  [New]
      The "Overview" and "Summary" tabs are the same thing twice.

   Numbers (e.g. 1 or 1,3), or enter for none:
```

Pressing enter links nothing, so having the feature on is never in the way.
A branch named `fix/issue-29-…` floats its ticket to the top; the rest are
ordered by what the branch and the ticket title have in common. Set
`[zenhub] ask = false` to stop being offered and link by hand instead.

The question comes from the **pre-push** step, which also asks "create a PR?"
The review itself runs detached in the background and has no terminal to ask on.
Your answer is held and applied once the PR exists, so it works on a first push
where the PR does not exist yet. You are asked once per branch: a branch that is
already linked, or that answered on an earlier push, is not asked again, and a
push with no terminal at all (CI, an IDE, an agent) is never asked and never
pays a Zenhub round trip for it.

Any PR can be linked later, including one Crux did not open:

```bash
crux zenhub link                      # this branch's PR, pick from a list
crux zenhub link --pr 42 --issue 29   # no picker; link outright
crux zenhub link --super 7            # a whole bundle (see below)
crux zenhub list                      # everything Crux is holding
crux zenhub unlink --pr 42
```

Crux records the link, draws Zenhub's own issue↔PR connection, and leaves a
sticky comment on the PR saying what landing it will close.

### Regular PRs and super PRs

The two shapes reach Zenhub differently, and Crux picks the right one:

|                | Regular PR | Super PR |
| -------------- | ---------- | -------- |
| The work is    | one PR in its own repo | several PRs, plus a brief issue in the `[super] home` repo |
| Tickets hang off | that PR | **the bundle** |
| They close when | that PR merges | **every** member PR has merged |
| The note goes on | the PR | the brief *and* each member PR |

The brief is Crux's own artifact, not a ticket, so it is never what gets
closed. And a cross-repo ticket is not done while half its repos are unmerged,
which is why a bundle's tickets wait for the last member rather than the first.
`crux zenhub link` on a branch that belongs to an open bundle says so and
points you at `--super N`, the same guard `crux merge` already has.

### Closing

Landing a PR through Crux (`crux merge`, `crux super merge`, or the brief's
Merge button) closes its tickets and moves them to `done_pipeline`. Closing
sets the ticket's state (and with it the GitHub issue behind it); the move is
what repositions the card, which boards do not always do on their own.

Crux has no webhook, so a PR merged from the GitHub UI, by a teammate, or by
automerge is caught by a command instead:

```bash
crux zenhub sync --dry-run   # say what would close
crux zenhub sync             # close it
```

Safe to run as often as you like: a link closes once, an unreadable PR state is
never read as "landed", and a ticket already closed is left alone.

## Memory

Crux remembers durable facts about each repo it reviews, including conventions,
architectural quirks and recurring pitfalls. It reads them into every later
review, so cards sharpen over time the way a longtime maintainer's reviews
would. The store maintains itself: each review may add up to 3 facts and retire
up to 3 it just disproved (a convention replaced, a pitfall designed out), and a
fact anchored to a file that later disappears is forgotten automatically. You
should not have to touch it, but you can, any time:

```sh
crux memory                    # list what Crux knows about this repo
crux memory add "Every new config key must also land in crux.toml.example" \
    --anchor crux.toml.example # pinned: reviews can never retire a fact you added
crux memory forget 1a2b3c4d    # drop one by its id
crux memory clear --yes        # forget everything about this repo
```

The store lives per machine in `~/.config/crux/memory/` (one JSON file per
repo, capped at 40 facts). `[memory] enabled = false` in `crux.toml` stops
reviews reading and writing it; see `crux.toml.example`.

## Windows

**Current limitation:** automatic reviews after pushes and automatic super-PR
refreshes depend on Unix-only file locking in
[`crux/superdebounce.py`](../crux/superdebounce.py) and fail on native Windows.
Manual `crux preview`, `crux run`, and `crux super refresh N` without `--delay`
bypass that code path. Use those manual commands, or a Linux environment such
as WSL for the automatic workflow. This limitation is established from the
current source, not a full Windows compatibility test.

For the manual workflow, use **PowerShell**, cmd, or Git Bash. Git for Windows
includes the `sh` used by the hook entry points; the Claude Code intent hook is
a console command, so it does not need a `python3` script path.

Install the prerequisites using
[winget](https://learn.microsoft.com/windows/package-manager/) (bundled with
Windows 10/11):

```powershell
winget install -e --id Git.Git
winget install -e --id GitHub.cli
winget install -e --id BurntSushi.ripgrep.MSVC
winget install -e --id Python.Python.3.12
python -m pip install --user pipx
python -m pipx ensurepath
```

Install **Claude Code** as well (see the Claude Code docs for the Windows
installer), and log in to `gh`:

```powershell
gh auth login          # GitHub.com → HTTPS → authenticate in the browser
```

Open a **new** terminal so the updated PATH takes effect, then follow the
[Quick start](../README.md#quick-start): clone, `pipx install .`, configure scope in the
target repo, and preview manually. Run the guided installer as `python install.py`
on Windows. It detects winget/scoop/choco, offers to install missing tools, and
saves the Slack token in `~/.config/crux/credentials.json`. Installing hooks does
not resolve the automatic-review limitation above.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| `preview` or `run` does nothing | Add the exact GitHub owner from your `origin` remote to `[scope] owners`. Run from the project you want to review. |
| `crux: skipped` instead of a card | The committed diff met all of the [gate's triviality checks](../README.md#how-it-works). This is an expected outcome. |
| Your latest edits are missing | Crux reviews commits. Commit the changes, and confirm `--base` is the branch you intend to merge into. |
| A push produces no automatic review | Open a new terminal and check that `_crux-hook` is on PATH. Check owner/repo scope and `~/.cache/crux/crux.log`. On native Windows, use the [manual workflow](#windows). |
| A push asks you to push again | Commit enrichment amended an outgoing commit. Read the message, then push again; set `[commit] enrich = false` to disable enrichment. |
| Analysis succeeds in a cloud session but no card appears | Use the [cloud publishing path](#cloud-sessions): ask for `/crux:run` so the session can post using its GitHub tools. |
| Card action links don't work | They reach your local `crux serve` on `127.0.0.1:8787`. Check that Crux is installed on the machine where you click. |
| A linked Zenhub ticket did not close | If the PR merged outside Crux, inspect `crux zenhub sync --dry-run`, then run `crux zenhub sync`. |

Run `crux <command> --help` for command options. Background failures are logged
in `~/.cache/crux/crux.log`.


## Developing Crux

From a checkout with Python 3.11+ and the relevant command-line prerequisites,
install pytest and setuptools in your development environment, then run:

```sh
python -m pytest
python scripts/check_copyright.py
```

The suite includes both `unittest` cases and pytest functions that use fixtures;
pytest runs both. Pytest and setuptools are development/build tools, not Crux
runtime dependencies. See `pyproject.toml` for the setuptools version requirement.
Keep new config keys documented in `crux.toml.example` and preserve the package's
prompt files.
