"""agent command — natural language agent with Telegram tool use."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_unread_messages",
        "description": "Получить непрочитанные сообщения из Telegram",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Максимальное количество сообщений (по умолчанию 50)",
                },
                "folder": {
                    "type": "string",
                    "description": "Название папки Telegram (опционально)",
                },
            },
        },
    },
    {
        "name": "send_message",
        "description": "Отправить сообщение пользователю в Telegram",
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Имя пользователя, телефон или имя контакта",
                },
                "message": {
                    "type": "string",
                    "description": "Текст сообщения",
                },
            },
            "required": ["username", "message"],
        },
    },
    {
        "name": "search_messages",
        "description": "Поиск сообщений в чатах Telegram",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос",
                },
                "username": {
                    "type": "string",
                    "description": "Искать только в этом чате (опционально)",
                },
                "days": {
                    "type": "integer",
                    "description": "Сколько дней назад искать (по умолчанию 7)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_chats",
        "description": "Список чатов/диалогов в Telegram",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Фильтровать по папке (опционально)",
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Показывать только непрочитанные",
                },
            },
        },
    },
    {
        "name": "aggregate_and_summarize",
        "description": "Агрегировать и суммаризировать непрочитанные сообщения",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "За сколько часов брать сообщения (по умолчанию 24)",
                },
                "save": {
                    "type": "boolean",
                    "description": "Сохранить дайджест в файл",
                },
            },
        },
    },
    {
        "name": "add_to_whitelist",
        "description": "Добавить группу в белый список для агрегации",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_name": {
                    "type": "string",
                    "description": "Название группы для добавления",
                },
            },
            "required": ["group_name"],
        },
    },
    {
        "name": "read_chat",
        "description": "Прочитать последние сообщения из конкретного чата",
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Имя пользователя, телефон или название чата",
                },
                "limit": {
                    "type": "integer",
                    "description": "Количество сообщений (по умолчанию 20)",
                },
            },
            "required": ["username"],
        },
    },
    {
        "name": "get_saved_digest",
        "description": "Получить последний сохранённый дайджест (суммаризации чатов)",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "set_keyword_alert",
        "description": "Установить оповещение по ключевому слову во входящих сообщениях",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Ключевое слово для отслеживания",
                },
                "action": {
                    "type": "string",
                    "enum": ["notify", "auto_reply"],
                    "description": "Действие: notify (оповестить) или auto_reply (авто-ответ)",
                },
            },
            "required": ["keyword", "action"],
        },
    },
]


# ---------------------------------------------------------------------------
# Async tool executor
# ---------------------------------------------------------------------------

class AsyncToolExecutor:
    """Executes agent tools asynchronously via Telegram and storage."""

    def __init__(self, tg, claude, storage) -> None:
        self.tg = tg
        self.claude = claude
        self.storage = storage

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        """Dispatch tool call to async handler. Returns string result."""
        handlers = {
            "get_unread_messages": self._get_unread_messages,
            "send_message": self._send_message,
            "search_messages": self._search_messages,
            "list_chats": self._list_chats,
            "aggregate_and_summarize": self._aggregate_and_summarize,
            "add_to_whitelist": self._add_to_whitelist,
            "set_keyword_alert": self._set_keyword_alert,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return f"Неизвестный инструмент: {tool_name}"
        return await handler(tool_input)

    async def _get_unread_messages(self, inp: dict) -> str:
        limit = inp.get("limit", 50)
        folder = inp.get("folder")
        dialogs = await self.tg.get_dialogs(folder=folder, unread_only=True, limit=limit)
        if not dialogs:
            return "Нет непрочитанных сообщений."
        lines = []
        for dialog in dialogs[:20]:
            name = self.tg.dialog_display_name(dialog)
            unread = dialog.unread_count
            last_text = ""
            if dialog.message and getattr(dialog.message, "text", None):
                last_text = dialog.message.text[:80]
            lines.append(f"• {name} [{unread} непрочитанных]: {last_text}")
        return "\n".join(lines)

    async def _send_message(self, inp: dict) -> str:
        username = inp.get("username", "")
        message = inp.get("message", "")
        if not username or not message:
            return "Ошибка: нужно указать username и message."
        try:
            entity = await self.tg.resolve_entity(username)
            await self.tg.send_message(entity, message)
            from tgai.telegram import _entity_display_name
            return f"Сообщение отправлено: {_entity_display_name(entity)}"
        except Exception as e:
            return f"Ошибка отправки: {e}"

    async def _search_messages(self, inp: dict) -> str:
        query = inp.get("query", "")
        username = inp.get("username")
        days = inp.get("days", 7)
        entity = None
        if username:
            try:
                entity = await self.tg.resolve_entity(username)
            except Exception:
                return f"Не удалось найти чат: {username}"
        try:
            messages = await self.tg.search_messages(query, entity=entity, days=days)
        except Exception as e:
            return f"Ошибка поиска: {e}"
        if not messages:
            return f"Ничего не найдено по запросу «{query}»."
        lines = [f"Найдено {len(messages)} сообщений по «{query}»:"]
        from tgai.telegram import _entity_display_name
        for m in messages[:10]:
            sender = getattr(m, "sender", None)
            sender_name = _entity_display_name(sender) if sender else "?"
            date_str = m.date.strftime("%Y-%m-%d %H:%M") if m.date else ""
            lines.append(f"  [{date_str}] {sender_name}: {m.text[:100]}")
        return "\n".join(lines)

    async def _list_chats(self, inp: dict) -> str:
        folder = inp.get("folder")
        unread_only = inp.get("unread_only", False)
        try:
            dialogs = await self.tg.get_dialogs(
                folder=folder, unread_only=unread_only, limit=50
            )
        except Exception as e:
            return f"Ошибка получения чатов: {e}"
        if not dialogs:
            return "Нет подходящих чатов."
        lines = [f"Найдено {len(dialogs)} чатов:"]
        for dialog in dialogs[:20]:
            name = self.tg.dialog_display_name(dialog)
            unread = f" [{dialog.unread_count}]" if dialog.unread_count > 0 else ""
            lines.append(f"  • {name}{unread}")
        return "\n".join(lines)

    async def _aggregate_and_summarize(self, inp: dict) -> str:
        hours = inp.get("hours", 24)
        save = inp.get("save", False)
        whitelist_ids = self.storage.load_whitelist_ids()
        try:
            messages_by_chat, messages_by_channel = await self.tg.get_unread(
                hours=hours, whitelist=whitelist_ids
            )
        except Exception as e:
            return f"Ошибка загрузки сообщений: {e}"
        all_messages = {**messages_by_chat, **messages_by_channel}
        if not all_messages:
            return "Нет непрочитанных сообщений."

        summaries: dict[str, str] = {}
        for chat_name, messages in all_messages.items():
            msg_dicts = []
            for m in reversed(messages):
                if not getattr(m, "text", None):
                    continue
                sender = getattr(m, "sender", None)
                from tgai.telegram import _entity_display_name
                sender_name = _entity_display_name(sender) if sender else "?"
                date_str = m.date.strftime("%H:%M") if m.date else ""
                msg_dicts.append({"sender": sender_name, "text": m.text, "date": date_str})
            if msg_dicts:
                try:
                    summary = self.claude.summarize_messages(msg_dicts)
                    summaries[chat_name] = summary
                except Exception as e:
                    summaries[chat_name] = f"Ошибка: {e}"

        lines = [f"Дайджест за последние {hours} ч.:"]
        for chat_name, summary in summaries.items():
            lines.append(f"\n## {chat_name}")
            lines.append(summary)

        result = "\n".join(lines)
        if save:
            path = self.storage.save_digest(result)
            result += f"\n\nСохранено: {path}"
        return result

    async def _add_to_whitelist(self, inp: dict) -> str:
        group_name = inp.get("group_name", "")
        if not group_name:
            return "Ошибка: нужно указать group_name."
        try:
            results = await self.tg.search_contacts(group_name)
        except Exception as e:
            return f"Ошибка поиска группы: {e}"
        from telethon.tl.types import Chat, Channel
        groups = [r for r in results if isinstance(r, (Chat, Channel))]
        if not groups:
            return f"Группа «{group_name}» не найдена."
        group = groups[0]
        self.storage.add_to_whitelist(group.id, getattr(group, "title", group_name))
        return f"Группа «{getattr(group, 'title', group_name)}» добавлена в белый список."

    async def _set_keyword_alert(self, inp: dict) -> str:
        keyword = inp.get("keyword", "")
        action = inp.get("action", "notify")
        if not keyword:
            return "Ошибка: нужно указать keyword."
        self.storage.add_alert(keyword, action)
        action_label = "оповещение" if action == "notify" else "авто-ответ"
        return f"Оповещение установлено: «{keyword}» → {action_label}"


# ---------------------------------------------------------------------------
# Async agent loop
# ---------------------------------------------------------------------------

_DESTRUCTIVE_TOOLS = {"send_message", "add_to_whitelist", "set_keyword_alert"}


async def _confirm_if_destructive(tool_name: str, text_mode: bool) -> bool:
    """Ask user to confirm destructive tools. Returns True if allowed."""
    if tool_name not in _DESTRUCTIVE_TOOLS:
        return True
    if text_mode:
        confirm_raw = await asyncio.get_running_loop().run_in_executor(
            None, lambda: input("Выполнить? [да/нет]: ").strip().lower()
        )
        return confirm_raw in ("да", "д", "y", "yes")
    try:
        import questionary as q
        confirmed = await asyncio.get_running_loop().run_in_executor(
            None, lambda: q.confirm("Выполнить это действие?").ask(),
        )
        return bool(confirmed)
    except Exception:
        confirm_raw = await asyncio.get_running_loop().run_in_executor(
            None, lambda: input("Выполнить? [да/нет]: ").strip().lower(),
        )
        return confirm_raw in ("да", "д", "y", "yes")


async def _async_agent_loop(
    claude,
    tools: list[dict],
    executor: AsyncToolExecutor,
    agent_system: str,
    text_mode: bool,
) -> None:
    """
    Async agent loop with tool_use (supports all providers).
    Handles all I/O and tool calls asynchronously.
    """
    try:
        import anthropic
    except ImportError:
        anthropic = None  # type: ignore

    messages: list[dict] = []
    print(
        "\nАгент запущен. Введите запрос на естественном языке "
        "(/q — назад).\n"
    )

    HELP_TRIGGERS = {
        "что ты умеешь", "help", "помощь", "помоги",
        "как пользоваться", "команды", "возможности",
    }

    while True:
        # Get user input
        try:
            if text_mode:
                user_input = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: input("Вы: ").strip()
                )
            else:
                from tgai.ui import chat_input as _agent_input
                user_input = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: _agent_input(claude)
                )
                if user_input is None:
                    break
                user_input = user_input.strip()
        except (KeyboardInterrupt, EOFError):
            print("\nВыход из агента.")
            break

        if not user_input or user_input.lower() in ("выход", "exit", "quit", "q", "/q", "/back", "/назад"):
            break

        # Check for help triggers
        user_lower = user_input.lower()
        if any(trigger in user_lower for trigger in HELP_TRIGGERS):
            print(_build_help_text())
            continue

        messages.append({"role": "user", "content": user_input})

        # Agentic loop — keep calling until no more tool_use
        is_openai = claude.provider_name in ("openai", "openrouter")
        is_gemini = claude.provider_name in ("gemini", "google")
        # Providers that simulate tool calling return OpenAI-format SimpleNamespace
        uses_prompt_tools = claude.provider_name in ("yandexgpt", "g4f")

        while True:
            try:
                response = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: claude._complete_with_tools(
                        messages, tools, system=agent_system,
                    ),
                )
            except Exception as e:
                print(f"Ошибка API: {e}")
                break

            # --- Parse response (provider-specific) ---
            # Detect format from response object if provider uses prompt-based tools
            if uses_prompt_tools:
                is_openai = True
            if is_gemini:
                # Gemini format
                text_parts = []
                fn_calls = []
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        text_parts.append(part.text)
                    if hasattr(part, "function_call") and part.function_call:
                        fn_calls.append(part.function_call)

                if not fn_calls:
                    if text_parts:
                        print(f"\nАгент: {' '.join(text_parts)}\n")
                    break

                # Process function calls
                fn_response_parts = []
                for fc in fn_calls:
                    tool_name = fc.name
                    tool_input = dict(fc.args) if fc.args else {}

                    print(f"\n[Агент хочет выполнить: {tool_name}]")
                    if tool_input:
                        for k, v in tool_input.items():
                            print(f"  {k}: {v}")

                    confirmed = await _confirm_if_destructive(tool_name, text_mode)
                    if not confirmed:
                        fn_response_parts.append({
                            "function_response": {
                                "name": tool_name,
                                "response": {"result": "Действие отменено пользователем."},
                            }
                        })
                        continue

                    try:
                        result = await executor.execute(tool_name, tool_input)
                    except Exception as e:
                        result = f"Ошибка при выполнении инструмента: {e}"

                    print(f"[Результат {tool_name}]: {str(result)[:200]}")
                    fn_response_parts.append({
                        "function_response": {
                            "name": tool_name,
                            "response": {"result": str(result)},
                        }
                    })

                # Gemini expects tool results as user message with function_response parts
                messages.append({"role": "model", "content": str(text_parts)})
                messages.append({"role": "user", "content": fn_response_parts})

            elif is_openai:
                choice = response.choices[0]
                text_reply = choice.message.content or ""
                tool_calls = choice.message.tool_calls or []
                def _msg_to_dict(msg):
                    if hasattr(msg, "to_dict"):
                        return msg.to_dict()
                    d = {"role": "assistant", "content": text_reply}
                    if tool_calls:
                        tcs = []
                        for tc in tool_calls:
                            if hasattr(tc, "to_dict"):
                                tcs.append(tc.to_dict())
                            else:
                                fn = tc.function if hasattr(tc, "function") else tc
                                tcs.append({
                                    "id": getattr(tc, "id", "call_0"),
                                    "type": "function",
                                    "function": {
                                        "name": getattr(fn, "name", "?"),
                                        "arguments": getattr(fn, "arguments", "{}"),
                                    },
                                })
                        d["tool_calls"] = tcs
                    return d
                messages.append(_msg_to_dict(choice.message))
                if not tool_calls:
                    if text_reply:
                        print(f"\nАгент: {text_reply}\n")
                    break

                tool_results_msgs = []
                for tc in tool_calls:
                    fn = tc.function
                    tool_name = fn.name
                    import json
                    try:
                        tool_input = json.loads(fn.arguments) if isinstance(fn.arguments, str) else fn.arguments
                    except json.JSONDecodeError:
                        tool_input = {}
                    tool_call_id = tc.id

                    print(f"\n[Агент хочет выполнить: {tool_name}]")
                    if tool_input:
                        for k, v in tool_input.items():
                            print(f"  {k}: {v}")

                    confirmed = await _confirm_if_destructive(tool_name, text_mode)
                    if not confirmed:
                        tool_results_msgs.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": "Действие отменено пользователем.",
                        })
                        continue

                    try:
                        result = await executor.execute(tool_name, tool_input)
                    except Exception as e:
                        result = f"Ошибка при выполнении инструмента: {e}"

                    print(f"[Результат {tool_name}]: {str(result)[:200]}")
                    tool_results_msgs.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(result),
                    })
                messages.extend(tool_results_msgs)

            else:
                # Anthropic format
                assistant_content = response.content
                messages.append({"role": "assistant", "content": assistant_content})

                if response.stop_reason == "end_turn":
                    text_blocks = [b.text for b in assistant_content if hasattr(b, "text")]
                    if text_blocks:
                        print(f"\nАгент: {' '.join(text_blocks)}\n")
                    break

                if response.stop_reason == "tool_use":
                    tool_results = []
                    for block in assistant_content:
                        if block.type != "tool_use":
                            continue

                        tool_name = block.name
                        tool_input = block.input
                        tool_use_id = block.id

                        print(f"\n[Агент хочет выполнить: {tool_name}]")
                        if tool_input:
                            for k, v in tool_input.items():
                                print(f"  {k}: {v}")

                        confirmed = await _confirm_if_destructive(tool_name, text_mode)
                        if not confirmed:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": "Действие отменено пользователем.",
                            })
                            continue

                        try:
                            result = await executor.execute(tool_name, tool_input)
                        except Exception as e:
                            result = f"Ошибка при выполнении инструмента: {e}"

                        print(f"[Результат {tool_name}]: {str(result)[:200]}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": str(result),
                        })

                    messages.append({"role": "user", "content": tool_results})
                else:
                    break

        # Trim to avoid token overflow
        if len(messages) > 40:
            messages = messages[-40:]


def _build_help_text() -> str:
    return """\

