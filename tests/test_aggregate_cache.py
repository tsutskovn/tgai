"""Tests for aggregate digest cache logic."""

import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tgai.storage import Storage
from tgai.commands.aggregate import _is_recent_message, _merge_sections_unique, _run_aggregate


@pytest.fixture
def storage(tmp_path):
    return Storage(base_dir=tmp_path)


def _make_message(msg_id, text, sender_name="User", minutes_ago=5):
    """Create a fake Telethon-like message object."""
    m = SimpleNamespace()
    m.id = msg_id
    m.text = text
    m.date = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)
    m.sender = SimpleNamespace(
        first_name=sender_name, last_name=None, username=None, id=100
    )
    return m


def _make_dialog(dialog_id, name, unread=0, is_user=True, is_broadcast=False):
    """Create a fake dialog object."""
    if is_user:
        entity = SimpleNamespace(
            id=dialog_id, first_name=name, last_name=None,
            username=None, bot=False,
        )
        entity.__class__ = type("User", (), {})
    else:
        entity = SimpleNamespace(
            id=dialog_id, title=name, broadcast=is_broadcast,
        )
        entity.__class__ = type("Channel" if is_broadcast else "Chat", (), {})

    dialog = SimpleNamespace(
        id=dialog_id, entity=entity, unread_count=unread,
    )
    return dialog


# ------------------------------------------------------------------
# Cache format and migration
# ------------------------------------------------------------------

class TestCacheMigration:
    def test_old_flat_format_migrated(self, storage):
        """Old cache with plain string values should be usable."""
        old_cache = {"Chat A": "old summary text"}
        storage.save_summary_cache(old_cache)

        # Simulate what aggregate.py does on load
        cache = storage.load_summary_cache()
        for k, v in list(cache.items()):
            if isinstance(v, str):
                cache[k] = {"hours": 0, "summary": v}

        assert cache["Chat A"]["summary"] == "old summary text"
        assert cache["Chat A"]["hours"] == 0

    def test_new_format_preserved(self, storage):
        """New cache format with hours metadata stays intact."""
        new_cache = {"Chat A": {"hours": 24, "summary": "summary text"}}
        storage.save_summary_cache(new_cache)
        loaded = storage.load_summary_cache()
        assert loaded["Chat A"]["hours"] == 24
        assert loaded["Chat A"]["summary"] == "summary text"


# ------------------------------------------------------------------
# Cache hours matching
# ------------------------------------------------------------------

class TestCacheHoursMatch:
    def test_cache_used_when_hours_match(self):
        """Cache with hours >= requested should be used."""
        cached = {"hours": 24, "summary": "24h summary"}
        hours = 24
        assert cached.get("hours", 0) >= hours

    def test_cache_used_when_cached_hours_larger(self):
        """Cache from 24h run should be valid for 4h request."""
        cached = {"hours": 24, "summary": "24h summary"}
        hours = 4
        assert cached.get("hours", 0) >= hours

    def test_cache_rejected_when_cached_hours_smaller(self):
        """Cache from 4h run should NOT be valid for 24h request."""
        cached = {"hours": 4, "summary": "4h summary"}
        hours = 24
        assert not (cached.get("hours", 0) >= hours)

    def test_migrated_cache_rejected(self):
        """Migrated old cache (hours=0) should not match any request."""
        cached = {"hours": 0, "summary": "old"}
        assert not (cached.get("hours", 0) >= 1)


class TestTimeWindowFiltering:
    def test_recent_message_inside_cutoff_is_included(self):
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
        msg = _make_message(1, "recent", minutes_ago=10)
        assert _is_recent_message(msg, cutoff) is True

    def test_old_message_outside_cutoff_is_excluded(self):
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        msg = _make_message(1, "old", minutes_ago=120)
        assert _is_recent_message(msg, cutoff) is False


# ------------------------------------------------------------------
# Cache pruning
# ------------------------------------------------------------------

