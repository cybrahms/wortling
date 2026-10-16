"""Architectural checks on irreversible API boundaries, alongside behavioral tests."""

import ast
from pathlib import Path


def test_only_full_sync_call_is_literal_download_in_bootstrap():
    calls = []
    for path in (Path(__file__).parents[1] / "src" / "wortling").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name = node.func.attr
                assert name not in {
                    "remove_notes",
                    "remove_cards",
                    "schedule_cards_as_new",
                    "reset_cards",
                    "set_due_date",
                    "full_upload_or_download_raw",
                }
                if name == "full_upload_or_download":
                    calls.append((path.name, node))
    assert len(calls) == 1
    assert calls[0][0] == "sync.py"
    upload = next(keyword.value for keyword in calls[0][1].keywords if keyword.arg == "upload")
    assert isinstance(upload, ast.Constant) and upload.value is False
