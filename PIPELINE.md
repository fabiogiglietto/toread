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
