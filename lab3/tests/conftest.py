"""Shared isolated database fixture."""

import pytest
from backend.app import create_app
from backend.seed import seed


@pytest.fixture
def env(tmp_path):
    clock = [1800000000.0]
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "testing-only",
            "CLOCK": lambda: clock[0],
        }
    )
    seed(app)
    return app, clock
