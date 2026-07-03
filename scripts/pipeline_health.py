#!/usr/bin/env python3
"""Pipeline freshness check (dead-man's switch).

The dispatch chain's worst failure mode is silent staleness: a dispatch is
dropped (continue-on-error, expired PIPELINE_DISPATCH_TOKEN), a scheduled
workflow is disabled, or a stage fails quietly — and nothing turns red.
This script asks the GitHub API when each repo's pipeline output was last
committed and alerts on Slack when anything exceeds its staleness threshold.

Run by .github/workflows/pipeline-health.yml (daily). Exit code 0 either way;
the alert is the signal, not the job status (a red health-check would just be
one more thing to notice).

Env:
  GITHUB_TOKEN         optional, avoids unauthenticated rate limits
  SLACK_WEBHOOK_URL    preferred alert transport
  SLACK_BOT_TOKEN + SLACK_ALERT_CHANNEL   fallback transport
  MAX_AGE_OVERRIDE_DAYS  optional, force one threshold for every target
                         (set to 0 in a workflow_dispatch run to test alerting)
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# (repo, path-or-None, max age in days, what staleness means)
TARGETS = [
    ("fabiogiglietto/toread", None, 3,
     "feed workflow not committing — cron disabled or failing?"),
    ("fabiogiglietto/mine-toread", None, 3,
     "team feed workflow not committing — cron disabled or failing?"),
    ("fabiogiglietto/fg-zettelkasten", None, 4,
     "vault not updating — dispatch chain or daily cron broken?"),
    ("fabiogiglietto/mine-zettelkasten", None, 4,
     "team vault not updating — pipeline-finalize dispatch or cron broken?"),
    ("fabiogiglietto/research-radio", "docs/episodes.json", 14,
     "no new podcast episodes — generation failing or feed dry?"),
    ("fabiogiglietto/fabiogiglietto.github.io", None, 3,
     "website data not updating — collect workflow broken?"),
]


def last_commit_age_days(repo: str, path: str | None) -> float:
    url = f"https://api.github.com/repos/{repo}/commits?per_page=1"
    if path:
        url += f"&path={path}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "pipeline-health",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        commits = json.load(resp)
    when = datetime.fromisoformat(
        commits[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def post_slack(text: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_ALERT_CHANNEL")
    if webhook:
        req = urllib.request.Request(
            webhook, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
    elif bot_token and channel:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {bot_token}"})
    else:
        print("No Slack credentials configured — printing alert instead:")
        print(text)
        return
    urllib.request.urlopen(req, timeout=30)


def main() -> int:
    override = os.environ.get("MAX_AGE_OVERRIDE_DAYS")
    stale, errors = [], []
    for repo, path, max_days, meaning in TARGETS:
        limit = float(override) if override not in (None, "") else max_days
        label = f"{repo}" + (f":{path}" if path else "")
        try:
            age = last_commit_age_days(repo, path)
        except Exception as exc:  # API error is itself worth reporting
            errors.append(f"• {label} — check failed: {exc}")
            continue
        status = "STALE" if age > limit else "ok"
        print(f"{status:>5}  {label}: last commit {age:.1f}d ago (limit {limit:g}d)")
        if age > limit:
            stale.append(f"• *{label}* last committed {age:.1f} days ago "
                         f"(limit {limit:g}) — {meaning}")
    if stale or errors:
        post_slack(":hourglass_flowing_sand: *Pipeline freshness check* "
                   "found problems:\n" + "\n".join(stale + errors))
    else:
        print("All pipeline outputs fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