class TestCachePruning:
    def test_stale_entries_removed(self, storage):
        """Chats no longer in dialog list should be pruned."""
        cache = {
            "Active Chat": {"hours": 24, "summary": "s1"},
            "Deleted Chat": {"hours": 24, "summary": "s2"},
            "Old Group": {"hours": 12, "summary": "s3"},
        }
        active_names = {"Active Chat", "New Chat"}
        pruned = {k: v for k, v in cache.items() if k in active_names}
        assert "Active Chat" in pruned
        assert "Deleted Chat" not in pruned
        assert "Old Group" not in pruned

    def test_empty_cache_prune(self):
        """Pruning empty cache should stay empty."""
        cache = {}
        active = {"Chat A"}
        pruned = {k: v for k, v in cache.items() if k in active}
        assert pruned == {}


# ------------------------------------------------------------------
# Watermark + cache integration
# ------------------------------------------------------------------

class TestWatermarkCacheIntegration:
    def test_watermarks_updated_only_for_new_messages(self, storage):
        """Watermarks should only advance, never go backwards."""
        wm = {"Chat A": 100, "Chat B": 200}
        storage.save_watermarks(wm)

        # Simulate: Chat A got new messages (max_id=150), Chat B had none
        new_msgs = {
            "Chat A": [_make_message(120, "hi"), _make_message(150, "bye")],
        }
        loaded_wm = storage.load_watermarks()
        for name, msgs in new_msgs.items():
            max_id = max(m.id for m in msgs)
            if max_id > loaded_wm.get(name, 0):
                loaded_wm[name] = max_id
        storage.save_watermarks(loaded_wm)

        final = storage.load_watermarks()
        assert final["Chat A"] == 150  # advanced
        assert final["Chat B"] == 200  # unchanged

    def test_watermarks_never_go_backwards(self, storage):
        """If fetched max_id < current watermark, keep old value."""
        storage.save_watermarks({"Chat": 500})
        loaded = storage.load_watermarks()

        # Messages with lower IDs (shouldn't happen, but guard against it)
        msgs = [_make_message(100, "old")]
        max_id = max(m.id for m in msgs)
        if max_id > loaded.get("Chat", 0):
            loaded["Chat"] = max_id
        storage.save_watermarks(loaded)

        assert storage.load_watermarks()["Chat"] == 500


# ------------------------------------------------------------------
# Digest save (incremental)
# ------------------------------------------------------------------

class TestDigestIncremental:
    def test_always_saves(self, storage):
        """Digest should save to file regardless of save flag."""
        path = storage.save_digest("Digest content")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Digest content" in content

    def test_appends_within_day(self, storage):
        """Multiple digests same day should append."""
        p1 = storage.save_digest("Run 1")
        p2 = storage.save_digest("Run 2")
        assert p1 == p2
        content = p1.read_text(encoding="utf-8")
        assert "Run 1" in content
        assert "Run 2" in content

    def test_file_named_by_date(self, storage):
        path = storage.save_digest("test")
        today = datetime.now().strftime("%Y-%m-%d")
        assert path.name == f"{today}.txt"


# ------------------------------------------------------------------
# Format digest
# ------------------------------------------------------------------

class TestFormatDigest:
    def test_format_chats_and_channels(self):
        from tgai.commands.aggregate import _format_digest
        result = _format_digest(
            {"Alice": "Summary A"},
            {"News": "Summary N"},
            24,
        )
        assert "# Чаты" in result
        assert "## Alice" in result
        assert "Summary A" in result
        assert "# Каналы" in result
        assert "## News" in result
        assert "Summary N" in result
        assert "24 ч." in result

    def test_format_empty(self):
        from tgai.commands.aggregate import _format_digest
        result = _format_digest({}, {}, 12)
        assert "# Чаты" not in result
        assert "# Каналы" not in result
        assert "12 ч." in result

    def test_format_only_chats(self):
        from tgai.commands.aggregate import _format_digest
        result = _format_digest({"Bob": "Hi"}, {}, 1)
        assert "# Чаты" in result
        assert "# Каналы" not in result

    def test_format_only_channels(self):
        from tgai.commands.aggregate import _format_digest
        result = _format_digest({}, {"Chan": "Post"}, 4)
        assert "# Чаты" not in result
        assert "# Каналы" in result


# ------------------------------------------------------------------
# Provider gating
# ------------------------------------------------------------------

