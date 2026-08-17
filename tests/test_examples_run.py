"""Every shipped example actually runs.

Added because a renamed function left `examples/model_workers.py` raising ImportError, and it
went unnoticed through two commits: the manual check was an `&&` chain, so a non-zero exit was
swallowed by the next command in the shell. A README that tells someone to run a file which
crashes on import is worse than no example, and nothing in the suite was watching.

These are subprocess runs, not imports, so an exception anywhere — including at module scope —
fails the test. Examples needing credentials or a platform are excluded by name, with the reason.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# Needs real infrastructure: Fly API token, live Postgres, a Gmail thread.
NEEDS_INFRA = {"smoke_fly_case.py", "smoke_recovery.py"}
# Imported by the CLI via --app, not run directly.
NOT_A_SCRIPT = {"cli_app.py"}

RUNNABLE = sorted(p.name for p in EXAMPLES.glob("*.py")
                  if p.name not in NEEDS_INFRA | NOT_A_SCRIPT)


def test_there_are_examples_to_check():
    """Guards the guard: a glob that silently matches nothing would make this file vacuous."""
    assert len(RUNNABLE) >= 5, RUNNABLE


@pytest.mark.parametrize("name", RUNNABLE)
def test_example_runs_clean(name):
    p = subprocess.run([sys.executable, str(EXAMPLES / name)],
                       capture_output=True, text=True, timeout=120,
                       cwd=str(EXAMPLES.parent))
    assert p.returncode == 0, f"{name} exited {p.returncode}\n{p.stdout[-1500:]}\n{p.stderr[-2000:]}"


@pytest.mark.parametrize("name", RUNNABLE)
def test_example_runs_clean_with_the_sample_allowlist(name, monkeypatch):
    """The other branch of the empty-default logic. Unconfigured is covered above; this covers
    the path where clearances exist, which is where the capability factories actually build."""
    env = {"ABEYANCE_USE_SAMPLE_ALLOWLIST": "1"}
    p = subprocess.run([sys.executable, str(EXAMPLES / name)],
                       capture_output=True, text=True, timeout=120,
                       cwd=str(EXAMPLES.parent),
                       env={**__import__("os").environ, **env})
    assert p.returncode == 0, f"{name} exited {p.returncode}\n{p.stdout[-1500:]}\n{p.stderr[-2000:]}"
