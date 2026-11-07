"""League fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.league.sample import copy_sample


@pytest.fixture
def sample_copy(tmp_path: Path) -> Path:
    return copy_sample(tmp_path)
