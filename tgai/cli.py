"""CLI entry point and interactive session orchestration.

This module owns two layers:

1. Argument parsing and command dispatch for direct CLI usage.
2. The interactive main menu that keeps a single Telegram connection open
   while the user moves between chat, digest, listen, and agent workflows.

The command implementations live under ``tgai.commands.*``.  This file is
intentionally kept focused on routing and high-level control flow rather than
Telegram or LLM details.
"""

from __future__ import annotations

import sys
import time


async def _maybe_show_yandex_warning(config: dict, storage, text_mode: bool) -> None:
    """Offer YandexGPT setup before entering the main menu."""
    from tgai.claude import create_llm_client

    try:
        claude = create_llm_client(config)
    except Exception:
        return
    if getattr(claude, "provider_name", "") == "yandexgpt":
        return

    import asyncio
    from tgai.ui import smart_select, _C, clear

    clear()
    loop = asyncio.get_running_loop()
    title = (
        "YandexGPT не подключен.\n"
        "Для стабильной работы очень рекомендуется подключить YandexGPT.\n"
        "Без Яндекса AI-функции отключены.\n"
        "Посмотри инструкцию в README при настройке провайдера."
    )
    result = await loop.run_in_executor(
        None,
        lambda: smart_select(
            [
                _C("Enter: открыть настройки YandexGPT", "settings"),
                _C("Продолжить без ИИ", "continue"),
            ],
            title=title,
        ),
    )
    if result is None:
        return
    _, action = result
    if action == "settings":
        await _menu_settings(config, storage, text_mode)


def _build_parser():
    """Construct the top-level ``argparse`` parser for all tgai commands."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="tgai",
        description="Telegram + Claude AI ассистент",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  tgai chat                      # выбрать чат стрелками
  tgai chat @username            # открыть конкретный чат
  tgai chat "Никита"             # поиск по имени
  tgai listen --auto             # полный автопилот
  tgai listen --batch 30m        # режим подтверждения ответов
  tgai listen --persona work     # использовать персону work
  tgai aggregate                 # дайджест непрочитанных
  tgai aggregate --hours 12      # за последние 12 часов
  tgai aggregate --all           # включая прочитанные
  tgai aggregate --save          # сохранить в ~/.tgai/digests/
  tgai agent                     # режим агента на естественном языке
  (внутри чата: /s текст         # отправить без меню Claude)
""",
    )

    parser.add_argument(
        "--text",
        action="store_true",
        default=False,
        help="Текстовый режим (без стрелок, только клавиши)",
    )
    parser.add_argument(
        "--persona",
        metavar="FILE",
        default=None,
        help="Переопределить персону для любой команды (имя файла без .txt)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<команда>")

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------
    chat_parser = subparsers.add_parser(
        "chat",
        help="Открыть чат с AI-ассистентом",
        description="Открыть чат. Без аргументов — выбор стрелками.",
    )
    chat_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="@username, телефон или имя контакта",
    )
    chat_parser.add_argument("--text", action="store_true", default=False)
    chat_parser.add_argument("--persona", metavar="FILE", default=None)

    # ------------------------------------------------------------------
    # listen
    # ------------------------------------------------------------------
    listen_parser = subparsers.add_parser(
        "listen",
        help="Мониторинг входящих с AI-ответами",
        description="Следить за входящими сообщениями и отвечать через Claude.",
    )
    listen_group = listen_parser.add_mutually_exclusive_group()
    listen_group.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Отвечать автоматически без подтверждения",
    )
    listen_group.add_argument(
        "--batch",
        metavar="ИНТЕРВАЛ",
        default=None,
        help="Режим подтверждения ответов (значение сохраняется для совместимости CLI)",
    )
    listen_parser.add_argument("--persona", metavar="FILE", default=None)
    listen_parser.add_argument("--text", action="store_true", default=False)

    # ------------------------------------------------------------------
    # aggregate
    # ------------------------------------------------------------------
    agg_parser = subparsers.add_parser(
        "aggregate",
        help="Дайджест непрочитанных сообщений",
        description="Суммаризировать непрочитанные сообщения через Claude.",
    )
    agg_parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="За сколько часов (по умолчанию из config, обычно 24)",
    )
    agg_parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        dest="all",
        help="Включить прочитанные сообщения",
    )
    agg_parser.add_argument(
        "--save",
        action="store_true",
        default=False,
        help="Сохранить дайджест в ~/.tgai/digests/",
    )
    agg_parser.add_argument("--text", action="store_true", default=False)
    agg_parser.add_argument("--persona", metavar="FILE", default=None)

    # ------------------------------------------------------------------
    # agent
    # ------------------------------------------------------------------
    agent_parser = subparsers.add_parser(
        "agent",
        help="Агент на естественном языке",
        description=(
            "Claude с инструментами Telegram. "
            "Управляйте мессенджером на естественном языке."
        ),
    )
    agent_parser.add_argument("--persona", metavar="FILE", default=None)
    agent_parser.add_argument("--text", action="store_true", default=False)

    return parser


