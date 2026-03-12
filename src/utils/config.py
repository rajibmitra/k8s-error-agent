"""Configuration loader with environment variable override support."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load configuration from YAML file with env var overrides for secrets."""
    path = config_path or DEFAULT_CONFIG_PATH

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            f"Copy config/config.example.yaml to {path} and update values."
        )

    with open(path) as f:
        config = yaml.safe_load(f)

    # Inject secrets from environment variables (never store in config file)
    config["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    config["jira"]["api_token"] = os.environ.get("JIRA_API_TOKEN", "")

    _validate_config(config)
    return config


def _validate_config(config: dict[str, Any]) -> None:
    """Validate required config fields are present."""
    if not config.get("anthropic_api_key"):
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")

    jira_cfg = config.get("jira", {})
    if not jira_cfg.get("api_token"):
        raise ValueError("JIRA_API_TOKEN environment variable is required")
    if not jira_cfg.get("server"):
        raise ValueError("jira.server must be set in config")
    if not jira_cfg.get("email"):
        raise ValueError("jira.email must be set in config")

    if not config.get("namespaces"):
        raise ValueError("At least one namespace must be configured")
