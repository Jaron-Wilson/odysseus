"""Guard: the test session must never resolve DATA_DIR to the live data dir.

tests/conftest.py forces a fresh temporary ODYSSEUS_DATA_DIR before any app
module is imported. If that ever regresses, test runs from a shell that
exports the live data dir write test plans, attachments and PDFs into it.
"""
import os
import tempfile

LIVE_DATA_DIR = "/home/jaron/odysseus-data"


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def test_env_data_dir_is_temporary():
    env_dir = os.environ.get("ODYSSEUS_DATA_DIR", "")
    assert env_dir, "conftest must set ODYSSEUS_DATA_DIR for the test session"
    assert _real(env_dir) != _real(LIVE_DATA_DIR)
    assert _real(env_dir).startswith(_real(tempfile.gettempdir()))
    assert os.path.basename(env_dir).startswith("odysseus-test-data-")


def test_constants_data_dir_is_not_live():
    from src import constants as src_constants
    from core import constants as core_constants

    for value in (src_constants.DATA_DIR, core_constants.DATA_DIR):
        assert _real(value) != _real(LIVE_DATA_DIR)
        assert not _real(value).startswith(_real(LIVE_DATA_DIR) + os.sep)
        assert _real(value) == _real(os.environ["ODYSSEUS_DATA_DIR"])


def test_database_url_is_not_in_live_dir():
    from core.database import DATABASE_URL

    assert LIVE_DATA_DIR not in DATABASE_URL
