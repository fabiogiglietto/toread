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

  run     — the newest *completed* pipeline-triggered run's conclusion, plus
            any run still going far past this pipeline's normal duration.
            Catches the break that leaves both signals above green for days:
            a step that hangs until the runner's 6h ceiling kills it. The
            cron probe cannot see it (it asks when the last success was, not
            what happened since) and output age is slack by design.

They are reported separately and never combined: an AND would mean the quiet
no-op (fresh cron, stale output) never alerts at all.

Every finding is confirmed before it is posted, because the Actions API has
served this check wholly stale reads (see confirmed_age and _runs_page): a
finding must survive a delayed re-read *and* recur on the next daily run
before anyone is paged. See hold_until_confirmed.

Run by .github/workflows/pipeline-health.yml (daily). Findings do not affect
the exit code — the alert is the signal, not the job status (a red health-check
would just be one more thing to notice). The single exception is failing to
*deliver* an alert, which exits 1: that is the one state nothing else can
report, and a red job also withholds the healthcheck ping so the external
watcher notices too.

Env:
  GITHUB_TOKEN         optional, avoids unauthenticated rate limits
  SLACK_WEBHOOK_URL    preferred alert transport
  SLACK_BOT_TOKEN + SLACK_ALERT_CHANNEL   fallback transport
  MAX_AGE_OVERRIDE_DAYS  optional, force one threshold for every check
                         (set to 0 in a workflow_dispatch run to test alerting)
  HEALTH_STATE_FILE      where the previous run's findings are remembered, so
                         a finding can be required to recur before it alerts.
                         Restored/saved by the workflow's cache step; a miss
                         degrades to alerting on one run, never to silence.
  CONFIRM_DELAY_SECONDS  gap before a stale reading is re-read (default 60;
                         set to 0 in tests)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


class Finding(NamedTuple):
    """One problem worth posting.

    `key` identifies the finding across runs — same problem, same key — so
    hold_until_confirmed can tell "still broken" from "flaked once".
    """
    key: str
    text: str


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

# Unfiltered run listing, used by both the cross-check in
# last_scheduled_run_age_days and the latest-run probe. Wide enough that a
# scheduled run is somewhere in it for every target: the busiest
# (update_feed.yml, ~every 30min) still spans ~15h at this size.
_RUNS_PAGE = 30

# Longest a run may be in flight before it is called hung. Measured
# 2026-08-20 over the last 100 successful runs of each target workflow:
# medians 0.5–11min, worst-case max 27min (fg-zettelkasten update-vault.yml).
# 3h is ~7x the worst legitimate run and still inside GitHub's 6h job ceiling,
# so a hang is reported while it is hanging rather than after the runner kills
# it. Re-measure before tightening.
_HUNG_AFTER_HOURS = 3.0

# Events this pipeline actually runs on. A failed `push` run is someone
# iterating on a branch, not a broken pipeline — the latest-run probe would
# page on every WIP commit if it looked at those.
_PIPELINE_EVENTS = ("schedule", "repository_dispatch")


def _runs_page(repo: str, workflow: str) -> list[dict]:
    """Newest runs of `workflow`, unfiltered by event or conclusion."""
    runs = _get(f"https://api.github.com/repos/{repo}/actions/workflows/"
                f"{workflow}/runs?per_page={_RUNS_PAGE}")
    return runs.get("workflow_runs") or []


def _started(run: dict) -> str:
    return run.get("run_started_at") or run["created_at"]


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
    ages = [_age_days(_started(r)) for r in (runs.get("workflow_runs") or [])]

    # Cross-check against the unfiltered listing. Both false alarms this check
    # has produced came from the *filtered* query above returning a wholly
    # stale page (14.9d and 33.0d claimed, ~0d and 2.0d true) while the same
    # query from a workstation was correct. Whether the unfiltered listing is
    # served from a different index is not something we can know from outside
    # — but taking the newer of the two answers can only ever make the reading
    # fresher, never staler, so a stale filtered read stops being able to page
    # anyone on its own. The request is one the latest-run probe already makes.
    try:
        ages += [_age_days(_started(r)) for r in _runs_page(repo, workflow)
                 if r.get("event") == "schedule" and r.get("conclusion") == "success"]
    except Exception:
        pass  # corroboration only; the filtered read still stands on its own

    return min(ages) if ages else None


