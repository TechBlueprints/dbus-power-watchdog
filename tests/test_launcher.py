"""The launcher runit actually runs is plain python3 (CONSUMERS.md rule 7).

On Venus, /service/dbus-power-watchdog is a symlink to this repo's root
service/ directory (verified on prod 2026-09-06: it resolves to
/data/apps/dbus-power-watchdog/service), so service/run here IS the real
launcher.  A migrated start script beside an unmigrated run is how a
consumer looks migrated while still on the interpreter shim:
ensure_ble_stack() returns "provided" and every check passes.

This pins the template.  The box-side check is install.sh's Step 5, which
resolves /service/<name>/run on the box and refuses to restart on residue.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "service" / "run"

# BCM's installer decides whether the shim is still needed on a box with this
# pattern: a literal /data/bcm/python3, BCM_PY, or a path built from
# ${BCM_ROOT:-/data/bcm}.  install.sh's pre-restart check uses it too.
SHIM_RESIDUE = re.compile(r'''bcm[^\s"']*/python3|BCM_PY''')


def _script_lines():
    return [
        line for line in RUN.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_run_execs_the_plain_interpreter():
    execs = [line for line in _script_lines() if line.strip().startswith("exec ")]
    # `exec 2>&1` is the log redirect; the last exec is the launcher.
    launcher = execs[-1].strip()
    assert re.fullmatch(r"exec python3 -u dbus-power-watchdog\.py", launcher), launcher


def test_run_never_mentions_the_shim():
    for line in _script_lines():
        assert not SHIM_RESIDUE.search(line), line
        assert "PYTHONPATH" not in line, line


@pytest.mark.parametrize("residue", [
    "exec /data/bcm/python3 -u main.py",
    "BCM_PY=/data/bcm/python3",
    'exec "${BCM_ROOT:-/data/bcm}/python3" -u main.py',
    "exec ${BCM_ROOT:-/data/bcm}/python3 main.py",
])
def test_residue_pattern_matches_every_shim_form(residue):
    assert SHIM_RESIDUE.search(residue), residue


def test_residue_pattern_ignores_the_plain_launcher():
    assert not SHIM_RESIDUE.search("exec python3 -u dbus-power-watchdog.py")


def test_installer_uses_the_same_pattern():
    # One pattern, two places: the repo test and the box-side check must
    # not drift apart.
    installer = (REPO / "install.sh").read_text()
    assert "bcm[^[:space:]\"'\"'\"']*/python3|BCM_PY" in installer


def test_no_other_launcher_exists():
    # One launcher: the file the /service symlink resolves to.  A second
    # run or start script is where an unmigrated exec hides.
    others = [
        p for p in REPO.rglob("*")
        if p.is_file()
        and (p.name == "run" or p.name.startswith("start-"))
        and not any(part in (".git", ".venv", ".claude", "ext") for part in p.parts)
        and p != RUN
        and p != REPO / "service" / "log" / "run"
    ]
    assert others == [], others
