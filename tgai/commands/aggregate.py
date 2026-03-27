"""Digest generation and digest viewer lifecycle.

This module owns the pipeline for:
- collecting candidate dialogs
- applying unread/time-window/scope filters
- summarizing grouped messages through the active LLM provider
- persisting digest state for instant reopen
- live-updating an open digest with new summaries and unread counters
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _to_utc(dt: datetime | None) -> datetime | None:
    """Normalize datetime to UTC for consistent comparisons."""
    if dt is None:
        return None
    from datetime import timezone
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_recent_message(msg: Any, cutoff: datetime | None) -> bool:
    """True when the message has text and is inside the requested time window."""
    if not getattr(msg, "text", None):
        return False
    if cutoff is None:
        return True
    msg_date = _to_utc(getattr(msg, "date", None))
    return msg_date is not None and msg_date >= cutoff


def _merge_sections_unique(existing: list[dict], incoming: list[dict]) -> bool:
    """Append only truly new sections, deduplicating by section name."""
    existing_names = {sec.get("name", "") for sec in existing}
    added = False
    for sec in incoming:
        name = sec.get("name", "")
        if not name or name in existing_names:
            continue
        existing.append(sec)
        existing_names.add(name)
        added = True
    return added


def _sort_digest_sections(sections: list[dict]) -> None:
    """Unread sections first, then newest activity first inside each group."""
    from datetime import timezone

    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    sections.sort(key=lambda s: s.get("date") or min_dt, reverse=True)
    sections.sort(key=lambda s: 0 if s.get("unread_count", 0) > 0 else 1)


def _normalize_summary_text(text: str) -> str:
    """Keep digest summaries compact and strip legacy 'Важно:' lines."""
    if not text:
        return ""
    lines = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("важно:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


async def _run_digest_viewer(loop, viewer_fn, sections: list[dict], app_holder: list):
    """Open the digest viewer and retry once on an immediate empty close.

    In practice prompt_toolkit can occasionally close a freshly created viewer
    immediately after start.  Retrying once smooths over that UX glitch
    without changing any actual digest semantics.
    """
    for _attempt in range(2):
        started = time.monotonic()
        chosen = await loop.run_in_executor(
            None, lambda s=sections: viewer_fn(s, app_holder=app_holder)
        )
        if chosen is not None or (time.monotonic() - started) > 0.35:
            return chosen
        app_holder.clear()
    return None


async def _collect_new_digest_sections(
    tg,
    claude_client,
    storage,
    sections: list,
    me_id: int,
    active_watermarks: dict[str, int],
    hours: int | None,
    include_all: bool,
    scope: str,
) -> tuple[list[dict], dict[str, int]]:
    """Discover dialogs that were not part of the current digest yet.

    This is used both while an already-open digest is live-updating and when an
    old digest is reopened from saved sections.  Any newly eligible dialog gets
    summarized into a full digest section and receives a fresh watermark.
    """
    if hours is None:
        return [], active_watermarks

    from telethon.tl.types import User, Chat, Channel
    from tgai.telegram import _dialog_display_name, _entity_display_name_with_mode, is_broadcast_channel
    from datetime import timedelta, timezone

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    whitelist_ids = set(storage.load_whitelist_ids())
    known_names = {sec.get("name", "") for sec in sections}
    dialogs = await tg.get_dialogs_fresh(limit=200)
    created: list[dict] = []

    for dialog in dialogs:
        name = _dialog_display_name(dialog)
        if name in known_names:
            continue

        entity = dialog.entity
        broadcast = is_broadcast_channel(entity)
        if scope == "chats" and broadcast:
            continue
        if scope == "channels" and not broadcast:
            continue

        is_personal = isinstance(entity, User) and not entity.bot
        is_group = isinstance(entity, (Chat, Channel)) and not broadcast
        if include_all:
            last_date = _to_utc(getattr(getattr(dialog, "message", None), "date", None))
            if last_date is None or last_date < cutoff:
                continue
            eligible = is_personal or (is_group and dialog.id in whitelist_ids) or broadcast
        else:
            eligible = (
                (is_personal and dialog.unread_count > 0)
                or (is_group and dialog.id in whitelist_ids)
                or (broadcast and dialog.unread_count > 0)
            )
        if not eligible:
            continue

        baseline_id = active_watermarks.get(name, 0)
        msgs = await tg.get_messages(entity, limit=50, min_id=baseline_id)
        recent_msgs = [m for m in msgs if _is_recent_message(m, cutoff)]
        if not recent_msgs:
            probe_id = max((getattr(m, "id", 0) for m in msgs), default=baseline_id)
            if probe_id > baseline_id:
                active_watermarks[name] = probe_id
            continue

        msg_dicts = []
        for m in reversed(recent_msgs[:claude_client.max_history]):
            sender_id = getattr(m, "sender_id", None)
            if sender_id == me_id:
                sender_name = "Вы"
            else:
                sender = getattr(m, "sender", None)
                sender_name = _entity_display_name_with_mode(sender, show_username=False) if sender else "?"
            msg_dicts.append({
                "sender": sender_name,
                "text": m.text,
                "date": m.date.strftime("%H:%M") if m.date else "",
            })
        if not msg_dicts:
            continue

        summary = await asyncio.get_running_loop().run_in_executor(
            None, lambda d=msg_dicts: claude_client.summarize_messages(d)
        )
        section = {
            "name": name,
            "summary": summary,
            "is_channel": broadcast,
            "entity": entity,
            "date": max(_to_utc(getattr(m, "date", None)) for m in recent_msgs if getattr(m, "date", None)),
            "unread_count": getattr(dialog, "unread_count", 0),
            "_poll_last_id": max(m.id for m in recent_msgs),
        }
        created.append(section)
        active_watermarks[name] = section["_poll_last_id"]
        known_names.add(name)

    return created, active_watermarks


async def _poll_digest_updates(
    tg, claude_client, sections: list, app_holder: list, watermarks: dict = None,
    storage: Any = None, hours: int | None = None, include_all: bool = False, scope: str = "all",
) -> None:
    """Background: watch for new messages and update digest sections live.

    If watermarks are provided, use them as baselines so messages that arrived
    since the digest was built are picked up on the first poll cycle.
    """
    loop = asyncio.get_running_loop()
    me = await tg.get_me()
    me_id = me.id
    active_watermarks = dict(watermarks or {})
    poll_tick = 0

    while True:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break

        poll_tick += 1
        app = app_holder[0] if app_holder else None
        updated = False
        cutoff = None
        unread_map: dict[str, int] = {}
        if hours is not None:
            from datetime import timedelta, timezone
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        try:
            from tgai.telegram import _dialog_display_name
            dialogs = await tg.get_dialogs(limit=200)
            unread_map = {
                _dialog_display_name(d): getattr(d, "unread_count", 0)
                for d in dialogs
            }
        except Exception:
            pass

        unread_updated = False
        for sec in sections:
            entity = sec.get("entity")
            if not entity:
                continue
            name = sec.get("name", "")
            current_unread = unread_map.get(name)
            if current_unread is not None and sec.get("unread_count", 0) != current_unread:
                sec["unread_count"] = current_unread
                updated = True
                unread_updated = True

        if unread_updated:
            _sort_digest_sections(sections)
            if storage is not None:
                storage.save_last_sections(sections)
            if app is not None:
                try:
                    if app.is_running:
                        app.invalidate()
                except Exception:
                    pass

        for sec in sections:
            entity = sec.get("entity")
            if not entity:
                continue
            try:
                last_id = sec.get("_poll_last_id")

                if last_id is None:
                    last_id = active_watermarks.get(name)
                    if last_id is None:
                        # Bootstrap: remember current newest ID, nothing to show yet
                        probe = await tg.get_messages(entity, limit=1)
                        if probe:
                            sec["_poll_last_id"] = probe[0].id
                        continue
                    sec["_poll_last_id"] = last_id

                # Fetch ALL messages newer than last_id (no cap on count)
                new_msgs = await tg.get_messages(entity, limit=200, min_id=last_id)
                new_msgs = [m for m in new_msgs if _is_recent_message(m, cutoff)]
                if not new_msgs:
                    continue

                sec["_poll_last_id"] = max(m.id for m in new_msgs)
                active_watermarks[sec.get("name", "")] = sec["_poll_last_id"]
                from tgai.telegram import _entity_display_name_with_mode
                msg_dicts = []
                for m in reversed(new_msgs[:5]):
                    sender_id = getattr(m, "sender_id", None)
                    if sender_id == me_id:
                        sender_name = "Вы"
                    else:
                        sender = getattr(m, "sender", None)
                        sender_name = _entity_display_name_with_mode(sender, show_username=False) if sender else "?"
                    msg_dicts.append({
                        "sender": sender_name,
                        "text": m.text,
                        "date": m.date.strftime("%H:%M") if m.date else "",
                    })
                if msg_dicts:
                    try:
                        new_part = await loop.run_in_executor(
                            None, lambda d=msg_dicts: claude_client.summarize_messages(d)
                        )
                        sec["summary"] = _normalize_summary_text(new_part)
                        if new_msgs:
                            sec["date"] = max(_to_utc(getattr(m, "date", None)) for m in new_msgs if getattr(m, "date", None))
                        updated = True
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        if storage is not None and hours is not None and poll_tick % 3 == 0:
            try:
                new_sections, active_watermarks = await _collect_new_digest_sections(
                    tg, claude_client, storage, sections, me_id,
                    active_watermarks, hours, include_all, scope,
                )
                if _merge_sections_unique(sections, new_sections):
                    updated = True
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        if updated:
            _sort_digest_sections(sections)
            if storage is not None:
                storage.save_last_sections(sections)
                storage.save_watermarks(active_watermarks)
        if updated and app is not None:
            try:
                if app.is_running:
                    app.invalidate()
            except Exception:
                pass


def _format_digest(
    summaries: dict[str, str],
    channel_summaries: dict[str, str],
    hours: int,
    unread_counts: dict[str, int] | None = None,
) -> str:
    """Format the full digest string with separate sections for chats and channels."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    unread_counts = unread_counts or {}
    lines = [
        f"Дайджест сообщений — {now}",
        f"Период: последние {hours} ч.",
        "=" * 60,
        "",
    ]
    if summaries:
        lines.append("# Чаты")
        lines.append("")
        for chat_name, summary in summaries.items():
            unread = unread_counts.get(chat_name, 0)
            suffix = f" [{unread} непроч.]" if unread > 0 else ""
            lines.append(f"## {chat_name}{suffix}")
            lines.append(summary)
            lines.append("")
    if channel_summaries:
        lines.append("# Каналы")
        lines.append("")
        for ch_name, summary in channel_summaries.items():
            unread = unread_counts.get(ch_name, 0)
            suffix = f" [{unread} непроч.]" if unread > 0 else ""
            lines.append(f"## {ch_name}{suffix}")
            lines.append(summary)
            lines.append("")
    return "\n".join(lines)


