"""Interactive chat workflows.

This module provides both:
- the classic text-mode chat loop
- the full-screen prompt_toolkit chat UI with scrolling and live polling

The chat command is where Telegram history, local LLM history, and reply
drafting come together most tightly, so the code here intentionally keeps the
runtime flow explicit.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional


def _print_sent(text: str) -> None:
    """Print a just-sent message in the same style as received messages."""
    time_str = datetime.now().strftime("%H:%M")
    print(f"\n[Вы  {time_str}]")
    print(text)
    print()

from tgai.ui import (
    action_menu,
    clear,
    display_messages,
    edit_message,
    reformulate_flow,
    select_chat_interactive,
    select_chat_text,
    ACTION_SEND,
    ACTION_EDIT,
    ACTION_REFORMULATE,
    ACTION_REMEMBER,
    ACTION_MANUAL,
    ACTION_SKIP,
)
from tgai.chat_view import (
    CHAT_BOTTOM_GAP,
    max_scroll as _chat_max_scroll,
    visible_lines as _chat_visible_lines,
    window_start as _chat_window_start,
)


async def _poll_chat_list(tg, dialogs_holder: list, app_holder: list) -> None:
    """Background task: refresh dialog list every 2s and trigger re-render."""
    last_ids_sum = 0
    while True:
        try:
            await asyncio.sleep(2.0)  # Increased from 0.5 to 2.0 to reduce load
        except asyncio.CancelledError:
            break
        try:
            # Fetching dialogs can be expensive. We fetch 50 instead of 100 to speed up.
            new_dialogs = await tg.get_dialogs(limit=50)
            
            # Simple check if anything changed before invalidating
            current_ids_sum = sum(d.id + getattr(d, 'unread_count', 0) for d in new_dialogs)
            if current_ids_sum == last_ids_sum:
                continue
            
            last_ids_sum = current_ids_sum
            dialogs_holder[0] = new_dialogs
            app = app_holder[0] if app_holder else None
            if app is not None:
                try:
                    if app.is_running:
                        app.invalidate()
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _poll_new_messages(
    tg, entity, last_id_holder: list, me_id: int,
    stop_event: asyncio.Event, ai_suggestion: list = None,
    claude=None, persona: str = "", all_loaded_msgs: list = None,
) -> None:
    """Background task: poll for new messages every 2s, print them,
    and regenerate AI suggestion when a new incoming message arrives."""
    from tgai.ui import format_messages
    from prompt_toolkit.patch_stdout import patch_stdout

    while not stop_event.is_set():
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        if stop_event.is_set():
            break
        try:
            msgs = await tg.get_messages(entity, limit=10)
            new_msgs = [m for m in msgs if m.id > last_id_holder[0] and getattr(m, "text", None)]
            if new_msgs:
                last_id_holder[0] = max(m.id for m in new_msgs)
                lines = format_messages(list(reversed(new_msgs)), me_id)
                with patch_stdout():
                    for line in lines:
                        print(line)
                    print()

                # Update loaded msgs reference
                if all_loaded_msgs is not None:
                    all_loaded_msgs[:0] = new_msgs

                # Regenerate AI suggestion if new incoming message
                has_incoming = any(
                    getattr(m, "sender_id", None) != me_id
                    for m in new_msgs
                )
                if has_incoming and ai_suggestion is not None and claude is not None:
                    def _regen():
                        try:
                            recent = msgs[:10] if msgs else []
                            chat_hist = []
                            for m in reversed(recent):
                                if not getattr(m, "text", None):
                                    continue
                                sid = getattr(m, "sender_id", None)
                                label = "Вы" if sid == me_id else "Собеседник"
                                chat_hist.append({"sender": label, "text": m.text})
                            incoming = next(
                                (m.text for m in recent
                                 if getattr(m, "sender_id", None) != me_id and m.text),
                                None,
                            )
                            if incoming and chat_hist:
                                ai_suggestion[0] = claude.propose_reply(
                                    incoming, chat_hist, persona
                                )
                        except Exception:
                            pass
                    threading.Thread(target=_regen, daemon=True).start()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _load_entity(tg, identifier: str) -> Optional[Any]:
    """Resolve an identifier string to a Telegram entity."""
    try:
        return await tg.resolve_entity(identifier)
    except ValueError as e:
        print(f"Ошибка: {e}")
        return None


async def _run_chat(
    tg,
    claude,
    storage,
    entity: Any,
    persona: str,
    text_mode: bool,
) -> Any:
    """Open a chat, restore local LLM history, and run the chosen UI mode."""
    me = await tg.get_me()
    chat_id = getattr(entity, "id", 0)
    ai_available = getattr(claude, "provider_name", "") == "yandexgpt"

    # Load persisted Claude history
    history = storage.load_history(chat_id)
    claude.set_history(chat_id, history)

    # --- REMOVED BLOCKING MESSAGE FETCH ---
    all_loaded_msgs = []
    last_local_message = [None]

    from tgai.telegram import is_broadcast_channel
    is_channel = is_broadcast_channel(entity)

    # Mark as read
    try:
        await tg.mark_read(entity)
    except Exception:
        pass

    if not text_mode:
        # Full-screen scrollable chat
        await _run_chat_fullscreen(
            tg, claude, storage, entity, persona, me,
            all_loaded_msgs, is_channel, chat_id, ai_available=ai_available,
            last_local_message=last_local_message,
        )
        return last_local_message[0]

    # --- Text mode fallback below ---
    if not all_loaded_msgs:
        print("Нет сообщений.")
    else:
        display_messages(all_loaded_msgs, me.id)

    # Quick reply shortcuts
    QUICK_REPLIES = {
        "\\o": "Привет!",
        "\\ok": "Ок",
        "\\thx": "Спасибо!",
        "\\bb": "До связи!",
    }

    # Main chat loop
    if is_channel:
        print("\nКанал (только чтение).  /ещ -- ещё  Esc -- назад\n")
    elif not ai_available:
        print("\nYandexGPT не подключен. AI-функции в чате отключены.\n")

    loop = asyncio.get_running_loop()
    _last_id = [all_loaded_msgs[0].id if all_loaded_msgs else 0]
    _poll_stop = asyncio.Event()

    # --- Mode detection ---
    def _is_reply_mode() -> bool:
        """True if last message is from the other person (reply mode)."""
        for m in all_loaded_msgs:
            if getattr(m, "text", None):
                return getattr(m, "sender_id", None) != me.id
        return False

    # --- AI suggestion (generated silently in background) ---
    _ai_suggestion = [""]

    def _generate_suggestion():
        """Generate AI reply in background from latest messages (reply mode only)."""
        try:
            src = all_loaded_msgs[:claude.max_history] if all_loaded_msgs else []
            chat_hist = []
            for m in reversed(src):
                if not getattr(m, "text", None):
                    continue
                sid = getattr(m, "sender_id", None)
                label = "Вы" if sid == me.id else "Собеседник"
                chat_hist.append({"sender": label, "text": m.text})
            # Only suggest if last message is from the other person (reply mode)
            if src and getattr(src[0], "sender_id", None) != me.id:
                incoming = next(
                    (m.text for m in src
                     if getattr(m, "sender_id", None) != me.id and m.text),
                    None,
                )
                if incoming and chat_hist:
                    _ai_suggestion[0] = claude.propose_reply(incoming, chat_hist, persona)
        except Exception:
            _ai_suggestion[0] = ""

    if ai_available and not is_channel and all_loaded_msgs:
        threading.Thread(target=_generate_suggestion, daemon=True).start()

    # Start message polling (also regenerates AI suggestion on new incoming)
    _poll_task = asyncio.create_task(
        _poll_new_messages(
            tg, entity, _last_id, me.id, _poll_stop,
            ai_suggestion=_ai_suggestion, claude=claude,
            persona=persona, all_loaded_msgs=all_loaded_msgs,
        )
    )

    async def _get_input() -> Optional[str]:
        try:
            if text_mode:
                return await loop.run_in_executor(None, lambda: input("Вы: ").strip())
            else:
                from tgai.ui import chat_input as _chat_input

                # In initiate mode (last msg is ours), provide draft_fn for Tab
                _draft_fn = None
                if ai_available and not _is_reply_mode():
                    src = all_loaded_msgs[:claude.max_history] if all_loaded_msgs else []
                    chat_hist = []
                    for m in reversed(src):
                        if not getattr(m, "text", None):
                            continue
                        sid = getattr(m, "sender_id", None)
                        label = "Вы" if sid == me.id else "Собеседник"
                        chat_hist.append({"sender": label, "text": m.text})
                    _hist_snap = list(chat_hist)
                    _draft_fn = lambda text: claude.draft_reply(text, _hist_snap, persona)

                # Pass suggestion as lambda so Tab always reads the latest value
                result = await loop.run_in_executor(
                    None, lambda: _chat_input(
                        claude,
                        ai_suggestion=lambda: _ai_suggestion[0],
                        draft_fn=_draft_fn,
                    )
                )
                return result.strip() if result is not None else None
        except (KeyboardInterrupt, EOFError):
            return None

    try:
        while True:
            user_input = await _get_input()
            if user_input is None:
                break

            if not user_input or user_input in ("/quit", "/q", "/back", "/н", "/выход"):
                break

            if user_input in ("/reset", "/сб", "/сброс"):
                claude.clear_history(chat_id)
                storage.clear_history(chat_id)
                print("История очищена.")
                continue

            if user_input in ("/ctx", "/кт", "/контекст"):
                from tgai.claude import CONTEXT_LEVELS
                levels = list(CONTEXT_LEVELS.keys())
                current_idx = levels.index(
                    next((k for k, v in CONTEXT_LEVELS.items() if v == claude.max_history), "base")
                )
                next_idx = (current_idx + 1) % len(levels)
                new_level = levels[next_idx]
                claude.max_history = CONTEXT_LEVELS[new_level]
                names = {"base": "Базовый (5)", "medium": "Средний (10)", "extended": "Расширенный (20)"}
                print(f"Контекст: {names[new_level]}")
                continue

            # Quick replies: \o \ok \thx \bb
            if user_input in QUICK_REPLIES and not is_channel:
                text = QUICK_REPLIES[user_input]
                try:
                    await tg.send_message(entity, text)
                    last_local_message[0] = SimpleNamespace(
                        id=None, text=text, message=text, date=datetime.now(), sender_id=me.id
                    )
                    _print_sent(text)
                except Exception as e:
                    print(f"Ошибка: {e}")
                continue

            # /о or /s — explicit send (kept for compatibility)
            send_match = re.search(r'^/[оoOs](?:end|тправить)?\s+', user_input)
            if send_match and not is_channel:
                text = user_input[send_match.end():].strip()
                if text:
                    try:
                        await tg.send_message(entity, text)
                        last_local_message[0] = SimpleNamespace(
                            id=None, text=text, message=text, date=datetime.now(), sender_id=me.id
                        )
                        _print_sent(text)
                    except Exception as e:
                        print(f"Ошибка: {e}")
                continue

            # Load more history
            if user_input in ("/more", "/ещ", "/ещё"):
                oldest_id = all_loaded_msgs[-1].id if all_loaded_msgs else 0
                if oldest_id <= 0:
                    print("Больше сообщений нет.")
                    continue
                extra = await tg.get_messages(entity, limit=50, offset_id=oldest_id)
                if extra:
                    all_loaded_msgs.extend(extra)
                    display_messages(all_loaded_msgs, me.id)
                else:
                    print("Больше сообщений нет.")
                continue

            # Channel is read-only
            if is_channel:
                continue

            # --- Check for inline {comments} ---
            has_comments = bool(re.search(r"\{[^}]+\}", user_input))

            if has_comments and ai_available:
                recent_messages = await tg.get_messages(entity, limit=10)
                context_lines = []
                for m in reversed(recent_messages):
                    if m.text:
                        sid = getattr(m, "sender_id", None)
                        label = "Вы" if sid == me.id else "Собеседник"
                        context_lines.append(f"{label}: {m.text}")
                context = "\n".join(context_lines)

                print("Обрабатываю комментарии...")
                try:
                    processed = claude.process_inline_comments(user_input, context, persona)
                except Exception as e:
                    print(f"Ошибка: {e}")
                    processed = re.sub(r"\s*\{[^}]+\}", "", user_input).strip()

                # Show result, Enter sends, other keys for actions
                action, typed = action_menu(
                    processed, text_mode=text_mode, provider_name=claude.display_name
                )
                await _handle_action(
                    action, typed or processed, claude, entity, tg, storage,
                    chat_id, persona, text_mode
                )
            else:
                # --- Send directly (reply mode or initiate mode after Tab) ---
                try:
                    plain_text = re.sub(r"\s*\{[^}]+\}", "", user_input).strip() if has_comments else user_input
                    await tg.send_message(entity, plain_text)
                    last_local_message[0] = SimpleNamespace(
                        id=None, text=plain_text, message=plain_text, date=datetime.now(), sender_id=me.id
                    )
                    _print_sent(plain_text)
                except Exception as e:
                    print(f"Ошибка отправки: {e}")

            # Refresh loaded messages so mode detection stays accurate
            try:
                fresh = await tg.get_messages(entity, limit=5)
                if fresh:
                    new_ids = {m.id for m in all_loaded_msgs[:20]}
                    for m in fresh:
                        if m.id not in new_ids:
                            all_loaded_msgs.insert(0, m)
                    _last_id[0] = max(_last_id[0], fresh[0].id)
            except Exception:
                pass

            # Persist updated history
            storage.save_history(chat_id, claude.get_history(chat_id))

            # Regenerate AI suggestion in background
            _ai_suggestion[0] = ""
            if ai_available:
                threading.Thread(target=_generate_suggestion, daemon=True).start()

    finally:
        _poll_stop.set()
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass

    if all_loaded_msgs:
        last_local_message[0] = all_loaded_msgs[0]
    return last_local_message[0]


async def _handle_action(
    action: str,
    proposed: str,
    claude,
    entity: Any,
    tg,
    storage,
    chat_id: int,
    persona: str,
    text_mode: bool,
) -> str:
    """Handle the result of an action menu choice. Returns final text."""
    if action == ACTION_SEND:
        try:
            await tg.send_message(entity, proposed)
            print("Отправлено.")
        except Exception as e:
            print(f"Ошибка отправки: {e}")
        return proposed

    if action == ACTION_EDIT:
        edited = edit_message(proposed, text_mode=text_mode)
        try:
            await tg.send_message(entity, edited)
            print("Отправлено.")
        except Exception as e:
            print(f"Ошибка отправки: {e}")
        return edited

    if action == ACTION_REFORMULATE:
        new_text, sub_action = reformulate_flow(claude, proposed, persona, text_mode)
        if sub_action not in (ACTION_REFORMULATE, ACTION_SKIP):
            return await _handle_action(
                sub_action, new_text, claude, entity, tg, storage,
                chat_id, persona, text_mode
            )
        return new_text

    if action == ACTION_MANUAL:
        if text_mode:
            try:
                manual = input("Вы: ").strip()
            except (KeyboardInterrupt, EOFError):
                manual = ""
        else:
            from tgai.ui import chat_input as _chat_input
            manual = _chat_input(claude)
            manual = manual.strip() if manual else ""
        if manual:
            try:
                await tg.send_message(entity, manual)
                _print_sent(manual)
            except Exception as e:
                print(f"Ошибка отправки: {e}")
        else:
            print("Пропущено.")
        return manual or proposed

    # ACTION_SKIP or anything else
    print("Пропущено.")
    return proposed


async def _run_chat_fullscreen(
    tg, claude, storage, entity, persona, me,
    all_loaded_msgs, is_channel, chat_id, ai_available: bool = True,
    last_local_message: list | None = None,
) -> None:
    """Run the prompt_toolkit full-screen chat experience.

    The layout is split into a scrollable transcript and a pinned input area.
    While the app is open we also keep background polling tasks running for:
    - newly arrived messages at the bottom
    - asynchronous loading of older history when the user scrolls up
    - regeneration of AI suggestions after incoming messages
    """
    import shutil
    from prompt_toolkit import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, VSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout.dimension import Dimension
    from tgai.ui import format_messages, _word_wrap, _fmt_date, action_menu, reformulate_flow
    from tgai.ui import ACTION_SEND, ACTION_EDIT, ACTION_REFORMULATE, ACTION_MANUAL, ACTION_SKIP
    from tgai.telegram import is_broadcast_channel

    loop = asyncio.get_running_loop()
    _app_holder = [None]

    # --- ASYNC MESSAGE LOADING START ---
    # We start with empty lines and load messages in background
    _lines: list[str] = []
    if all_loaded_msgs:
        _lines = format_messages(list(all_loaded_msgs), me.id)
    
    _scroll = [0]  # lines scrolled up from bottom (0 = at bottom)
    _is_loading_initial = [not bool(all_loaded_msgs)]

    async def _process_media_async(msg: Any):
        """Analyze media in background using Vision + OCR and update message text."""
        if not hasattr(msg, "photo") or not msg.photo:
            return
        
        try:
            # Download image bytes
            img_bytes = await tg.download_media(msg)
            if not img_bytes:
                return
            
            loop = asyncio.get_running_loop()
            
            # Get description and OCR in parallel
            description, raw_ocr = await asyncio.gather(
                loop.run_in_executor(None, lambda: claude.describe_image(img_bytes)),
                loop.run_in_executor(None, lambda: claude.ocr_image(img_bytes))
            )
            
            # Refine OCR text via LLM
            clean_ocr = ""
            if raw_ocr:
                clean_ocr = await loop.run_in_executor(
                    None, lambda: claude.clean_ocr_text(raw_ocr)
                )
            
            # Build result text
            parts = []
            if description:
                parts.append(f"[media]: {description}")
            else:
                parts.append("[media]") 
                
            if clean_ocr:
                parts.append(f"[текст]: {clean_ocr}")
            
            new_media_line = " | ".join(parts)
            
            # Update message text
            original = getattr(msg, "text", "") or ""
            if original == "[media]":
                new_text = new_media_line
            else:
                # If there was a caption, keep it
                new_text = f"{new_media_line}\n{original}" if original else new_media_line
            
            setattr(msg, "text", new_text)
            setattr(msg, "message", new_text)
            _rebuild_lines()
            _invalidate()
        except Exception:
            pass

    async def _initial_load_messages():
        nonlocal all_loaded_msgs, _lines
        if not all_loaded_msgs:
            try:
                msgs = await tg.get_messages(entity, limit=100)
                all_loaded_msgs.extend(msgs)
                
                # --- OPTIMIZED MEDIA PROCESSING ---
                from datetime import datetime, timedelta, timezone
                now = datetime.now(timezone.utc)
                day_ago = now - timedelta(days=1)
                
                processed_count = 0
                # Process only top 10 messages (newest ones)
                for m in msgs[:10]:
                    has_photo = hasattr(m, "photo") and m.photo
                    if not has_photo:
                        continue
                    
                    # Set placeholder immediately
                    if not getattr(m, "text", ""):
                        setattr(m, "text", "[media]")
                    
                    # Conditions for AI analysis:
                    # 1. Message is newer than 24 hours
                    # 2. We haven't reached the limit (3 images per open)
                    msg_date = getattr(m, "date", None)
                    if msg_date and msg_date.replace(tzinfo=timezone.utc) > day_ago:
                        if processed_count < 3:
                            asyncio.create_task(_process_media_async(m))
                            processed_count += 1

                _rebuild_lines()
                _is_loading_initial[0] = False
                _invalidate()
                
                # Update last_id for poller
                if all_loaded_msgs:
                    _last_id[0] = all_loaded_msgs[0].id
            except Exception:
                _is_loading_initial[0] = False
                _invalidate()
    
    asyncio.create_task(_initial_load_messages())
    # --- ASYNC MESSAGE LOADING END ---

    def _max_scroll():
        _, terminal_lines = shutil.get_terminal_size((80, 24))
        return _chat_max_scroll(len(_lines), terminal_lines)

    def _invalidate():
        app = _app_holder[0]
        if app and getattr(app, "is_running", False):
            try:
                app.invalidate()
            except Exception:
                pass

    def _rebuild_lines() -> None:
        nonlocal _lines
        _lines = format_messages(list(all_loaded_msgs), me.id) if all_loaded_msgs else []

    def _append_lines(new_lines: list[str]) -> None:
        _lines.extend(new_lines)
        if _scroll[0] == 0:
            pass  # Stay at bottom
        else:
            _scroll[0] += len(new_lines)  # Keep relative position

    _loading_more = [False]
    _history_exhausted = [False]

    def _load_more_history_async() -> None:
        """Load older messages in background without blocking the UI."""
        if _loading_more[0] or _history_exhausted[0]:
            return
        oldest_id = all_loaded_msgs[-1].id if all_loaded_msgs else 0
        if oldest_id <= 0:
            _history_exhausted[0] = True
            return

        _loading_more[0] = True

        def _worker():
            try:
                extra = asyncio.run_coroutine_threadsafe(
                    tg.get_messages(entity, limit=100, offset_id=oldest_id), loop
                ).result(timeout=10)
            except Exception:
                extra = None

            def _apply():
                _loading_more[0] = False
                if not extra:
                    _history_exhausted[0] = True
                    _invalidate()
                    return
                before_len = len(_lines)
                all_loaded_msgs.extend(extra)
                _rebuild_lines()
                _scroll[0] += max(0, len(_lines) - before_len)
                _invalidate()

            loop.call_soon_threadsafe(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    # --- AI suggestion ---
    _ai_suggestion = [""]
    _ai_state = {"showing": False, "user_text": ""}

    def _generate_suggestion():
        try:
            src = list(all_loaded_msgs[:claude.max_history])
            chat_hist = []
            for m in reversed(src):
                if not getattr(m, "text", None):
                    continue
                sid = getattr(m, "sender_id", None)
                label = "Вы" if sid == me.id else "Собеседник"
                chat_hist.append({"sender": label, "text": m.text})
            if src and getattr(src[0], "sender_id", None) != me.id:
                incoming = next(
                    (m.text for m in src if getattr(m, "sender_id", None) != me.id and m.text),
                    None,
                )
                if incoming and chat_hist:
                    _ai_suggestion[0] = claude.propose_reply(incoming, chat_hist, persona)
                    _invalidate()
        except Exception:
            _ai_suggestion[0] = ""

    if ai_available and not is_channel and all_loaded_msgs:
        threading.Thread(target=_generate_suggestion, daemon=True).start()

    # --- Message content: render only visible lines based on scroll position ---
    def _all_msg_content():
        _, terminal_lines = shutil.get_terminal_size((80, 24))
        visible = _chat_visible_lines(terminal_lines)
        
        # If still loading initial messages and no messages are present
        if not _lines and _is_loading_initial[0]:
            return FormattedText([('italic ansigray', '\n  [Загрузка истории...]')])

        padded_lines = _lines + ([""] * CHAT_BOTTOM_GAP)
        start = _chat_window_start(len(_lines), _scroll[0], terminal_lines)
        visible_lines = padded_lines[start:start + visible]
        parts = [("", line + "\n") for line in visible_lines]
        if not parts:
            parts = [("", "\n")]
        return FormattedText(parts)

    msg_control = FormattedTextControl(_all_msg_content, focusable=True)

    # --- Input buffer ---
    input_buf = Buffer()

    QUICK_REPLIES = {"\\o": "Привет!", "\\ok": "Ок", "\\thx": "Спасибо!", "\\bb": "До связи!"}

    def _rephrase_ai_suggestion() -> None:
        """Rephrase the current AI suggestion in place."""
        base_text = input_buf.text.strip() if _ai_state["showing"] else _ai_suggestion[0].strip()
        if not base_text:
            return

        previous_text = _ai_state["user_text"] if _ai_state["showing"] else input_buf.text
        _ai_state["user_text"] = previous_text
        _ai_state["showing"] = True
        input_buf.text = "..."
        input_buf.cursor_position = len(input_buf.text)
        _invalidate()

        result = [base_text]

        def _run():
            try:
                result[0] = claude.reformulate(
                    base_text,
                    "Перефразируй по-другому, сохрани смысл, тон и краткость. "
                    "Пиши естественно, по-человечески.",
                    persona,
                )
            except Exception:
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)
        _ai_suggestion[0] = result[0]
        input_buf.text = result[0]
        input_buf.cursor_position = len(result[0])
        _invalidate()

    async def _do_send(text: str) -> None:
        """Send message and append to display."""
        try:
            sent = await tg.send_message(entity, text)
            if sent is not None:
                sent_id = getattr(sent, "id", None)
                known_ids = {getattr(m, "id", None) for m in all_loaded_msgs[:20]}
                if sent_id not in known_ids:
                    all_loaded_msgs.insert(0, sent)
                if last_local_message is not None:
                    last_local_message[0] = sent
            elif last_local_message is not None:
                last_local_message[0] = SimpleNamespace(
                    id=None, text=text, message=text, date=datetime.now(), sender_id=me.id
                )
            # Update last_id so the poller doesn't display this message again
            if sent is not None:
                sent_id = getattr(sent, "id", None)
                if sent_id is not None and sent_id > _last_id[0]:
                    _last_id[0] = sent_id
            time_str = datetime.now().strftime("%H:%M")
            new = ["", f"[Вы  {time_str}]"]
            cols = shutil.get_terminal_size((80, 24)).columns - 2
            for wrapped in _word_wrap(text, cols):
                new.append(wrapped)
            _append_lines(new)
            _invalidate()
        except Exception as e:
            _append_lines([f"Ошибка: {e}"])
            _invalidate()

    # --- Key bindings ---
    kb = KeyBindings()

    @kb.add("up", eager=True)
    def _(e):
        if _scroll[0] >= _max_scroll():
            _load_more_history_async()
        _scroll[0] = min(_scroll[0] + 1, _max_scroll())
        _invalidate()

    @kb.add("down", eager=True)
    def _(e):
        _scroll[0] = max(_scroll[0] - 1, 0)
        _invalidate()

    @kb.add("pageup", eager=True)
    @kb.add("c-b", eager=True)
    def _(e):
        _, terminal_lines = shutil.get_terminal_size((80, 24))
        if _scroll[0] >= _max_scroll():
            _load_more_history_async()
        _scroll[0] = min(_scroll[0] + max(1, terminal_lines - 4), _max_scroll())
        _invalidate()

    @kb.add("pagedown", eager=True)
    @kb.add("c-f", eager=True)
    def _(e):
        _, terminal_lines = shutil.get_terminal_size((80, 24))
        _scroll[0] = max(_scroll[0] - max(1, terminal_lines - 4), 0)
        _invalidate()

    @kb.add("c-home", eager=True)
    def _(e):
        _scroll[0] = _max_scroll()
        _load_more_history_async()
        _invalidate()

    @kb.add("c-end", eager=True)
    def _(e):
        _scroll[0] = 0
        _invalidate()

    @kb.add("<scroll-up>", eager=True)
    def _(e):
        if _scroll[0] >= _max_scroll():
            _load_more_history_async()
        _scroll[0] = min(_scroll[0] + 3, _max_scroll())
        _invalidate()

    @kb.add("<scroll-down>", eager=True)
    def _(e):
        _scroll[0] = max(_scroll[0] - 3, 0)
        _invalidate()

    @kb.add("escape")
    @kb.add("c-c")
    def _(e):
        e.app.exit()

    @kb.add("left")
    def _(e):
        if input_buf.cursor_position == 0:
            e.app.exit()
        else:
            input_buf.cursor_left()

    @kb.add("right")
    def _(e):
        if input_buf.cursor_position < len(input_buf.text):
            input_buf.cursor_right()

    @kb.add("c-r")
    def _(e):
        if not ai_available:
            return
        if is_channel:
            return
        if input_buf.text.strip() and not _ai_state["showing"]:
            return
        _rephrase_ai_suggestion()

    @kb.add("tab", eager=True)
    def _(e):
        if not ai_available or _ai_state.get("processing_tab"):
            return
        
        if _ai_state["showing"]:
            _ai_state["showing"] = False
            input_buf.text = _ai_state["user_text"]
            input_buf.cursor_position = len(input_buf.text)
            _invalidate()
            return

        # If suggestion exists and field is empty - just insert it
        if _ai_suggestion[0] and not input_buf.text.strip():
            _ai_state["user_text"] = ""
            _ai_state["showing"] = True
            input_buf.text = _ai_suggestion[0]
            input_buf.cursor_position = len(input_buf.text)
            _invalidate()
            return

        # Otherwise, improve existing text or generate new one
        current = input_buf.text
        _ai_state["user_text"] = current
        
        # Start async loading indicator
        input_buf.text = "Думаю..."
        _invalidate()

        _ai_state["processing_tab"] = True

        def _worker():
            try:
                if not current.strip():
                    # Generate from scratch if empty
                    src = list(all_loaded_msgs[:claude.max_history])
                    chat_hist = []
                    for m in reversed(src):
                        if not getattr(m, "text", None): continue
                        sid = getattr(m, "sender_id", None)
                        label = "Вы" if sid == me.id else "Собеседник"
                        chat_hist.append({"sender": label, "text": m.text})
                    
                    incoming = next((m.text for m in src if getattr(m, "sender_id", None) != me.id and m.text), None)
                    res = claude.propose_reply(incoming, chat_hist, persona) if incoming else ""
                else:
                    # Improve existing
                    src = list(all_loaded_msgs[:claude.max_history])
                    chat_hist = []
                    for m in reversed(src):
                        if not getattr(m, "text", None): continue
                        sid = getattr(m, "sender_id", None)
                        label = "Вы" if sid == me.id else "Собеседник"
                        chat_hist.append({"sender": label, "text": m.text})
                    res = claude.draft_reply(current, chat_hist, persona)
                
                def _done():
                    _ai_state["showing"] = True
                    _ai_state["processing_tab"] = False
                    input_buf.text = res
                    input_buf.cursor_position = len(res)
                    _invalidate()
                
                loop.call_soon_threadsafe(_done)
            except Exception:
                def _fail():
                    _ai_state["processing_tab"] = False
                    input_buf.text = current
                    _invalidate()
                loop.call_soon_threadsafe(_fail)

        threading.Thread(target=_worker, daemon=True).start()

    @kb.add("enter")
    def _(e):
        if is_channel:
            return
        text = input_buf.text.strip()
        if not text or text == "Думаю...":
            return

        # Quick replies
        if text in QUICK_REPLIES:
            text = QUICK_REPLIES[text]

        input_buf.text = ""
        _ai_state["showing"] = False
        _ai_state["user_text"] = ""
        _ai_suggestion[0] = ""
        if last_local_message is not None:
            last_local_message[0] = SimpleNamespace(
                id=None,
                text=text,
                message=text,
                date=datetime.now(),
                sender_id=me.id,
            )

        # Check inline comments
        if re.search(r"\{[^}]+\}", text):
            # Can't do action menu in fullscreen mode cleanly — just strip comments and send
            import re as _re
            processed = _re.sub(r"\s*\{[^}]+\}", "", text).strip()
            asyncio.run_coroutine_threadsafe(_do_send(processed or text), loop)
        else:
            asyncio.run_coroutine_threadsafe(_do_send(text), loop)

        # Regen AI suggestion after send
        if ai_available:
            threading.Thread(target=_generate_suggestion, daemon=True).start()

    @kb.add("backspace")
    @kb.add("c-h")
    def _(e):
        input_buf.delete_before_cursor(1)

    @kb.add("<any>")
    def _(e):
        ch = e.data
        if len(ch) == 1 and (ch.isprintable() or ch == " ") and input_buf.text != "Думаю...":
            input_buf.insert_text(ch)

    # --- Layout: HSplit keeps input pinned at bottom, cursor-pos scrolls messages ---
    def _hint_text():
        hint = (
            "↑↓ прокрутка  Tab ИИ  Ctrl+R перефраз  Enter отправить  ← назад"
            if (not is_channel and ai_available) else
            "↑↓ прокрутка  ← назад"
        )
        return FormattedText([("italic", f" {hint}")])

    input_control = BufferControl(buffer=input_buf, focusable=True)

    layout = Layout(
        HSplit([
            Window(content=msg_control),
            Window(height=1, char="─"),
            Window(content=FormattedTextControl(_hint_text), height=1),
            VSplit([
                Window(
                    content=FormattedTextControl([("bold", "Вы: ")]),
                    width=Dimension.exact(4),
                    height=1,
                ),
                Window(content=input_control, height=1),
            ]),
        ]),
        focused_element=input_control,
    )

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        mouse_support=True,
    )
    _app_holder[0] = app

    # --- Background: poll new messages ---
    _last_id = [all_loaded_msgs[0].id if all_loaded_msgs else 0]
    _poll_stop = asyncio.Event()

    async def _poll_messages():
        while not _poll_stop.is_set():
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            if _poll_stop.is_set():
                break
            try:
                msgs = await tg.get_messages(entity, limit=10)
                new_msgs = [m for m in msgs if m.id > _last_id[0] and (getattr(m, "text", None) or getattr(m, "photo", None))]
                if new_msgs:
                    _last_id[0] = max(m.id for m in new_msgs)
                    all_loaded_msgs[:0] = new_msgs
                    
                    # Trigger media processing for new photos (no strict limit for live messages)
                    for m in new_msgs:
                        if hasattr(m, "photo") and m.photo:
                            if not getattr(m, "text", ""):
                                setattr(m, "text", "[media]")
                            asyncio.create_task(_process_media_async(m))

                    cols = shutil.get_terminal_size((80, 24)).columns - 2
                    new_lines = []
                    last_header = None
                    from tgai.telegram import _entity_short_name_with_mode
                    for msg in reversed(new_msgs):
                        if not getattr(msg, "text", None):
                            continue
                        sid = getattr(msg, "sender_id", None)
                        label = (
                            "Вы" if sid == me.id else
                            _entity_short_name_with_mode(getattr(msg, "sender", None), show_username=False) or "?"
                        )
                        time_str = _fmt_date(getattr(msg, "date", None))
                        header_key = (label, time_str)
                        if header_key != last_header:
                            new_lines.append("")
                            new_lines.append(f"[{label}  {time_str}]")
                            last_header = header_key
                        for wrapped in _word_wrap(msg.text, cols):
                            new_lines.append(wrapped)
                    _append_lines(new_lines)
                    # Regen suggestion if incoming message
                    has_incoming = any(getattr(m, "sender_id", None) != me.id for m in new_msgs)
                    if has_incoming and ai_available:
                        threading.Thread(target=_generate_suggestion, daemon=True).start()
                    _invalidate()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    poll_task = asyncio.create_task(_poll_messages())

    try:
        await loop.run_in_executor(None, app.run)
    finally:
        _poll_stop.set()
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

    storage.save_history(chat_id, claude.get_history(chat_id))


def run(args: Any, config: dict, storage: Any) -> None:
    """Entry point for `tgai chat`."""
    from tgai.telegram import TelegramManager, SessionLockedError
    from tgai.claude import create_llm_client
    from pathlib import Path

    tg_cfg = config.get("telegram", {})
    defaults = config.get("defaults", {})

    tg = TelegramManager(
        api_id=tg_cfg["api_id"],
        api_hash=tg_cfg["api_hash"],
        session_path=str(Path.home() / ".tgai" / "session"),
    )
    claude = create_llm_client(config)

    persona_name = getattr(args, "persona", None) or defaults.get("persona", "default")
    persona = storage.load_persona(persona_name)
    text_mode = getattr(args, "text", False)
    identifier = getattr(args, "target", None)

    async def main():
        await tg.start()
        try:
            if identifier:
                entity = await _load_entity(tg, identifier)
                if entity is None:
                    return
                await _run_chat(tg, claude, storage, entity, persona, text_mode)
            else:
                # --- LAZY LOADING START ---
                dialogs_holder: list = [[]]
                folders_holder: list = [[]]
                app_holder: list = []
                
                async def _initial_fetch():
                    try:
                        # Fetch initial set fast
                        d = await tg.get_dialogs(limit=50)
                        dialogs_holder[0] = d
                        
                        # Invalidate UI if picker is open
                        app = app_holder[0] if app_holder else None
                        if app: app.invalidate()
                        
                        # Fetch the rest
                        f = await tg.get_folders()
                        folders_holder[0] = f
                        if app: app.invalidate()
                    except Exception:
                        pass
                
                fetch_task = asyncio.create_task(_initial_fetch())
                # --- LAZY LOADING END ---

                while True:
                    # Poller for continuous updates (already exists but now it's primary)
                    _poll_task = asyncio.create_task(
                        _poll_chat_list(tg, dialogs_holder, app_holder)
                    )
                    
                    try:
                        loop = asyncio.get_running_loop()
                        # Pass holders instead of static lists
                        dialog = await loop.run_in_executor(
                            None,
                            lambda: select_chat_interactive(
                                dialogs_holder[0], folders_holder[0],
                                dialogs_holder=dialogs_holder,
                                app_holder=app_holder,
                            ),
                        )
                    finally:
                        _poll_task.cancel()
                        try:
                            await _poll_task
                        except asyncio.CancelledError:
                            pass
                    
                    if dialog is None:
                        break
                    
                    entity = dialog.entity
                    await _run_chat(tg, claude, storage, entity, persona, text_mode)
        finally:
            fetch_task.cancel()
            try:
                await fetch_task
            except asyncio.CancelledError:
                pass
            await tg.stop()


    try:
        asyncio.run(main())
    except SessionLockedError as e:
        print(f"\nОшибка: {e}")
    except KeyboardInterrupt:
        print("\nВыход.")
