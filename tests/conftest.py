from datetime import datetime

import pytest

from app.config import Settings
from app.db import Database
from app.scheduling import Scheduler

# Monday 2026-10-12, 10:05 clinic time.
NOW = datetime(2026, 10, 12, 10, 5)


@pytest.fixture
def settings(tmp_path):
    return Settings(database_path=str(tmp_path / "test.db"), claude_fallbacks=False)


@pytest.fixture
def scheduler(settings):
    return Scheduler(Database(settings.database_path), settings, now=lambda: NOW)
