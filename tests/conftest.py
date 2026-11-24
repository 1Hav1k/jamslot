import pytest

from support.harness import System


@pytest.fixture
def system(tmp_path):
    s = System(tmp_path)
    yield s
    s.close()


@pytest.fixture
def short_hold_system(tmp_path):
    s = System(tmp_path, {"BOOKING_HOLD_SECONDS": "60"})
    yield s
    s.close()
