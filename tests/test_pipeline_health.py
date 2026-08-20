"""Tests for the pipeline freshness check.

The check's whole job is telling three failure modes apart, so the tests are
about which alert fires — a quiet no-op (green cron, stale output) must still
alert, and a healthy-but-idle pipeline must not.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pipeline_health.py"
_spec = importlib.util.spec_from_file_location("pipeline_health", SCRIPT)
pipeline_health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipeline_health)

Target = pipeline_health.Target

# Captured before quiet_run_probe (autouse) stubs it out, so the probe's own
# tests exercise the real thing.
_REAL_LATEST_RUN_FINDINGS = pipeline_health.latest_run_findings

TARGET = Target("owner/repo", "update_feed.yml", 1, "output/feed.json", 10,
                "the Paperpile export URL")


@pytest.fixture(autouse=True)
def no_confirm_delay(monkeypatch):
    """confirmed_age sleeps before re-reading; tests must not."""
    monkeypatch.setenv("CONFIRM_DELAY_SECONDS", "0")


@pytest.fixture(autouse=True)
def quiet_run_probe(monkeypatch):
    """Default the latest-run probe to clean, so age tests stay about ages."""
    monkeypatch.setattr(pipeline_health, "latest_run_findings",
                        lambda repo, workflow: [])


@pytest.fixture
def fake_ages(monkeypatch):
    """Pin both age probes; returns a setter taking (cron_age, output_age)."""

    def _set(cron_age, output_age):
        monkeypatch.setattr(pipeline_health, "last_scheduled_run_age_days",
                            lambda repo, workflow: cron_age)
        monkeypatch.setattr(pipeline_health, "last_commit_age_days",
                            lambda repo, path: output_age)

    return _set


class TestCheck:
    """Tests for check()."""

    def test_idle_but_healthy_does_not_alert(self, fake_ages):
        """Cron green and output quiet for days is the normal duty cycle."""
        fake_ages(0.2, 2.9)
        alerts, errors = pipeline_health.check(TARGET, None)
        assert alerts == []
        assert errors == []

    def test_dead_cron_alerts(self, fake_ages):
        """A cron that stopped succeeding is the dead-man's switch firing."""
        fake_ages(4.0, 0.1)
        alerts, errors = pipeline_health.check(TARGET, None)
        assert len(alerts) == 1
        assert "last succeeded on schedule 4.0 days ago" in alerts[0].text
        assert errors == []

    def test_quiet_no_op_alerts_despite_green_cron(self, fake_ages):
        """The mode an AND of the two signals would silence entirely."""
        fake_ages(0.1, 30.0)
        alerts, errors = pipeline_health.check(TARGET, None)
        assert len(alerts) == 1
        assert "the cron is running" in alerts[0].text
        assert TARGET.input_hint in alerts[0].text

    def test_both_stale_reports_both(self, fake_ages):
        fake_ages(9.0, 30.0)
        alerts, errors = pipeline_health.check(TARGET, None)
        assert len(alerts) == 2

    def test_no_scheduled_run_on_record_alerts(self, fake_ages):
        """A long-disabled cron ages out of GitHub's run retention window."""
        fake_ages(None, 0.1)
        alerts, errors = pipeline_health.check(TARGET, None)
        assert len(alerts) == 1
        assert "no successful scheduled run on record" in alerts[0].text

    def test_override_applies_to_both_thresholds(self, fake_ages):
        """MAX_AGE_OVERRIDE_DAYS=0 must exercise every alert path."""
        fake_ages(0.1, 0.1)
        alerts, _ = pipeline_health.check(TARGET, 0)
        assert len(alerts) == 2

    def test_cron_api_error_is_reported_but_output_still_checked(
            self, monkeypatch):
        """One failing probe must not swallow the other's verdict."""

        def boom(repo, workflow):
            raise RuntimeError("403 rate limited")

        monkeypatch.setattr(pipeline_health, "last_scheduled_run_age_days", boom)
        monkeypatch.setattr(pipeline_health, "last_commit_age_days",
                            lambda repo, path: 30.0)
        alerts, errors = pipeline_health.check(TARGET, None)
        assert len(errors) == 1
        assert "403 rate limited" in errors[0].text
        assert len(alerts) == 1

    def test_missing_commits_is_an_error_not_an_alert(self, fake_ages):
        fake_ages(0.1, None)
        alerts, errors = pipeline_health.check(TARGET, None)
        assert alerts == []
        assert len(errors) == 1


