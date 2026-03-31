"""Tests for config.py — OAI config loading and API key resolution."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# Ensure repo root is importable
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import _resolve_oai_api_keys, load_oai_config_list


# ---------------------------------------------------------------------------
# _resolve_oai_api_keys
# ---------------------------------------------------------------------------
class TestResolveOaiApiKeys:
    def test_plain_key_unchanged(self):
        items = [{"model": "gpt-4", "api_key": "sk-abc123"}]
        result = _resolve_oai_api_keys(items)
        assert result[0]["api_key"] == "sk-abc123"

    def test_env_key_resolved(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "resolved-value")
        items = [{"model": "gpt-4", "api_key": "ENV:MY_SECRET_KEY"}]
        result = _resolve_oai_api_keys(items)
        assert result[0]["api_key"] == "resolved-value"

    def test_env_key_missing_returns_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR_12345", raising=False)
        items = [{"model": "gpt-4", "api_key": "ENV:NONEXISTENT_VAR_12345"}]
        result = _resolve_oai_api_keys(items)
        assert result[0]["api_key"] == ""

    def test_non_string_key_unchanged(self):
        items = [{"model": "gpt-4", "api_key": 12345}]
        result = _resolve_oai_api_keys(items)
        assert result[0]["api_key"] == 12345

    def test_empty_list(self):
        assert _resolve_oai_api_keys([]) == []

    def test_original_not_mutated(self):
        original = [{"model": "gpt-4", "api_key": "ENV:X"}]
        _resolve_oai_api_keys(original)
        assert original[0]["api_key"] == "ENV:X"


# ---------------------------------------------------------------------------
# load_oai_config_list
# ---------------------------------------------------------------------------
class TestLoadOaiConfigList:
    def test_valid_json_list(self, tmp_path):
        cfg = [{"model": "gpt-4", "api_key": "sk-test"}]
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_oai_config_list(str(p))
        assert len(result) == 1
        assert result[0]["model"] == "gpt-4"

    def test_nonexistent_file_returns_empty(self):
        result = load_oai_config_list("/nonexistent/path/config.json")
        assert result == []

    def test_invalid_json_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json at all", encoding="utf-8")
        result = load_oai_config_list(str(p))
        assert result == []

    def test_json_not_a_list_returns_empty(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text('{"model": "gpt-4"}', encoding="utf-8")
        result = load_oai_config_list(str(p))
        assert result == []

    def test_non_dict_items_filtered(self, tmp_path):
        cfg = [{"model": "gpt-4", "api_key": "sk-x"}, "stray-string", 42]
        p = tmp_path / "mixed.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_oai_config_list(str(p))
        assert len(result) == 1

    def test_env_resolution_in_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY_99", "secret")
        cfg = [{"model": "gpt-4", "api_key": "ENV:TEST_API_KEY_99"}]
        p = tmp_path / "env.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_oai_config_list(str(p))
        assert result[0]["api_key"] == "secret"