async def _run_aggregate(
    tg,
    claude,
    storage,
    hours: int,
    include_all: bool,
    save: bool,
    text_mode: bool,
    scope: str = "all",
    listen_collector=None,
) -> None:
    """Build a digest and optionally open it interactively.

    ``scope`` controls whether we include chats, channels, or both.  The
    function handles the entire digest lifecycle:
    - read cache and watermarks
    - collect relevant messages
    - summarize fresh content
    - merge with allowed cached summaries
    - save digest text and fast-reopen sections
    - open the interactive viewer with live polling
    """
    if getattr(claude, "provider_name", "") != "yandexgpt":
        print(
            "Дайджесты и суммаризация доступны только с подключенным YandexGPT."
        )
        return

    me = await tg.get_me()
    loop = asyncio.get_running_loop()

    _should_refresh = True
    while _should_refresh:
        _should_refresh = False
        whitelist_ids = storage.load_whitelist_ids()
        watermarks = storage.load_watermarks()
        summary_cache = storage.load_summary_cache()
        # Migrate old flat format if needed
        for k, v in list(summary_cache.items()):
            if isinstance(v, str):
                summary_cache[k] = {"hours": 0, "summary": v}

        print(f"Загружаю сообщения за последние {hours} ч...\n")

        if include_all:
            from telethon.tl.types import User, Chat, Channel
            from telethon.errors import FloodWaitError
            from tgai.telegram import _dialog_display_name, is_broadcast_channel
            from datetime import timezone
            import datetime as dt_module

            messages_by_chat: dict[str, list] = {}
            messages_by_channel: dict[str, list] = {}
            cached_chat_summaries: dict[str, str] = {}
            cached_channel_summaries: dict[str, str] = {}

            whitelist_set = set(whitelist_ids)
            cutoff = datetime.now(tz=timezone.utc) - dt_module.timedelta(hours=hours)

            dialogs = await tg.get_dialogs_fresh(limit=200)
            candidates = []
            for dialog in dialogs:
                entity = dialog.entity
                broadcast = is_broadcast_channel(entity)
                if scope == "chats" and broadcast:
                    continue
                if scope == "channels" and not broadcast:
                    continue
                is_personal = isinstance(entity, User) and not entity.bot
                is_group = isinstance(entity, (Chat, Channel)) and not broadcast
                if is_personal or (is_group and dialog.id in whitelist_set) or broadcast:
                    last_date = getattr(dialog.message, 'date', None)
                    if last_date:
                        if last_date.tzinfo is None:
                            last_date = last_date.replace(tzinfo=timezone.utc)
                        if last_date < cutoff:
                            continue
                    candidates.append((dialog, broadcast))

            print(f"Кандидатов: {len(candidates)}. Проверяю новые сообщения...")
            print(f"Из кэша: 0, загружу с API: {len(candidates)}")

            for i, (dialog, broadcast) in enumerate(candidates, 1):
                name = _dialog_display_name(dialog)
                print(f"  [{i}/{len(candidates)}] {name}...", end="", flush=True)
                try:
                    msgs = await tg.get_messages(dialog.entity, limit=50)
                    recent = [
                        m for m in msgs if _is_recent_message(m, cutoff)
                    ]
                    if recent:
                        if broadcast:
                            messages_by_channel[name] = recent
                        else:
                            messages_by_chat[name] = recent
                        print(f" {len(recent)} новых сообщ.")
                    else:
                        print(" вне окна")
                except FloodWaitError as e:
                    print(f" FloodWait {e.seconds}с, жду...")
                    await asyncio.sleep(e.seconds + 1)
                except Exception:
                    print(" ошибка")
                    continue
        else:
            messages_by_chat, messages_by_channel = await tg.get_unread(
                hours=hours, whitelist=whitelist_ids, watermarks=watermarks
            )
            # In unread-only mode, only use cache for chats that still have
            # unread messages (were returned by get_unread or have new content).
            # Don't show cached summaries for chats that have been read since.
            from tgai.telegram import _dialog_display_name
            try:
                _dialogs_for_cache = await tg.get_dialogs_fresh(limit=200)
                _unread_names = set()
                for d in _dialogs_for_cache:
                    if d.unread_count > 0:
                        _unread_names.add(_dialog_display_name(d))
            except Exception:
                _unread_names = set()  # if can't check, exclude all cached

            cached_chat_summaries = {}
            cached_channel_summaries = {}
            for name, cached in summary_cache.items():
                if (name not in messages_by_chat
                        and name not in messages_by_channel
                        and isinstance(cached, dict)
                        and cached.get("hours", 0) >= hours):
                    # Skip if chat has been read (no longer unread)
                    if name not in _unread_names:
                        continue
                    if cached.get("is_channel", False):
                        cached_channel_summaries[name] = cached["summary"]
                    else:
                        cached_chat_summaries[name] = cached["summary"]

        # Apply scope filter for unread-only mode.
        # include_all is filtered earlier, before any API calls, so the logs stay honest.
        if not include_all:
            if scope == "chats":
                messages_by_channel = {}
                cached_channel_summaries = {}
            elif scope == "channels":
                messages_by_chat = {}
                cached_chat_summaries = {}

        total = len(messages_by_chat) + len(messages_by_channel)
        has_cached = len(cached_chat_summaries) + len(cached_channel_summaries)

        if not total and not has_cached:
            print("Нет новых сообщений.")
            return

        if total:
            print(f"Суммаризую {len(messages_by_chat)} чатов и {len(messages_by_channel)} каналов...\n")

        def _summarize_batch(batch: dict[str, list]) -> dict[str, str]:
            from tgai.telegram import _entity_display_name_with_mode
            result: dict[str, str] = {}
            for chat_name, messages in batch.items():
                msg_dicts = []
                for m in reversed(messages[-claude.max_history:]):
                    if not getattr(m, "text", None):
                        continue
                    sender_id = getattr(m, "sender_id", None)
                    if sender_id == me.id:
                        sender_name = "Вы"
                    else:
                        sender = getattr(m, "sender", None)
                        sender_name = _entity_display_name_with_mode(sender, show_username=False) if sender else "?"
                    date_str = m.date.strftime("%H:%M") if m.date else ""
                    msg_dicts.append({"sender": sender_name, "text": m.text, "date": date_str})
                if not msg_dicts:
                    continue
                print(f"  Суммаризую: {chat_name}...")
                try:
                    summary = claude.summarize_messages(msg_dicts)
                except Exception as e:
                    from tgai.claude import InsufficientCreditsError
                    if isinstance(e, InsufficientCreditsError):
                        raise
                    summary = f"Ошибка суммаризации: {e}"
                result[chat_name] = summary
            return result

        new_summaries = _summarize_batch(messages_by_chat)
        new_channel_summaries = _summarize_batch(messages_by_channel)

        # Merge: prepend new summary to cached if both exist
        summaries = {name: _normalize_summary_text(text) for name, text in cached_chat_summaries.items()}
        for name, new in new_summaries.items():
            summaries[name] = _normalize_summary_text(new)

        channel_summaries = {name: _normalize_summary_text(text) for name, text in cached_channel_summaries.items()}
        for name, new in new_channel_summaries.items():
            channel_summaries[name] = _normalize_summary_text(new)

        fresh = len(new_summaries) + len(new_channel_summaries)
        cached_count = len(cached_chat_summaries) + len(cached_channel_summaries)
        if fresh or cached_count:
            print(f"\nДайджест: {fresh} новых, {cached_count} из кэша.")

        if not summaries and not channel_summaries:
            print("Нет текстовых сообщений для дайджеста.")
            return

        for name, text in new_summaries.items():
            summary_cache[name] = {"hours": hours, "summary": text, "is_channel": False}
        for name, text in new_channel_summaries.items():
            summary_cache[name] = {"hours": hours, "summary": text, "is_channel": True}
        if include_all:
            active_names = {_dialog_display_name(d) for d, _ in candidates}
            summary_cache = {k: v for k, v in summary_cache.items() if k in active_names}
        storage.save_summary_cache(summary_cache)

        for name, msgs in {**messages_by_chat, **messages_by_channel}.items():
            max_id = max((m.id for m in msgs if hasattr(m, "id")), default=0)
            if max_id > watermarks.get(name, 0):
                watermarks[name] = max_id
        storage.save_watermarks(watermarks)

        unread_map: dict[str, int] = {}
        if include_all:
            from tgai.telegram import _dialog_display_name
            for d in dialogs:
                unread_map[_dialog_display_name(d)] = getattr(d, "unread_count", 0)
        else:
            try:
                all_dialogs = await tg.get_dialogs_fresh(limit=200)
                from tgai.telegram import _dialog_display_name
                for d in all_dialogs:
                    unread_map[_dialog_display_name(d)] = getattr(d, "unread_count", 0)
            except Exception:
                pass

        digest_text = _format_digest(summaries, channel_summaries, hours, unread_map)
        path = storage.save_digest(digest_text)
        if save:
            print(f"\nДайджест сохранён: {path}")

        # Build entity map
        entity_map: dict[str, Any] = {}
        if include_all:
            from tgai.telegram import _dialog_display_name
            for d in dialogs:
                entity_map[_dialog_display_name(d)] = d.entity
        else:
            try:
                all_dialogs = await tg.get_dialogs_fresh(limit=200)
                from tgai.telegram import _dialog_display_name
                for d in all_dialogs:
                    entity_map[_dialog_display_name(d)] = d.entity
            except Exception:
                pass

        # Build date map for sorting (from messages + dialogs for cached)
        from datetime import timezone as _tz
        date_map: dict[str, datetime] = {}
        for name, msgs in {**messages_by_chat, **messages_by_channel}.items():
            dates = [m.date for m in msgs if getattr(m, "date", None)]
            if dates:
                d = max(dates)
                date_map[name] = d.replace(tzinfo=_tz.utc) if d.tzinfo is None else d
        # For cached sections without messages, use dialog's last message date
        try:
            _sort_dialogs = await tg.get_dialogs_fresh(limit=200)
            from tgai.telegram import _dialog_display_name as _ddn2
            for d in _sort_dialogs:
                dname = _ddn2(d)
                if dname not in date_map:
                    msg_date = getattr(getattr(d, 'message', None), 'date', None)
                    if msg_date:
                        date_map[dname] = msg_date.replace(tzinfo=_tz.utc) if msg_date.tzinfo is None else msg_date
        except Exception:
            pass

        def _section_date(name: str) -> datetime:
            d = date_map.get(name)
            if d is None:
                return datetime.min.replace(tzinfo=_tz.utc)
            return d

        sections = []
        for name, summary in summaries.items():
            sections.append({
                "name": name, "summary": summary, "is_channel": False,
                "entity": entity_map.get(name), "date": _section_date(name),
                "unread_count": unread_map.get(name, 0),
            })
        for name, summary in channel_summaries.items():
            sections.append({
                "name": name, "summary": summary, "is_channel": True,
                "entity": entity_map.get(name), "date": _section_date(name),
                "unread_count": unread_map.get(name, 0),
            })
        _sort_digest_sections(sections)
        storage.save_last_sections(sections)

        # Interactive viewer — poll handles live updates, 'о' key for full refresh
        from tgai.ui import digest_viewer, _DIGEST_REFRESH
        while True:
            app_holder: list = []
            _poll_task = asyncio.create_task(
                _poll_digest_updates(
                    tg, claude, sections, app_holder,
                    watermarks=watermarks, storage=storage,
                    hours=hours, include_all=include_all, scope=scope,
                )
            )
            try:
                chosen_section = await _run_digest_viewer(loop, digest_viewer, sections, app_holder)
            finally:
                _poll_task.cancel()
                try:
                    await _poll_task
                except asyncio.CancelledError:
                    pass

            if chosen_section == _DIGEST_REFRESH:
                _should_refresh = True
                break

            if chosen_section is None:
                break  # q/Esc — truly exit

            entity = chosen_section.get("entity")
            if entity is None:
                try:
                    from tgai.telegram import _dialog_display_name
                    dialogs = await tg.get_dialogs(limit=200)
                    entity_map = {_dialog_display_name(d): d.entity for d in dialogs}
                    entity = entity_map.get(chosen_section.get("name", ""))
                except Exception:
                    entity = None
            if entity:
                print(f"Открываю {chosen_section['name']}...")
                from tgai.commands.chat import _run_chat
                persona = storage.load_persona("default")
                if listen_collector is not None:
                    listen_collector.pause()
                try:
                    await _run_chat(tg, claude, storage, entity, persona, text_mode)
                finally:
                    if listen_collector is not None:
                        listen_collector.resume()
                if not include_all:
                    _should_refresh = True
                    break
                # Return to digest viewer with same sections for include_all


def run(args: Any, config: dict, storage: Any) -> None:
    """Entry point for `tgai aggregate`."""
    from tgai.telegram import TelegramManager, SessionLockedError
    from tgai.claude import create_llm_client, InsufficientCreditsError

    tg_cfg = config.get("telegram", {})
    defaults = config.get("defaults", {})

    tg = TelegramManager(
        api_id=tg_cfg["api_id"],
        api_hash=tg_cfg["api_hash"],
        session_path=str(Path.home() / ".tgai" / "session"),
    )
    claude = create_llm_client(config)

    hours = getattr(args, "hours", None) or defaults.get("aggregate_hours", 24)
    include_all = getattr(args, "all", False)
    save = getattr(args, "save", False)
    text_mode = getattr(args, "text", False)

    async def main():
        await tg.start()
        try:
            await _run_aggregate(tg, claude, storage, hours, include_all, save, text_mode)
        finally:
            await tg.stop()

    try:
        asyncio.run(main())
    except SessionLockedError as e:
        print(f"\nОшибка: {e}")
    except InsufficientCreditsError:
        print("\nБаланс API исчерпан.")
        print("   Пополните на https://console.anthropic.com → Plans & Billing\n")
    except KeyboardInterrupt:
        print("\nВыход.")
