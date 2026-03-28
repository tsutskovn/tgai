"""UI components: chat selector, action menus, reformulate flow."""

from __future__ import annotations

import asyncio
import curses
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.completion import WordCompleter


def clear() -> None:
    """Clear terminal screen."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Smart hybrid selector (arrows ↔ text mode)
# ---------------------------------------------------------------------------

def smart_select(choices: list, title: str = ""):
    """
    Hybrid selector:
      - Arrow keys navigate, Enter selects  → returns ('select', value)
      - Printable key switches to text mode → returns ('text', typed_string)
      - Escape in text mode → back to arrow mode
      - Backspace to empty in text mode → back to arrow mode
      - Escape / Ctrl-C in arrow mode → returns None
    choices: list of strings or questionary.Choice objects
    """
    from prompt_toolkit import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    state = {'idx': 0, 'mode': 'select', 'typed': '', 'result': None}

    def _label(c):
        if isinstance(c, str):
            return c
        return getattr(c, 'title', str(c))

    def _value(c):
        if isinstance(c, str):
            return c
        return getattr(c, 'value', c)

    def render():
        parts = []
        if title:
            parts.append(('bold', title + '\n'))
        if state['mode'] == 'text':
            parts.append(('', '\n> '))
            parts.append(('bold', state['typed']))
            parts.append(('', '▌'))
        else:
            for i, c in enumerate(choices):
                lbl = _label(c)
                if i == state['idx']:
                    parts.append(('bold ansicyan', f'❯ {lbl}\n'))
                else:
                    parts.append(('', f'  {lbl}\n'))
        return FormattedText(parts)

    kb = KeyBindings()

    @kb.add('up')
    def _(e):
        if state['mode'] == 'select':
            state['idx'] = (state['idx'] - 1) % len(choices)

    @kb.add('down')
    def _(e):
        if state['mode'] == 'select':
            state['idx'] = (state['idx'] + 1) % len(choices)

    @kb.add('enter')
    @kb.add('right')
    def _(e):
        if state['mode'] == 'select':
            state['result'] = ('select', _value(choices[state['idx']]))
        else:
            state['result'] = ('text', state['typed'])
        e.app.exit()

    @kb.add('escape')
    @kb.add('left')
    def _(e):
        if state['mode'] == 'text':
            state['mode'] = 'select'
            state['typed'] = ''
        else:
            e.app.exit()

    @kb.add('backspace')
    @kb.add('c-h')
    def _(e):
        if state['mode'] == 'text':
            state['typed'] = state['typed'][:-1]
            if not state['typed']:
                state['mode'] = 'select'

    @kb.add('c-c')
    def _(e):
        e.app.exit()

    @kb.add('<any>')
    def _(e):
        ch = e.data
        if len(ch) == 1 and ch.isprintable():
            state['mode'] = 'text'
            state['typed'] += ch

    app = Application(
        layout=Layout(Window(FormattedTextControl(render))),
        key_bindings=kb,
        full_screen=True,
    )

    def _run():
        app.run()
        return state['result']

    return _q(_run)


def _q(fn):
    """Run a questionary prompt safely — works both inside and outside an async event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fn)
            return future.result()
    return fn()


def chat_input(
    claude_client: Any,
    ai_suggestion: str = "",
    default: str = "",
    draft_fn=None,
) -> Optional[str]:
    """
    Inline chat input — stays below printed messages.
      - Left arrow at pos 0 = back (returns None)
      - Right arrow at end = cycle context level
      - Tab = toggle between user text and AI suggestion/improvement
      - draft_fn(text) -> str: called on Tab in initiate mode to improve user text
      - ai_suggestion: pre-generated reply for reply mode
      - default = pre-fill input text
      - Otherwise normal prompt_toolkit editing
    """
    import threading
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from tgai.claude import CONTEXT_LEVELS

    state = {'go_back': False, 'user_text': '', 'showing_ai': False}
    kb = KeyBindings()

    @kb.add('left')
    def _left(event):
        buf = event.app.current_buffer
        if buf.cursor_position == 0:
            state['go_back'] = True
            event.app.exit(result='')
        else:
            buf.cursor_left()

    @kb.add('right')
    def _right(event):
        buf = event.app.current_buffer
        if buf.cursor_position < len(buf.text):
            buf.cursor_right()
        else:
            levels = list(CONTEXT_LEVELS.keys())
            current_idx = 0
            for i, (k, v) in enumerate(CONTEXT_LEVELS.items()):
                if v == claude_client.max_history:
                    current_idx = i
                    break
            next_idx = (current_idx + 1) % len(levels)
            new_level = levels[next_idx]
            claude_client.max_history = CONTEXT_LEVELS[new_level]
            names = {"base": "Базовый 5", "medium": "Средний 10", "extended": "Расширенный 20"}
            print(f"\n  Контекст: {names[new_level]}")

    @kb.add('tab')
    def _tab(event):
        buf = event.app.current_buffer
        if state['showing_ai']:
            # Toggle back to original user text
            state['showing_ai'] = False
            buf.text = state['user_text']
            buf.cursor_position = len(buf.text)
        elif draft_fn is not None and buf.text.strip():
            # Initiate mode: improve user text with AI (blocking call in thread)
            current = buf.text
            state['user_text'] = current
            buf.text = "..."
            result = [current]

            def _run():
                try:
                    result[0] = draft_fn(current)
                except Exception:
                    pass

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=15)
            state['showing_ai'] = True
            buf.text = result[0]
            buf.cursor_position = len(result[0])
        else:
            # Reply mode: show pre-generated suggestion (read dynamically)
            current_suggestion = ai_suggestion() if callable(ai_suggestion) else ai_suggestion
            if current_suggestion:
                state['user_text'] = buf.text
                state['showing_ai'] = True
                buf.text = current_suggestion
                buf.cursor_position = len(current_suggestion)

    _toolbar = "Enter отправить  Tab ИИ  ← назад"
    session = PromptSession(key_bindings=kb, bottom_toolbar=_toolbar)

    def _run():
        from prompt_toolkit.patch_stdout import patch_stdout
        try:
            with patch_stdout():
                return session.prompt("Вы: ", default=default)
        except (KeyboardInterrupt, EOFError):
            return None

    result = _q(_run)
    if state.get('go_back'):
        return None
    return result


