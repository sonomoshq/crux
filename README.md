<h1 align="center">Crux</h1>

<p align="center"><strong>Get to the crux of every pull request.</strong></p>
<p align="center">Less diff archaeology. More time reviewing what matters.</p>

<p align="center">
  <a href="#quick-start"><img src="docs/assets/button-get-started.svg" alt="Get started" height="36"></a>
  <a href="docs/examples/review-card.md"><img src="docs/assets/button-see-example.svg" alt="See a review card" height="36"></a>
  <a href="#help-make-crux-better"><img src="docs/assets/button-contribute.svg" alt="Contribute" height="36"></a>
</p>

<p align="center">
  <a href="LICENSE">Apache 2.0</a> · Python 3.11+ · <a href="https://sonomos.ai">Sonomos</a>
</p>

**Crux helps you find the changes worth your attention.** It turns a GitHub PR into one self-updating review card: what changed, why it matters, and which lines deserve a closer look. You still review the code. Crux helps you start in the right place.

Originally developed internally by **Jack Rose, co-founder of [Sonomos](https://sonomos.ai)**, Crux is a tool we use in our own development and code review. We open-sourced it for teams who'd rather understand a PR than spend the first 20 minutes reconstructing it.

## Review the change, not the noise

| Without Crux | With Crux |
| --- | --- |
| Dig through a large diff to figure out its purpose | **The big picture:** a plain-language summary and change context |
| Guess which edits deserve scrutiny | **Must read** and **Worth a skim** pointers with links to the code |
| Start over when new commits arrive | **One review card** refreshed in place on later runs |
| Piece together a feature spanning repositories | **Super PRs** with one combined brief and coordinated merge order |

Small, routine changes can skip model analysis. For substantial changes, Crux adds a starting point, **not** a rubber-stamp approval or a claim that the rest of the diff is safe.

## See the review card

[![Illustrative Crux review card showing the big picture, two must-read changes, and one item worth a skim.](docs/assets/crux-review-example.svg)](docs/examples/review-card.md)

[Explore the full example, with links to actual Crux code →](docs/examples/review-card.md)

*Illustrative example, rendered with Crux's own card renderer and hand-written annotations. Not a captured AI review or benchmark.*

## Quick start

Start with a terminal preview. No GitHub comment, bot, or automatic hook is required.

**1. Install.** You'll need Python 3.11+, Git, [ripgrep](https://github.com/BurntSushi/ripgrep), `pipx`, and an authenticated [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/overview). See the [full installation guide](docs/reference.md#install) for alternatives and Windows notes.

```sh
git clone https://github.com/sonomoshq/crux.git
cd crux
pipx install .
pipx ensurepath
```

Open a new terminal and check `crux --help`.

**2. Choose your repo.** In the **project you want to review**, create or edit `crux.toml`. Its GitHub owner must be explicitly allowlisted:

```toml
[scope]
owners = ["your-github-org-or-username"]
```

Use the owner from that project's `origin` remote, not necessarily `sonomoshq`. Without it, Crux won't analyze the repository.

**3. Preview your committed changes.** From that project's feature branch:

```sh
crux preview --base main
```

Use your actual target branch in place of `main`. Uncommitted edits are not included. If Crux reports `crux: skipped`, the change satisfied its triviality checks. Add `--no-llm` to see local triage without invoking Claude Code.

**4. Publish only when ready.** With an authenticated GitHub CLI (`gh auth login`) and your branch pushed:

```sh
crux run
```

This finds or can offer to create a PR, updates its title and description, and publishes or refreshes its review card. [Publishing behavior and other prerequisites →](docs/reference.md#install)

## How it works

1. **Gather evidence locally.** Git and ripgrep inspect the committed diff, related code, history, and nearby tests. A configurable gate avoids model review for trivial changes.
2. **Explain the relevant change.** Crux uses your Claude Code CLI to analyze selected code excerpts and repository context, then organizes a summary and reading priorities.
3. **Keep one place to review.** Published runs update a single GitHub PR card; optional Slack and Zenhub integrations connect it to your team's workflow.

Crux runs on your machine, **but it's not an offline AI model**. Relevant source excerpts and context are passed to Claude Code. Review labels are heuristic, and a successful Crux status does not mean tests passed or a human approved the PR. [Details on data flow and configuration →](docs/reference.md#configure)

## Commands at a glance

| Command | Use it to |
| --- | --- |
| `crux preview` | Read a review card in your terminal, without publishing |
| `crux run` | Post or update the review card on a GitHub PR |
| `crux prs` | See open PRs across configured repositories |
| `crux super new` | Review related PRs across multiple repositories together |
| `crux merge` | Review, approve as an eligible reviewer, and attempt a merge |
| `crux memory` | Manage the repository facts Crux remembers |
| `crux zenhub` | Link PRs to tickets and sync merged work |
| `crux install-hooks` | Add automatic push reviews and related hooks |

See `crux <command> --help` for options. The [reference guide](docs/reference.md) covers super-PR merge controls, Slack, Zenhub, memory, and the local action service.

## Use it your way

- **Prefer Claude Code?** [Install the plugin](docs/reference.md#claude-code-plugin) for `/crux:preview`, `/crux:run`, and automatic hooks.
- **Want reviews after pushes?** [Install Git hooks](docs/reference.md#hook-up-git-once-per-machine) and configure your repository scope.
- **Working across services?** [Super PRs](docs/reference.md#super-prs-optional) assemble related changes into one review workflow.
- **Want team updates?** [Slack](docs/reference.md#slack-notifications-optional) threads reviews; [Zenhub](docs/reference.md#zenhub-optional) links tickets to the code that ships.

**Before enabling hooks:** commit-message enrichment is on by default and may amend outgoing commits. Set `[commit] enrich = false` before installing hooks if you don't want that behavior. Automatic delayed reviews currently have a [native Windows limitation](docs/reference.md#windows); manual commands remain available. Some [cloud sessions](docs/reference.md#cloud-sessions) require manual GitHub posting.

## More depth, zero guesswork

| Guide | What's inside |
| --- | --- |
| [Full reference](docs/reference.md) | Setup, config, hooks, Claude Code/cloud, Windows, Slack, Zenhub, super PRs, memory, troubleshooting |
| [Configuration example](crux.toml.example) | Every option and its intended behavior |
| [Architecture](DESIGN.md) | Design decisions, implementation, and module contracts |
| [Launch assets](docs/launch-kit.md) | Social images, copy, and repository metadata |

## Help make Crux better

The biggest opportunity: **review cards that explain a change instead of listing it**. We also welcome integrations and improvements to setup, speed, and error messages.

Try it on a PR you know well. **What did Crux miss?** [Open an issue](https://github.com/sonomoshq/crux/issues/new) with a concrete example or contribute a focused PR. [Development and testing instructions →](docs/reference.md#developing-crux)

Crux is © 2026 Sonomos, Inc., licensed under [Apache 2.0](LICENSE). See [NOTICE](NOTICE).
