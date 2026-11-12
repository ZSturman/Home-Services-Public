import json
import shutil
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", message=".*httpx.*")

from home_services.config import load_config  # noqa: E402
from home_services.contract import _fixture_config  # noqa: E402
from home_services.devfixtures import create_dev_fixtures  # noqa: E402
from home_services.runtime import FixedClock, SequentialIds  # noqa: E402
from home_services.services import build_services  # noqa: E402

HAS_RESTIC = shutil.which("restic") is not None
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class Env:
    def __init__(self, root: Path, with_restic: bool = False):
        self.root = root
        self.fx = create_dev_fixtures(root, with_restic=with_restic)
        self.config_path = _fixture_config(root)
        self.config = load_config(self.config_path)
        self.clock = FixedClock(1792483200.0)
        self.s = build_services(self.config, clock=self.clock, ids=SequentialIds())
        self.mount = Path(self.fx["mount"])
        self.files = self.mount / "home-services/files"
        self.table = Path(self.fx["mount_table"])
        self._original_table = self.table.read_text()

    def unplug(self):
        t = json.loads(self.table.read_text())
        t.pop(str(self.mount))
        self.table.write_text(json.dumps(t))

    def wrong_disk(self):
        t = json.loads(self._original_table)
        t[str(self.mount)]["uuid"] = "OTHER-DISK"
        self.table.write_text(json.dumps(t))

    def replug(self):
        self.table.write_text(self._original_table)

    def client(self):
        from fastapi.testclient import TestClient
        from home_services.api.app import create_app
        return TestClient(create_app(self.s))

    def token(self, *scopes):
        tok, _ = self.s.tokens.create("test", list(scopes))
        return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.s.db.close()


@pytest.fixture
def restic_env(tmp_path):
    if not HAS_RESTIC:
        pytest.skip("restic not installed")
    e = Env(tmp_path, with_restic=True)
    yield e
    e.s.db.close()
