#!/usr/bin/env python3
"""Pipeline freshness check (dead-man's switch).

The dispatch chain's worst failure mode is silent staleness: a dispatch is
dropped (continue-on-error, expired PIPELINE_DISPATCH_TOKEN), a scheduled
workflow is disabled, or a stage fails quietly — and nothing turns red.

Two independent signals, because no single one covers all three failure modes:

  cron    — age of the last *successful scheduled* run of the producing
            workflow. Catches a disabled or failing cron within a day. Cannot
            see a stage that runs and quietly no-ops.
  output  — age of the last commit that touched the workflow's output path.
            Catches the quiet no-op (expired Paperpile URL, dead upstream
            feed), which leaves the cron green. Deliberately slack: these
            pipelines commit only when there is new input, so a few quiet days
            is the normal duty cycle, not a fault. Thresholds sit above the
            widest gap observed over the preceding months — tightening them
            just recreates the false alarm they replaced.

They are reported separately and never combined: an AND would mean the quiet
no-op (fresh cron, stale output) never alerts at all.

Run by .github/workflows/pipeline-health.yml (daily). Exit code 0 either way;
the alert is the signal, not the job status (a red health-check would just be
one more thing to notice).

Env:
  GITHUB_TOKEN         optional, avoids unauthenticated rate limits
  SLACK_WEBHOOK_URL    preferred alert transport
  SLACK_BOT_TOKEN + SLACK_ALERT_CHANNEL   fallback transport
  MAX_AGE_OVERRIDE_DAYS  optional, force one threshold for every check
                         (set to 0 in a workflow_dispatch run to test alerting)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import NamedTuple


class Target(NamedTuple):
    repo: str
    workflow: str      # producing workflow file; its cron is the liveness probe
    cron_max_days: float
    path: str | None   # output path the workflow commits to
    output_max_days: float
    input_hint: str    # what to look at when the cron is green but output is stale


# cron_max_days is calibrated per workflow against the widest gap actually
# observed between successful scheduled runs — GitHub drops scheduled ticks
# under load, so a daily cron routinely skips a day or two and "one missed
# run" of margin is not margin at all. Measured 2026-08-10 (comments give the
# observed max gap and the window it was measured over); re-measure before
# tightening any of these.
TARGETS = [
    Target("fabiogiglietto/toread", "update_feed.yml", 1,          # 0.4d / 19d
           "output/feed.json", 10,
           "Paperpile export URL or the Slack ingest"),
    Target("fabiogiglietto/mine-toread", "update_feed.yml", 1,     # 0.4d / 23d
           "output/feed.json", 10,
           "Paperpile export URL or the Slack ingest"),
    Target("fabiogiglietto/fg-zettelkasten", "update-vault.yml", 4,  # 3.0d / 83d
           "vault", 10,
           "the toread feed (no new papers are reaching the vault)"),
    Target("fabiogiglietto/mine-zettelkasten", "update-vault.yml", 4,  # 2.0d / 40d
           "vault", 10,
           "the mine-toread feed (no new papers are reaching the vault)"),
    Target("fabiogiglietto/research-radio", "check_papers.yml", 7,  # 5.0d / 85d
           "docs/episodes.json", 14,
           "the vault summaries the podcast is built from"),
    Target("fabiogiglietto/fabiogiglietto.github.io", "update-site.yml", 3,  # 2.1d / 99d
           None, 5,
           "the collect step's upstream sources"),
]


def _get(url: str):
    """GET as JSON, authenticated when a token is available.

    GITHUB_TOKEN is scoped to the repo running this check, but every target
    is public and five of the six are *other* repos — the Actions API in
    particular can reject a foreign token. Retry unauthenticated rather than
    turn that into a daily "check failed" alert; the token is only ever an
    optimisation here (rate limits), never an access requirement.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pipeline-health",
    }
    token = os.environ.get("GITHUB_TOKEN")
    for auth in ([token] if token else []) + [None]:
        req = urllib.request.Request(url, headers=dict(headers))
        if auth:
            req.add_header("Authorization", f"Bearer {auth}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if auth is not None and exc.code in (401, 403, 404):
                continue  # foreign-token rejection — fall back to public read
            raise


def _age_days(iso: str) -> float:
    when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


# Never ask GitHub for per_page=1 and trust position 0. Ask for a page and take
# the newest: robust to a short page and to any ordering surprise, at the cost
# of the same single request.
_PAGE = 10

# A page is not enough on its own, though: a lagging replica returns a wholly
# stale page, which no page size can rescue. See confirmed_age() below.


def last_commit_age_days(repo: str, path: str | None) -> float | None:
    """Age of the newest commit touching `path` (whole repo when None)."""
    url = f"https://api.github.com/repos/{repo}/commits?per_page={_PAGE}"
    if path:
        url += f"&path={path}"
    commits = _get(url)
    if not commits:
        return None
    return min(_age_days(c["commit"]["committer"]["date"]) for c in commits)


def last_scheduled_run_age_days(repo: str, workflow: str) -> float | None:
    """Age of the newest *successful scheduled* run of `workflow`.

    Scheduled only: a workflow that is also dispatch-triggered would otherwise
    look alive on dispatches alone while its cron sat disabled — exactly the
    failure this check exists to catch. None when GitHub has no such run (a
    disabled cron eventually ages out of the 90-day run retention window).
    """
    runs = _get(f"https://api.github.com/repos/{repo}/actions/workflows/"
                f"{workflow}/runs?event=schedule&status=success"
                f"&per_page={_PAGE}")
    workflow_runs = runs.get("workflow_runs") or []
    if not workflow_runs:
        return None
    return min(_age_days(r.get("run_started_at") or r["created_at"])
               for r in workflow_runs)


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


def confirmed_age(probe, limit: float, *args) -> float | None:
    """Read once; if that reads as stale, read again and keep the newer answer.

    GitHub has served this check a stale read for the *first* Actions-API
    request of a job: `update_feed.yml` came back with a newest scheduled run
    5 days old, from an index whose total_count was 3k short of the truth,
    while the same query moments later — and every query from a workstation —
    returned a run minutes old. Observed twice in CI, always on the first
    request, never on a later one.

    A stale read can only ever make something look *older*, so confirming
    before alerting is enough: no extra request on the healthy path, one on
    the path that was about to page someone. Which is the whole point — an
    alert nobody can trust is the failure this check is being fixed for.
    """
    age = probe(*args)
    if age is not None and age <= limit:
        return age
    second = probe(*args)
    if age is None or second is None:
        return second if age is None else age
    return min(age, second)


def check(target: Target, override: float | None) -> tuple[list[str], list[str]]:
    """Return (alerts, errors) for one target."""
    alerts, errors = [], []

    cron_limit = override if override is not None else target.cron_max_days
    try:
        age = confirmed_age(last_scheduled_run_age_days, cron_limit,
                            target.repo, target.workflow)
    except Exception as exc:  # API error is itself worth reporting
        errors.append(f"• {target.repo} `{target.workflow}` — cron check "
                      f"failed: {exc}")
    else:
        if age is None:
            print(f"STALE  {target.repo} `{target.workflow}`: no successful "
                  f"scheduled run on record")
            alerts.append(f"• *{target.repo}* — `{target.workflow}` has no "
                          f"successful scheduled run on record — cron disabled?")
        else:
            stale = age > cron_limit
            print(f"{'STALE' if stale else '   ok':>5}  {target.repo} "
                  f"`{target.workflow}`: last scheduled success {age:.1f}d ago "
                  f"(limit {cron_limit:g}d)")
            if stale:
                alerts.append(
                    f"• *{target.repo}* — `{target.workflow}` last succeeded on "
                    f"schedule {age:.1f} days ago (limit {cron_limit:g}) — cron "
                    f"disabled or the workflow is failing")

    out_limit = override if override is not None else target.output_max_days
    label = f"{target.repo}" + (f":{target.path}" if target.path else "")
    try:
        age = confirmed_age(last_commit_age_days, out_limit,
                            target.repo, target.path)
    except Exception as exc:
        errors.append(f"• {label} — output check failed: {exc}")
        return alerts, errors
    if age is None:
        errors.append(f"• {label} — output check failed: no commits found")
        return alerts, errors
    stale = age > out_limit
    print(f"{'STALE' if stale else '   ok':>5}  {label}: last commit "
          f"{age:.1f}d ago (limit {out_limit:g}d)")
    if stale:
        alerts.append(f"• *{label}* — unchanged for {age:.1f} days "
                      f"(limit {out_limit:g}) — the cron is running, so check "
                      f"{target.input_hint}")
    return alerts, errors


def main() -> int:
    raw = os.environ.get("MAX_AGE_OVERRIDE_DAYS")
    override = float(raw) if raw not in (None, "") else None
    alerts, errors = [], []
    for target in TARGETS:
        a, e = check(target, override)
        alerts += a
        errors += e
    if alerts or errors:
        post_slack(":hourglass_flowing_sand: *Pipeline freshness check* "
                   "found problems:\n" + "\n".join(alerts + errors))
    else:
        print("All pipeline outputs fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