class TestGet:
    """GITHUB_TOKEN is repo-scoped; five of the six targets are other repos."""

    def test_falls_back_to_unauthenticated_on_foreign_token_rejection(
            self, monkeypatch):
        """A 403 from a foreign repo must not become a daily 'check failed'."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        state = {"n": 0}

        def fake_urlopen(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                assert req.get_header("Authorization") == "Bearer tok"
                raise pipeline_health.urllib.error.HTTPError(
                    "u", 403, "Forbidden", {}, None)
            assert req.get_header("Authorization") is None

            class _Resp:
                def read(self):
                    return b'{"ok": true}'

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return _Resp()

        monkeypatch.setattr(pipeline_health.urllib.request, "urlopen",
                            fake_urlopen)
        assert pipeline_health._get("https://example.invalid") == {"ok": True}
        assert state["n"] == 2

    def test_non_auth_errors_still_raise(self, monkeypatch):
        """A 500 is a real failure — do not retry it into silence."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        def fake_urlopen(req, timeout=None):
            raise pipeline_health.urllib.error.HTTPError(
                "u", 500, "Server Error", {}, None)

        monkeypatch.setattr(pipeline_health.urllib.request, "urlopen",
                            fake_urlopen)
        with pytest.raises(pipeline_health.urllib.error.HTTPError):
            pipeline_health._get("https://example.invalid")


class TestNewestOfPage:
    """GitHub answered per_page=1 from a stale partial index — see the note
    above _PAGE. Both probes must survive a page whose head is not newest."""

    def test_run_age_takes_the_newest_of_the_page(self, monkeypatch):
        page = {"workflow_runs": [
            {"run_started_at": "2026-08-05T11:33:35Z"},   # stale head
            {"run_started_at": "2026-08-10T08:19:05Z"},   # actual newest
        ]}
        monkeypatch.setattr(pipeline_health, "_get", lambda url: page)
        age = pipeline_health.last_scheduled_run_age_days("r", "w.yml")
        stale_age = pipeline_health._age_days("2026-08-05T11:33:35Z")
        assert age < stale_age

    def test_run_age_falls_back_to_created_at(self, monkeypatch):
        page = {"workflow_runs": [{"created_at": "2026-08-10T08:19:05Z"}]}
        monkeypatch.setattr(pipeline_health, "_get", lambda url: page)
        assert pipeline_health.last_scheduled_run_age_days("r", "w.yml") == \
            pytest.approx(pipeline_health._age_days("2026-08-10T08:19:05Z"),
                          abs=1e-4)

    def test_commit_age_takes_the_newest_of_the_page(self, monkeypatch):
        page = [{"commit": {"committer": {"date": "2026-08-01T00:00:00Z"}}},
                {"commit": {"committer": {"date": "2026-08-09T00:00:00Z"}}}]
        monkeypatch.setattr(pipeline_health, "_get", lambda url: page)
        age = pipeline_health.last_commit_age_days("r", "p")
        assert age == pytest.approx(
            pipeline_health._age_days("2026-08-09T00:00:00Z"), abs=1e-4)

    def test_probes_never_request_a_single_item_page(self, monkeypatch):
        """per_page=1 is the shape that returned the stale index."""
        seen = []

        def fake_get(url):
            seen.append(url)
            return {"workflow_runs": [{"run_started_at": "2026-08-10T00:00:00Z"}]}

        monkeypatch.setattr(pipeline_health, "_get", fake_get)
        pipeline_health.last_scheduled_run_age_days("r", "w.yml")
        monkeypatch.setattr(pipeline_health, "_get",
                            lambda url: (seen.append(url),
                                         [{"commit": {"committer": {
                                             "date": "2026-08-10T00:00:00Z"}}}])[1])
        pipeline_health.last_commit_age_days("r", "p")
        assert seen and all("per_page=1&" not in u and not u.endswith("per_page=1")
                            for u in seen), seen


