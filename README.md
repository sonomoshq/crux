# Crux

Finds the crux of a PR. Runs locally after you push, skips trivial changes, and posts one
self-updating comment on the PR: the lines you actually need to read, in causal order, with
evidence, plus proof the rest is safe to skim

```
crux run              # analyze current branch, post/update the PR card
crux preview          # same analysis, print the card to the terminal
crux prs              # list open PRs across all your repos, in parallel
crux merge            # approve this branch's PR as you, then merge it
crux serve            # the Merge / Set-up-to-test / Close buttons on Crux's cards
crux memory           # what Crux remembers about this repo — list/add/forget/clear
crux install-hooks    # pre-push trigger + Claude Code intent-capture hook
```

## Want to help? Where Crux could be better

Contributions are welcome. These are the areas where Crux most needs work:

- **Cards that explain, not just list.** The cards are still too detailed. A
  reviewer should come away with a sense of what is happening with the code:
  what changed, why, and what it means for the system. Today they get a
  line-by-line inventory. Shorter cards that tell the story of the change would
  be a big win.
- **Integration with other tools.** Crux talks to GitHub, Slack, Zenhub and
  Claude Code today. Linear, Jira, GitLab, Discord, other editors and other
  coding agents are all open territory.
- **Quality of life.** Faster runs, clearer errors, smoother setup, better
  defaults. The small things that make Crux pleasant to live with every day.

## Quick setup

```sh
git clone https://github.com/sonomoshq/crux.git
cd crux
python3 install.py
```

`install.py` (stdlib only, no dependencies) walks the whole setup: it checks the
prerequisites below and offers to install any that are missing, installs the
`crux` CLI with pipx, hooks up git (global or per-repo), scaffolds config (global
or per-repo), and can configure Slack — guiding you through creating the app if
you don't have one, writing the channel to your config, and saving the bot token
to the crux credentials file. It's safe to re-run; every step is idempotent.

Prefer to do it by hand, or need to understand a step? The manual instructions
below cover exactly what the script automates.

## Claude Code plugin

Crux is also installable as a Claude Code plugin — this repo is its own
marketplace:

```
/plugin marketplace add sonomoshq/crux
/plugin install crux@crux
```

The plugin adds `/crux:preview`, `/crux:run`, `/crux:prs`, and `/crux:setup`, the intent-
capture Stop hook, a SessionStart hook that starts `crux serve` if it is down (so the
super PR Merge/Close buttons survive a reboot), and a PostToolUse hook that watches every session for
`git push` / `gh pr create` / the GitHub MCP push+PR tools and detaches
`crux run` automatically — so a PR shipped from a Claude Code session gets
its review card (and Slack announcement) on any surface: the terminal CLI,
the desktop app, or a claude.ai/code cloud session. Repos are opted in via
`[scope] owners` / `[scope] repos` in `crux.toml`; if the machine also has
`crux install-hooks`' git pre-push hook, plain pushes are left to it (no
double review).

For claude.ai/code cloud sessions, plugins can't be installed interactively.
Two ways to get the plugin into a session, belt and suspenders:

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

2. **Add two lines to the claude.ai/code environment's setup script** — the
   reliable path. Cloud sessions have been observed starting with the repo
   settings above present but the plugin not installed; the setup script runs
   before Claude Code launches, so the plugin and its hooks are in place from
   the first turn:

   ```sh
   claude plugin marketplace add sonomoshq/crux || true
   claude plugin install crux@crux || true
   ```

In a cloud session the plugin also takes care of the CLI itself: a
SessionStart hook pip-installs `crux` from the plugin's own checkout whenever
it's missing (cloud containers only — on a laptop the hook never touches pip;
install the CLI as below). Two cloud-specific notes:

- **Analysis works out of the box.** `crux preview` and the plugin's hooks
  need only `git`, ripgrep, Python ≥ 3.11, and the `claude` CLI — all present
  in cloud containers.