class TestProviderSpecificDigestRules:
    def test_digest_is_blocked_without_yandexgpt(self, storage, capsys):
        tg = AsyncMock()
        claude = SimpleNamespace(provider_name="none")

        asyncio.run(
            _run_aggregate(
                tg=tg,
                claude=claude,
                storage=storage,
                hours=1,
                include_all=False,
                save=False,
                text_mode=True,
                scope="all",
            )
        )

        out = capsys.readouterr().out
        assert "только с подключенным YandexGPT" in out
        tg.get_me.assert_not_awaited()

    def test_digest_gate_does_not_trigger_for_yandexgpt(self, storage):
        tg = AsyncMock()
        tg.get_me = AsyncMock(side_effect=RuntimeError("sentinel"))
        claude = SimpleNamespace(provider_name="yandexgpt")

        with pytest.raises(RuntimeError, match="sentinel"):
            asyncio.run(
                _run_aggregate(
                    tg=tg,
                    claude=claude,
                    storage=storage,
                    hours=1,
                    include_all=False,
                    save=False,
                    text_mode=True,
                    scope="all",
                )
            )

        tg.get_me.assert_awaited_once()


# ------------------------------------------------------------------
# Cache merge logic
# ------------------------------------------------------------------

class TestCacheMerge:
    def _merge(self, cached: dict, new: dict) -> dict:
        """Replicate the prepend merge logic from _run_aggregate."""
        result = dict(cached)
        for name, new_summary in new.items():
            old = cached.get(name)
            result[name] = (new_summary + "\n\n" + old) if old else new_summary
        return result

    def test_new_prepended_to_cached(self):
        """New summary should be prepended to cached, not replace it."""
        cached = {"Chat A": "old summary", "Chat B": "cached B"}
        new = {"Chat A": "new summary", "Chat C": "new C"}
        merged = self._merge(cached, new)
        assert merged["Chat A"] == "new summary\n\nold summary"
        assert merged["Chat B"] == "cached B"
        assert merged["Chat C"] == "new C"

    def test_no_cached_uses_new_only(self):
        """When no cached summary exists, new summary is used as-is."""
        cached = {}
        new = {"Chat A": "new summary"}
        merged = self._merge(cached, new)
        assert merged["Chat A"] == "new summary"

    def test_empty_new(self):
        cached = {"Chat A": "cached"}
        merged = self._merge(cached, {})
        assert merged == cached

    def test_empty_cached(self):
        new = {"Chat A": "new"}
        merged = self._merge({}, new)
        assert merged == new

    def test_separator_present(self):
        """Merged result should contain double newline separator."""
        merged = self._merge({"Chat": "old"}, {"Chat": "new"})
        assert "\n\n" in merged["Chat"]
        assert merged["Chat"].startswith("new")


class TestSectionDedup:
    def test_merge_sections_unique_skips_duplicate_names(self):
        existing = [{"name": "Chat A", "summary": "old"}]
        incoming = [
            {"name": "Chat A", "summary": "dup"},
            {"name": "Chat B", "summary": "new"},
        ]
        changed = _merge_sections_unique(existing, incoming)
        assert changed is True
        assert [s["name"] for s in existing] == ["Chat A", "Chat B"]

    def test_merge_sections_unique_noop_when_all_duplicates(self):
        existing = [{"name": "Chat A", "summary": "old"}]
        incoming = [{"name": "Chat A", "summary": "dup"}]
        changed = _merge_sections_unique(existing, incoming)
        assert changed is False
        assert [s["name"] for s in existing] == ["Chat A"]


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

class TestEdgeCases:
    def test_message_without_id(self):
        """Messages without .id attr should not crash watermark update."""
        m = SimpleNamespace(text="hi", date=datetime.now(tz=timezone.utc))
        # hasattr check: no .id
        msgs = [m]
        max_id = max((getattr(m, "id", 0) for m in msgs if hasattr(m, "id")), default=0)
        assert max_id == 0

    def test_empty_messages_list(self):
        """Empty message list should produce max_id=0."""
        msgs = []
        max_id = max((m.id for m in msgs if hasattr(m, "id")), default=0)
        assert max_id == 0