def latest_run_findings(repo: str, workflow: str) -> list[str]:
    """Problems visible in the newest runs that age-of-last-success cannot see.

    Two shapes, both observed on research-radio's check_papers.yml while every
    other signal stayed green:

      hung    — a run still going long past this pipeline's normal duration.
                Reported while it hangs, before the 6h ceiling kills it.
      failed  — the newest *completed* pipeline run did not succeed. `cancelled`
                is only counted when the run had already outlived
                _HUNG_AFTER_HOURS, because that is a runner kill; a shorter
                cancel is a human pressing the button or a concurrency group
                doing its job, neither of which is a fault.
    """
    runs = [r for r in _runs_page(repo, workflow)
            if r.get("event") in _PIPELINE_EVENTS]
    if not runs:
        return []
    runs.sort(key=_started, reverse=True)
    problems = []

    newest = runs[0]
    if newest.get("status") != "completed":
        hours = _age_days(_started(newest)) * 24
        if hours > _HUNG_AFTER_HOURS:
            problems.append(
                f"has been {newest.get('status')} for {hours:.1f}h "
                f"(normal runs finish well inside {_HUNG_AFTER_HOURS:g}h) — "
                f"hung, and the runner will kill it at 6h")

    done = next((r for r in runs if r.get("status") == "completed"), None)
    if done is not None:
        conclusion = done.get("conclusion")
        ran_hours = 0.0
        if done.get("updated_at"):
            ran_hours = max(0.0, (_age_days(_started(done))
                                  - _age_days(done["updated_at"])) * 24)
        killed = conclusion == "cancelled" and ran_hours > _HUNG_AFTER_HOURS
        if conclusion in ("failure", "timed_out", "startup_failure") or killed:
            problems.append(
                f"last completed run {conclusion} after {ran_hours:.1f}h "
                f"({_age_days(_started(done)):.1f} days ago)")
    return problems


def post_slack(text: str) -> None:
    """Post the alert, and raise if Slack did not accept it.

    chat.postMessage answers HTTP 200 with {"ok": false, "error": ...} for a
    channel the bot is not in — the exact state a freshly created private ops
    channel is in. Ignoring the body, as this did, means every alert can
    disappear while the job stays green and the healthcheck ping still fires:
    a dead-man's switch that is itself dead, which is worse than the noise
    this whole check is being fixed for. Raise instead, so the job goes red
    and the "Ping healthcheck" step (if: success()) stays silent — that is
    what makes the external watcher notice.
    """
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", "replace")
    # Webhooks answer "ok" as plain text; the Web API answers JSON.
    try:
        payload = json.loads(body)
    except ValueError:
        return
    if isinstance(payload, dict) and not payload.get("ok", True):
        raise RuntimeError(
            f"Slack rejected the alert: {payload.get('error')} "
            f"(is the bot in the alert channel?)")


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

    The re-read waits first. Confirming immediately, as this did originally,
    is no confirmation at all: the whole check completed in ~14 seconds, so
    both reads landed on the same lagging replica milliseconds apart and it
    alerted anyway — twice, wrongly, in its first ten days.
    """
    age = probe(*args)
    if age is not None and age <= limit:
        return age
    time.sleep(float(os.environ.get("CONFIRM_DELAY_SECONDS") or 60))
    second = probe(*args)
    if age is None or second is None:
        return second if age is None else age
    return min(age, second)


def _runs_url(repo: str, workflow: str) -> str:
    return f"https://github.com/{repo}/actions/workflows/{workflow}"


def _commits_url(repo: str, path: str | None) -> str:
    url = f"https://github.com/{repo}/commits"
    return f"{url}/HEAD/{path}" if path else url


def check(target: Target, override: float | None
          ) -> tuple[list[Finding], list[Finding]]:
    """Return (alerts, errors) for one target.

    Every line carries the link that confirms or refutes it. The old text
    asserted a cause ("cron disabled or the workflow is failing") that was
    wrong both times it fired; a reading plus the page it came from lets
    whoever reads the alert settle it in one click instead of an investigation.
    """
    alerts, errors = [], []
    runs_url = _runs_url(target.repo, target.workflow)

    cron_limit = override if override is not None else target.cron_max_days
    try:
        age = confirmed_age(last_scheduled_run_age_days, cron_limit,
                            target.repo, target.workflow)
    except Exception as exc:  # API error is itself worth reporting
        errors.append(Finding(
            f"{target.repo}|{target.workflow}|cron-error",
            f"\u2022 {target.repo} `{target.workflow}` \u2014 cron check "
            f"failed: {exc}"))
    else:
        key = f"{target.repo}|{target.workflow}|cron"
        if age is None:
            print(f"STALE  {target.repo} `{target.workflow}`: no successful "
                  f"scheduled run on record")
            alerts.append(Finding(key, (
                f"\u2022 *{target.repo}* \u2014 `{target.workflow}` has no successful "
                f"scheduled run on record (GitHub keeps 90 days) \u2014 "
                f"<{runs_url}|run list>")))
        else:
            stale = age > cron_limit
            print(f"{'STALE' if stale else '   ok':>5}  {target.repo} "
                  f"`{target.workflow}`: last scheduled success {age:.1f}d ago "
                  f"(limit {cron_limit:g}d)")
            if stale:
                alerts.append(Finding(key, (
                    f"\u2022 *{target.repo}* \u2014 `{target.workflow}` last succeeded "
                    f"on schedule {age:.1f} days ago (limit {cron_limit:g}) "
                    f"\u2014 <{runs_url}|run list>")))

    try:
        problems = latest_run_findings(target.repo, target.workflow)
    except Exception as exc:
        errors.append(Finding(
            f"{target.repo}|{target.workflow}|run-error",
            f"\u2022 {target.repo} `{target.workflow}` \u2014 latest-run check "
            f"failed: {exc}"))
    else:
        for i, problem in enumerate(problems):
            print(f"STALE  {target.repo} `{target.workflow}`: {problem}")
            alerts.append(Finding(
                f"{target.repo}|{target.workflow}|run{i}",
                f"\u2022 *{target.repo}* \u2014 `{target.workflow}` {problem} "
                f"\u2014 <{runs_url}|run list>"))
        if not problems:
            print(f"   ok  {target.repo} `{target.workflow}`: latest runs clean")

    out_limit = override if override is not None else target.output_max_days
    label = f"{target.repo}" + (f":{target.path}" if target.path else "")
    commits_url = _commits_url(target.repo, target.path)
    try:
        age = confirmed_age(last_commit_age_days, out_limit,
                            target.repo, target.path)
    except Exception as exc:
        errors.append(Finding(f"{label}|output-error",
                              f"\u2022 {label} \u2014 output check failed: {exc}"))
        return alerts, errors
    if age is None:
        errors.append(Finding(f"{label}|output-error",
                              f"\u2022 {label} \u2014 output check failed: no commits "
                              f"found"))
        return alerts, errors
    stale = age > out_limit
    print(f"{'STALE' if stale else '   ok':>5}  {label}: last commit "
          f"{age:.1f}d ago (limit {out_limit:g}d)")
    if stale:
        alerts.append(Finding(f"{label}|output", (
            f"\u2022 *{label}* \u2014 unchanged for {age:.1f} days "
            f"(limit {out_limit:g}) \u2014 the cron is running, so check "
            f"{target.input_hint} \u2014 <{commits_url}|commits>")))
    return alerts, errors


def state_path() -> Path:
    return Path(os.environ.get("HEALTH_STATE_FILE")
                or ".health-state/pipeline_health.json")


def load_previous_keys() -> set[str] | None:
    """Keys flagged by the previous run, or None when that is unknowable.

    None is the important case: a cache miss, a first run, a corrupt file.
    It must not read as "nothing was wrong yesterday", because that would
    hold every finding forever and turn the dead-man's switch off silently —
    a worse failure than the noise this is fixing. Callers alert immediately
    when they get None.
    """
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None
    keys = data.get("keys")
    return set(keys) if isinstance(keys, list) else None


def save_keys(keys: set[str]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"keys": sorted(keys)}))
    except OSError as exc:
        print(f"could not save state to {path}: {exc}")


def hold_until_confirmed(findings: list[Finding],
                         previous: set[str] | None
                         ) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (post now, hold for tomorrow).

    A finding posts once it has been seen on two consecutive daily runs. On a
    day-scale dead-man's switch that costs at most one day of notice on a real
    outage — the cron thresholds are 1–7 days — and makes a single stale read
    structurally unable to page anyone, which is what both false alarms were.

    `previous is None` means the state was unavailable; post everything rather
    than hold it, so a lost cache degrades to the old, noisier behaviour and
    never to silence.
    """
    if previous is None:
        return findings, []
    post = [f for f in findings if f.key in previous]
    held = [f for f in findings if f.key not in previous]
    return post, held


