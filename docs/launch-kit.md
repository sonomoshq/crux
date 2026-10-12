# Crux launch kit

Crux is a tool Sonomos uses internally for development and code review. Sonomos
co-founder **Jack Rose developed the original internal version**. Lead with that
origin, then show how a review brief gives someone a clearer place to start.

This document contains proposed repository settings and publication drafts for
review. Applying the GitHub About text, topics and social preview, and publishing
the posts, are separate steps after review; merging this file does not apply them.

## Proposed GitHub settings

### About description

221 characters, within the 350-character limit:

```text
PR review briefs for GitHub: the big picture, linked code changes, and a place to start. Used at Sonomos for development and code review. Originally built internally by co-founder Jack Rose. Runs locally with Claude Code.
```

### Topics

These nine topics describe the current project:

```text
code-review pull-requests github claude-code developer-tools cli python git-hooks ai
```

They follow [GitHub's topic rules](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics):
lowercase letters, numbers and hyphens, up to 50 characters per topic and no more
than 20 topics. Apply them through the repository's About settings after review.

### Social preview

Upload [crux-social-wide.png](assets/crux-social-wide.png) through the repository's
**Settings → Social preview → Edit** after review. GitHub recommends 1280 × 640
pixels for best display and accepts PNG, JPG or GIF files under 1 MB. See
[GitHub's social preview instructions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).

## Assets and alt text

| Asset | Dimensions | Intended use |
| --- | --- | --- |
| [crux-social-wide.png](assets/crux-social-wide.png) | 1280 × 640 | GitHub social preview; LinkedIn or X image attachment |
| [crux-social-square.png](assets/crux-social-square.png) | 1080 × 1080 | Instagram feed post; other square placements |
| [crux-review-example.svg](assets/crux-review-example.svg) | 1000 × 660 | README product example; link to the [full example](examples/review-card.md) when discussing the review format |

**Wide alt text**

> Crux by Sonomos. Find the changes that matter. One clear starting point for every code review. Built at Sonomos by co-founder Jack Rose. github.com/sonomoshq/crux. A small abstract illustration shows gray code lines with a few mint highlights on a near-white background.

**Square alt text**

> Crux by Sonomos. Find the changes that matter. Built at Sonomos by co-founder Jack Rose. github.com/sonomoshq/crux. Gray code lines with three mint highlights sit below the headline on a near-white background.

**Review example alt text**

> Illustrative Crux review card about coordinating repeated review requests from the same branch. It shows the big picture, two Must read items and one Worth a skim item, with code references.

Both social graphics include Jack Rose's credit. The square omits the wide
graphic's supporting sentence. See the [asset notes](assets/README.md) for
production details and brand sources.

The review example uses hand-written annotations about Crux's own code. Keep its
illustrative label visible when sharing it, and adjust the alt text if you crop
an attachment.

## Ready-to-review post drafts

### LinkedIn

Attach the wide graphic. This version is written for the Sonomos account or a
team member sharing on its behalf.

```text
Start your next code review with a clearer picture of the change.

We use Crux inside Sonomos for development and code review. Our co-founder Jack Rose built the original internal version.

Crux produces a review brief for GitHub with the big picture, links to the code to read first, and a review card that updates in place on later runs.

It runs locally with Claude Code and is Apache-2.0 licensed.

We'd love help making the cards shorter and better at explaining why a change matters. What's the one piece of context you most often have to reconstruct during a review?

Try it: https://github.com/sonomoshq/crux#quick-start
Follow the README setup and scope configuration, then preview your committed changes with crux preview --base main. Replace main with your intended base branch.

Share a concrete example in an issue, or send a PR if you have an improvement.

#CodeReview #DeveloperTools #OpenSource
```

### Instagram

Attach the square graphic. The caption includes the repository address so it
does not depend on an existing profile link.

```text
A clearer starting point for code review.

Crux is a tool we use inside Sonomos for development and code review. Our co-founder Jack Rose built the original internal version.

It gives GitHub PRs a big-picture summary and links to the code to read first. Runs locally with Claude Code; Apache-2.0 licensed.

What context do you wish every PR included?

Try it: github.com/sonomoshq/crux
Follow Quick start in the README, including scope, and preview your committed changes. Tell us what you'd improve in the card.

#CodeReview #DeveloperTools #OpenSource
```

### X

Attach the wide graphic. **270 characters**, counting the URL as 23 characters:

```text
Crux is the PR review tool we use at Sonomos, originally built internally by our co-founder Jack Rose.

Get a review brief with links to the code to read first. Runs locally with Claude Code.

Try it: https://github.com/sonomoshq/crux#quick-start
What context do your PR reviews usually lack?
```

### Optional Show HN

Use this if a maintainer is available to discuss the implementation and respond
to feedback. Submit the repository URL and use the body as an introductory
comment.

**Title** (51 characters):

```text
Show HN: Crux, the PR review tool we use at Sonomos
```

**URL:** <https://github.com/sonomoshq/crux>

**Introductory comment:**

```text
Hi HN, we use Crux inside Sonomos for development and code review. Our co-founder Jack Rose developed the original internal version. It's Apache-2.0 licensed.

Crux is a Python CLI for GitHub PRs that uses Claude Code to produce a review brief. A card can include a big-picture summary, a change map and links to the changes to read first. Repeated runs update the same review card.

Start with Quick start in the README: install the prerequisites, configure the repository's GitHub owner under [scope], then run crux preview --base main from a feature branch with committed changes. Replace main with your intended base branch. The prerequisites include Python 3.11+, git, ripgrep and an authenticated Claude Code CLI. Publishing to GitHub also needs the authentication described in the README.

The brief is a starting point for a reviewer, and its reasoning should be checked against the code. We're especially interested in examples where the card is too long or doesn't explain the change clearly. There are also opportunities to improve setup and integrations.

What would a brief need to explain before it would be useful in your review process? Concrete examples and contributions are welcome.
```

### Optional hashtag bank

Pick two or three that fit the post: `#CodeReview`, `#DeveloperTools`,
`#OpenSource`, `#ClaudeCode`, `#GitHub`.

## First-try path to share in replies

Point people to [Quick start in the README](../README.md#quick-start) for the maintained
installation steps. The sequence is:

1. Install Crux and its prerequisites, including an authenticated Claude Code CLI.
2. Configure the target repository's GitHub owner in `[scope] owners`, using
   [crux.toml.example](../crux.toml.example) as a reference.
3. From a feature branch with committed changes in that configured repository,
   run `crux preview --base main` to inspect a brief in the terminal. Replace
   `main` with the branch you intend to merge into.
4. Share an [issue](https://github.com/sonomoshq/crux/issues) with the context the
   card missed, an unclear passage or a reproducible setup problem. A PR with an
   improvement is welcome; a star is appreciated if Crux is useful to you.

Use the README when explaining publishing or hooks so people can understand
the workflow actions before enabling them.

## Short launch checklist

1. Review the copy and final images, check the repository link, and merge the
   documentation changes.
2. Apply the approved About description, topics and social preview in GitHub.
3. Record the starting metrics, then publish the selected drafts with their
   matching images and alt text.
4. Answer questions and turn concrete feedback into issues. Review the results
   after one week and choose the next improvement.

## What to measure

| Signal | What to record | How to use it |
| --- | --- | --- |
| Useful feedback | Issues or replies with a concrete example, plus contributed PRs | Prioritize clearer cards, missing context and setup fixes. |
| First attempts | Reports that someone tried the preview, including where setup stopped | Improve the first-run documentation and error messages. |
| Repository interest | Visits and clones where GitHub traffic data is available; changes in stars | Track interest alongside actual trials and contributions. |
| Review needs | Recurring answers to the context question in the posts | Learn which explanations reviewers want most. |

Keep the publication dates and starting counts alongside the follow-up counts.
Treat feedback and usable contributions as the main result; stars are a
secondary signal.
