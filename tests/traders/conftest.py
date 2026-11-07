import pytest

from tests.traders._bundles import make_bundle


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(tmp_path)