Я — AI-агент для управления Telegram. Вот что я умею:

Чтение сообщений:
  • Показать непрочитанные сообщения
  • Список всех чатов
  • Поиск сообщений по ключевым словам
  • Дайджест/суммаризация непрочитанных

Отправка:
  • Отправить сообщение любому контакту

Настройки:
  • Добавить группу в список для агрегации
  • Установить оповещение по ключевому слову

Команды интерфейса tgai:
  • tgai chat [@username]            — открыть чат с AI-помощью
  • tgai listen [--auto|--batch 30m] — мониторинг и автоответы
  • tgai aggregate [--hours 12]      — дайджест непрочитанных
  • tgai agent                       — этот режим
  • внутри чата: /send текст         — отправить без AI

Просто напишите, что вы хотите сделать, на русском языке!
"""


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------

def run(args: Any, config: dict, storage: Any) -> None:
    """Entry point for `tgai agent`."""
    from tgai.telegram import TelegramManager, SessionLockedError
    from tgai.claude import create_llm_client

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

    agent_system = (
        persona
        + "\n\n"
        "Ты — AI-агент для управления Telegram. "
        "Используй доступные инструменты для выполнения задач пользователя. "
        "Перед отправкой сообщений или изменением данных всегда уточняй намерение. "
        "Отвечай на русском языке.\n\n"
        "Если пользователь спрашивает 'что ты умеешь', 'help' или 'помощь', "
        "выдай полный список возможностей без вызова инструментов."
    )

    async def main():
        await tg.start()
        try:
            executor = AsyncToolExecutor(tg, claude, storage)
            await _async_agent_loop(claude, TOOLS, executor, agent_system, text_mode)
        finally:
            await tg.stop()

    try:
        asyncio.run(main())
    except SessionLockedError as e:
        print(f"\nОшибка: {e}")
    except KeyboardInterrupt:
        print("\nВыход из агента.")