def main() -> int:
    raw = os.environ.get("MAX_AGE_OVERRIDE_DAYS")
    override = float(raw) if raw not in (None, "") else None
    findings = []
    for target in TARGETS:
        alerts, errors = check(target, override)
        findings += alerts + errors

    previous = load_previous_keys()
    if previous is None:
        print("no previous state (cache miss or first run) — "
              "posting unconfirmed findings")
    post, held = hold_until_confirmed(findings, previous)
    save_keys({f.key for f in findings})

    for finding in held:
        print(f"HELD   {finding.key} — first sighting, alerts if it recurs "
              f"tomorrow")

    # An override run is a test of the alerting path, so it must post every
    # line on the spot — holding any of them would make MAX_AGE_OVERRIDE_DAYS=0
    # exercise only whichever findings happened to be confirmed already.
    if override is not None:
        post = post + held

    if post:
        try:
            post_slack(":hourglass_flowing_sand: *Pipeline freshness check* "
                       "found problems:\n" + "\n".join(f.text for f in post))
        except Exception as exc:
            # The one failure that must turn the job red: findings exist and
            # nobody was told. Everything else here exits 0 by design.
            print(f"FAILED to post {len(post)} finding(s): {exc}")
            for finding in post:
                print(finding.text)
            return 1
    elif findings:
        print(f"{len(findings)} unconfirmed finding(s) held — nothing posted.")
    else:
        print("All pipeline outputs fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