def main() -> None:
    """Validate configuration, then route to a direct command or menu session."""
    parser = _build_parser()
    args = parser.parse_args()

    # Load config and storage
    from tgai.config import load_config, ensure_directories
    from tgai.storage import Storage

    ensure_directories()

    config = load_config()
    storage = Storage()

    # Validate config has required fields
    tg_cfg = config.get("telegram", {})

    if not tg_cfg.get("api_id") or not tg_cfg.get("api_hash"):
        print("Ошибка: не настроен Telegram API. Удалите ~/.tgai/config.json и запустите снова.")
        sys.exit(1)

    # Route to command
    command = getattr(args, "command", None)

    if command is None:
        _run_interactive(config, storage, args)
        return

    if command == "chat":
        from tgai.commands.chat import run
        run(args, config, storage)

    elif command == "listen":
        from tgai.commands.listen import run
        run(args, config, storage)

    elif command == "aggregate":
        from tgai.commands.aggregate import run
        run(args, config, storage)

    elif command == "agent":
        from tgai.commands.agent import run
        run(args, config, storage)

    else:
        parser.print_help()
        sys.exit(1)


def _run_interactive(config: dict, storage, args) -> None:
    """Launch the interactive menu with one shared Telegram session.

    Interactive mode is the primary UX for the project.  We keep a single
    ``TelegramManager`` and LLM client alive so switching between menu items
    does not reconnect to Telegram every time.
    """
    import asyncio
    from pathlib import Path
    from tgai.telegram import TelegramManager, SessionLockedError
    from tgai.claude import create_llm_client

    tg_cfg = config.get("telegram", {})
    defaults = config.get("defaults", {})
    text_mode = getattr(args, "text", False)

    tg = TelegramManager(
        api_id=tg_cfg["api_id"],
        api_hash=tg_cfg["api_hash"],
        session_path=str(Path.home() / ".tgai" / "session"),
    )
    claude = create_llm_client(config)
    async def menu_loop():
        listen_collector = None
        clear_on_exit = False
        await tg.start()
        try:
            await _maybe_show_yandex_warning(config, storage, text_mode)
            claude = create_llm_client(config)
            persona_name = getattr(args, "persona", None) or defaults.get("persona", "default")
            persona = storage.load_persona(persona_name)
            if getattr(claude, "provider_name", "") == "yandexgpt":
                from tgai.commands.listen import ListenBackgroundCollector
                listen_collector = ListenBackgroundCollector(tg, claude, storage, persona)
                await listen_collector.start()
            clear_on_exit = await _async_main_menu(
                tg, claude, config, storage, persona, text_mode, defaults, listen_collector
            )
        finally:
            if listen_collector is not None:
                try:
                    await listen_collector.stop()
                except Exception:
                    pass
            await tg.stop()
        return clear_on_exit

    try:
        clear_on_exit = asyncio.run(menu_loop())
        if clear_on_exit:
            storage.clear_user_local_state(preserve_telegram_app=True)
    except SessionLockedError as e:
        print(f"\nОшибка: {e}")
    except KeyboardInterrupt:
        print("\nВыход.")


