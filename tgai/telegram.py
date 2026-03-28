"""Telethon wrapper used by the rest of tgai.

The command modules do not talk to Telethon directly.  Instead, they use the
helpers in this module so Telegram-specific conventions stay in one place:
- display-name formatting
- dialog and message retrieval
- unread collection for digests
- session-lock handling
- safety policy around destructive operations
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Suppress noisy telethon messages ("Server sent a very old message...", security warnings)
logging.getLogger("telethon").setLevel(logging.ERROR)

from telethon import TelegramClient, events, functions, types
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
)
from telethon.tl.types import (
    Dialog,
    DialogFilter,
    Message,
    User,
    Chat,
    Channel,
    InputPeerEmpty,
)


def _is_sqlite_readonly_error(exc: BaseException) -> bool:
    """True for SQLite readonly/session write failures from Telethon."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return (
        "readonly database" in text
        or "attempt to write a readonly database" in text
    )


def _entity_display_name(entity: Any) -> str:
    """Return a human-readable display name for any entity (full, for lists)."""
    return _entity_display_name_with_mode(entity, show_username=True)


def _entity_display_name_with_mode(entity: Any, show_username: bool = True) -> str:
    """Return a human-readable display name, optionally hiding @username when name exists."""
    if entity is None:
        return "Неизвестно"
    if isinstance(entity, User):
        parts = []
        if entity.first_name:
            parts.append(entity.first_name)
        if entity.last_name:
            parts.append(entity.last_name)
        name = " ".join(parts) if parts else ""
        if entity.username:
            if not show_username and name:
                return name
            if name:
                return f"{name} (@{entity.username})"
            return f"@{entity.username}"
        return name or str(entity.id)
    if isinstance(entity, (Chat, Channel)):
        t = getattr(entity, "title", None)
        if t:
            return getattr(t, "text", str(t))
        return str(entity.id)
    return str(getattr(entity, "id", entity))


def _entity_short_name(entity: Any) -> str:
    """Return a short name for in-chat display (no username, no last name)."""
    return _entity_short_name_with_mode(entity, show_username=True)


def _entity_short_name_with_mode(entity: Any, show_username: bool = True) -> str:
    """Return a short name for in-chat display, optionally hiding username."""
    if entity is None:
        return "?"
    if isinstance(entity, User):
        if entity.first_name:
            return entity.first_name
        if entity.username:
            if not show_username:
                return str(entity.id)
            return f"@{entity.username}"
        return str(entity.id)
    if isinstance(entity, (Chat, Channel)):
        t = getattr(entity, "title", None)
        if t:
            return getattr(t, "text", str(t))
        return str(entity.id)
    return str(getattr(entity, "id", entity))


def _dialog_display_name(dialog: Dialog) -> str:
    return _entity_display_name(dialog.entity)


def display_name_for_ui(name: str, show_username: bool = False) -> str:
    """Hide trailing username in strings like 'Name (@username)' unless explicitly requested."""
    if show_username:
        return name
    return re.sub(r"\s+\(@[^)]+\)$", "", name).strip()


def is_broadcast_channel(entity: Any) -> bool:
    """True if entity is a broadcast channel (not a megagroup)."""
    return isinstance(entity, Channel) and getattr(entity, "broadcast", False)


class SessionLockedError(RuntimeError):
    """Raised when the Telethon SQLite session is locked by another process."""