# ---------------------------------------------------------------------------
# Action menu choices
# ---------------------------------------------------------------------------

ACTION_SEND = "Отправить"
ACTION_EDIT = "Редактировать"
ACTION_REFORMULATE = "Переформулировать"
ACTION_REMEMBER = "Запомнить без отправки"
ACTION_SKIP = "Пропустить"
ACTION_MANUAL = "Написать своё"

ACTION_CHOICES = [
    ACTION_SEND,
    ACTION_EDIT,
    ACTION_REFORMULATE,
    ACTION_MANUAL,
    ACTION_SKIP,
]

ACTION_SHORTCUTS = {
    "о": ACTION_SEND,
    "р": ACTION_EDIT,
    "c-r": ACTION_REFORMULATE,
    "н": ACTION_MANUAL,
    "с": ACTION_SKIP,
    "o": ACTION_SEND,
    "e": ACTION_EDIT,
    "r": ACTION_REFORMULATE,
    "m": ACTION_MANUAL,
    "k": ACTION_SKIP,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a datetime to local timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _fmt_date(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    local = _to_local(dt)
    now = datetime.now().astimezone()
    diff = now - local
    if diff.days == 0:
        return local.strftime("%H:%M")
    if diff.days < 7:
        days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        return days[local.weekday()]
    return local.strftime("%d.%m")


def _truncate(text: str, max_len: int = 40) -> str:
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _dialog_date(dialog: Any) -> datetime:
    """Normalized message datetime for dialog ordering."""
    dt = getattr(getattr(dialog, 'message', None), 'date', None)
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _sort_dialogs(dialogs: list[Any]) -> list[Any]:
    """Sort dialogs with unread on top, preserving newest-first order inside each group."""
    by_date = sorted(dialogs, key=_dialog_date, reverse=True)
    return sorted(by_date, key=lambda d: 0 if getattr(d, 'unread_count', 0) > 0 else 1)


# ---------------------------------------------------------------------------
# Chat selector
# ---------------------------------------------------------------------------

class _C:
    """Simple choice with title and value."""
    __slots__ = ('title', 'value')
    def __init__(self, title: str, value: Any = None):
        self.title = title
        self.value = value if value is not None else title


def chat_list_viewer(
    initial_dialogs: list[Any],
    title: str = "Выберите чат:",
    dialogs_holder: list = None,
    app_holder: list = None,
    preview_fn=None,
) -> Optional[Any]:
    """
    Full-screen chat list with live search and real-time updates.
    Type to filter by name, ↑↓ navigate, Enter select, Esc exit.
    """
    from prompt_toolkit import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from tgai.telegram import _dialog_display_name, display_name_for_ui

    state = {
        'idx': 0, 'query': '', 'result': None, '_preview_dialog': None,
        'show_usernames': False,
    }

    def _source() -> list[Any]:
        return dialogs_holder[0] if dialogs_holder else initial_dialogs

    def _by_date(dialogs: list[Any]) -> list[Any]:
        return _sort_dialogs(dialogs)

    def _filtered() -> list[Any]:
        q = state['query'].lower()
        src = _source()
        if q:
            return _by_date([d for d in src if q in _dialog_display_name(d).lower()])
        return _by_date(src)[:50]

    def render():
        cols = shutil.get_terminal_size((80, 24)).columns
        parts: list = []
        parts.append(('bold', f'{title}\n'))
        q = state['query']
        if q:
            parts.append(('', 'Поиск: '))
            parts.append(('bold ansicyan', q + '▌\n'))
        else:
            nick_label = '[ники]' if state['show_usernames'] else '[имена]'
            parts.append(('italic', f'Поиск: печатайте  ↑↓  Enter выбор  Tab просмотр  - ники {nick_label}\n'))
        parts.append(('', '─' * min(cols - 1, 60) + '\n'))

        filtered = _filtered()
        n = len(filtered)
        
        # --- LOADING INDICATOR ---
        if not _source() and not q:
            parts.append(('italic ansigray', '  [Загрузка чатов...]\n'))
            return FormattedText(parts)

        if n:
            state['idx'] = state['idx'] % n

        if not filtered:
            parts.append(('', '  Ничего не найдено.\n'))
        else:
            for i, d in enumerate(filtered[:50]):
                name = display_name_for_ui(_dialog_display_name(d), show_username=state['show_usernames'])
                unread = f' [{d.unread_count}]' if d.unread_count > 0 else ''
                if i == state['idx']:
                    parts.append(('bold ansicyan', f'❯ {name}{unread}\n'))
                    msg = d.message
                    preview_text = None
                    if msg:
                        preview_text = getattr(msg, 'text', None) or getattr(msg, 'message', None)
                        if not preview_text and hasattr(msg, 'media') and msg.media:
                            preview_text = "[медиа]"
                    if preview_text:
                        preview = _truncate(preview_text, cols - 6)
                        parts.append(('ansicyan', f'    {preview}\n'))
                else:
                    parts.append(('', f'  {name}{unread}\n'))
        return FormattedText(parts)

    kb = KeyBindings()

    @kb.add('up')
    def _(e):
        n = len(_filtered())
        if n:
            state['idx'] = (state['idx'] - 1) % n

    @kb.add('down')
    def _(e):
        n = len(_filtered())
        if n:
            state['idx'] = (state['idx'] + 1) % n

    @kb.add('enter')
    @kb.add('right')
    def _(e):
        filtered = _filtered()
        idx = state['idx']
        if filtered and 0 <= idx < len(filtered):
            state['result'] = filtered[idx]
            e.app.exit()

    @kb.add('tab')
    def _(e):
        filtered = _filtered()
        idx = state['idx']
        if not filtered or not (0 <= idx < len(filtered)):
            return
        dialog = filtered[idx]
        if preview_fn is not None:
            # Exit app, run pager, then re-open app via _run loop
            state['_preview_dialog'] = dialog
            e.app.exit()
        else:
            state['result'] = ('preview', dialog)
            e.app.exit()

    @kb.add('-')
    def _(e):
        state['show_usernames'] = not state['show_usernames']

    @kb.add('backspace')
    @kb.add('c-h')
    def _(e):
        if state['query']:
            state['query'] = state['query'][:-1]
            state['idx'] = 0

    @kb.add('escape')
    @kb.add('left')
    @kb.add('c-c')
    def _(e):
        if state['query']:
            state['query'] = ''
            state['idx'] = 0
        else:
            e.app.exit()

    @kb.add('<any>')
    def _(e):
        ch = e.data
        if len(ch) == 1 and ch.isprintable():
            state['query'] += ch
            state['idx'] = 0

    app = Application(
        layout=Layout(Window(FormattedTextControl(render))),
        key_bindings=kb,
        full_screen=True,
    )
    if app_holder is not None:
        app_holder.clear()
        app_holder.append(app)

    def _run():
        while True:
            state['_preview_dialog'] = None
            app.reset()
            app.run()
            dlg = state['_preview_dialog']
            if dlg is None:
                break
            # Show preview pager, then loop back into the same chat list
            try:
                preview_fn(dlg)
            except Exception:
                pass
        return state['result']

    return _q(_run)


def select_chat_interactive(
    dialogs: list[Any],
    folders: list[Any],
    dialogs_holder: list = None,
    folders_holder: list = None,
    app_holder: list = None,
    preview_fn=None,
    open_fn=None,
) -> Optional[Any]:
    """
    Chat selector: category picker → chat_list_viewer with live search + real-time updates.
    Returns selected dialog or None if cancelled.
    """
    from telethon.tl.types import User, Chat, Channel
    from tgai.telegram import is_broadcast_channel, _dialog_display_name

    # Always exclude bots
    def _base(src: list[Any]) -> list[Any]:
        return [
            d for d in src
            if not (isinstance(d.entity, User) and d.entity.bot)
        ]

    def _sort_by_date(lst: list[Any]) -> list[Any]:
        return _sort_dialogs(lst)

    def _filter_dialogs(src: list[Any], mode: str, sub_mode: str = "all") -> list[Any]:
        b = _base(src)
        if mode == "personal":
            pool = [d for d in b if isinstance(d.entity, User)]
        elif mode == "groups":
            pool = [d for d in b if isinstance(d.entity, (Chat, Channel)) and not is_broadcast_channel(d.entity)]
        elif mode == "channels":
            pool = [d for d in b if is_broadcast_channel(d.entity)]
        else:
            pool = b
        if sub_mode == "unread":
            pool = [d for d in pool if d.unread_count > 0]
        return _sort_by_date(pool)

    def _folder_name(f) -> str:
        t = getattr(f, 'title', f)
        return getattr(t, 'text', str(t))

    # Category picker (no real-time needed here)
    while True:
        # Determine actual sources
        actual_dialogs = dialogs_holder[0] if dialogs_holder else dialogs
        actual_folders = folders_holder[0] if folders_holder else folders
        
        cat_choices = [
            _C("Все чаты", "all"),
            _C("Личные чаты", "personal"),
            _C("Группы", "groups"),
            _C("Каналы", "channels"),
        ]
        # Show folders button if they are loading (None) or loaded and not empty
        if actual_folders is None or actual_folders:
            cat_choices.append(_C("Папки", "folders"))

        result = smart_select(cat_choices, title="Чат — выберите категорию:")
        if result is None:
            return None

        sel_mode, cat = result
        if sel_mode == 'text':
            # Quick text search from category screen
            q = cat.lower()
            matches = [d for d in _base(actual_dialogs) if q in _dialog_display_name(d).lower()]
            if matches:
                return chat_list_viewer(
                    matches, title=f'Поиск: {cat}',
                    dialogs_holder=dialogs_holder, app_holder=app_holder,
                    preview_fn=preview_fn,
                )
            continue

        if cat == "folders":
            # Wait a bit if folders are still loading (None)
            import time
            wait_count = 0
            while (folders_holder and folders_holder[0] is None) and wait_count < 10:
                print("Ожидание загрузки папок...")
                time.sleep(0.5)
                wait_count += 1
                
            actual_folders = folders_holder[0] if folders_holder else folders
            if not actual_folders:
                print("Папки не найдены.")
                time.sleep(1)
                continue

            folder_choices = [_C(_folder_name(f), f) for f in actual_folders]
            fr = smart_select(folder_choices, title="Выберите папку:")
            if fr is None:
                continue
            fmode, selected_folder = fr
            if fmode == 'text':
                q = selected_folder.lower()
                matches = [d for d in _base(actual_dialogs) if q in _dialog_display_name(d).lower()]
                if matches:
                    return chat_list_viewer(
                        matches, title=f'Поиск: {selected_folder}',
                        dialogs_holder=dialogs_holder, app_holder=app_holder,
                        preview_fn=preview_fn,
                    )
                continue
            folder_peer_ids: set[int] = set()
            for peer in (
                list(getattr(selected_folder, 'include_peers', []))
                + list(getattr(selected_folder, 'pinned_peers', []))
            ):
                pid = (getattr(peer, 'user_id', None)
                       or getattr(peer, 'chat_id', None)
                       or getattr(peer, 'channel_id', None))
                if pid:
                    folder_peer_ids.add(pid)
            
            def _folder_holder_gen():
                src = dialogs_holder[0] if dialogs_holder else actual_dialogs
                return [d for d in _base(src)
                        if getattr(d.entity, 'id', None) in folder_peer_ids]

            folder_dialogs = _folder_holder_gen()
            
            class _FolderHolder:
                def __getitem__(self, idx): return _folder_holder_gen()
                def __bool__(self): return True

            return chat_list_viewer(
                folder_dialogs,
                title=f'Папка: {_folder_name(selected_folder)}',
                dialogs_holder=_FolderHolder(),
                app_holder=app_holder,
                preview_fn=preview_fn,
            )

        # Sub-filter (all / unread)
        while True:
            sub_choices = [
                _C("Все", "all"),
                _C("Непрочитанные", "unread"),
            ]
            sub_result = smart_select(sub_choices, title="Фильтр:")
            if sub_result is None:
                break

            smode, sub = sub_result
            if smode == 'text':
                q = sub.lower()
                matches = [d for d in _base(actual_dialogs) if q in _dialog_display_name(d).lower()]
                if matches:
                    return chat_list_viewer(
                        matches, title=f'Поиск: {sub}',
                        dialogs_holder=dialogs_holder, app_holder=app_holder,
                        preview_fn=preview_fn,
                    )
                continue

            # Build filtered dialogs_holder for this category+sub
            def _make_filtered_holder(m=cat, s=sub):
                src = dialogs_holder[0] if dialogs_holder else actual_dialogs
                return _filter_dialogs(src, m, s)

            initial = _make_filtered_holder()

            class _FilteredHolder:
                """List-like proxy that re-filters on each read."""
                def __getitem__(self, idx):
                    return _make_filtered_holder()
                def __bool__(self):
                    return True

            chat_title = f'{"Непрочитанные" if sub == "unread" else "Все"} чаты:'
            fholder = _FilteredHolder() if dialogs_holder else None
            while True:
                chosen = chat_list_viewer(
                    initial,
                    title=chat_title,
                    dialogs_holder=fholder,
                    app_holder=app_holder,
                    preview_fn=preview_fn,
                )
                if chosen is None:
                    break  # Back to category picker
                
                if open_fn is not None:
                    open_fn(chosen)  # Blocking: opens chat, returns when done
                else:
                    return chosen


def select_chat_text(dialogs: list[Any]) -> Optional[Any]:
    """Text-mode chat selector: numbered list + input."""
    from tgai.telegram import _dialog_display_name

    sorted_dialogs = _sort_dialogs(dialogs)

    print("\nДоступные чаты:")
    for i, dialog in enumerate(sorted_dialogs[:50], 1):
        name = _dialog_display_name(dialog)
        unread = f" [{dialog.unread_count}]" if dialog.unread_count > 0 else ""
        print(f"  {i:2}. {name}{unread}")

    while True:
        try:
            raw = input("\nВведите номер чата (или часть имени): ").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(sorted_dialogs[:50]):
                return sorted_dialogs[idx]
            print("Неверный номер.")
        else:
            raw_lower = raw.lower()
            matches = [
                d for d in sorted_dialogs
                if raw_lower in _dialog_display_name(d).lower()
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                print("Найдено несколько совпадений:")
                for i, m in enumerate(matches[:10], 1):
                    print(f"  {i}. {_dialog_display_name(m)}")
                try:
                    n = int(input("Номер: ").strip())
                    return matches[n - 1]
                except (ValueError, IndexError):
                    pass
            else:
                print("Чат не найден.")


# ---------------------------------------------------------------------------
# Action menu
# ---------------------------------------------------------------------------

def action_menu_interactive(proposed: str, provider_name: str = "ИИ") -> tuple[str, Optional[str]]:
    """
    Inline action menu — Enter sends, hotkeys for other actions.
    Returns (action, edited_text_or_None).
    """
    from prompt_toolkit import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    print(f"\n{provider_name}: {proposed}")

    state = {'result': (ACTION_SKIP, None)}

    def render():
        parts: list = [
            ('', '  '),
            ('bold ansicyan', 'Enter'),
            ('', ' отправить  '),
            ('bold ansicyan', '[р]'),
            ('', ' ред.  '),
            ('bold ansicyan', '[Ctrl+R]'),
            ('', ' перефразир.  '),
            ('bold ansicyan', '[н]'),
            ('', ' написать  '),
            ('bold ansicyan', '[с]'),
            ('', ' пропустить'),
        ]
        return FormattedText(parts)

    kb = KeyBindings()

    @kb.add('enter')
    def _(e):
        state['result'] = (ACTION_SEND, None)
        e.app.exit()

    # Edit
    @kb.add('р')
    @kb.add('e')
    def _(e):
        state['result'] = (ACTION_EDIT, None)
        e.app.exit()

    # Reformulate
    @kb.add('п')
    @kb.add('f')
    def _(e):
        state['result'] = (ACTION_REFORMULATE, None)
        e.app.exit()

    # Manual input (discard AI, write own)
    @kb.add('н')
    @kb.add('m')
    def _(e):
        state['result'] = (ACTION_MANUAL, None)
        e.app.exit()

    # Skip
    @kb.add('с')
    @kb.add('k')
    def _(e):
        state['result'] = (ACTION_SKIP, None)
        e.app.exit()

    @kb.add('c-c')
    @kb.add('escape')
    def _(e):
        e.app.exit()

    app = Application(
        layout=Layout(Window(FormattedTextControl(render))),
        key_bindings=kb,
        full_screen=False,
    )

    def _run():
        app.run()
        return state['result']

    return _q(_run)


def action_menu_text(proposed: str, provider_name: str = "ИИ") -> tuple[str, Optional[str]]:
    """Text-mode action menu. Enter sends, other keys for actions."""
    print(f"\n{provider_name}: «{proposed}»")
    print("  Enter отправить  [р]ед.  [Ctrl+R]ефразиров.  [н]аписать  [с]кип")
    while True:
        try:
            key = input("Действие: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return ACTION_SKIP, None
        if not key:
            return ACTION_SEND, None
        if key in ACTION_SHORTCUTS:
            return ACTION_SHORTCUTS[key], None
        for action in ACTION_CHOICES:
            if action.lower().startswith(key):
                return action, None
        print("Неизвестный выбор. Используйте: р/п/н/с или Enter")


def action_menu(proposed: str, text_mode: bool = False, provider_name: str = "ИИ") -> tuple[str, Optional[str]]:
    """Returns (action, optional_edited_text)."""
    if text_mode:
        return action_menu_text(proposed, provider_name)
    return action_menu_interactive(proposed, provider_name)


# ---------------------------------------------------------------------------
# Inline edit
# ---------------------------------------------------------------------------

def edit_message_interactive(current: str) -> str:
    """Open message for inline editing with prompt_toolkit."""
    print("Редактирование (Enter для сохранения, Ctrl+C для отмены):")
    try:
        result = _q(lambda: pt_prompt("> ", default=current))
        return result.strip() if result else current
    except KeyboardInterrupt:
        return current


def edit_message_text(current: str) -> str:
    """Text-mode inline edit."""
    print(f"Текущий текст: {current}")
    try:
        new_text = input("Новый текст (Enter = оставить): ").strip()
        return new_text if new_text else current
    except (KeyboardInterrupt, EOFError):
        return current


def edit_message(current: str, text_mode: bool = False) -> str:
    if text_mode:
        return edit_message_text(current)
    return edit_message_interactive(current)


# ---------------------------------------------------------------------------
# Reformulate flow
# ---------------------------------------------------------------------------

def reformulate_flow(
    claude_client: Any,
    original: str,
    persona: str,
    text_mode: bool = False,
) -> tuple[str, str]:
    """
    Reformulate loop: ask for instruction, get new proposal, show action menu.
    Returns (final_text, action).
    """
    current = original
    while True:
        if text_mode:
            try:
                instruction = input("Что изменить?: ").strip()
            except (KeyboardInterrupt, EOFError):
                return current, ACTION_SKIP
        else:
            try:
                import questionary
                instruction = _q(lambda: questionary.text("Что изменить?:").ask())
                if instruction is None:
                    return current, ACTION_SKIP
                instruction = instruction.strip()
            except Exception:
                try:
                    instruction = input("Что изменить?: ").strip()
                except (KeyboardInterrupt, EOFError):
                    return current, ACTION_SKIP

        if not instruction:
            return current, ACTION_SKIP

        new_proposal = claude_client.reformulate(current, instruction, persona)
        action, typed = action_menu(new_proposal, text_mode=text_mode)

        if action == ACTION_REFORMULATE:
            current = new_proposal
            continue
        return typed or new_proposal, action


# ---------------------------------------------------------------------------
# Message history display
# ---------------------------------------------------------------------------

def _word_wrap(text: str, width: int) -> list[str]:
    """Wrap text at word boundaries."""
    import re
    def _vlen(s: str) -> int:
        return len(re.sub(r'\033\[[0-9;]*m', '', s))

    if width < 10:
        width = 60
    result: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            result.append("")
            continue
        words = raw_line.split(" ")
        cur = ""
        for w in words:
            if cur and (_vlen(cur) + 1 + _vlen(w)) > width:
                result.append(cur)
                cur = w
            elif cur:
                cur += " " + w
            else:
                cur = w
            while _vlen(cur) > width:
                # Truncate the visible part, keep the ANSI codes intact if possible.
                # For simplicity in this app, we just slice the string directly if it's a huge word.
                result.append(cur[:width])
                cur = cur[width:]
        if cur:
            result.append(cur)
    return result


def format_messages(messages, me_id, show_usernames=False):
    from tgai.telegram import _entity_short_name_with_mode
    import shutil
    from datetime import timedelta
    cols = shutil.get_terminal_size((80, 24)).columns
    reversed_msgs = list(reversed(messages))
    last_header = None
    lines = []
    line_to_msg = []
    max_w = cols - 2
    for msg in reversed_msgs:
        text = getattr(msg, "text", "") or getattr(msg, "message", "") or ""
        # Ultra-robust media detection without class imports
        has_media = False
        if hasattr(msg, "photo") and msg.photo: 
            has_media = True
        elif hasattr(msg, "media") and msg.media:
            has_media = True
        
        if not text and not has_media: continue
        
        if not text and has_media: text = "[32m[медиа][0m"
        elif has_media:
            text = text.replace("[media]", "[32m[медиа][0m").replace("[медиа]", "[32m[медиа][0m").replace("[текст]", "[36m[текст][0m")
        
        sender_id = getattr(msg, "sender_id", None)
        sender_label = "Вы" if sender_id == me_id else (_entity_short_name_with_mode(getattr(msg, "sender", None), show_username=show_usernames) if getattr(msg, "sender", None) else "?")
        msg_date = getattr(msg, "date", None)
        time_str = _fmt_date(msg_date)
        should_show_header = True
        if last_header:
            l_lab, l_time = last_header
            if sender_label == l_lab and msg_date and l_time:
                if abs((msg_date - l_time).total_seconds()) < 15 * 60: should_show_header = False
        if should_show_header:
            if last_header is not None:
                lines.append(""); line_to_msg.append(None)
            lines.append(f"[{sender_label}  {time_str}]"); line_to_msg.append(None)
            last_header = (sender_label, msg_date)
        wrapped = _word_wrap(text, max_w)
        for w_line in wrapped:
            lines.append(w_line); line_to_msg.append(msg)
    return lines, line_to_msg


def display_messages(messages: list[Any], me_id: int, show_usernames: bool = False) -> None:
    """Print messages to terminal (stays in scrollback buffer)."""
    lines = format_messages(messages, me_id, show_usernames=show_usernames)
    if not lines:
        return
    for line in lines:
        print(line)
    print()


def _pager(lines: list[str], status_hint: str = "") -> None:
    """Curses-based full-screen scrollable pager."""
    if not lines:
        return

    def _run(stdscr):
        curses.curs_set(0)
        curses.use_default_colors()
        stdscr.keypad(True)  # Enable arrow keys
        # Init color pair for status bar
        try:
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)
        except Exception:
            pass

        rows, cols = stdscr.getmaxyx()
        visible = rows - 1  # 1 for status bar
        total = len(lines)
        offset = max(0, total - visible)  # start at bottom

        while True:
            stdscr.erase()
            for i in range(visible):
                line_idx = offset + i
                if line_idx >= total:
                    break
                line = lines[line_idx]
                if len(line) >= cols:
                    line = line[:cols - 1]
                try:
                    stdscr.addstr(i, 0, line)
                except curses.error:
                    pass

            # Status bar
            if total <= visible:
                pct_str = ""
            else:
                pct = min(100, int((offset + visible) / total * 100))
                pct_str = f" {pct}%"
            hint = status_hint or "↑↓ прокрутка  q/Esc назад"
            status = f" [{total} строк]{pct_str}  {hint} "
            if len(status) >= cols:
                status = status[:cols - 1]
            try:
                stdscr.attron(curses.color_pair(1))
                stdscr.addstr(rows - 1, 0, status.ljust(cols - 1)[:cols - 1])
                stdscr.attroff(curses.color_pair(1))
            except curses.error:
                pass

            stdscr.refresh()
            key = stdscr.getch()

            if key in (ord('q'), ord('Q'), 27, curses.KEY_LEFT):
                break
            elif key in (curses.KEY_UP, ord('k')):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, ord('j')):
                offset = min(max(0, total - visible), offset + 1)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - visible)
            elif key in (curses.KEY_NPAGE, ord(' ')):
                offset = min(max(0, total - visible), offset + visible)
            elif key in (curses.KEY_HOME, ord('g')):
                offset = 0
            elif key in (curses.KEY_END, ord('G')):
                offset = max(0, total - visible)

    try:
        curses.wrapper(_run)
    except Exception:
        # Fallback: just print
        for line in lines[-40:]:
            print(line)


_DIGEST_REFRESH = '__refresh__'


def digest_viewer(sections: list[dict], refresh_after_secs: int = 0, app_holder: list = None):
    """
    Full-screen interactive digest viewer.
    ↑↓ navigate, Enter opens chat, r refreshes, q exits.
    Returns: section dict | '__refresh__' | None (exit).
    """
    import threading
    from prompt_toolkit import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not sections:
        print("Дайджест пуст.")
        return None

    state = {
        'idx': 0, 'result': None, 'countdown': refresh_after_secs,
        'expanded': False, 'scroll': 0, 'show_usernames': False,
    }

    def _summary_lines(text: str, max_lines: int = 0) -> list[str]:
        """Return summary lines. max_lines=0 means all."""
        lines = []
        for para in text.split('\n'):
            para = para.strip()
            if para:
                lines.append(para)
            if max_lines and len(lines) >= max_lines:
                break
        return lines

    def _build_digest_lines(cols: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Return sticky header lines and scrollable body lines."""
        from tgai.telegram import display_name_for_ui
        max_w = max(40, cols - 8)
        header_lines: list[tuple[str, str]] = []
        body_lines: list[tuple[str, str]] = []
        expanded = state['expanded']
        mode_label = '[развёрнуто]' if expanded else '[компактно]'
        nick_label = '[ники]' if state['show_usernames'] else '[имена]'
        hint = f'↑↓  Enter чат  о обновить  в вид  - ники {mode_label} {nick_label}'
        if state['countdown'] > 0:
            hint += f'  (авто {state["countdown"]}с)'
        header_lines.append(('bold', f'Дайджест  {hint}'))
        header_lines.append(('', '─' * min(cols - 1, 60)))

        for i, sec in enumerate(sections):
            is_channel = sec.get('is_channel', False)
            name = display_name_for_ui(sec.get('name', '?'), show_username=state['show_usernames'])
            tag = ' [канал]' if is_channel else ''
            unread = sec.get('unread_count', 0)
            unread_tag = f' [{unread}]' if unread > 0 else ''
            selected = i == state['idx']

            if expanded:
                style = 'bold ansicyan' if selected else 'bold'
                prefix = '❯ ' if selected else '  '
                body_lines.append((style, f'{prefix}{name}{tag}{unread_tag}'))
                summary_style = 'ansicyan' if selected else ''
                for line in _summary_lines(sec.get('summary', '')):
                    for wrapped in _word_wrap(line, max_w):
                        body_lines.append((summary_style, f'    {wrapped}'))
                body_lines.append(('', ''))
            else:
                if selected:
                    body_lines.append(('bold ansicyan', f'❯ {name}{tag}{unread_tag}'))
                    for line in _summary_lines(sec.get('summary', ''), 4):
                        for wrapped in _word_wrap(line, max_w):
                            body_lines.append(('ansicyan', f'    {wrapped}'))
                    body_lines.append(('', ''))
                else:
                    body_lines.append(('', f'  {name}{tag}{unread_tag}'))

        return header_lines, body_lines

    def _selected_body_line(expanded: bool) -> int:
        """Line index of selected section within the scrollable body."""
        line_idx = 0
        for i in range(len(sections)):
            if i == state['idx']:
                return line_idx
            if expanded:
                line_idx += 2
                for line in _summary_lines(sections[i].get('summary', '')):
                    line_idx += len(_word_wrap(line, max(40, shutil.get_terminal_size((80, 24)).columns - 8)))
            else:
                line_idx += 1
        return 0

    def render():
        cols = shutil.get_terminal_size((80, 24)).columns
        rows = shutil.get_terminal_size((80, 24)).lines
        expanded = state['expanded']
        header_lines, body_lines = _build_digest_lines(cols)

        header_count = len(header_lines)
        visible = max(rows - header_count, 3)
        sel_line = _selected_body_line(expanded)

        scroll = state['scroll']
        if sel_line < scroll:
            scroll = sel_line
        elif sel_line >= scroll + visible:
            scroll = sel_line - visible + 1
        max_scroll = max(0, len(body_lines) - visible)
        state['scroll'] = max(0, min(scroll, max_scroll))

        parts: list = []
        for style, text in header_lines:
            parts.append((style, text + '\n'))
        for style, text in body_lines[state['scroll']:state['scroll'] + visible]:
            parts.append((style, text + '\n'))
        return FormattedText(parts)

    kb = KeyBindings()

    def _open_selected(e) -> None:
        sec = sections[state['idx']]
        state['result'] = sec
        e.app.exit()

    def _close_viewer(e) -> None:
        e.app.exit()

    @kb.add('up')
    def _(e):
        state['idx'] = (state['idx'] - 1) % len(sections)

    @kb.add('down')
    def _(e):
        state['idx'] = (state['idx'] + 1) % len(sections)

    @kb.add('enter')
    def _(e):
        _open_selected(e)

    @kb.add('right')
    def _(e):
        _open_selected(e)

    # в — переключить вид (развернуть/свернуть все)
    @kb.add('в')
    @kb.add('В')
    def _(e):
        state['expanded'] = not state['expanded']
        state['scroll'] = 0

    # о — обновить дайджест
    @kb.add('о')
    @kb.add('О')
    def _(e):
        state['result'] = _DIGEST_REFRESH
        e.app.exit()

    @kb.add('-')
    def _(e):
        state['show_usernames'] = not state['show_usernames']

    # Выход
    @kb.add('left')
    @kb.add('escape')
    def _(e):
        _close_viewer(e)

    app = Application(
        layout=Layout(Window(FormattedTextControl(render))),
        key_bindings=kb,
        full_screen=True,
    )
    if app_holder is not None:
        app_holder.append(app)

    def _run():
        # Auto-refresh countdown thread
        if refresh_after_secs > 0:
            def _timer():
                import time
                remaining = refresh_after_secs
                while remaining > 0 and app.is_running:
                    time.sleep(1)
                    remaining -= 1
                    state['countdown'] = remaining
                    try:
                        app.invalidate()
                    except Exception:
                        pass
                if app.is_running:
                    state['result'] = _DIGEST_REFRESH
                    app.exit()
            threading.Thread(target=_timer, daemon=True).start()

        app.run()
        return state['result']

    return _q(_run)


def _render_digest_rich(sections: list[dict]) -> list[str]:
    """Render digest with rich formatting into plain lines for curses."""
    lines = []
    chat_sections = [s for s in sections if not s.get("is_channel")]
    channel_sections = [s for s in sections if s.get("is_channel")]

    if chat_sections:
        lines.append("# Чаты")
        lines.append("━" * 50)
        lines.append("")
        for s in chat_sections:
            lines.append(f"## {s['name']}")
            lines.append("")
            for para in s["summary"].split("\n"):
                if para.strip():
                    lines.append(f"  {para.strip()}")
            lines.append("")

    if channel_sections:
        lines.append("# Каналы")
        lines.append("━" * 50)
        lines.append("")
        for s in channel_sections:
            lines.append(f"## {s['name']}")
            lines.append("")
            for para in s["summary"].split("\n"):
                if para.strip():
                    lines.append(f"  {para.strip()}")
            lines.append("")

    return lines


def _render_digest_plain(sections: list[dict]) -> list[str]:
    """Fallback plain-text rendering."""
    return _render_digest_rich(sections)  # same logic, no rich needed


# ---------------------------------------------------------------------------
# Batch confirmation (listen --batch mode)
# ---------------------------------------------------------------------------

def confirm_batch_send(approved: list[dict], total_count: int, text_mode: bool = False) -> bool:
    """Ask for one final confirmation before sending the reviewed batch."""
    sendable = [i for i in approved if i.get("send", True)]
    print(f"\n{len(sendable)} из {total_count} сообщений будут отправлены.")

    if text_mode:
        try:
            confirm = input("Отправить все? [да/нет]: ").strip().lower()
            if confirm not in ("да", "д", "y", "yes"):
                print("Пакет отменён.")
                return False
        except (KeyboardInterrupt, EOFError):
            return False
    else:
        try:
            import questionary
            confirmed = _q(lambda: questionary.confirm("Отправить одобренные?").ask())
            if not confirmed:
                print("Пакет отменён.")
                return False
        except Exception:
            pass

    return True


def batch_confirm(
    batch: list[dict],
    text_mode: bool = False,
    confirm_send: bool = True,
    reformulate_fn=None,
) -> list[dict]:
    if not batch:
        return []

    print(f"\n{'='*60}")
    print(f"Пакетное подтверждение — {len(batch)} сообщений")
    print(f"{'='*60}\n")

    approved = []
    for item in batch:
        current = dict(item)
        while True:
            print(f"От: {current['sender']}")
            print(f"Сообщение: {current['message']}")
            action, typed = action_menu(current["reply"], text_mode=text_mode)

            if action == ACTION_SEND:
                approved.append(current)
                break

            if action == ACTION_EDIT:
                if typed:
                    new_text = typed
                else:
                    new_text = edit_message(current["reply"], text_mode=text_mode)
                if not (new_text or "").strip():
                    current = dict(current, send=False)
                    approved.append(current)
                    break
                current = dict(current, reply=new_text)
                approved.append(current)
                break

            if action == ACTION_MANUAL:
                new_text = edit_message("", text_mode=text_mode)
                if not (new_text or "").strip():
                    current = dict(current, send=False)
                    approved.append(current)
                    break
                current = dict(current, reply=new_text)
                approved.append(current)
                break

            if action == ACTION_REFORMULATE:
                if reformulate_fn is not None:
                    try:
                        current = dict(current, reply=reformulate_fn(current["reply"]))
                    except Exception:
                        pass
                else:
                    current = dict(current, reply=edit_message(current["reply"], text_mode=text_mode))
                print()
                continue

            if action in (ACTION_SKIP, ACTION_REMEMBER):
                current = dict(current, send=False)
                approved.append(current)
                break

            break

        print()

    if not confirm_send:
        return approved

    if not confirm_batch_send(approved, len(batch), text_mode=text_mode):
        return []
    return approved