async def _async_main_menu(
    tg,
    claude,
    config,
    storage,
    persona: str,
    text_mode: bool,
    defaults: dict,
    listen_collector=None,
) -> None:
    """Async main menu loop — stays connected between actions."""
    import asyncio
    from tgai.commands.listen import ListenBackgroundCollector
    from tgai.claude import InsufficientCreditsError, create_llm_client
    from tgai.ui import smart_select, _C, clear

    clear_on_exit = False

    async def _recreate_listen_collector(current):
        if current is not None:
            try:
                await current.stop()
            except Exception:
                pass
        if getattr(claude, "provider_name", "") != "yandexgpt":
            return None
        collector = ListenBackgroundCollector(tg, claude, storage, persona)
        await collector.start()
        return collector

    while True:
        menu_choices = [
            _C("Чат", "chat"),
            _C("Настройки", "settings"),
            _C("Выход и очистка", "exit"),
        ]
        if getattr(claude, "provider_name", "") == "yandexgpt":
            menu_choices.insert(1, _C("Дайджест", "aggregate"))
            menu_choices.insert(2, _C("Слушать", "listen"))

        clear()
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: smart_select(menu_choices, title="tgai:")
        )
        if result is None:
            break
        _, choice = result

        if choice == "exit":
            clear_on_exit = True
            break

        try:
            if choice == "chat":
                await _menu_chat(tg, claude, storage, persona, text_mode, listen_collector)

            elif choice == "aggregate":
                await _menu_aggregate(tg, claude, storage, persona, text_mode, defaults, listen_collector)

            elif choice == "listen":
                await _menu_listen(tg, claude, storage, persona, text_mode, defaults, listen_collector)
                listen_collector = await _recreate_listen_collector(listen_collector)

            elif choice == "settings":
                await _menu_settings(config, storage, text_mode)
                # Reload LLM client after settings change
                try:
                    claude = create_llm_client(config)
                except Exception:
                    pass
                listen_collector = await _recreate_listen_collector(listen_collector)

        except InsufficientCreditsError:
            print("\nБаланс API исчерпан.")
            print("   Пополните баланс провайдера или добавьте другой в Настройках.")
        except (KeyboardInterrupt, asyncio.CancelledError, EOFError):
            print()

    return clear_on_exit


async def _menu_chat(tg, claude, storage, persona, text_mode, listen_collector=None):
    import asyncio
    from tgai.commands.chat import _run_chat, _poll_chat_list
    from tgai.ui import select_chat_interactive, select_chat_text, clear, _pager

    clear()
    
    # --- OPTIMIZATION: REMOVED BLOCKING FETCH ---
    # We no longer wait for dialogs/folders here. 
    # The chat command will handle lazy loading.
    dialogs = []
    folders = []

    me = await tg.get_me()
    loop = asyncio.get_running_loop()
    dialogs_holder: list | None = None
    app_holder: list | None = None

    async def _refresh_dialogs_after_return() -> list:
        return await tg.get_dialogs_fresh(limit=100)

    def _refresh_dialogs_background() -> None:
        async def _run():
            nonlocal dialogs
            try:
                latest = await _refresh_dialogs_after_return()
                dialogs = latest
                if dialogs_holder is not None:
                    dialogs_holder[0] = latest
                if app_holder is not None and app_holder:
                    app = app_holder[0]
                    if app is not None and getattr(app, "is_running", False):
                        app.invalidate()
            except Exception:
                pass
        asyncio.create_task(_run())

    def _preview_fn(dialog, show_usernames: bool = False):
        """Sync preview: fetch messages and show pager (called from executor thread)."""
        from tgai.ui import format_messages
        from tgai.telegram import _dialog_display_name, display_name_for_ui
        try:
            msgs = asyncio.run_coroutine_threadsafe(
                tg.get_messages(dialog.entity, limit=30), loop
            ).result(timeout=10)
        except Exception:
            msgs = []
        lines = format_messages(msgs, me.id) if msgs else ["Нет сообщений"]
        name = display_name_for_ui(_dialog_display_name(dialog), show_username=False)
        _pager(lines, status_hint=f"{name}  ↑↓ прокрутка  q назад")

    def _open_fn(dialog):
        """Open chat, blocking until user exits. Called from executor thread."""
        nonlocal dialogs
        asyncio.run_coroutine_threadsafe(
            _open_and_refresh(dialog), loop
        ).result()

    async def _open_and_refresh(dialog):
        nonlocal dialogs
        clear()
        try:
            setattr(dialog, "unread_count", 0)
            if dialogs_holder:
                app_dialogs = dialogs_holder[0]
                for d in app_dialogs:
                    if getattr(getattr(d, "entity", None), "id", None) == getattr(getattr(dialog, "entity", None), "id", None):
                        setattr(d, "unread_count", 0)
                        break
        except Exception:
            pass
        latest_message = await _run_chat(tg, claude, storage, dialog.entity, persona, text_mode)
        try:
            if latest_message is not None:
                setattr(dialog, "message", latest_message)
                if dialogs_holder:
                    app_dialogs = dialogs_holder[0]
                    for d in app_dialogs:
                        if getattr(getattr(d, "entity", None), "id", None) == getattr(getattr(dialog, "entity", None), "id", None):
                            setattr(d, "message", latest_message)
                            break
        except Exception:
            pass
        _refresh_dialogs_background()
        clear()

    if text_mode:
        # Text mode: simple loop
        while True:
            dialog = select_chat_text(dialogs)
            if dialog is None:
                return
            clear()
            latest_message = await _run_chat(tg, claude, storage, dialog.entity, persona, text_mode)
            try:
                if latest_message is not None:
                    setattr(dialog, "message", latest_message)
            except Exception:
                pass
            _refresh_dialogs_background()
            clear()
        return

    # Interactive mode: select_chat_interactive handles the full loop
    dialogs_holder = [dialogs]
    app_holder = []
    _poll_task = asyncio.create_task(
        _poll_chat_list(tg, dialogs_holder, app_holder)
    )
    try:
        await loop.run_in_executor(
            None,
            lambda: select_chat_interactive(
                dialogs, folders,
                dialogs_holder=dialogs_holder,
                app_holder=app_holder,
                preview_fn=_preview_fn,
                open_fn=_open_fn,
            ),
        )
    finally:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass


async def _menu_agent(tg, claude, storage, persona, text_mode):
    from tgai.commands.agent import TOOLS, AsyncToolExecutor, _async_agent_loop

    agent_system = (
        persona + "\n\n"
        "Ты — AI-агент для управления Telegram. "
        "Используй доступные инструменты для выполнения задач пользователя. "
        "Перед отправкой сообщений или изменением данных всегда уточняй намерение. "
        "Когда просят обзор событий — сделай одно краткое сообщение: "
        "кто что написал, кому ответить, список действий (если есть) в конце. "
        "Отвечай на русском, по-человечески, без формальностей."
    )
    executor = AsyncToolExecutor(tg, claude, storage)
    await _async_agent_loop(claude, TOOLS, executor, agent_system, text_mode)


async def _menu_aggregate(tg, claude, storage, persona, text_mode, defaults, listen_collector=None):
    import asyncio
    from tgai.commands.aggregate import _run_aggregate
    from tgai.ui import smart_select, _C, clear
    from tgai.claude import CONTEXT_LEVELS

    clear()
    loop = asyncio.get_running_loop()

    # Offer quick reopen if last sections exist
    last = storage.load_last_digest_settings()
    last_sections = storage.load_last_sections()
    if last_sections:
        scope_label = {"all": "чаты+каналы", "chats": "чаты", "channels": "каналы"}.get(
            (last or {}).get("scope", "all"), "все")
        inc_label = "все" if (last or {}).get("include_all") else "непрочит."
        hours = (last or {}).get("hours", "?")
        result = await loop.run_in_executor(
            None, lambda: smart_select([
                _C(f"Последний дайджест ({hours}ч, {inc_label}, {scope_label})", "last"),
                _C("Новый дайджест", "new"),
            ], title="Дайджест:")
        )
        if result is None:
            return
        _, action = result
        if action == "last":
            if last:
                claude.max_history = CONTEXT_LEVELS.get(last.get("ctx_level", "base"), 5)
            await _open_last_sections(tg, claude, storage, last_sections, text_mode, listen_collector)
            return

    # What to summarize?
    SCOPE_OPTIONS = [
        _C("Всё (чаты + каналы)", "all"),
        _C("Только личные чаты", "chats"),
        _C("Только каналы", "channels"),
    ]
    result = await loop.run_in_executor(
        None, lambda: smart_select(SCOPE_OPTIONS, title="Что суммаризировать?")
    )
    if result is None:
        return
    _, scope = result

    # Hours
    HOUR_OPTIONS = [
        _C("1 час", 1),
        _C("4 часа", 4),
        _C("12 часов", 12),
        _C("24 часа", 24),
    ]
    result = await loop.run_in_executor(
        None, lambda: smart_select(HOUR_OPTIONS, title="Дайджест за:")
    )
    if result is None:
        return
    _, hours = result

    # Include all?
    YN = [_C("Нет", False), _C("Да", True)]
    result = await loop.run_in_executor(
        None, lambda: smart_select(YN, title="Включить прочитанные?")
    )
    if result is None:
        return
    _, include_all = result

    # Save?
    result = await loop.run_in_executor(
        None, lambda: smart_select(YN, title="Показать путь к файлу?")
    )
    if result is None:
        return
    _, save = result

    # Context size
    from tgai.claude import CONTEXT_LEVELS
    ctx_choices = [
        _C(f"Базовый ({CONTEXT_LEVELS['base']} сообщ.)", "base"),
        _C(f"Средний ({CONTEXT_LEVELS['medium']} сообщ.)", "medium"),
        _C(f"Расширенный ({CONTEXT_LEVELS['extended']} сообщ.)", "extended"),
    ]
    result = await loop.run_in_executor(
        None, lambda: smart_select(ctx_choices, title="Контекст на чат:")
    )
    if result is None:
        return
    _, ctx_level = result
    claude.max_history = CONTEXT_LEVELS[ctx_level]

    storage.save_last_digest_settings({
        "hours": hours, "include_all": include_all,
        "save": save, "scope": scope, "ctx_level": ctx_level,
    })
    await _run_aggregate(
        tg, claude, storage, hours, include_all, save, text_mode, scope=scope, listen_collector=listen_collector
    )