class TelegramManager:
    """Thin façade over ``TelegramClient`` with tgai-specific behavior."""

    # Permanently block any attempt to delete messages
    async def delete_messages(self, *args, **kwargs):
        raise PermissionError("tgai: удаление сообщений запрещено")

    async def delete_dialog(self, *args, **kwargs):
        raise PermissionError("tgai: удаление диалогов запрещено")

    def __init__(self, api_id: int, api_hash: str, session_path: str) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self._session_warned_readonly = False
        self._ensure_session_parent()
        self.client: TelegramClient = TelegramClient(
            session_path, api_id, api_hash
        )
        self._me: Optional[User] = None
        self._install_session_guards()

        # Block delete methods on the raw client too
        async def _blocked_delete(*a, **kw):
            raise PermissionError("tgai: удаление сообщений запрещено")
        self.client.delete_messages = _blocked_delete

    def _ensure_session_parent(self) -> None:
        """Create the session directory and eagerly prepare a writable SQLite file."""
        session_file = Path(self.session_path).with_suffix(".session")
        session_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            session_file.touch(exist_ok=True)
        except OSError:
            pass
        for candidate in (
            session_file,
            session_file.with_name(session_file.name + "-wal"),
            session_file.with_name(session_file.name + "-shm"),
        ):
            if candidate.exists():
                try:
                    candidate.chmod(0o600)
                except OSError:
                    pass
        try:
            if not os.access(session_file.parent, os.W_OK):
                raise SessionLockedError(
                    "Каталог ~/.tgai недоступен для записи. "
                    "Проверьте права доступа перед запуском tgai."
                )
        except OSError:
            pass

    def _warn_session_readonly(self) -> None:
        """Print a one-time warning when Telethon cannot persist session entities."""
        if self._session_warned_readonly:
            return
        self._session_warned_readonly = True
        print(
            "\nПредупреждение: Telegram session открыта только для чтения. "
            "Состояние сессии не будет сохраняться. "
            "Закройте другие процессы tgai и при необходимости удалите ~/.tgai/session.session."
        )

    def _install_session_guards(self) -> None:
        """Wrap Telethon session writes so readonly-session errors do not explode in background tasks."""
        session = self.client.session

        original_process_entities = getattr(session, "process_entities", None)
        if callable(original_process_entities):
            def _safe_process_entities(*args, **kwargs):
                try:
                    return original_process_entities(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if _is_sqlite_readonly_error(exc):
                        self._warn_session_readonly()
                        return None
                    raise
            session.process_entities = _safe_process_entities

        original_save = getattr(session, "save", None)
        if callable(original_save):
            def _safe_save(*args, **kwargs):
                try:
                    return original_save(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if _is_sqlite_readonly_error(exc):
                        self._warn_session_readonly()
                        return None
                    raise
            session.save = _safe_save

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect and authenticate the Telethon client.

        Telethon stores session state in SQLite.  When another ``tgai`` process
        still owns that file, surface a dedicated error with a human-readable
        explanation instead of leaking the raw SQLite traceback to the user.
        """
        try:
            await self.client.start()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                raise SessionLockedError(
                    "Сессия Telegram занята другим процессом tgai. "
                    "Закройте или остановите другой экземпляр и попробуйте снова."
                ) from e
            if _is_sqlite_readonly_error(e):
                raise SessionLockedError(
                    "Файл сессии Telegram недоступен для записи. "
                    "Проверьте права на ~/.tgai и при необходимости удалите ~/.tgai/session.session."
                ) from e
            raise
        self._me = await self.client.get_me()

    async def stop(self) -> None:
        """Disconnect from Telegram."""
        try:
            await self.client.disconnect()
        except sqlite3.OperationalError as e:
            if _is_sqlite_readonly_error(e):
                self._warn_session_readonly()
                return
            raise

    async def get_me(self) -> User:
        if self._me is None:
            self._me = await self.client.get_me()
        return self._me

    async def is_authorized(self) -> bool:
        """Check if the client is already authorized with Telegram."""
        if not self.client.is_connected():
            await self.client.connect()
        return await self.client.is_user_authorized()

    # ------------------------------------------------------------------
    # Dialogs / Folders
    # ------------------------------------------------------------------

    async def get_dialogs(
        self,
        folder: Optional[str] = None,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[Dialog]:
        """Return list of dialogs, optionally filtered."""
        dialogs = await self.client.get_dialogs(limit=limit, folder=0)
        result = list(dialogs)

        if folder:
            filters = await self.get_folders()
            matched = next(
                (f for f in filters if f.title.lower() == folder.lower()), None
            )
            if matched:
                folder_dialogs = await self.client.get_dialogs(
                    limit=limit, folder=matched.id
                )
                result = list(folder_dialogs)

        if unread_only:
            result = [d for d in result if d.unread_count > 0]

        return result

    async def get_dialogs_fresh(
        self,
        folder: Optional[str] = None,
        unread_only: bool = False,
        limit: int = 100,
        settle_rounds: int = 2,
        settle_delay: float = 0.08,
    ) -> list[Dialog]:
        """Fetch dialogs with a short settle window so unread counters catch up.

        Telegram/Telethon can lag briefly after mark-as-read or after returning
        from another screen. This helper retries a few times and returns the
        latest snapshot.
        """
        latest: list[Dialog] = []
        rounds = max(1, settle_rounds)
        for idx in range(rounds):
            latest = await self.get_dialogs(folder=folder, unread_only=unread_only, limit=limit)
            if idx + 1 < rounds:
                await asyncio.sleep(settle_delay)
        return latest

    async def get_folders(self) -> list[Any]:
        """Return list of Telegram dialog filters (folders)."""
        result = await self.client(functions.messages.GetDialogFiltersRequest())
        # Newer Telethon returns DialogFilters object with .filters attribute
        filters = getattr(result, "filters", result)
        return [f for f in filters if isinstance(f, DialogFilter)]

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def get_messages(
        self, entity: Any, limit: int = 50, offset_id: int = 0, min_id: int = 0
    ) -> list[Message]:
        """Retrieve messages from a chat, newest first."""
        kwargs: dict[str, Any] = {"limit": limit}
        if offset_id:
            kwargs["offset_id"] = offset_id
        if min_id:
            kwargs["min_id"] = min_id
        messages = await self.client.get_messages(entity, **kwargs)
        return list(messages)

    async def get_unread(
        self,
        hours: int = 24,
        whitelist: Optional[list[int]] = None,
        watermarks: Optional[dict[str, int]] = None,
    ) -> tuple[dict[str, list[Message]], dict[str, list[Message]]]:
        """
        Return unread messages grouped by dialog name.

        The return value is ``(chats, channels)`` so digest generation can keep
        those categories separate.  Before loading message history for a dialog
        we apply several cheap filters:
        - unread count
        - whitelist rules for non-broadcast groups
        - last-message time window
        - digest watermarks to skip dialogs with nothing newer
        """
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        dialogs = await self.client.get_dialogs(limit=200, folder=0)
        chats: dict[str, list[Message]] = {}
        channels: dict[str, list[Message]] = {}
        whitelist_set = set(whitelist or [])
        wm = watermarks or {}

        for dialog in dialogs:
            entity = dialog.entity
            broadcast = is_broadcast_channel(entity)
            is_personal = isinstance(entity, User) and not entity.bot
            is_whitelisted = (
                isinstance(entity, (Chat, Channel))
                and not broadcast
                and dialog.id in whitelist_set
                and dialog.unread_count > 0
            )
            is_channel = broadcast and dialog.unread_count > 0

            if not (is_personal and dialog.unread_count > 0) and not is_whitelisted and not is_channel:
                continue

            last_date = getattr(getattr(dialog, "message", None), "date", None)
            if last_date is not None:
                if last_date.tzinfo is None:
                    last_date = last_date.replace(tzinfo=timezone.utc)
                else:
                    last_date = last_date.astimezone(timezone.utc)
                if last_date < cutoff:
                    continue

            name = _dialog_display_name(dialog)
            min_id = wm.get(name, 0)
            last_msg_id = getattr(getattr(dialog, "message", None), "id", 0) or 0
            if min_id and last_msg_id and last_msg_id <= min_id:
                continue

            try:
                kwargs: dict[str, Any] = {"limit": 50}
                if min_id:
                    kwargs["min_id"] = min_id
                messages = await self.client.get_messages(entity, **kwargs)
                recent = [
                    m
                    for m in messages
                    if m.date and m.date.replace(tzinfo=timezone.utc) >= cutoff
                    and m.text
                ]
                if recent:
                    if broadcast:
                        channels[name] = recent
                    else:
                        chats[name] = recent
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                continue

        return chats, channels

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_message(self, entity: Any, text: str):
        """Send a text message to entity (username, phone, peer, etc.)."""
        return await self.client.send_message(entity, text)

    async def download_media(self, message: Message) -> bytes:
        """Download media from a message and return as bytes."""
        return await self.client.download_media(message, file=bytes)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_messages(
        self,
        query: str,
        entity: Any = None,
        days: int = 7,
    ) -> list[Message]:
        """Search messages in a chat or globally."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        if entity is not None:
            messages = await self.client.get_messages(entity, search=query, limit=50)
        else:
            result = await self.client(
                functions.messages.SearchGlobalRequest(
                    q=query,
                    filter=types.InputMessagesFilterEmpty(),
                    min_date=int(cutoff.timestamp()),
                    max_date=0,
                    offset_rate=0,
                    offset_peer=InputPeerEmpty(),
                    offset_id=0,
                    limit=50,
                )
            )
            messages = getattr(result, "messages", [])
        return [
            m
            for m in messages
            if isinstance(m, Message)
            and m.date
            and m.date.replace(tzinfo=timezone.utc) >= cutoff
        ]

    async def search_contacts(self, query: str) -> list[Any]:
        """Search contacts and dialogs by name or username."""
        found: list[Any] = []
        query_lower = query.lower().lstrip("@")

        # Try username lookup first
        try:
            entity = await self.client.get_entity(query)
            found.append(entity)
        except (ValueError, UsernameNotOccupiedError, UsernameInvalidError):
            pass
        except Exception:
            pass

        # Search in dialogs
        dialogs = await self.client.get_dialogs(limit=200)
        for dialog in dialogs:
            name = _dialog_display_name(dialog).lower()
            username = ""
            if isinstance(dialog.entity, User):
                username = (dialog.entity.username or "").lower()
            if query_lower in name or (username and query_lower in username):
                if not any(
                    getattr(e, "id", None) == getattr(dialog.entity, "id", None)
                    for e in found
                ):
                    found.append(dialog.entity)

        return found

    async def resolve_entity(self, identifier: str) -> Any:
        """Resolve a username, phone number, or display name to an entity."""
        # Try direct resolution
        try:
            return await self.client.get_entity(identifier)
        except (ValueError, TypeError):
            pass
        except Exception:
            pass

        # Search contacts
        results = await self.search_contacts(identifier)
        if results:
            return results[0]

        raise ValueError(f"Не удалось найти контакт: {identifier!r}")

    async def get_entity_by_id(self, chat_id: int) -> Any:
        """Resolve a Telegram entity by numeric chat/user/channel id."""
        return await self.client.get_entity(chat_id)

    # ------------------------------------------------------------------
    # Read status
    # ------------------------------------------------------------------

    async def mark_read(self, entity: Any) -> None:
        """Mark all messages in a dialog as read."""
        await self.client.send_read_acknowledge(entity)

    # ------------------------------------------------------------------
    # Listen / new messages handler
    # ------------------------------------------------------------------

    async def listen(self, callback: Callable) -> None:
        """Register a NewMessage event handler and run until disconnected."""

        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event: events.NewMessage.Event) -> None:
            await callback(event)

        await self.client.run_until_disconnected()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def entity_display_name(self, entity: Any) -> str:
        return _entity_display_name(entity)

    def dialog_display_name(self, dialog: Dialog) -> str:
        return _dialog_display_name(dialog)

    async def get_dialog_by_identifier(self, identifier: str) -> Optional[Dialog]:
        """Find a dialog matching a username, phone, or display name."""
        dialogs = await self.client.get_dialogs(limit=200)
        identifier_lower = identifier.lower().lstrip("@")
        for dialog in dialogs:
            name = _dialog_display_name(dialog).lower()
            username = ""
            if isinstance(dialog.entity, User):
                username = (dialog.entity.username or "").lower()
            if identifier_lower in name or (
                username and identifier_lower == username
            ):
                return dialog
        return None
