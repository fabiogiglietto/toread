# Research Pipeline — Orchestration

Canonical description of how the pipeline repositories fit together. This file
lives in `toread` (the pipeline root); the other repos link here rather than
re-describing the pipeline, so there is one source of truth.

There are **two chains**: the personal fg chain (four repos) and the parallel
team MINE chain (two repos, forks of the fg ones — see
[The MINE team chain](#the-mine-team-chain)).

fg chain repos:

- **toread** — Paperpile BibTeX + Slack `#zettelkasten` suggestions → enriched JSON feed
- **research-radio** — feed → AI-generated podcast episodes
- **fabiogiglietto.github.io** — academic website
- **fg-zettelkasten** — Obsidian Zettelkasten vault

MINE chain repos:

- **mine-toread** — fork of `toread` for the MINE team Slack workspace
- **mine-zettelkasten** — fork of `fg-zettelkasten`, the team vault

## Dependency DAG

The four repos form a chain, but **fg-zettelkasten runs twice** — because of a
genuine cycle. research-radio scaffolds its podcast script from a
fg-zettelkasten *summary*; a fg-zettelkasten *note* (and the Slack digest)
links the research-radio *podcast*. So the summary must be produced before the
podcast, and the note/Slack after it. fg-zettelkasten's work is split into a
`summarize` stage (early) and an `update` stage (late):

```
toread                       Paperpile + Slack #zettelkasten -> output/feed.json
  |  new / edited papers
  v
fg-zettelkasten : summarize   feed -> data/summaries/<key>.json     (stage 1)
  |
  v
research-radio                summary scaffold + PDF -> podcast, docs/episodes.json
  |
  v
fabiogiglietto.github.io      feed + episodes -> website
  |
  v
fg-zettelkasten : update      summaries + episodes + topics -> vault notes + Slack
```

Each stage consumes the **published artifacts** of the stages above it —
fetched live from GitHub (raw URLs, the Contents API, or Releases), never from
a local sibling working copy. This keeps the repos decoupled and independently
deployable.

| Stage                      | Consumes                                                        | Produces                              |
|----------------------------|-----------------------------------------------------------------|---------------------------------------|
| toread                     | Paperpile BibTeX export + Slack `#zettelkasten` suggestions     | `output/feed.json`                    |
| fg-zettelkasten : summarize| feed, Paperpile Drive PDFs                                      | `data/summaries/<key>.json`           |
| research-radio             | feed, fg-zettelkasten summaries, Drive PDFs                     | `docs/episodes.json` + audio Releases |
| github.io                  | feed, research-radio episodes                                   | the website                           |
| fg-zettelkasten : update   | feed, summaries, research-radio episodes, github.io topics      | `vault/` notes + Slack digest         |

Everything joins on the paper's BibTeX key (`bibtex:AuthorYear-xx`). The feed
contract is specified in `SCHEMA.md`.

## Orchestration — event-driven chain

`toread` polls Paperpile every 30 min (it is the clock). It also polls the
`#toread` Slack channel for messages tagged `#zettelkasten`; see
`src/slack_ingest.py`. When a run detects either a change in the **Paperpile
library** (new or edited papers, via the `bib-check` step) **or** new entries
in `data/slack_inbox.bib` (via the `slack-ingest` step), it fires a
`repository_dispatch` event down the chain. Cache-only metadata refreshes
such as citation-count updates do **not** cascade. Each stage
runs on its event and dispatches the next, so the pipeline runs in strict
topological order and only when there is genuinely new input.

| Hop                              | Event type           |
|----------------------------------|----------------------|
| toread → fg-zettelkasten         | `pipeline-summarize` |
| fg-zettelkasten → research-radio | `pipeline-tick`      |
| research-radio → github.io       | `pipeline-tick`      |
| github.io → fg-zettelkasten      | `pipeline-finalize`  |

`fg-zettelkasten`'s `update-vault.yml` listens for both events and branches on
`github.event.action`:

- `pipeline-summarize` → run `summarize`, commit `data/summaries/`, then
  dispatch `pipeline-tick` to research-radio.
- `pipeline-finalize` → run `update` (themes, notes with the real podcast link,
  Slack digest). End of chain — dispatches nothing.

Every repo keeps a **daily fallback cron** in case a dispatch is missed. The
fg-zettelkasten `update` cron is self-sufficient: run on its own it summarizes
any paper the `summarize` stage did not reach, so a dropped event self-heals.

**Setup — `PIPELINE_DISPATCH_TOKEN`:** cross-repo `repository_dispatch` cannot
use the default `GITHUB_TOKEN`. One fine-grained PAT, stored as the secret
`PIPELINE_DISPATCH_TOKEN`:

- **Repository access:** all four repos.
- **Permission:** `Contents` → **Read and write** (`POST /repos/.../dispatches`
  requires it; `Metadata: read` is auto-added).
- **Store the secret in** `toread`, `fg-zettelkasten`, `research-radio`, and
  `github.io` — every repo that dispatches. (fg-zettelkasten dispatches
  research-radio on the summarize leg, so it needs the secret too.)

## Automatic failure triage

Every producing workflow ends with an `if: failure()` step that posts a
`:rotating_light:` alert into the ops channel named by the `OPS_SLACK_CHANNEL`
repo variable (`#pipeline-ops`). `.github/workflows/ops-autofix.yml` closes the
loop on that alert.

It is triggered by `workflow_run: completed` on this repo's producing workflow —
the same event that posts the alert, so it starts within seconds of it — and
runs Claude Code (`anthropics/claude-code-action`) against the failed run. The
agent reads the failing step's log, classifies the failure, and takes one of two
paths:

- **code defect** → a branch `ops/autofix-<run-id>`, the smallest fix that
  addresses the root cause, the test suite run as evidence, and a PR labelled
  `ops-autofix` whose body carries the diagnosis. Slack gets *"merge it into
  `main` and delete the branch"*.
- **anything else** — transient, credential, or an upstream contract change →
  no PR. Slack gets the diagnosis and the exact action required of a human.

An escalation is a successful outcome. The agent is told explicitly that a
guessed patch is worse than a clear "here is what broke and what you must do",
because a patch that silences a symptom looks like a fix.

**Auth is workload identity federation, not an API key** — the same
`ANTHROPIC_FEDERATION_RULE_ID` / `_ORGANIZATION_ID` / `_SERVICE_ACCOUNT_ID` /
`_WORKSPACE_ID` repo variables the pipeline itself uses, with `id-token: write`
on the job. Do not add `anthropic_api_key`: it outranks federation in the
credential chain and would silently shadow it.

### What it will not touch

`data/`, `output/`, `vault/`, `cache/`, `quartz/public/` — all generated. A
"fix" there is erased by the next producing run and hides the real bug in the
meantime. It never pushes to `main`, and it cannot read or set a secret.

On `mine-toread` and `mine-zettelkasten` the agent additionally refuses to
patch `src/` or `scripts/` in place, and escalates naming the upstream repo —
these are config-diff forks, and a local code fix there would fork the
codebases for real. See [The MINE team chain](#the-mine-team-chain).

### Guards

The pipeline's alerting is deliberately quiet — one silent Pages-deploy retry
before alerting, `hold_until_confirmed` in `scripts/pipeline_health.py` — and
auto-repair must not undo that by opening a PR for every flake. A preflight
step exits before spending a token when:

| Guard | Why |
|---|---|
| head branch starts with `ops/autofix-` | loop break: a test failure on an autofix PR must not trigger another autofix |
| `run_attempt` > 1 | a re-run that fails again is a human's re-run; they are already watching |
| a later run of the same workflow succeeded | the failure was transient and has self-healed |
| an open `ops-autofix` PR carries the same signature | one PR per failure, however often it recurs |

The signature is `sha1(workflow name + failing step name)`, truncated — two runs
that die in the same step of the same workflow are the same bug. The agent
writes it into the PR body as `<!-- autofix-signature: … -->`, which is what the
fourth guard greps for. `workflow_dispatch` with `skip_guards: true` replays a
past run by id regardless, which is how the workflow is rehearsed.

The Slack report step is `if: always()`: if the triage run itself dies — a
crash, `--max-turns`, the job timeout — that is reported too. Silence is never
the outcome, because silence is indistinguishable from success.

### Deliberate gap

The `:hourglass_flowing_sand:` *Pipeline freshness check* digest from
`pipeline-health.yml` is **not** covered. That workflow succeeds while reporting
its findings, so a `workflow_run: failure` trigger never sees it, and its
findings are cross-repo infra — a disabled cron, a dropped dispatch, a hung run
— rather than a defect in the repo that would fix them. Those still need a human.

### Setup per repo

1. The four `ANTHROPIC_*` repo variables (Console → Workload identity).
2. Settings → Actions → General → **Allow GitHub Actions to create and approve
   pull requests**, or `gh pr create` 403s under `GITHUB_TOKEN`.
3. The `ops-autofix` label, which the fourth guard queries.

Note that a PR opened with `GITHUB_TOKEN` does not itself trigger `tests.yml` —
GitHub suppresses workflow-triggering-workflow. That is why the agent runs the
suite inside its own job and pastes the output into the PR body: evidence, not a
green check. Merging to `main` runs the real thing.

## The MINE team chain

A second, shorter chain serves the MINE research team. `mine-toread` and
`mine-zettelkasten` are forks of `toread` and `fg-zettelkasten`, re-pointed at
the team's Slack workspace and feed:

```
mine-toread                  Paperpile + team Slack #zettelkasten -> output/feed.json
  |  pipeline-finalize
  v
mine-zettelkasten            feed -> team vault notes + Slack digest + Quartz site
```

Differences from the fg chain, all deliberate:

- **No podcast / website legs.** The team kasten has no research-radio stage,
  so mine-toread dispatches `pipeline-finalize` **directly** to
  mine-zettelkasten (there is no separate `summarize` stage; the vault run
  summarizes what it needs).
- **The dispatch step uses `continue-on-error: true`.** A missing or expired
  `PIPELINE_DISPATCH_TOKEN` must not fail the feed run; mine-zettelkasten's
  daily cron picks up anything a dropped event missed.
- **Team attribution.** The feed's `_slack_suggestion` object carries
  `submitted_by` / `submitted_by_id`, and team-submitted papers render a
  "suggested by" line in the vault (`kind: team`).
- **Drive auth.** mine-toread uploads suggestion PDFs with **OAuth user
  credentials** (`GOOGLE_OAUTH_*`) into a My-Drive inbox folder, instead of the
  fg chain's service account; mine-zettelkasten reads that folder via
  `SLACK_INBOX_DRIVE_FOLDER_ID`.
- Both mine repos still read `research-radio` episodes and
  `own-publications.json` where relevant, since the team corpus was seeded from
  the fg one.

The forks are maintained as **config-diff forks**: code changes land in
`toread` / `fg-zettelkasten` (behind config flags defaulting to fg behavior)
and flow downstream via `git merge upstream/main`; the mine repos should only
permanently differ in `config.yml` values, repo Actions variables/secrets, doc
stubs, and generated content (`data/`, `output/`, `vault/`). Do not land
feature code directly in a mine repo.

## Changing the contract

The pipeline's APIs are the published artifacts:

- **`output/feed.json`** — JSON Feed + `_academic` extensions; see `SCHEMA.md`.
- **`data/summaries/<key>.json`** — fg-zettelkasten structured summaries;
  research-radio reads the fields `key_claims`, `contributions`, `methods`,
  `findings`, `framing` as a script scaffold.
- **`docs/episodes.json`** — research-radio episode metadata + audio URLs.

Additive changes are safe; renaming/removing a field or changing the `id`
format is breaking — update the relevant doc and all consumers before
publishing.
