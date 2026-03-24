from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent


def _default_oai_config_path() -> str:
    explicit = os.environ.get("DEVOPS_OAI_CONFIG_PATH", "").strip()
    if explicit:
        return explicit
    for candidate in ("OAI_CONFIG_LIST.local", "OAI_CONFIG_LIST"):
        path = REPO_ROOT / candidate
        if path.exists():
            return str(path)
    return str(REPO_ROOT / "OAI_CONFIG_LIST.local")


DEFAULT_OAI_CONFIG_PATH = _default_oai_config_path()


def _resolve_oai_api_keys(config_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for entry in config_list:
        item = dict(entry)
        api_key = item.get("api_key")
        if isinstance(api_key, str) and api_key.startswith("ENV:"):
            item["api_key"] = os.environ.get(api_key[4:], "")
        resolved.append(item)
    return resolved


def load_oai_config_list(path: str = "") -> list[dict[str, Any]]:
    cfg_path = Path(path or DEFAULT_OAI_CONFIG_PATH)
    if not cfg_path.exists():
        return []
    try:
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    normalized = [item for item in payload if isinstance(item, dict)]
    return _resolve_oai_api_keys(normalized)
