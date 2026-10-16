import json

import pytest

from lifeguard import settings as cfg
from lifeguard.app import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A fresh app with its own database, config and secret key; no camera threads."""
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(cfg, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({}))
    monkeypatch.setenv("LIFEGUARD_ADMIN_PASSWORD", "AdminPass123!")
    app = create_app(start_cameras=False)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


@pytest.fixture
def admin(client):
    assert login(client, "admin", "AdminPass123!").status_code == 200
    return client
