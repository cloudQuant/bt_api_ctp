"""The installed package reports the version advertised by its metadata."""

from __future__ import annotations

import re
from pathlib import Path

import bt_api_ctp


def test_source_and_distribution_version_agree():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = re.search(
        r'^version = "([^"]+)"$', pyproject.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert version is not None
    declared = version.group(1)
    assert bt_api_ctp.__version__ == declared
