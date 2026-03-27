"""Tests for config loading."""

import tempfile
from pathlib import Path

import pytest
import yaml

from universal_debug_agent.config import load_profile


def _write_yaml(data: dict, path: Path) -> None:
    path.write_text(yaml.dump(data))


def test_load_valid_profile():
    data = {
        "project": {"name": "Test Project"},
        "code": {"root_dir": "/tmp/test"},
    }
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        profile = load_profile(f.name)
        assert profile.project.name == "Test Project"


def test_load_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        load_profile("/nonexistent/path.yaml")


def test_load_empty_file():
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write("")
        f.flush()
        with pytest.raises(ValueError, match="Empty profile"):
            load_profile(f.name)


def test_load_full_profile():
    data = {
        "project": {"name": "Full", "description": "desc"},
        "environment": {"type": "api", "base_url": "http://localhost:3000"},
        "auth": {"method": "token"},
        "code": {"root_dir": "/tmp/code", "entry_dirs": ["src"]},
        "mcp_servers": {
            "db": {
                "enabled": True,
                "command": "node",
                "args": ["db.js"],
                "env": {"DB_HOST_ENV": "HOST"},
            }
        },
        "boundaries": {"max_steps": 15},
    }
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        profile = load_profile(f.name)
        assert profile.environment.type == "api"
        assert profile.boundaries.max_steps == 15
        assert "db" in profile.mcp_servers