- **Posting goes through the session's GitHub tools, and that step is
  manual.** Cloud containers have no `gh`, and the claude.ai/code egress
  proxy answers GitHub API reads with its own credential while blocking raw
  writes. The PostToolUse hook still detaches a background `crux run`, and
  what that run does depends on whether a token is set — neither outcome
  ends with a card:

  - **No token (the default state).** Crux's REST fallback refuses to send a
    request with no token at all, so the run fails at its *first* step, PR
    discovery, and stops there — **before any analysis**. `crux.log` gets
    `crux run failed: gh not installed and no GitHub token found …`.
  - **A token set.** Reads now succeed (the proxy answers them with its own
    credential), so the analysis does run — and then every write is refused.
    `sync_pr_metadata` and the commit status log a warning each and carry on;
    the card upsert is what actually fails the run.

  Either way the failure only reaches `~/.cache/crux/crux.log` — nothing
  routes it back into the session. To get the card up, ask for `/crux:run`
  (or run `crux preview` yourself): that command tells the session to take
  the rendered card and create/update the single `<!-- crux:card -->`
  comment with the GitHub MCP tools. On *other* gh-less hosts (generic CI,
  containers with open egress), no such step is needed — Crux posts directly
  through the GitHub REST API using `GH_TOKEN`/`GITHUB_TOKEN`.
- **Two things still need `gh` on every host**, with no REST fallback:
  `crux prs`, and the repo clone inside `crux super`. Both are effectively
  terminal-and-desktop only.

The plugin wraps, but does not replace, the `crux` CLI — outside cloud
sessions, install it as below.

## Contents

