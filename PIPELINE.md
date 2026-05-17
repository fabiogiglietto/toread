# Research Pipeline — Orchestration

Canonical description of how four repositories fit together. This file lives in
`toread` (the pipeline root); the other three repos link here rather than
re-describing the pipeline, so there is one source of truth.

Repos:

- **toread** — Paperpile BibTeX → enriched JSON feed
- **research-radio** — feed → AI-generated podcast episodes
- **fabiogiglietto.github.io** — academic website
- **fg-zettelkasten** — Obsidian Zettelkasten vault

## Dependency DAG

```
        toread  (Paperpile → output/feed.json)
          │
          ▼
   research-radio  (feed.json → docs/episodes.json + audio Releases)
          │
          ▼
 fabiogiglietto.github.io  (feed.json + episodes → site)
          │
          ▼
   fg-zettelkasten  (feed.json + episodes.json + github.io topics → vault)
```

`toread` is the only repo with an external input (Paperpile). Each downstream
repo consumes the **published artifacts** of the repos above it — fetched live
from their `raw.githubusercontent.com` URLs / GitHub Releases, never from a
local sibling working copy. This keeps the four repos decoupled and
independently deployable.

Direct dependencies:

| Repo               | Consumes                                                        |
|--------------------|-----------------------------------------------------------------|
| toread             | Paperpile BibTeX export                                         |
| research-radio     | toread `feed.json`                                              |
| github.io          | toread `feed.json`, research-radio `episodes.json`              |
| fg-zettelkasten    | toread `feed.json`, research-radio `episodes.json`, github.io   |

Everything joins on the paper's BibTeX key (`bibtex:AuthorYear-xx`). The feed
contract is specified in `SCHEMA.md`.

## Orchestration

### Current state — independent cron

Each repo schedules its own GitHub Actions workflow on a fixed UTC time:

| Repo            | Workflow              | Schedule                          |
|-----------------|-----------------------|-----------------------------------|
| toread          | `update_feed.yml`     | every 30 min                      |
| research-radio  | `check_papers.yml`    | hourly                            |
| github.io       | `update-site.yml`     | daily 06:00                       |
| fg-zettelkasten | `update-vault.yml`    | daily 05:00, recluster Mon 04:00  |

Downstream repos run on a timer and *hope* upstream has already published.
github.io and fg-zettelkasten can read a feed that a mid-flight toread run is
about to replace. Time-based ordering is best-effort, not guaranteed.

### Target state — event-driven chain

Convert the timers into a dispatch chain that runs strictly in topological
order, only when there is genuinely new upstream data:

```
toread  ──(feed.json changed)──▶  research-radio  ──▶  github.io  ──▶  fg-zettelkasten
```

- `toread` keeps its 30-min cron (it polls Paperpile — it is the clock). When a
  run produces a **changed** `feed.json`, a final step sends a
  `repository_dispatch` event (`pipeline-tick`) to `research-radio`.
- `research-radio`, `github.io` each run on that event, then **always**
  dispatch `pipeline-tick` to the next repo — so a toread change propagates the
  whole way down even through a stage that itself committed nothing (e.g.
  research-radio is rate-limited to one episode per 24 h and often no-ops).
- `fg-zettelkasten` is the end of the chain and dispatches nothing.
- Every repo **keeps a daily fallback cron** in case a dispatch is missed.

**Setup requirement:** cross-repo `repository_dispatch` cannot use the default
`GITHUB_TOKEN`. Create one fine-grained PAT with **Actions: read & write** on
all four repos and store it as the secret `PIPELINE_DISPATCH_TOKEN` in `toread`,
`research-radio`, and `github.io` (fg-zettelkasten does not dispatch, so it does
not need the secret).

## Changing the contract

The feed (`SCHEMA.md`) and the episodes JSON are the pipeline's APIs. Additive
changes are safe; renaming/removing a field or changing the `id` format is
breaking — update the schema doc and all consumers before publishing.
