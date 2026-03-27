"""Tests for tgai.storage — persistence layer."""

import json
import tempfile
from pathlib import Path

import pytest

from tgai.storage import Storage


@pytest.fixture
def storage(tmp_path):
    return Storage(base_dir=tmp_path)


# ------------------------------------------------------------------
# Summary cache
# ------------------------------------------------------------------

class TestSummaryCache:
    def test_load_empty(self, storage):
        assert storage.load_summary_cache() == {}

    def test_save_and_load(self, storage):
        cache = {"Chat A": {"hours": 24, "summary": "test"}}
        storage.save_summary_cache(cache)
        loaded = storage.load_summary_cache()
        assert loaded == cache

    def test_overwrite(self, storage):
        storage.save_summary_cache({"a": "1"})
        storage.save_summary_cache({"b": "2"})
        loaded = storage.load_summary_cache()
        assert loaded == {"b": "2"}

    def test_corrupt_file_returns_empty(self, storage):
        path = storage._summary_cache_path
        path.write_text("not json", encoding="utf-8")
        assert storage.load_summary_cache() == {}

    def test_non_dict_returns_empty(self, storage):
        path = storage._summary_cache_path
        path.write_text("[1,2,3]", encoding="utf-8")
        assert storage.load_summary_cache() == {}


# ------------------------------------------------------------------
# Watermarks
# ------------------------------------------------------------------

class TestWatermarks:
    def test_load_empty(self, storage):
        assert storage.load_watermarks() == {}

    def test_save_and_load(self, storage):
        wm = {"Chat A": 100, "Chat B": 200}
        storage.save_watermarks(wm)
        assert storage.load_watermarks() == wm

    def test_corrupt_returns_empty(self, storage):
        storage._watermark_path.write_text("{bad", encoding="utf-8")
        assert storage.load_watermarks() == {}


# ------------------------------------------------------------------
# Digests
# ------------------------------------------------------------------

class TestDigests:
    def test_save_creates_file(self, storage):
        path = storage.save_digest("Hello digest")
        assert path.exists()
        assert "Hello digest" in path.read_text(encoding="utf-8")

    def test_save_appends_same_day(self, storage):
        p1 = storage.save_digest("First")
        p2 = storage.save_digest("Second")
        assert p1 == p2
        content = p1.read_text(encoding="utf-8")
        assert "First" in content
        assert "Second" in content
        assert "=" * 60 in content  # separator


# ------------------------------------------------------------------
# Whitelist
# ------------------------------------------------------------------

class TestWhitelist:
    def test_empty(self, storage):
        assert storage.load_whitelist() == []
        assert storage.load_whitelist_ids() == []

    def test_add_and_load(self, storage):
        storage.add_to_whitelist(123, "Test Group")
        wl = storage.load_whitelist()
        assert len(wl) == 1
        assert wl[0]["id"] == 123
        assert storage.load_whitelist_ids() == [123]

    def test_no_duplicate(self, storage):
        storage.add_to_whitelist(123, "Test")
        storage.add_to_whitelist(123, "Test Again")
        assert len(storage.load_whitelist()) == 1

    def test_remove(self, storage):
        storage.add_to_whitelist(1, "A")
        storage.add_to_whitelist(2, "B")
        assert storage.remove_from_whitelist(1) is True
        assert storage.load_whitelist_ids() == [2]
        assert storage.remove_from_whitelist(999) is False


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

class TestConfig:
    def test_load_empty(self, storage):
        assert storage.load_config() == {}

    def test_save_and_load(self, storage):
        storage.save_config({"key": "value"})
        assert storage.load_config() == {"key": "value"}


# ------------------------------------------------------------------
# Personas
# ------------------------------------------------------------------

class TestPersonas:
    def test_default_fallback(self, storage):
        assert storage.load_persona("nonexistent") == ""

    def test_save_and_load(self, storage):
        storage.save_persona("test", "You are helpful.")
        assert storage.load_persona("test") == "You are helpful."

    def test_list(self, storage):
        storage.save_persona("a", "aaa")
        storage.save_persona("b", "bbb")
        names = storage.list_personas()
        assert set(names) == {"a", "b"}

    def test_default_fallback_file(self, storage):
        storage.save_persona("default", "Default persona")
        assert storage.load_persona("nonexistent") == "Default persona"


# ------------------------------------------------------------------
# History
# ------------------------------------------------------------------

class TestHistory:
    def test_load_empty(self, storage):
        assert storage.load_history(999) == []

    def test_save_and_load(self, storage):
        h = [{"role": "user", "content": "hi"}]
        storage.save_history(42, h)
        assert storage.load_history(42) == h

    def test_clear(self, storage):
        storage.save_history(42, [{"role": "user", "content": "hi"}])
        storage.clear_history(42)
        assert storage.load_history(42) == []

    def test_clear_nonexistent(self, storage):
        storage.clear_history(999)  # should not raise


# ------------------------------------------------------------------
# Alerts
# ------------------------------------------------------------------

class TestAlerts:
    def test_empty(self, storage):
        assert storage.load_alerts() == []

    def test_add(self, storage):
        storage.add_alert("urgent", "notify")
        alerts = storage.load_alerts()
        assert len(alerts) == 1
        assert alerts[0]["keyword"] == "urgent"

    def test_no_duplicate(self, storage):
        storage.add_alert("urgent", "notify")
        storage.add_alert("urgent", "other")
        assert len(storage.load_alerts()) == 1


# ------------------------------------------------------------------
# Last digest settings
# ------------------------------------------------------------------

class TestLastDigestSettings:
    def test_load_empty(self, storage):
        assert storage.load_last_digest_settings() == {}

    def test_save_and_load(self, storage):
        settings = {"hours": 24, "all": True, "scope": "all"}
        storage.save_last_digest_settings(settings)
        loaded = storage.load_last_digest_settings()
        assert loaded == settings

    def test_overwrite(self, storage):
        storage.save_last_digest_settings({"hours": 4})
        storage.save_last_digest_settings({"hours": 12, "scope": "chats"})
        loaded = storage.load_last_digest_settings()
        assert loaded["hours"] == 12
        assert loaded["scope"] == "chats"

    def test_corrupt_returns_empty(self, storage):
        path = storage.base_dir / "last_digest.json"
        path.write_text("invalid json", encoding="utf-8")
        assert storage.load_last_digest_settings() == {}
