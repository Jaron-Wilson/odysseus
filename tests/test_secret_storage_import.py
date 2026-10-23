"""src.secret_storage must import on its own: core.database imports it lazily,
and a module-level `import core...` in it made that a circular import (the
"secret_storage import failed ... partially initialized module" warning)."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_secret_storage_imports_standalone_without_circular_warning():
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, ODYSSEUS_DATA_DIR=d, DATABASE_URL="sqlite:///:memory:", PYTHONPATH=ROOT)
        r = subprocess.run([sys.executable, "-c",
                            "import src.secret_storage as s; assert s.decrypt(s.encrypt('x')) == 'x'"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "circular import" not in r.stderr and "secret_storage import failed" not in r.stderr
