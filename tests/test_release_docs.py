from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_documents_scoped_bridge_keys_only() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "AI_BRIDGE_READ_KEY=" in env_example
    assert "AI_BRIDGE_SUBMIT_KEY=" in env_example
    assert "AI_BRIDGE_ADMIN_KEY=" in env_example
    assert "AI_BRIDGE_PORT=" in env_example


def test_config_example_documents_canonicalize_and_histogram_metrics() -> None:
    config_example = (REPO_ROOT / "config.yaml.example").read_text(encoding="utf-8")

    assert "canonicalize: false" in config_example
    assert "worker_call_seconds_buckets" in config_example
    assert "ai_bridge_worker_call_seconds" in config_example
