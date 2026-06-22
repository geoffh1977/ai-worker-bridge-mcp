from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_bridge.config import ConfigError, load_config


def test_load_config_reports_missing_file_without_raw_os_error(tmp_path):
    missing = tmp_path / "missing-config.yaml"

    with pytest.raises(ConfigError) as excinfo:
        load_config(missing)

    assert str(missing) in str(excinfo.value)
    assert "not found" in str(excinfo.value).lower()
    assert not isinstance(excinfo.value.__cause__, FileNotFoundError)


def test_load_config_reports_permission_denied_without_raw_os_error(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("workers: []\n", encoding="utf-8")

    def raise_permission_error(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "open", raise_permission_error)

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)

    assert str(config_path) in str(excinfo.value)
    assert "permission denied" in str(excinfo.value).lower()
    assert not isinstance(excinfo.value.__cause__, PermissionError)


def test_load_config_requires_at_least_one_scoped_key(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
server:
  host: 127.0.0.1
  port: 8080
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://worker.local:8642
    auth_type: none
    model_name: local-worker
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        load_config(config_path)

    assert "auth.scoped_keys must define at least one scoped key" in str(excinfo.value)
