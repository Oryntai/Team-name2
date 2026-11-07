"""Exercise checklist/journal JavaScript with Node's built-in runner (no model or browser)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_checklist_and_journal_components():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for UI component tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", str(root / "tests/ui/checklist-journal.test.mjs")],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