async def _open_last_sections(tg, claude, storage, sections: list, text_mode: bool, listen_collector=None) -> None:
    """Open the previously saved digest and reconcile it with current Telegram state.

    The saved digest is shown immediately from local storage for snappy UX.
    A background task then:
    - resolves Telegram entities back onto saved sections,
    - refreshes unread counters,
    - removes sections that no longer belong in unread-only mode,
    - catches up summaries for messages newer than stored watermarks,
    - discovers newly eligible chats or channels.
    """
    import asyncio
    from tgai.ui import digest_viewer, _DIGEST_REFRESH
    from tgai.commands.aggregate import (
        _collect_new_digest_sections,
        _merge_sections_unique,
        _normalize_summary_text,
        _poll_digest_updates,
        _sort_digest_sections,
    )

    loop = asyncio.get_running_loop()
    _entity_cache: dict = {}
    _sort_digest_sections(sections)
    for sec in sections:
        sec["summary"] = _normalize_summary_text(sec.get("summary", ""))
    storage.save_last_sections(sections)
    summarize_enabled = getattr(claude, "provider_name", "") == "yandexgpt"

    async def _run_viewer_once(app_holder: list):
        for _attempt in range(2):
            started = time.monotonic()
            chosen = await loop.run_in_executor(
                None, lambda s=sections: digest_viewer(s, app_holder=app_holder)
            )
            if chosen is not None or (time.monotonic() - started) > 0.35:
                return chosen
            app_holder.clear()
        return None

    async def _quick_refresh_sections() -> None:
        """Fast pre-open refresh for the saved digest.

        This path runs before the first viewer paint, so keep it cheap:
        one regular dialogs snapshot is enough to refresh entities and unread
        counters. The heavier settle/catch-up work continues in background
        tasks after the digest is already visible.
        """
        try:
            from tgai.telegram import _dialog_display_name
            dialogs = await tg.get_dialogs(limit=200)
            unread_map = {}
            for d in dialogs:
                name = _dialog_display_name(d)
                _entity_cache[name] = d.entity
                unread_map[name] = getattr(d, "unread_count", 0)

            for sec in sections:
                if sec.get("entity") is None:
                    entity = _entity_cache.get(sec.get("name", ""))
                    if entity is not None:
                        sec["entity"] = entity
                name = sec.get("name", "")
                if name in unread_map:
                    sec["unread_count"] = unread_map[name]

            last_cfg = storage.load_last_digest_settings() or {}
            if not last_cfg.get("include_all", False):
                unread_names = {
                    _dialog_display_name(d) for d in dialogs
                    if d.unread_count > 0
                }
                sections[:] = [
                    s for s in sections
                    if s.get("is_channel") or s.get("name") in unread_names
                ]
            _sort_digest_sections(sections)
            storage.save_last_sections(sections)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _resolve_and_catchup(app_holder: list) -> None:
        """Background: resolve entities → filter read chats → fetch missed messages → prepend summaries."""
        any_updated = False
        try:
            from tgai.telegram import _dialog_display_name
            dialogs = await tg.get_dialogs_fresh(limit=200)
            for d in dialogs:
                _entity_cache[_dialog_display_name(d)] = d.entity
            unread_map = {
                _dialog_display_name(d): getattr(d, "unread_count", 0)
                for d in dialogs
            }
            for sec in sections:
                if sec.get("entity") is None:
                    e = _entity_cache.get(sec.get("name", ""))
                    if e:
                        sec["entity"] = e
                name = sec.get("name", "")
                if name in unread_map and sec.get("unread_count", 0) != unread_map[name]:
                    sec["unread_count"] = unread_map[name]
                    any_updated = True

            # Remove sections for chats that have been fully read
            last_cfg = storage.load_last_digest_settings() or {}
            if not last_cfg.get("include_all", False):
                # Unread-only digest: drop sections where chat is now read
                unread_names = {
                    _dialog_display_name(d) for d in dialogs
                    if d.unread_count > 0
                }
                removed = [s for s in sections if s.get("name") not in unread_names and not s.get("is_channel")]
                for s in removed:
                    sections.remove(s)
                if removed:
                    # Persist immediately so read chats don't reappear on next open
                    storage.save_last_sections(sections)
                    app = app_holder[0] if app_holder else None
                    if app and getattr(app, "is_running", False):
                        app.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

        # Catch up on messages since last build
        # Use dialog metadata to skip chats with no new messages (no extra API calls)
        watermarks = storage.load_watermarks()
        dialog_last_msg_id: dict[int, int] = {}
        for d in dialogs:
            eid = getattr(d.entity, "id", None)
            mid = getattr(d.message, "id", None)
            if eid is not None and mid is not None:
                dialog_last_msg_id[eid] = mid

        for sec in sections:
            entity = sec.get("entity")
            if not entity:
                continue
            name = sec.get("name", "")
            wm = watermarks.get(name)
            if wm is None:
                continue
            # Skip API call if dialog's last message ID <= watermark
            eid = getattr(entity, "id", None)
            last_mid = dialog_last_msg_id.get(eid)
            if last_mid is not None and last_mid <= wm:
                sec["_poll_last_id"] = wm
                continue
            try:
                msgs = await tg.get_messages(entity, limit=50)
                if not msgs:
                    continue
                new_msgs = [m for m in msgs if m.id > wm and getattr(m, "text", None)]
                if not new_msgs:
                    sec["_poll_last_id"] = max(m.id for m in msgs)
                    continue
                sec["_poll_last_id"] = max(m.id for m in msgs)
                msg_dicts = [
                    {"sender": "?", "text": m.text,
                     "date": m.date.strftime("%H:%M") if m.date else ""}
                    for m in reversed(new_msgs[:20])
                ]
                if msg_dicts:
                    new_part = await loop.run_in_executor(
                        None, lambda d=msg_dicts: claude.summarize_messages(d)
                    )
                    sec["summary"] = _normalize_summary_text(new_part)
                    any_updated = True
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        if any_updated:
            _sort_digest_sections(sections)
            storage.save_last_sections(sections)
            # Update watermarks
            for sec in sections:
                pid = sec.get("_poll_last_id")
                if pid:
                    watermarks[sec.get("name", "")] = pid
            storage.save_watermarks(watermarks)

        # Also discover brand-new chats/channels that now match the digest filter
        last_cfg = storage.load_last_digest_settings() or {}
        try:
            new_sections, watermarks = await _collect_new_digest_sections(
                tg, claude, storage, sections, (await tg.get_me()).id,
                watermarks, last_cfg.get("hours"),
                last_cfg.get("include_all", False), last_cfg.get("scope", "all"),
            )
            if _merge_sections_unique(sections, new_sections):
                _sort_digest_sections(sections)
                storage.save_last_sections(sections)
                storage.save_watermarks(watermarks)
                any_updated = True
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

        # Trigger re-render
        app = app_holder[0] if app_holder else None
        if app is not None:
            try:
                if app.is_running:
                    app.invalidate()
            except Exception:
                pass

    _should_refresh = True
    _initial_launch = True
    while _should_refresh:
        _should_refresh = False
        app_holder: list = []
        
        # Start background tasks
        _quick_task = asyncio.create_task(_quick_refresh_sections())
        _catchup_task = asyncio.create_task(_resolve_and_catchup(app_holder))
        
        _watermarks = storage.load_watermarks()
        _last_cfg = storage.load_last_digest_settings() or {}
        _poll_task = asyncio.create_task(
            _poll_digest_updates(
                tg, claude, sections, app_holder,
                watermarks=_watermarks,
                storage=storage,
                hours=_last_cfg.get("hours"),
                include_all=_last_cfg.get("include_all", False),
                scope=_last_cfg.get("scope", "all"),
            )
        )

        try:
            # First launch is now instant, no blocking await
            chosen = await _run_viewer_once(app_holder)
        finally:
            for t in (_poll_task, _catchup_task, _quick_task):
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        if chosen == _DIGEST_REFRESH:
            _should_refresh = True
            _needs_blocking_refresh = True
            continue

        if chosen is None:
            break

        name = chosen.get("name", "")
        entity = chosen.get("entity") or _entity_cache.get(name)
        if entity is None:
            try:
                from tgai.telegram import _dialog_display_name
                dialogs = await tg.get_dialogs(limit=200)
                for d in dialogs:
                    _entity_cache[_dialog_display_name(d)] = d.entity
                entity = _entity_cache.get(name)
            except Exception:
                pass
        if entity:
            print(f"Открываю {name}...")
            from tgai.commands.chat import _run_chat
            persona_text = storage.load_persona("default")
            if listen_collector is not None:
                listen_collector.pause()
            try:
                await _run_chat(tg, claude, storage, entity, persona_text, text_mode)
            finally:
                if listen_collector is not None:
                    listen_collector.resume()
            _should_refresh = True
            _needs_blocking_refresh = False


