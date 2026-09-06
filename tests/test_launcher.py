"""The launcher runit actually runs is plain python3 (CONSUMERS.md rule 7).

On Venus, /service/dbus-power-watchdog is a symlink to this repo's root
service/ directory (verified on prod 2026-09-06: it resolves to
/data/apps/dbus-power-watchdog/service), so service/run here IS the real
launcher.  A migrated start script beside an unmigrated run is how a
consumer looks migrated while still on the interpreter shim:
ensure_ble_stack() returns "provided" and every check passes.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "service" / "run"


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
        assert "/data/bcm/python3" not in line, line
        assert "BCM_PY" not in line, line
        assert "PYTHONPATH" not in line, line


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
