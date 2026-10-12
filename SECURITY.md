# Security Policy

Crux can read repository content, invoke Claude Code, install Git and Claude Code hooks, and interact with GitHub, Slack, and Zenhub using the permissions available on your machine. We take vulnerabilities affecting those trust boundaries seriously.

## Supported versions

| Version | Security fixes |
| --- | --- |
| Current `main` branch | Supported on a best-effort basis |
| Older commits and forks | Not maintained |

Crux is an early-stage open-source project. There is no guaranteed security-response or patch timeline, and no separately maintained long-term-support branch.

## Report a vulnerability privately

**Do not disclose an unpatched vulnerability in a public issue, pull request, discussion, or social post.**

1. Go to [Crux security advisories](https://github.com/sonomoshq/crux/security/advisories) and select **Report a vulnerability** if private vulnerability reporting is enabled.
2. If that option is unavailable, [open a public issue](https://github.com/sonomoshq/crux/issues/new) **requesting a private security contact method only**. Do not include vulnerability details, proof-of-concept code, sensitive logs, or secrets in that issue.

Please include, when possible:

- A short description of the vulnerability and its potential impact.
- Affected commit or version, operating system, and relevant configuration.
- Minimal steps to reproduce and a proof of concept using only test accounts or sanitized data.
- Any preconditions, required permissions, and suggested mitigation.

Never send real access tokens, passwords, private repository contents, or another party's data as part of a report.

## What is in scope

Examples of actionable security reports include:

- Exposure or mishandling of GitHub tokens, Slack bot tokens, Zenhub API keys, or other credentials.
- Unauthorized GitHub actions, PR approvals, merges, or command execution through Crux, its hooks, or its local action service.
- Cross-site request forgery or other bypasses of protections around the local `crux serve` endpoints.
- Untrusted repository, PR, or model-generated content crossing a security boundary to execute commands, exfiltrate data, or perform unauthorized actions.
- Unintended disclosure of repository content or user data beyond the integrations and permissions that were configured.

Ordinary AI review mistakes, inaccurate prioritization, formatting problems, and feature requests belong in [public issues](https://github.com/sonomoshq/crux/issues), unless they demonstrate an exploitable security impact.

## Responsible testing and disclosure

Test only repositories, accounts, and systems that you own or are explicitly authorized to assess. Avoid disruption, credential collection, accessing third-party data, or targeting Sonomos infrastructure without permission. Use the minimum proof needed to demonstrate the issue.

We will review reports as resources permit, try to coordinate a fix and disclosure with the reporter, and credit researchers where appropriate and agreed. Please give maintainers a reasonable opportunity to investigate before public disclosure. Crux does not currently offer a bug bounty or guaranteed response time.

## Guidance for users

- Review `[scope]` allowlists and the permissions of your GitHub, Slack, and Zenhub credentials. Use least privilege.
- Understand what `crux install-hooks` changes before enabling it. Its commit-enrichment feature can amend commits; disable it with `[commit] enrich = false` if unwanted.
- Crux runs locally, but AI analysis sends selected repository context through your configured Claude Code CLI. Check your organization's data-handling requirements before use.
- Do not expose or port-forward the `crux serve` listener on `127.0.0.1`, which provides locally authenticated GitHub actions.
- Keep `~/.config/crux/credentials.json` and other credentials out of source control. On Windows, use appropriate filesystem access controls; POSIX `0600` permissions do not protect files there.
- If a token is exposed, revoke or rotate it through its provider immediately. Do not rely on deleting the exposed text alone.

Thank you for helping keep Crux and its users secure.