class TestConfirmedAge:
    """GitHub served a 5-day-stale read on a job's first Actions-API call."""

    def test_fresh_reading_is_not_re_queried(self):
        calls = []

        def probe():
            calls.append(1)
            return 0.5

        assert pipeline_health.confirmed_age(probe, 1) == 0.5
        assert len(calls) == 1

    def test_stale_reading_is_confirmed_and_the_newer_answer_wins(self):
        answers = iter([4.9, 0.02])  # stale first read, truth on the retry

        assert pipeline_health.confirmed_age(lambda: next(answers), 1) == 0.02

    def test_genuinely_stale_stays_stale(self):
        """Confirming must not blunt a real outage."""
        answers = iter([9.0, 9.0])
        assert pipeline_health.confirmed_age(lambda: next(answers), 1) == 9.0

    def test_none_is_confirmed_before_being_believed(self):
        """An empty result set is a 'no runs on record' alert — verify it."""
        answers = iter([None, 0.3])
        assert pipeline_health.confirmed_age(lambda: next(answers), 1) == 0.3

    def test_none_on_both_reads_stays_none(self):
        assert pipeline_health.confirmed_age(lambda: None, 1) is None

    def test_second_read_returning_none_keeps_the_first_age(self):
        answers = iter([9.0, None])
        assert pipeline_health.confirmed_age(lambda: next(answers), 1) == 9.0

    def test_args_are_forwarded_to_the_probe(self):
        seen = []
        pipeline_health.confirmed_age(
            lambda *a: (seen.append(a), 0.1)[1], 1, "repo", "wf.yml")
        assert seen == [("repo", "wf.yml")]


class TestTargets:
    """The configured targets are the thing that produced the false alarms."""

    # Widest gap actually observed per target, measured 2026-08-10 over the
    # windows recorded next to TARGETS. A threshold at or below its target's
    # figure is a scheduled false alarm, which is what this guards.
    OBSERVED_MAX_GAP_DAYS = {
        "fabiogiglietto/toread": (0.4, 7.5),
        "fabiogiglietto/mine-toread": (0.4, 7.5),
        "fabiogiglietto/fg-zettelkasten": (3.0, 5.6),
        "fabiogiglietto/mine-zettelkasten": (2.0, 5.6),
        "fabiogiglietto/research-radio": (5.0, 8.0),
        "fabiogiglietto/fabiogiglietto.github.io": (2.1, 1.1),
    }

    def test_every_target_is_calibrated(self):
        """A new target without measured gaps is a false alarm waiting."""
        assert ({t.repo for t in pipeline_health.TARGETS}
                == set(self.OBSERVED_MAX_GAP_DAYS))

    def test_thresholds_clear_the_observed_gaps(self):
        """The old 3d/4d output limits sat *below* the normal duty cycle."""
        for target in pipeline_health.TARGETS:
            cron_gap, output_gap = self.OBSERVED_MAX_GAP_DAYS[target.repo]
            assert target.cron_max_days > cron_gap, target.repo
            assert target.output_max_days > output_gap, target.repo

    def test_cron_thresholds_are_tighter_than_output_thresholds(self):
        """The cron probe is the sharp signal; output age is the backstop."""
        for target in pipeline_health.TARGETS:
            assert target.cron_max_days < target.output_max_days, target.repo

    def test_every_target_names_a_workflow_file(self):
        for target in pipeline_health.TARGETS:
            assert target.workflow.endswith(".yml")


class TestCrossCheck:
    """The filtered runs query is what served both false alarms."""

    def test_stale_filtered_read_is_rescued_by_the_unfiltered_listing(
            self, monkeypatch):
        """A wholly stale filtered page must not survive as the answer."""
        monkeypatch.setattr(
            pipeline_health, "_get",
            lambda url: {"workflow_runs": [
                {"run_started_at": "2026-08-05T00:00:00Z"}]})   # 14d-stale read
        monkeypatch.setattr(pipeline_health, "_runs_page", lambda repo, wf: [
            {"run_started_at": "2026-08-20T00:00:00Z", "event": "schedule",
             "conclusion": "success"}])
        age = pipeline_health.last_scheduled_run_age_days("r", "w.yml")
        assert age == pytest.approx(
            pipeline_health._age_days("2026-08-20T00:00:00Z"), abs=1e-4)

    def test_cross_check_ignores_non_scheduled_and_failed_runs(
            self, monkeypatch):
        """Corroboration must hold the cron probe to the same question."""
        monkeypatch.setattr(
            pipeline_health, "_get",
            lambda url: {"workflow_runs": [
                {"run_started_at": "2026-08-05T00:00:00Z"}]})
        monkeypatch.setattr(pipeline_health, "_runs_page", lambda repo, wf: [
            {"run_started_at": "2026-08-20T00:00:00Z", "event": "push",
             "conclusion": "success"},
            {"run_started_at": "2026-08-20T00:00:00Z", "event": "schedule",
             "conclusion": "failure"},
        ])
        assert pipeline_health.last_scheduled_run_age_days("r", "w.yml") == \
            pytest.approx(pipeline_health._age_days("2026-08-05T00:00:00Z"),
                          abs=1e-4)

    def test_a_failing_cross_check_does_not_break_the_probe(self, monkeypatch):
        """Corroboration is a bonus; losing it must not lose the reading."""
        monkeypatch.setattr(
            pipeline_health, "_get",
            lambda url: {"workflow_runs": [
                {"run_started_at": "2026-08-05T00:00:00Z"}]})

        def boom(repo, wf):
            raise RuntimeError("500")

        monkeypatch.setattr(pipeline_health, "_runs_page", boom)
        assert pipeline_health.last_scheduled_run_age_days("r", "w.yml") \
            is not None


