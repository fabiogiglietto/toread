"""requirements.txt and requirements.lock must agree.

Nothing installs requirements.txt — `tests.yml` and `update_feed.yml` both
`pip install -r requirements.lock`, and the lock is compiled by hand with
`uv pip compile`. So a constraint bump that lands without a recompile is
invisible: CI stays green while the declared floor drifts above the pinned
version, and the bump has no effect on anything that actually runs. That is
exactly what happened with four dependabot PRs (#14, #16, #21, #22).

This asserts *consistency*, not freshness. A recompile-and-diff would fail
every time any dependency publishes a new release — daily noise unrelated to
this repo. Checking that each pin satisfies its declared constraint is
deterministic, offline, and only fails when something is genuinely wrong.

`packaging` is not in the lock, but it is a hard dependency of pytest, so it is
always present wherever this test runs.
"""

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"


def _direct_requirements():
    """The hand-written constraints, as {canonical name: Requirement}."""
    out = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        req = Requirement(line)
        out[canonicalize_name(req.name)] = req
    return out


def _locked_pins():
    """The compiled pins, as {canonical name: Version}."""
    out = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        name = name.split("[", 1)[0]  # drop extras: `pkg[extra]==1.0`
        out[canonicalize_name(name)] = Version(version.strip())
    return out


def test_every_direct_requirement_is_locked():
    missing = sorted(set(_direct_requirements()) - set(_locked_pins()))
    assert not missing, (
        f"declared in requirements.txt but absent from requirements.lock: "
        f"{missing} — recompile with "
        f"`uv pip compile requirements.txt -o requirements.lock "
        f"--python-version 3.11`"
    )


def test_locked_versions_satisfy_declared_constraints():
    """The drift this exists to catch: a bumped floor above the pinned version,
    e.g. `arxiv>=4.0.1` in the .txt against `arxiv==4.0.0` in the lock."""
    pins = _locked_pins()
    violations = []
    for name, req in sorted(_direct_requirements().items()):
        pinned = pins.get(name)
        if pinned is None:
            continue  # covered by the test above
        if not req.specifier.contains(pinned, prereleases=True):
            violations.append(f"{req} is not satisfied by locked {name}=={pinned}")

    assert not violations, (
        "requirements.lock is stale relative to requirements.txt:\n  "
        + "\n  ".join(violations)
        + "\nRecompile with `uv pip compile requirements.txt -o "
          "requirements.lock --python-version 3.11`"
    )
