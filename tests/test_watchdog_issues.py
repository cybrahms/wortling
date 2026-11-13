"""Every issue name the watchdog can raise is declared, and says what it is about.

The bug this closes is not a wrong word. `scripts/monitor.py` and the worker's
`ops-check` between them emit free-form strings into one list, and on 2026-11-01 a
probe reported its own failure under the worker's name across 173 records: the
reader was told the worker was broken when the probe was. Nothing asserted the
names were a closed set, so a new emitter could reintroduce that by choosing a
name that already belonged to a component, and nothing would notice.

The assert is on the emitted set, not on the registry: the registry is only
useful if it covers what the code actually raises, so this reads the emitters
(the AST of both, plus a live run through the worker) and fails on any name the
registry does not hold. Adding a name to `config/watchdog-issues.json` is
therefore never enough on its own - `test_every_declared_name_says_what_it_is_about`
makes it explain itself, and `test_no_declared_name_is_dead` makes it reachable.
"""

import ast
import runpy
from pathlib import Path

import pytest

from wortling.ops import operational_status

ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / "scripts" / "monitor.py"
WORKER = ROOT / "src" / "wortling" / "ops.py"
REGISTRY = ROOT / "config" / "watchdog-issues.json"


def load_monitor():
    """The monitor as a namespace, so its fakes can be swapped in.

    `run_path` returns the module's globals dict and the functions defined in it
    hold that same dict, so replacing `probe_worker` here replaces what
    `collect()` calls.
    """
    return runpy.run_path(str(MONITOR))


def declared():
    import json

    return json.loads(REGISTRY.read_text())["issues"]


def appended_literals(path):
    """Every string literal `issues.append(...)` is handed, anywhere in the file.

    Read off the syntax tree rather than by importing, because the point is the
    set the code CAN emit: a branch no test happens to reach still has to name a
    declared issue, or the next person to reach it reports a fault the registry
    cannot describe. `issues.extend(report["issues"])` in the monitor is how the
    worker's names arrive, which is why WORKER is scanned as well.
    """
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func.value
        if node.func.attr != "append":
            continue
        if not (isinstance(target, ast.Name) and target.id == "issues"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                names.add(argument.value)
            else:
                pytest.fail(
                    f"{path.name}: issues.append() with a non-literal name: {ast.dump(argument)}"
                )
    return names


def test_the_scanner_sees_the_names_it_is_meant_to_guard():
    # A guard that scans nothing passes forever. Both emitters are known to raise
    # these, so finding none of them means the scanner is looking at nothing.
    assert {"probe_cannot_run", "worker_probe_failed"} <= appended_literals(MONITOR)
    assert {"failed_jobs", "provider_probe_failed"} <= appended_literals(WORKER)


@pytest.mark.parametrize("emitter", [MONITOR, WORKER], ids=["monitor", "worker"])
def test_every_emitted_name_is_declared(emitter):
    known = declared()
    undeclared = sorted(appended_literals(emitter) - set(known))
    assert undeclared == [], (
        f"{emitter.name} can raise {undeclared}, which config/watchdog-issues.json "
        "does not hold. Declare each one with the component it speaks about, or "
        "rename it to the component that actually failed."
    )


def test_a_live_worker_run_only_names_declared_issues(tmp_path, settings):
    # The AST scan above cannot see a name that is computed, and the worker is
    # the half that runs inside a container where nobody is watching. This is the
    # same assertion through the code path that actually reports.
    from wortling.db import Database
    from wortling.worker import heartbeat

    Database(settings.data_dir).initialize(settings)
    heartbeat(settings)
    report = operational_status(settings)
    assert report["issues"], (
        "a fresh worker with no backup run should report backup_missing_or_stale"
    )
    assert sorted(set(report["issues"]) - set(declared())) == []


def test_a_name_the_registry_does_not_hold_is_visible():
    # The negative control: without this, a registry that held everything would
    # look identical to a registry that held nothing and never matched.
    monitor = load_monitor()
    components, undeclared = monitor["describe"](["failed_jobs", "invented_issue"])
    assert components == {"failed_jobs": "queue"}
    assert undeclared == ["invented_issue"]


def test_an_unreadable_registry_does_not_swallow_the_fault(tmp_path):
    # Losing a real signal because a label lookup failed is worse than the
    # missing label, so the lookup degrades instead of raising.
    monitor = load_monitor()
    components, undeclared = monitor["describe"](["failed_jobs"], path=tmp_path / "absent.json")
    assert components == {}
    assert undeclared == ["failed_jobs"]


def test_every_declared_name_says_what_it_is_about():
    # "Add it to the registry and forget the meaning" is the failure mode a bare
    # allow-list invites. A name that does not name a component cannot be acted
    # on by the person woken by it.
    known = declared()
    assert known, "the registry is empty; a closed set of nothing asserts nothing"
    for name, entry in known.items():
        assert set(entry) == {"component", "meaning"}, name
        assert entry["component"].strip(), name
        assert len(entry["meaning"].split()) >= 8, f"{name}: the meaning has to be a sentence"


def test_no_declared_name_is_dead():
    # The other direction: a name nothing can raise is a description of a fault
    # that no longer exists, and a reader who greps for it learns nothing.
    emitted = appended_literals(MONITOR) | appended_literals(WORKER)
    assert sorted(set(declared()) - emitted) == []


def test_the_monitor_files_the_component_beside_each_name(tmp_path):
    # The payload is what a crew member reads first. Before this, it carried the
    # names and left the reader to infer which component each belonged to.
    monitor = load_monitor()
    filed = {}

    def filer(title, payload, key):
        filed.update(payload)

    monitor["notify"](
        {"issues": ["probe_cannot_run", "failed_jobs"], "metrics": {}},
        tmp_path / "state.json",
        filer=filer,
        clock=lambda: 1_700_000_000,
    )
    assert filed["components"] == {"probe_cannot_run": "watchdog", "failed_jobs": "queue"}
    assert filed["undeclared_issues"] == []