def _run(started, *, status="completed", conclusion="success",
         event="schedule", updated=None):
    return {"run_started_at": started, "status": status,
            "conclusion": conclusion, "event": event,
            "updated_at": updated or started}


def _hours_ago(hours):
    from datetime import timedelta
    return (pipeline_health.datetime.now(pipeline_health.timezone.utc)
            - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestLatestRunFindings:
    """The signal that was missing while research-radio hung for two days."""

    def _page(self, monkeypatch, runs):
        monkeypatch.setattr(pipeline_health, "_runs_page",
                            lambda repo, workflow: runs)

    def test_healthy_latest_run_is_silent(self, monkeypatch):
        self._page(monkeypatch, [_run(_hours_ago(2))])
        assert _REAL_LATEST_RUN_FINDINGS("r", "w.yml") == []

    def test_run_in_flight_past_the_ceiling_is_hung(self, monkeypatch):
        """49s is a normal run; 4h in progress is a hang, reported before the
        runner kills it at 6h."""
        self._page(monkeypatch, [
            _run(_hours_ago(4), status="in_progress", conclusion=None),
            _run(_hours_ago(28))])
        found = _REAL_LATEST_RUN_FINDINGS("r", "w.yml")
        assert len(found) == 1
        assert "hung" in found[0]

    def test_run_in_flight_within_normal_duration_is_not_hung(self, monkeypatch):
        """The check runs shortly after the crons — an in-flight run is normal."""
        self._page(monkeypatch, [
            _run(_hours_ago(0.5), status="in_progress", conclusion=None)])
        assert _REAL_LATEST_RUN_FINDINGS("r", "w.yml") == []

    def test_failed_latest_completed_run_alerts(self, monkeypatch):
        self._page(monkeypatch, [_run(_hours_ago(3), conclusion="failure")])
        found = _REAL_LATEST_RUN_FINDINGS("r", "w.yml")
        assert len(found) == 1
        assert "failure" in found[0]

    def test_short_cancel_is_not_a_fault(self, monkeypatch):
        """A human pressing cancel, or a concurrency group doing its job."""
        self._page(monkeypatch, [
            _run(_hours_ago(3), conclusion="cancelled",
                 updated=_hours_ago(2.9))])
        assert _REAL_LATEST_RUN_FINDINGS("r", "w.yml") == []

    def test_cancel_at_the_six_hour_ceiling_is_a_runner_kill(self, monkeypatch):
        """The exact shape of the runs this check failed to report."""
        self._page(monkeypatch, [
            _run(_hours_ago(12), conclusion="cancelled",
                 updated=_hours_ago(6))])
        found = _REAL_LATEST_RUN_FINDINGS("r", "w.yml")
        assert len(found) == 1
        assert "cancelled" in found[0]

    def test_a_failed_push_run_is_not_the_pipeline_breaking(self, monkeypatch):
        """Someone iterating on a branch must not page anyone."""
        self._page(monkeypatch, [
            _run(_hours_ago(1), conclusion="failure", event="push"),
            _run(_hours_ago(4))])
        assert _REAL_LATEST_RUN_FINDINGS("r", "w.yml") == []

    def test_an_in_flight_run_does_not_hide_the_last_failure(self, monkeypatch):
        """The newest run is still going; the newest *completed* one failed."""
        self._page(monkeypatch, [
            _run(_hours_ago(0.5), status="in_progress", conclusion=None),
            _run(_hours_ago(24), conclusion="failure")])
        found = _REAL_LATEST_RUN_FINDINGS("r", "w.yml")
        assert len(found) == 1
        assert "failure" in found[0]

    def test_no_runs_is_not_a_finding(self, monkeypatch):
        """Absence is the cron probe's question, not this one's."""
        self._page(monkeypatch, [])
        assert _REAL_LATEST_RUN_FINDINGS("r", "w.yml") == []


class TestHoldUntilConfirmed:
    """Both false alarms were single stale reads. One run must not page."""

    F1 = pipeline_health.Finding("repo|wf|cron", "cron is stale")
    F2 = pipeline_health.Finding("repo|out|output", "output is stale")

    def test_a_first_sighting_is_held(self):
        post, held = pipeline_health.hold_until_confirmed([self.F1], set())
        assert post == []
        assert held == [self.F1]

    def test_a_finding_that_recurs_is_posted(self):
        post, held = pipeline_health.hold_until_confirmed(
            [self.F1], {self.F1.key})
        assert post == [self.F1]
        assert held == []

    def test_holding_is_per_finding_not_per_run(self):
        post, held = pipeline_health.hold_until_confirmed(
            [self.F1, self.F2], {self.F2.key})
        assert post == [self.F2]
        assert held == [self.F1]

    def test_unknown_state_posts_rather_than_holds(self):
        """A lost cache must degrade to noise, never to a silent switch."""
        post, held = pipeline_health.hold_until_confirmed([self.F1], None)
        assert post == [self.F1]
        assert held == []


class TestState:
    """State is cache-restored, so every read must tolerate it being absent."""

    def test_missing_file_reads_as_unknown(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HEALTH_STATE_FILE", str(tmp_path / "none.json"))
        assert pipeline_health.load_previous_keys() is None

    def test_corrupt_file_reads_as_unknown(self, monkeypatch, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{not json")
        monkeypatch.setenv("HEALTH_STATE_FILE", str(path))
        assert pipeline_health.load_previous_keys() is None

    def test_round_trip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HEALTH_STATE_FILE", str(tmp_path / "d" / "s.json"))
        pipeline_health.save_keys({"a", "b"})
        assert pipeline_health.load_previous_keys() == {"a", "b"}

    def test_an_empty_save_is_remembered_as_empty_not_unknown(
            self, monkeypatch, tmp_path):
        """A clean run must let yesterday's held finding be held again."""
        monkeypatch.setenv("HEALTH_STATE_FILE", str(tmp_path / "s.json"))
        pipeline_health.save_keys(set())
        assert pipeline_health.load_previous_keys() == set()


class TestMain:
    """End to end: what actually reaches Slack."""

    @pytest.fixture
    def posted(self, monkeypatch, tmp_path):
        sent = []
        monkeypatch.setenv("HEALTH_STATE_FILE", str(tmp_path / "s.json"))
        # Steady state: yesterday's run happened and found nothing. Without
        # this the state is *unknown*, which deliberately posts unconfirmed.
        pipeline_health.save_keys(set())
        monkeypatch.setattr(pipeline_health, "post_slack", sent.append)
        monkeypatch.setattr(pipeline_health, "TARGETS", [TARGET])
        return sent

    def test_a_one_day_flake_never_reaches_slack(self, posted, fake_ages,
                                                 monkeypatch):
        """Day one flags it, day two is clean — nobody is paged."""
        fake_ages(9.0, 0.1)
        pipeline_health.main()
        assert posted == []
        fake_ages(0.1, 0.1)
        pipeline_health.main()
        assert posted == []

    def test_a_real_outage_posts_on_the_second_run(self, posted, fake_ages):
        fake_ages(9.0, 0.1)
        pipeline_health.main()
        assert posted == []
        pipeline_health.main()
        assert len(posted) == 1
        assert "last succeeded on schedule" in posted[0]

    def test_the_alert_carries_the_link_that_settles_it(self, posted,
                                                        fake_ages):
        fake_ages(9.0, 0.1)
        pipeline_health.main()
        pipeline_health.main()
        assert (f"https://github.com/{TARGET.repo}/actions/workflows/"
                f"{TARGET.workflow}") in posted[0]

    def test_override_posts_immediately(self, posted, fake_ages):
        """MAX_AGE_OVERRIDE_DAYS=0 is a test of the alerting path itself."""
        fake_ages(0.1, 0.1)
        import os as _os
        _os.environ["MAX_AGE_OVERRIDE_DAYS"] = "0"
        try:
            pipeline_health.main()
        finally:
            del _os.environ["MAX_AGE_OVERRIDE_DAYS"]
        assert len(posted) == 1

    def test_a_healthy_run_posts_nothing(self, posted, fake_ages):
        fake_ages(0.1, 0.1)
        pipeline_health.main()
        pipeline_health.main()
        assert posted == []
