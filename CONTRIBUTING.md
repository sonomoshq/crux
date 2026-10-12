# Contributing to Crux

Thanks for helping improve Crux. We use it at [Sonomos](https://sonomos.ai) for development and code review and welcome focused fixes, clearer reviews, documentation improvements, and practical integrations.

**New here?** Try [the quick start](README.md#quick-start), then read [a real Crux review](docs/examples/real-pr-review.md) to see the kind of output users experience.

## Before opening an issue or PR

- Check [existing issues](https://github.com/sonomoshq/crux/issues) and [pull requests](https://github.com/sonomoshq/crux/pulls) for related work.
- For a reproducible failure, use the [bug report template](https://github.com/sonomoshq/crux/issues/new/choose). Include the expected and actual behavior, your environment, and *sanitized* logs.
- For a new capability, explain the developer workflow it would improve. A small, focused issue is easier to evaluate than a broad feature wish list.
- **Security vulnerabilities belong in [SECURITY.md](SECURITY.md)**, not public bug reports. Do not publish an exploit, credentials, or private code in an issue.

For larger integrations or behavior changes, open an issue before investing in a substantial implementation so maintainers can discuss the approach. Small fixes and documentation changes can go straight to a PR.

## Development setup

Crux requires **Python 3.11+**. You will also need Git and ripgrep (`rg`); integration features may require the authenticated GitHub (`gh`) and Claude Code (`claude`) CLIs.

From your fork or working clone:

```sh
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
python -m pip install --upgrade 'setuptools>=77' pytest
python -m pip install -e .
python -m pytest
python scripts/check_copyright.py
```

On Windows, activate the virtual environment with `.venv\Scripts\Activate.ps1` in PowerShell, then run the `python -m pip` and test commands. The [reference guide](docs/reference.md#windows) describes the existing limitation affecting *automatic delayed reviews* on native Windows.

For faster iteration, run a focused module, for example:

```sh
python -m pytest tests/test_render.py
```

**Do not run `crux install-hooks` just to execute tests.** Hook installation can change Git and Claude Code settings and enable commit-message enrichment. Keep development configuration and test credentials separate from production ones.

## What to change

| Area | Start here |
| --- | --- |
| Review content or prioritization | `crux/analyze.py`, `crux/render.py`, `crux/prompts/` |
| CLI, GitHub posting, and hooks | `crux/cli.py`, `crux/post.py`, `crux/gitio.py` |
| Multi-repository reviews | `crux/superpr.py`, `crux/superanalyze.py`, `crux/supermerge.py` |
| Integrations | `crux/slack.py`, `crux/zenhub.py`, `crux/serve.py` |
| Documentation and onboarding | `README.md`, `docs/reference.md`, `docs/examples/` |

For architecture details, see [DESIGN.md](DESIGN.md). If you change a configuration option, also update [crux.toml.example](crux.toml.example). If you touch the illustrative README image or buttons, regenerate them with:

```sh
python scripts/render_readme_example.py
```

## Pull requests

1. Fork or branch from current `main`. Keep your PR focused on one problem.
2. Add or update tests when behavior changes. Explain meaningful edge cases and any tests you could not run.
3. Update the relevant README/reference documentation when users will notice a change.
4. Run `python -m pytest` and `python scripts/check_copyright.py` where your environment allows.
5. Open a PR using the [pull request template](.github/PULL_REQUEST_TEMPLATE.md). Explain **why** the change matters, not only which files changed, and link related issues.

If your change touches credentials, GitHub merge/approval actions, local HTTP endpoints, model-input trust boundaries, or installed hooks, call out its security implications explicitly.

## Helpful feedback

Our biggest need is review cards that explain **what changed and why it matters**, rather than a long inventory of lines. A small before-and-after example from a repository you can share is especially valuable. Please scrub organization secrets, private source, tokens, and personal data before posting logs or examples.

Be constructive and respectful of contributors. Maintainers may ask to narrow a PR, revise the approach, or add regression tests. All contributions are reviewed under the project's [Apache 2.0 license](LICENSE).
