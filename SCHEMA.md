# Feed Contract — `output/feed.json`

> **Normative contract:** [`schema/feed.schema.json`](schema/feed.schema.json)
> (JSON Schema 2020-12). This document is the human commentary; where the two
> disagree, the schema wins. CI validates every regenerated feed against it,
> and consumers should validate on ingest. Note two places where practice
> diverges from the tables below: `date_published` and `content_text` can be
> `null` for unresolved/unenriched items — consumers must handle that.

This file is the **published contract** between ToRead and its downstream
consumers. ToRead is the producer; `research-radio`, `fabiogiglietto.github.io`,
and `fg-zettelkasten` are consumers. See `PIPELINE.md` for the full DAG.

**Canonical URL** (what consumers fetch — never a local working copy):

```
https://raw.githubusercontent.com/fabiogiglietto/toread/main/output/feed.json
```

If you change anything in this document, treat it as a breaking change and
check all three consumers before publishing.

## Format

[JSON Feed 1.1](https://www.jsonfeed.org/version/1.1/) with a custom `_academic`
object and `_`-prefixed fields per item. Standard JSON Feed readers ignore the
extensions; consumers in this pipeline rely on them.

## The join key

Every item `id` is `bibtex:<BibTeXKey>` (e.g. `bibtex:Boyd2026-pm`). The BibTeX
key is the **stable identity of a paper across all four repos** — consumers join
on it. It is assigned by Paperpile and must never be rewritten by ToRead.

## Top-level object

| Field           | Type     | Notes                                  |
|-----------------|----------|----------------------------------------|
| `version`       | string   | `https://jsonfeed.org/version/1.1`     |
| `title`         | string   | Feed title                             |
| `description`   | string   | Feed description                       |
| `home_page_url` | string   | Feed home page                         |
| `feed_url`      | string   | Should equal the canonical URL above   |
| `language`      | string   | e.g. `en-us`                           |
| `authors`       | array    | `[{name, url}]`                        |
| `items`         | array    | Paper items, see below                 |

## Item object

Standard JSON Feed fields:

| Field            | Type   | Req. | Notes                                          |
|------------------|--------|------|------------------------------------------------|
| `id`             | string | yes  | `bibtex:<key>` — the join key                  |
| `title`          | string | yes  | Paper title                                    |
| `content_text`   | string | yes  | Abstract, plain text (may be truncated)        |
| `content_html`   | string | yes  | Abstract, HTML (escaped, XSS-safe)             |
| `date_published` | string | yes  | RFC 3339 / ISO 8601 UTC                        |
| `url`            | string | yes  | Primary link (usually DOI resolver)            |
| `external_url`   | string | yes  | Canonical external link                        |
| `authors`        | array  | yes  | `[{name}]`                                     |
| `tags`           | array  | yes  | Strings — item type, venue, etc.               |

ToRead extensions (`_`-prefixed — do not assume a generic reader keeps these):

| Field              | Type    | Notes                                              |
|--------------------|---------|----------------------------------------------------|
| `_discovery_date`  | string  | When ToRead first saw this paper (RFC 3339 UTC)    |
| `_date_estimated`  | boolean | `true` if `date_published` was inferred            |
| `_academic`        | object  | See below                                          |
| `_slack_suggestion`| object  | Present iff this paper entered via a Slack `#zettelkasten` suggestion; see below |

### `_academic` object

All keys are **optional** — presence depends on which API resolved the paper.
Consumers must tolerate any subset.

| Field              | Type    | Notes                                          |
|--------------------|---------|------------------------------------------------|
| `doi`              | string  | Digital Object Identifier                      |
| `citation_count`   | integer | Citation count at enrichment time              |
| `reference_count`  | integer | Number of references                           |
| `type`             | string  | e.g. `article`                                 |
| `publisher`        | string  | Publisher name                                 |
| `volume`           | string  | Journal volume                                 |
| `pages`            | string  | Page range                                     |
| `subjects`         | array   | Subject / field-of-study strings               |
| `open_access`      | boolean | Open-access status                             |
| `metadata_source`  | string  | API that resolved it: `crossref`, `openalex`, … |
| `confidence_score` | number  | 0–1, match confidence                          |
| `quality_score`    | number  | 0–100, metadata completeness                   |
| `quality_issues`   | array   | Strings describing metadata gaps               |

### `_slack_suggestion` object

Emitted only for items whose BibTeX source is the Slack inbox
(`data/slack_inbox.bib`, populated by `src/slack_ingest.py`). By default the
object omits any suggester identity — that stays in `data/slack_state.json`
and never reaches the published feed. The team (MINE) chain opts in to
attribution via the `SLACK_ATTRIBUTE_SUGGESTERS` repo variable, which adds
the two `submitted_by*` fields below.

| Field        | Type   | Notes                                                  |
|--------------|--------|--------------------------------------------------------|
| `channel_id` | string | Slack channel ID (typically `#zettelkasten` / `#toread`) |
| `ts`         | string | Slack message timestamp (`seconds.microseconds`)        |
| `permalink`  | string | Slack message permalink (may be absent on dry-runs)     |
| `pdf_source` | string | `slack_attachment` \| `arxiv` \| `unpaywall` \| `slack_attachment_followup` |
| `submitted_by` | string | Suggester display name — **team chain only** (attribution flag on) |
| `submitted_by_id` | string | Opaque Slack user-id for @-mentions — **team chain only** |

Consumers can use this to render a "Suggested via Slack" badge or to link
back to the original message.

## Consumer expectations

A consumer parsing this feed should:

1. Iterate `items`; derive the paper key as `id.removeprefix("bibtex:")`.
2. Treat every field except `id`, `title`, and `date_published` as
   possibly-missing and code defensively.
3. Never write back to this feed — it is read-only and owned by ToRead.

## Compatibility policy

- **Additive changes** (new optional `_academic` key) — safe, ship freely.
- **Renaming or removing** any field above, or changing the `id` format —
  breaking. Update this file, bump consumers, then publish.
