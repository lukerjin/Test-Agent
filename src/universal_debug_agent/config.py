"""Load and validate Project Profile from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from universal_debug_agent.schemas.profile import ProjectProfile


def load_profile(path: str | Path) -> ProjectProfile:
    """Load a project profile from a YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Empty profile: {path}")

    return ProjectProfile.model_validate(raw)