async def _menu_listen(tg, claude, storage, persona, text_mode, defaults, listen_collector=None):
    """Listen submenu: auto / batch."""
    import asyncio
    from tgai.commands.listen import _listen_auto, _listen_batch, _listen_pending_forever
    from tgai.ui import smart_select, _C, clear

    clear()
    loop = asyncio.get_running_loop()

    MODE_CHOICES = [
        _C("Автопилот", "auto"),
        _C("С подтверждением", "batch"),
    ]
    result = await loop.run_in_executor(
        None, lambda: smart_select(MODE_CHOICES, title="Режим:")
    )
    if result is None:
        return
    _, mode = result

    if mode == "auto":
        print("Режим автопилота. Ctrl+C -- назад.\n")
        try:
            if listen_collector is not None:
                await listen_collector.stop()
            await _listen_auto(tg, claude, storage, persona)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            if listen_collector is not None:
                await listen_collector.start()
    else:
        print("Режим подтверждения. Первое сообщение показывается сразу. Ctrl+C -- назад.\n")
        try:
            if listen_collector is not None:
                await _listen_pending_forever(listen_collector, tg, claude, storage, text_mode)
            else:
                await _listen_batch(tg, claude, storage, persona, 0, text_mode)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass


async def _menu_settings(config: dict, storage, text_mode: bool) -> None:
    """Settings menu — manage LLM providers."""
    import asyncio
    import questionary
    import questionary as _q2
    import questionary as _qf
    from tgai.claude import validate_yandex_credentials
    from tgai.ui import smart_select, _C, clear
    from tgai.config import save_config, PROVIDER_INFO, DEFAULT_MODELS

    loop = asyncio.get_running_loop()

    def _wait_for_enter() -> None:
        prompt = "Нажмите Enter, чтобы продолжить..."
        try:
            questionary.text(prompt, default="").ask()
        except Exception:
            input(f"{prompt}\n")

    while True:
        clear()
        # Show current providers
        llm_cfg = config.get("llm", [])
        if not isinstance(llm_cfg, list):
            llm_cfg = [llm_cfg] if isinstance(llm_cfg, dict) else []

        # Also show legacy anthropic if present
        legacy_key = config.get("anthropic", {}).get("api_key")
        if legacy_key and not llm_cfg:
            llm_cfg = [{"provider": "anthropic", "api_key": legacy_key}]

        print("\n--- LLM-провайдеры ---")
        if llm_cfg:
            for i, entry in enumerate(llm_cfg, 1):
                p = entry.get("provider", "?")
                m = entry.get("model", DEFAULT_MODELS.get(p, "?"))
                key = entry.get("api_key", "")
                masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
                label = PROVIDER_INFO.get(p, {}).get("label", p)
                print(f"  {i}. {label}  model={m}  key={masked}")
        else:
            print("  Нет настроенных провайдеров.")
        print()

        choices = [
            _C("Добавить провайдер", "add"),
            _C("Удалить провайдер", "remove"),
            _C("Контекст", "context"),
        ]

        result = await loop.run_in_executor(
            None, lambda: smart_select(choices, title="Настройки:")
        )
        if result is None:
            return  # back to main menu
        _, action = result

        if action == "add":
            available = [
                _C(info["label"], name)
                for name, info in PROVIDER_INFO.items()
            ]
            result = await loop.run_in_executor(
                None, lambda: smart_select(available, title="Провайдер:")
            )
            if result is None:
                continue
            _, provider = result

            needs_key = PROVIDER_INFO[provider].get("needs_key", True)
            if needs_key:
                api_key = await loop.run_in_executor(
                    None, lambda: questionary.password(f"API ключ для {PROVIDER_INFO[provider]['label']}:").ask()
                )
                if not api_key:
                    continue
                api_key = api_key.strip()
            else:
                api_key = ""

            default_model = DEFAULT_MODELS.get(provider, "")
            model = await loop.run_in_executor(
                None, lambda: _q2.text("Модель:", default=default_model).ask()
            )
            if model is None:
                continue

            entry = {"provider": provider, "api_key": api_key, "model": model.strip()}
            if provider == "openrouter":
                entry["base_url"] = "https://openrouter.ai/api/v1"
            if PROVIDER_INFO[provider].get("needs_folder_id"):
                folder_id = await loop.run_in_executor(
                    None, lambda: _qf.text("Yandex Folder ID:").ask()
                )
                if not folder_id:
                    continue
                entry["folder_id"] = folder_id.strip()

            ok, error = await loop.run_in_executor(
                None,
                lambda: validate_yandex_credentials(
                    entry.get("api_key", ""),
                    entry.get("folder_id", ""),
                    entry.get("model", ""),
                ),
            )
            if not ok:
                print(error)
                print("Провайдер не сохранен. Проверь API ключ, folder_id и инструкцию в README.")
                await loop.run_in_executor(None, _wait_for_enter)
                continue

            llm_cfg.append(entry)
            config["llm"] = llm_cfg
            config.pop("anthropic", None)
            save_config(config)
            print(f"  {PROVIDER_INFO[provider]['label']} добавлен.")
            await loop.run_in_executor(None, _wait_for_enter)

        elif action == "remove":
            if not llm_cfg:
                print("Нечего удалять.")
                continue

            remove_choices = [
                _C(
                    f"{PROVIDER_INFO.get(e.get('provider','?'), {}).get('label', e.get('provider','?'))} ({e.get('model', '?')})",
                    i,
                )
                for i, e in enumerate(llm_cfg)
            ]
            result = await loop.run_in_executor(
                None, lambda: smart_select(remove_choices, title="Удалить:")
            )
            if result is None:
                continue
            _, idx = result

            removed = llm_cfg.pop(idx)
            config["llm"] = llm_cfg
            config.pop("anthropic", None)
            save_config(config)
            p = removed.get("provider", "?")
            print(f"  {PROVIDER_INFO.get(p, {}).get('label', p)} удален.")
            await loop.run_in_executor(None, _wait_for_enter)

        elif action == "context":
            await _menu_context_settings(config, text_mode)


async def _menu_context_settings(config: dict, text_mode: bool) -> None:
    """Configure context window size for LLM calls."""
    import asyncio
    from tgai.ui import smart_select, _C
    from tgai.config import save_config

    defaults = config.setdefault("defaults", {})
    current = defaults.get("context_level", "base")

    LEVELS = [
        _C("Базовый (5 сообщений)", "base"),
        _C("Средний (10 сообщений)", "medium"),
        _C("Расширенный (20 сообщений)", "extended"),
    ]

    names = {"base": "Базовый", "medium": "Средний", "extended": "Расширенный"}
    print(f"\nТекущий: {names.get(current, current)}")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: smart_select(LEVELS, title="Уровень контекста:")
    )
    if result is None:
        return

    _, chosen = result
    defaults["context_level"] = chosen
    config["defaults"] = defaults
    save_config(config)
    print(f"  Контекст: {names[chosen]}")


if __name__ == "__main__":
    main()