- [Install](#install) — macOS / Linux / Windows quickstart
  - [Hook up git](#hook-up-git-once-per-machine)
  - [Configure](#configure-optional)
  - [Slack notifications](#slack-notifications-optional)
  - [Zenhub](#zenhub-optional) — close tickets when their PRs land
- [Memory](#memory) — durable facts Crux carries between reviews
- [Windows](#windows) — installing the prerequisites (native, no WSL)
- [Design](#design) — `DESIGN.md`

## Install

Crux runs on **macOS, Linux, and native Windows** — any OS, any shell. Its git
hooks are OS-agnostic (thin `sh` shims that hand off to Python, which git runs
through its own bundled shell on every platform, Git for Windows included), so
there's no WSL requirement and PowerShell/cmd are fine. On Windows, see the
[Windows](#windows) note below for installing the prerequisites; every other
step is identical.

Requirements:

- Python ≥ 3.11
- `git` — 2.38 or newer for [super PRs](#super-prs-optional) (`git merge-tree
  --write-tree`); everything else runs on older gits. Ubuntu and Pop!_OS 22.04
  ship 2.34: `sudo add-apt-repository ppa:git-core/ppa && sudo apt update &&
  sudo apt install git`. `python install.py` warns when yours is too old.
- ripgrep (`rg`)
- GitHub CLI (`gh`), authenticated — run `gh auth login` if you haven't.
  Generic hosts without `gh` (CI runners, plain containers) can post with a
  `GH_TOKEN`/`GITHUB_TOKEN` env var instead. claude.ai/code cloud sessions
  can use neither: no `gh`, and their egress proxy blocks the writes a token
  would carry — the session posts the card itself (plugin section above).
  Analysis (`crux preview`) needs none of this.
- Claude Code (`claude`) on your PATH

Install the `crux` CLI from a clone of this repo (no third-party Python dependencies).
**`pipx` is recommended** — it installs into an isolated environment, puts `crux` on your
PATH, and is the only option that works cleanly on distros with an *externally-managed*
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
  overrides the PEP 668 guard but fights your package manager — prefer `pipx`.

Verify it works (and that the pre-push hook will find it):

```sh
crux --help
which crux        # should be on your PATH, e.g. ~/.local/bin/crux
```

> **Open a new terminal first.** `pipx install` puts `crux` and the private
> `_crux-hook` on PATH only for shells started *after* it runs — including the
> shell you just installed from. The hooks are deliberately silent when
> `_crux-hook` isn't found (`command -v _crux-hook || exit 0`), so a commit or
> push from a stale-PATH terminal simply does nothing rather than erroring. If
> hooks seem to no-op, this is almost always why — restart your terminal and
> confirm `command -v _crux-hook` resolves.

### Hook up git (once per machine)

```sh
crux install-hooks
```

This is global — run it once, not per repo. It installs the pre-push hook into
`~/.config/crux/git-hooks` and points `git config --global core.hooksPath` at it,
so `crux run` fires after every push in every repo. Crux only *acts* on repos whose
origin owner is allowlisted (there is no default: `install.py` asks, or see the `[scope]` section in
`crux.toml.example`); everywhere else the hook exits quietly.

Because a global `core.hooksPath` would otherwise disable per-repo hooks, the
directory also contains pass-through shims: every repo's own `.git/hooks` (husky,
pre-commit, etc.) keeps running, and a failing local pre-push still aborts the push.

The command also installs the Claude Code intent-capture hook (D10): it merges a
`Stop` hook entry into `~/.claude/settings.json`, preserving whatever is already
there. If your settings file can't be parsed, it is left untouched and manual
merge instructions are printed instead.

- Already have a global `core.hooksPath`? Crux installs its pre-push into your
  existing directory and leaves your config and other hooks untouched.
- Prefer per-repo? `crux install-hooks --local` scopes both hooks to the current
  repo: the pre-push goes into `.git/hooks`, the Claude hook into the repo's
  `.claude/settings.json`.

### Configure (optional)

Config is layered, like the hooks: a machine-global file applies to every repo,
and a repo's own file overrides it key by key.

- **Global (recommended for machine-wide things like Slack):** copy
  `crux.toml.example` to `~/.config/crux/crux.toml` (or
  `$XDG_CONFIG_HOME/crux/crux.toml`) — same directory as the global git hooks.
- **Per repo:** copy it to `crux.toml` at the repo root. Only the keys you set
  there override the global file; everything else is inherited.

All keys are optional; without any config, defaults apply.

### Super PRs (optional)

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
crux super remove 3             # detach one — the PR itself is untouched
crux super refresh 3            # re-brief after the PRs move
crux super merge 3              # land them all, reporting whatever blocked
crux super order 3              # show the landing order and merge method
crux super order 3 web#12 api#34 worker#56 --method merge
                                # pin them: re-briefs keep it, Merge follows it
crux super list | show 3
```

`python install.py` prompts for `home` and adds this block to an existing
config. There is nothing else to declare: the picker offers the repos already
in `[scope]` — your `repos` allowlist if you set one, else every repo of your
`owners` — and the PRs you pick are the super PR. It lists branches in your
local clones alongside their open PRs, newest first; picking a branch with no
PR opens one — it names every repo it is about to open a PR in and asks once
(`--yes` skips the ask). Name the bundle with `--name` if the default (the
first selected branch) is not what you want.

`--dry-run` on `new`, `add` or `remove` prints the brief the change would
produce and changes nothing: no bundle is saved, no branch pushed, no PR
opened (a picked branch that has no PR yet is named and left out of the
preview). With no one at the keyboard — stdin closed or piped — the pickers
and the merge confirmation cancel instead of crashing, and say how to pass the
answer on the command line (the picker numbers, or `--yes`).

A super PR is one change all the way through: one review pass over the combined
diff, one set of prompts, one Slack message. The pushes it makes stand the
pre-push hook down, so bundling five branches does not also produce five
single-repo review cards and five announcements. Later pushes to a member
branch re-brief the bundle instead of posting a per-PR card, and the brief
carries a short "how to verify this" walkthrough that crosses the repos.

### Landing order and merge method

The review pass proposes a landing order, and by default every re-brief —
including the automatic one after a push to a member branch — proposes it
again. When the order is yours to decide (one PR needs another's API, or the
branches are stacked), pin it:

```
crux super order 3 your-org/web#12 api#34 worker#56
```

List every member that has not landed yet, in order, as `owner/repo#N` or
just `repo#N` when the repo name is unique in the bundle. A partial list is
refused rather than guessed at — the error prints the full command, ready to
edit. From then on re-briefs keep your order (the brief still shows the review
pass's suggestion and its reasoning when it disagrees), a member added later
goes last, and a removed one drops out. `--unpin` hands the order back to the
review pass. `crux super order 3` on its own shows the current order and
method.

`--method merge|squash|rebase` sets how this bundle merges. Squash is the
default, and it is wrong for PRs built on each other's commits: once the first
is squashed, the later ones conflict with the very commits they contain — use
`--method merge` there. The method is stored on the bundle and in its brief,
so the brief's Merge button uses it on whoever's machine it is pressed. The
order of precedence is `crux super merge --method` (one run) > the bundle's
method > `[super] merge_method` in `crux.toml` > squash; `--method default`
clears the bundle's own.

Either change rewrites the brief's "Landing order" section in place — no new
review pass, nothing else on the brief touched — and the brief always shows
the method next to the order. `--no-brief` saves the change without touching
the brief.

### Merge and Close, from the brief

```
crux serve                      # optional: the service also starts itself
```

You do not normally run that. Every push, and every card or brief Crux
publishes, starts the service if it is not already listening — detached, the
same way the background review runs, so it works the same on macOS, Linux and
Windows with no systemd, launchd or Task Scheduler involved. It comes back by
itself after a reboot, the first time you push.

The brief carries three links to `http://127.0.0.1:8787`. The same link works for
everyone reading it — loopback resolves on the machine of whoever clicks, so
the request reaches *their* Crux and `gh` approves in *their* name. Merge
approves every member PR as you and lands them in the brief's order, with its
merge method. **Set up to
test** puts every member repo on its PR branch — fetch, switch, fast-forward,
never a merge or a stash, and repos with uncommitted work are reported and left
alone — then shows the verification steps right there. Close retires the bundle,
with closing the member PRs offered as a separate, deliberate second button.

You cannot approve your own work. Wrote every PR in the bundle? The page
refuses and offers to ask in Slack for someone else. Wrote one of them? The
rest is approved and landed, and yours is skipped with a reason naming it. The
card can't hide the button from you — GitHub renders one issue body for
everyone — so the rule is enforced when you open the link, and
`crux super merge` follows the same one.

When the approval path can't be satisfied, **Admin merge** lands the bundle on
admin rights without approving anything. It needs admin on every member repo,
and it writes who overrode — and that nothing was approved — into the merge
report and the Slack reply. Ordinary single-PR cards carry the same pair of
buttons.

If `[slack] channel` is configured, publishing a bundle announces it there too,
and every later refresh plus the final merge report replies in that same
thread — one bundle, one conversation.

Two properties worth knowing, because they are the point of the feature: a
bundle of 8 PRs costs **one** review pass (the PR heads are merged in memory
with `git merge-tree`, which never touches your working tree), and the brief is
**one screen** whether the bundle holds 3 PRs or 30. Merges are land-what-can:
a cross-repo merge cannot be atomic, so anything blocked is reported with a
reason and re-running is safe.

### Slack notifications (optional)

Crux can announce each PR to a Slack channel and thread later updates under the
same message. It's off until you set both a channel and a bot token. Configure
it once in the global `~/.config/crux/crux.toml` and it works for every repo;
a repo can pick a different channel in its own `crux.toml`, or opt out with
`channel = ""`.

1. **Create a Slack app** at <https://api.slack.com/apps> → *Create New App* →
   *From scratch*, and pick your workspace.
2. **Add bot scopes** under *OAuth & Permissions* → *Bot Token Scopes*:
   - `chat:write` — post messages
   - `channels:history` — check whether a PR is already linked (add
     `groups:history` too for private channels)
   - `channels:read` — resolve a channel name to its ID (add `groups:read`
     for private channels; not needed if you configure a channel **ID**)
3. **Install the app** to your workspace (*Install App*) and copy the
   **Bot User OAuth Token** (`xoxb-…`).
4. **Invite the bot** to the channel: in Slack, `/invite @YourApp` in
   `#pull-requests`.
5. **Point Crux at it** — in `~/.config/crux/crux.toml` (all repos) or a
   repo's `crux.toml` (that repo only):

   ```toml
   [slack]
   channel = "pull-requests"    # channel name or ID (C0123…)
   # token_env = "SLACK_BOT_TOKEN"   # env var Crux reads the token from
   ```

6. **Give Crux the token.** `python3 install.py`'s Slack step saves it to the
   crux credentials file (`~/.config/crux/credentials.json`, mode `0600`),
   which Crux reads directly — so it works under any shell (bash/zsh/fish/
   PowerShell) and even when a push comes from an IDE, GUI client, or agent
   with no shell environment. To set it by hand:

   ```sh
   mkdir -p ~/.config/crux
   printf '{"slack_bot_token": "xoxb-…"}\n' > ~/.config/crux/credentials.json
   chmod 600 ~/.config/crux/credentials.json
   ```

   Prefer an environment variable instead? Crux still honors one — export
   `SLACK_BOT_TOKEN` (or the name set by `token_env`) and it takes precedence
   over the file. The credentials file is the shell-independent default.

   > **On Windows the `0600` mode is a no-op.** NTFS doesn't honor POSIX
   > permission bits, so `credentials.json` gets no owner-only protection there —
   > it's readable by anything running as your user. Treat it like any other
   > secret on that machine (rely on your account's file ACLs), or use the
   > `SLACK_BOT_TOKEN` env var if your setup manages secrets that way.

On the first push Crux checks the channel for the PR link; if it's absent it
posts a new message, and every later push replies in that message's thread. It's
best-effort — if the token or scopes are missing, Crux logs a warning and the
review proceeds normally.

### Zenhub (optional)

A ticket in Zenhub and the pull request that implements it are two records of
one piece of work. Crux already knows when a PR lands — it is the thing that
lands it — so it can close the ticket too, and the board stops drifting from
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

Keys are made at **app.zenhub.com/settings/tokens** (Zenhub shows one once —
copy it before closing the dialog). `crux zenhub status` shows what is
configured; `crux zenhub doctor` checks Crux's queries against the live Zenhub
schema, which is where to look first if something silently does nothing.

#### Linking

On `git push`, Crux offers the tickets your branch is probably about — ranked,
with each one's number, title and description — and you pick:

```
🎫 Zenhub — which ticket does example-org/Widget#42 close?

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

The question comes from the **pre-push** step, alongside "create a PR?" — the
review itself runs detached in the background and has no terminal to ask on.
Your answer is held and applied once the PR exists, so it works on a first push
where the PR does not exist yet. You are asked once per branch: a branch that is
already linked, or that answered on an earlier push, is not asked again, and a
push with no terminal at all (CI, an IDE, an agent) is never asked and never
pays a Zenhub round trip for it.

Any PR can be linked later — including one Crux did not open:

```bash
crux zenhub link                      # this branch's PR, pick from a list
crux zenhub link --pr 42 --issue 29   # no picker; link outright
crux zenhub link --super 7            # a whole bundle (see below)
crux zenhub list                      # everything Crux is holding
crux zenhub unlink --pr 42
```

Crux records the link, draws Zenhub's own issue↔PR connection, and leaves a
sticky comment on the PR saying what landing it will close.

#### Regular PRs and super PRs

The two shapes reach Zenhub differently, and Crux picks the right one:

|                | Regular PR | Super PR |
| -------------- | ---------- | -------- |
| The work is    | one PR in its own repo | several PRs, plus a brief issue in the `[super] home` repo |
| Tickets hang off | that PR | **the bundle** |
| They close when | that PR merges | **every** member PR has merged |
| The note goes on | the PR | the brief *and* each member PR |

The brief is Crux's own artifact, not a ticket — so it is never what gets
closed. And a cross-repo ticket is not done while half its repos are unmerged,
which is why a bundle's tickets wait for the last member rather than the first.
`crux zenhub link` on a branch that belongs to an open bundle says so and
points you at `--super N`, the same guard `crux merge` already has.

#### Closing

Landing a PR through Crux — `crux merge`, `crux super merge`, or the brief's
Merge button — closes its tickets and moves them to `done_pipeline`. Closing
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

Crux remembers durable facts about each repo it reviews — conventions,
architectural quirks, recurring pitfalls — and reads them into every later
review, so cards sharpen over time the way a longtime maintainer's reviews
would. The store maintains itself: each review may add up to 3 facts and retire
up to 3 it just disproved (a convention replaced, a pitfall designed out), and a
fact anchored to a file that later disappears is forgotten automatically. You
should not have to touch it — but you can, any time:

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

Crux runs on **native Windows** — no WSL. The git hooks it installs are thin
`sh` shims, and git runs hooks through the `sh` that ships with Git for Windows,
so they work the same as on macOS/Linux; the hook logic itself is Python, and
the Claude Code intent hook is a console command (not a `python3` script path),
so nothing depends on a POSIX shell or a `python3` on PATH. You can drive
everything from **PowerShell** (or cmd, or Git Bash).

The only Windows-specific part is installing the prerequisites. Using
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
[Install](#install) steps exactly as written — clone, `pipx install .`,
`crux install-hooks`. `install.py` also works on Windows: it detects
winget/scoop/choco, installs missing tools, and persists the Slack token with
`setx`. Everything after prerequisites is identical across platforms.

## Design

See `DESIGN.md` for the full design.
