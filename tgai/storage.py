"""Local persistence layer for tgai.

The application intentionally stores its state in a simple file tree under
``~/.tgai`` instead of adding a database dependency of its own.  This module
is the single place that knows about those files and their formats.

Stored concerns include:
- provider configuration
- personas
- per-chat LLM history
- digest files and reopen state
- whitelist/alerts
- digest cache and watermarks
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

TGAI_DIR = Path.home() / ".tgai"


class Storage:
    """Manages all persistent data for tgai.

    The class provides a thin, explicit API over small JSON/text files.  The
    rest of the codebase should use these helpers instead of touching
    ``~/.tgai`` directly so storage formats stay easy to evolve.
    """

    def __init__(self, base_dir: Path = TGAI_DIR) -> None:
        self.base_dir = base_dir
        self.personas_dir = base_dir / "personas"
        self.history_dir = base_dir / "history"
        self.digests_dir = base_dir / "digests"
        self.whitelist_path = base_dir / "whitelist.json"
        self.config_path = base_dir / "config.json"
        self._ensure_dirs()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for d in (self.base_dir, self.personas_dir, self.history_dir, self.digests_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def load_config(self) -> dict:
        return self._read_json(self.config_path, {})

    def save_config(self, config: dict) -> None:
        self._write_json(self.config_path, config)

    # ------------------------------------------------------------------
    # Chat history (Claude message history per chat)
    # ------------------------------------------------------------------

    def load_history(self, chat_id: int) -> list[dict]:
        """Load Claude message history for a given chat."""
        path = self.history_dir / f"{chat_id}.json"
        data = self._read_json(path, [])
        return data if isinstance(data, list) else []

    def save_history(self, chat_id: int, history: list[dict]) -> None:
        """Save Claude message history for a given chat."""
        path = self.history_dir / f"{chat_id}.json"
        self._write_json(path, history)

    def clear_history(self, chat_id: int) -> None:
        """Delete Claude message history for a given chat."""
        path = self.history_dir / f"{chat_id}.json"
        if path.exists():
            path.unlink()

    # ------------------------------------------------------------------
    # Personas
    # ------------------------------------------------------------------

    def load_persona(self, name: str) -> str:
        """Load persona text from personas/{name}.txt."""
        path = self.personas_dir / f"{name}.txt"
        if not path.exists():
            # Fall back to default
            default_path = self.personas_dir / "default.txt"
            if default_path.exists():
                return default_path.read_text(encoding="utf-8")
            return ""
        return path.read_text(encoding="utf-8")

    def save_persona(self, name: str, content: str) -> None:
        """Save persona text to personas/{name}.txt."""
        path = self.personas_dir / f"{name}.txt"
        path.write_text(content, encoding="utf-8")

    def list_personas(self) -> list[str]:
        """Return list of available persona names."""
        return [p.stem for p in self.personas_dir.glob("*.txt")]

    # ------------------------------------------------------------------
    # Whitelist (group chat_ids for aggregate)
    # ------------------------------------------------------------------

    def load_whitelist(self) -> list[dict]:
        """Load whitelist entries [{id: int, name: str}]."""
        data = self._read_json(self.whitelist_path, [])
        return data if isinstance(data, list) else []

    def save_whitelist(self, whitelist: list[dict]) -> None:
        """Save whitelist entries."""
        self._write_json(self.whitelist_path, whitelist)

    def load_whitelist_ids(self) -> list[int]:
        """Return just the chat IDs from whitelist."""
        return [entry["id"] for entry in self.load_whitelist() if "id" in entry]

    def add_to_whitelist(self, chat_id: int, name: str) -> None:
        """Add a group to the whitelist if not already present."""
        whitelist = self.load_whitelist()
        existing_ids = {entry["id"] for entry in whitelist}
        if chat_id not in existing_ids:
            whitelist.append({"id": chat_id, "name": name})
            self.save_whitelist(whitelist)

    def remove_from_whitelist(self, chat_id: int) -> bool:
        """Remove a group from whitelist. Returns True if removed."""
        whitelist = self.load_whitelist()
        new_whitelist = [e for e in whitelist if e.get("id") != chat_id]
        if len(new_whitelist) < len(whitelist):
            self.save_whitelist(new_whitelist)
            return True
        return False

    # ------------------------------------------------------------------
    # Keyword alerts
    # ------------------------------------------------------------------

    @property
    def alerts_path(self) -> Path:
        return self.base_dir / "alerts.json"

    def load_alerts(self) -> list[dict]:
        """Load keyword alerts [{keyword, action}]."""
        data = self._read_json(self.alerts_path, [])
        return data if isinstance(data, list) else []

    def save_alerts(self, alerts: list[dict]) -> None:
        self._write_json(self.alerts_path, alerts)

    def add_alert(self, keyword: str, action: str) -> None:
        alerts = self.load_alerts()
        existing = {a["keyword"] for a in alerts}
        if keyword not in existing:
            alerts.append({"keyword": keyword, "action": action})
            self.save_alerts(alerts)

    # ------------------------------------------------------------------
    # Digests
    # ------------------------------------------------------------------

    def save_digest(self, content: str) -> Path:
        """Save a digest snapshot into the per-day digest file.

        Multiple digests produced on the same day are appended to the same
        file with a visual separator so users keep one chronological digest
        log per day.
        """
        now = datetime.now()
        filename = now.strftime("%Y-%m-%d") + ".txt"
        path = self.digests_dir / filename
        # Append if file exists (multiple digests in same day)
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            content = existing + "\n\n" + ("=" * 60) + "\n\n" + content
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Digest watermark — track last-seen message per chat
    # ------------------------------------------------------------------

    @property
    def _watermark_path(self) -> Path:
        return self.base_dir / "digest_watermark.json"

    def load_watermarks(self) -> dict[str, int]:
        """Load {chat_name: last_message_id} mapping."""
        data = self._read_json(self._watermark_path, {})
        return data if isinstance(data, dict) else {}

    def save_watermarks(self, watermarks: dict[str, int]) -> None:
        self._write_json(self._watermark_path, watermarks)

    # ------------------------------------------------------------------
    # Summary cache — {chat_name: summary_text}, persists across runs
    # ------------------------------------------------------------------

    @property
    def _summary_cache_path(self) -> Path:
        return self.base_dir / "summary_cache.json"

    def load_summary_cache(self) -> dict[str, str]:
        data = self._read_json(self._summary_cache_path, {})
        return data if isinstance(data, dict) else {}

    def save_summary_cache(self, cache: dict[str, str]) -> None:
        self._write_json(self._summary_cache_path, cache)

    def load_last_digest_settings(self) -> dict:
        return self._read_json(self.base_dir / "last_digest.json", {})

    def save_last_digest_settings(self, settings: dict) -> None:
        self._write_json(self.base_dir / "last_digest.json", settings)

    # ------------------------------------------------------------------
    # Last digest sections (for instant reopen without network calls)
    # ------------------------------------------------------------------

    def save_last_sections(self, sections: list[dict]) -> None:
        """Persist digest sections for instant reopen.

        Runtime-only objects such as resolved Telegram entities or raw datetime
        instances are stripped before saving.  The reopen path later restores
        what it can from live Telegram state.
        """
        saveable = [
            {k: v for k, v in s.items() if k not in ("entity", "date")}
            for s in sections
        ]
        self._write_json(self.base_dir / "last_sections.json", saveable)

    def load_last_sections(self) -> list[dict]:
        data = self._read_json(self.base_dir / "last_sections.json", [])
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Listen pending queue
    # ------------------------------------------------------------------

    @property
    def _listen_pending_path(self) -> Path:
        return self.base_dir / "listen_pending.json"

    def load_listen_pending(self) -> list[dict]:
        data = self._read_json(self._listen_pending_path, [])
        return data if isinstance(data, list) else []

    def save_listen_pending(self, pending: list[dict]) -> None:
        self._write_json(self._listen_pending_path, pending)

    # ------------------------------------------------------------------
    # Privacy cleanup
    # ------------------------------------------------------------------

    def clear_user_local_state(self, preserve_telegram_app: bool = True) -> None:
        """Delete local user data, session files, caches, and provider secrets.

        When ``preserve_telegram_app`` is true, Telegram application registration
        (``api_id`` / ``api_hash``) and non-sensitive defaults stay in
        ``config.json``.  Provider entries that require secrets are removed.
        """
        for path in (
            self.history_dir,
            self.digests_dir,
            self.base_dir / "summary_cache.json",
            self.base_dir / "digest_watermark.json",
            self.base_dir / "last_digest.json",
            self.base_dir / "last_sections.json",
            self.base_dir / "listen_pending.json",
            self.base_dir / "alerts.json",
            self.base_dir / "whitelist.json",
        ):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

        for name in ("session.session", "session.session-wal", "session.session-shm"):
            session_path = self.base_dir / name
            if session_path.exists():
                try:
                    session_path.unlink()
                except OSError:
                    pass

        config = self.load_config()
        if preserve_telegram_app:
            config = {
                "telegram": {
                    "api_id": config.get("telegram", {}).get("api_id"),
                    "api_hash": config.get("telegram", {}).get("api_hash"),
                },
                "defaults": config.get("defaults", {}),
                "llm": [],
            }
        else:
            config = {
                "telegram": {"api_id": None, "api_hash": None},
                "defaults": config.get("defaults", {}),
                "llm": [],
            }
        self.save_config(config)
        self._ensure_dirs()
