import os
import pytest

os.environ.setdefault("TRUSTMERGE_LLM", "fake")

from fixtures import build_scenario


@pytest.fixture
def scenario(tmp_path):
    return build_scenario(tmp_path / "repo")
