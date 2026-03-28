"""listen command — monitor incoming messages and reply via Claude."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from tgai.ui import batch_confirm, confirm_batch_send


def _parse_interval(interval_str: str) -> int:
    """Parse interval string like '30m', '1h', '90s' into seconds."""
    interval_str = interval_str.strip().lower()
    if interval_str.endswith("h"):
        return int(interval_str[:-1]) * 3600
    if interval_str.endswith("m"):
        return int(interval_str[:-1]) * 60
    if interval_str.endswith("s"):
        return int(interval_str[:-1])
    return int(interval_str)  # assume seconds


async def _listen_auto(tg, claude, storage, persona: str) -> None:
    """Auto mode: reply immediately to every incoming message without confirmation."""
    me = await tg.get_me()
    print("Режим автопилота. Ожидание сообщений... (Ctrl+C для выхода)\n")

    async def handle(event) -> None:
        msg = event.message
        if not getattr(msg, "text", None):
            return

        sender_id = getattr(msg, "sender_id", None)
        if sender_id == me.id:
            return  # ignore own messages

        # Skip broadcast channels and bots
        chat_entity = await event.get_chat()
        from tgai.telegram import _entity_display_name, is_broadcast_channel
        from telethon.tl.types import User
        if is_broadcast_channel(chat_entity):
            return

        sender = await event.get_sender()
        if isinstance(sender, User) and sender.bot:
            return
        sender_name = _entity_display_name(sender)
        chat_id = event.chat_id

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {sender_name}: {msg.text}")

        # Build context from recent Telegram messages
        recent_msgs = await tg.get_messages(chat_entity, limit=claude.max_history)
        chat_history = []
        for m in reversed(recent_msgs):
            if not getattr(m, "text", None):
                continue
            sid = getattr(m, "sender_id", None)
            label = "Вы" if sid == me.id else sender_name
            chat_history.append({"sender": label, "text": m.text})

        try:
            reply = claude.propose_reply(msg.text, chat_history, persona)
        except Exception as e:
            print(f"Ошибка Claude: {e}")
            return

        print(f"Клод отвечает: {reply}")
        try:
            await tg.send_message(chat_entity, reply)
            try:
                await tg.mark_read(chat_entity)
            except Exception:
                pass
            print("Отправлено.")
        except Exception as e:
            print(f"Ошибка отправки: {e}")
            return

        # Save history
        storage.save_history(chat_id, claude.get_history(chat_id))

        # Check for keyword alerts
        alerts = storage.load_alerts()
        for alert in alerts:
            kw = alert.get("keyword", "").lower()
            if kw and kw in msg.text.lower():
                print(f"[ОПОВЕЩЕНИЕ] Ключевое слово «{kw}» найдено!")

    await tg.listen(handle)


async def _build_pending_item(tg, claude, persona: str, me, event) -> dict | None:
    """Turn an incoming Telegram event into a lightweight pending listen item."""
    msg = event.message
    if not getattr(msg, "text", None):
        return None

    sender_id = getattr(msg, "sender_id", None)
    if sender_id == me.id:
        return None

    chat_entity = await event.get_chat()
    
    # --- CHECK IF ALREADY REPLIED ---
    # Fetch the very last message in this chat. If it's from us, skip.
    try:
        last_msgs = await tg.get_messages(chat_entity, limit=1)
        if last_msgs and last_msgs[0].sender_id == me.id:
            return None
    except Exception:
        pass

    from tgai.telegram import _entity_display_name, is_broadcast_channel
    from telethon.tl.types import User
    if is_broadcast_channel(chat_entity):
        return None

    sender = await event.get_sender()
    if isinstance(sender, User) and sender.bot:
        return None
    sender_name = _entity_display_name(sender)
    chat_id = event.chat_id
    return {
        "message_id": getattr(msg, "id", None),
        "sender": sender_name,
        "message": msg.text,
        "chat_id": chat_id,
        "send": True,
    }


async def _ensure_pending_reply(tg, claude, persona: str, item: dict) -> dict:
    """Generate a reply for a pending listen item if it doesn't have one yet."""
    if item.get("reply"):
        return item

    me = await tg.get_me()
    entity = await tg.get_entity_by_id(item["chat_id"])
    recent_msgs = await tg.get_messages(entity, limit=claude.max_history)
    sender_name = item.get("sender", "?")
    chat_history = []
    for m in reversed(recent_msgs):
        if not getattr(m, "text", None):
            continue
        sid = getattr(m, "sender_id", None)
        label = "Вы" if sid == me.id else sender_name
        chat_history.append({"sender": label, "text": m.text})

    loop = asyncio.get_running_loop()
    reply = await loop.run_in_executor(
        None,
        lambda: claude.propose_reply(item.get("message", ""), chat_history, persona),
    )
    updated = dict(item)
    updated["reply"] = reply
    return updated


class ListenBackgroundCollector:
    """Collect pending listen replies in the background while tgai stays open."""

    def __init__(self, tg, claude, storage, persona: str) -> None:
        self.tg = tg
        self.claude = claude
        self.storage = storage
        self.persona = persona
        self.pending: list[dict] = storage.load_listen_pending()
        self._me = None
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._incoming_handler = None
        self._outgoing_handler = None
        self._running = False
        self._paused = False
        self._tasks: set[asyncio.Task] = set()
        self._incoming_queue: asyncio.Queue = asyncio.Queue()
        self._queued_message_ids: set[int] = set()
        self._worker_task: asyncio.Task | None = None

        if self.pending:
            self._wakeup.set()

    async def start(self) -> None:
        if self._running:
            return
        self._me = await self.tg.get_me()
        from telethon import events as tg_events

        async def _incoming_handler(event):
            if self._paused:
                return
            msg_id = getattr(getattr(event, "message", None), "id", None)
            if msg_id is not None and msg_id in self._queued_message_ids:
                return
            if msg_id is not None:
                self._queued_message_ids.add(msg_id)
            try:
                self._incoming_queue.put_nowait(event)
            except Exception:
                if msg_id is not None:
                    self._queued_message_ids.discard(msg_id)

        async def _outgoing_handler(event):
            if self._paused:
                return
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                return
            async with self._lock:
                kept = [item for item in self.pending if item.get("chat_id") != chat_id]
                if len(kept) != len(self.pending):
                    self.pending = kept
                    self.storage.save_listen_pending(self.pending)

        async def _worker():
            while self._running:
                try:
                    event = await self._incoming_queue.get()
                except asyncio.CancelledError:
                    break

                msg_id = getattr(getattr(event, "message", None), "id", None)
                try:
                    item = await _build_pending_item(self.tg, self.claude, self.persona, self._me, event)
                    if not item:
                        continue
                    async with self._lock:
                        item_msg_id = item.get("message_id")
                        if item_msg_id is not None and any(existing.get("message_id") == item_msg_id for existing in self.pending):
                            continue
                        self.pending.append(item)
                        self.storage.save_listen_pending(self.pending)
                        self._wakeup.set()
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass
                finally:
                    if msg_id is not None:
                        self._queued_message_ids.discard(msg_id)
                    await asyncio.sleep(0)

        self._incoming_handler = _incoming_handler
        self._outgoing_handler = _outgoing_handler
        self.tg.client.add_event_handler(_incoming_handler, tg_events.NewMessage(incoming=True))
        self.tg.client.add_event_handler(_outgoing_handler, tg_events.NewMessage(outgoing=True))
        self._running = True
        self._worker_task = asyncio.create_task(_worker())

    async def stop(self) -> None:
        if not self._running:
            return
        if self._incoming_handler is not None:
            self.tg.client.remove_event_handler(self._incoming_handler)
            self._incoming_handler = None
        if self._outgoing_handler is not None:
            self.tg.client.remove_event_handler(self._outgoing_handler)
            self._outgoing_handler = None
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._running = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def wait_for_pending(self) -> None:
        while True:
            async with self._lock:
                if self.pending:
                    return
            await self._wakeup.wait()
            self._wakeup.clear()

    async def pop_pending_batch(self) -> list[dict]:
        async with self._lock:
            current = list(self.pending)
            self.pending.clear()
            self.storage.save_listen_pending(self.pending)
            return current


async def _listen_batch(
    tg, claude, storage, persona: str, interval_seconds: int, text_mode: bool
) -> None:
    """
    Confirmation mode: confirm the first reply immediately.
    If more messages arrive while the user is confirming, merge them into the
    same packet before the final send confirmation.
    """
    me = await tg.get_me()
    batch: list[dict] = []
    lock = asyncio.Lock()
    wakeup = asyncio.Event()
    confirm_in_progress = [False]

    print(
        "Режим подтверждения. Первое сообщение показывается сразу. "
        "Если во время подтверждения придут новые, они добавятся в этот же пакет. "
        "Ожидание сообщений... (Ctrl+C для выхода)\n"
    )

    async def handle(event) -> None:
        msg = event.message
        if not getattr(msg, "text", None):
            return

        sender_id = getattr(msg, "sender_id", None)
        if sender_id == me.id:
            return

        # Skip broadcast channels and bots
        chat_entity = await event.get_chat()
        from tgai.telegram import _entity_display_name, is_broadcast_channel
        from telethon.tl.types import User
        if is_broadcast_channel(chat_entity):
            return

        sender = await event.get_sender()
        if isinstance(sender, User) and sender.bot:
            return
        sender_name = _entity_display_name(sender)
        chat_id = event.chat_id

        # Build context from recent Telegram messages
        recent_msgs = await tg.get_messages(chat_entity, limit=claude.max_history)
        chat_history = []
        for m in reversed(recent_msgs):
            if not getattr(m, "text", None):
                continue
            sid = getattr(m, "sender_id", None)
            label = "Вы" if sid == me.id else sender_name
            chat_history.append({"sender": label, "text": m.text})

        try:
            reply = claude.propose_reply(msg.text, chat_history, persona)
        except Exception as e:
            print(f"Ошибка Claude: {e}")
            return

        async with lock:
            batch.append({
                "sender": sender_name,
                "message": msg.text,
                "reply": reply,
                "entity": chat_entity,
                "chat_id": chat_id,
                "send": True,
            })
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Добавлено: {sender_name}: {msg.text[:50]}"
            )
            if not confirm_in_progress[0]:
                wakeup.set()

    # Register handler
    from telethon import events as tg_events

    @tg.client.on(tg_events.NewMessage(incoming=True))
    async def _handler(event):
        await handle(event)

    async def _drain_pending() -> None:
        while True:
            await wakeup.wait()
            wakeup.clear()

            async with lock:
                if confirm_in_progress[0] or not batch:
                    continue
                confirm_in_progress[0] = True
                current_batch = list(batch)
                batch.clear()

            try:
                await _flush_batch(current_batch, tg, claude, storage, text_mode, persona=persona)
            finally:
                async with lock:
                    confirm_in_progress[0] = False
                    if batch:
                        wakeup.set()

    try:
        await _drain_pending()
    except asyncio.CancelledError:
        pass
    finally:
        async with lock:
            current_batch = list(batch)
            batch.clear()
        if current_batch:
            await _flush_batch(current_batch, tg, claude, storage, text_mode, persona=persona)


async def _listen_pending_forever(
    collector: ListenBackgroundCollector,
    tg,
    claude,
    storage,
    text_mode: bool,
) -> None:
    """Open the listen confirmation UI over the shared background queue."""
    print(
        "Слушать открыто. Накопленные ответы будут показываться сразу. "
        "Новые сообщения, пришедшие во время подтверждения, добавятся в текущий пакет. "
        "(Ctrl+C для выхода)\n"
    )
    while True:
        await collector.wait_for_pending()
        current_batch = await collector.pop_pending_batch()
        if current_batch:
            await _flush_batch(
                current_batch,
                tg,
                claude,
                storage,
                text_mode,
                collector=collector,
                persona=collector.persona,
            )


async def _flush_batch(
    batch: list[dict],
    tg,
    claude,
    storage,
    text_mode: bool,
    collector: ListenBackgroundCollector | None = None,
    persona: str = "",
) -> None:
    """Process a batch of accumulated replies."""
    if not batch:
        return

    pending_review = [await _ensure_pending_reply(tg, claude, persona, item) for item in list(batch)]
    approved: list[dict] = []
    total_count = 0

    while pending_review:
        total_count += len(pending_review)
        approved.extend(
            batch_confirm(
                list(pending_review),
                text_mode=text_mode,
                confirm_send=False,
                reformulate_fn=lambda text: claude.reformulate(
                    text,
                    "Перефразируй по-другому, сохрани смысл, тон и краткость.",
                    persona,
                ),
            )
        )
        if collector is None:
            break
        pending_review = [
            await _ensure_pending_reply(tg, claude, persona, item)
            for item in await collector.pop_pending_batch()
        ]
        if pending_review:
            print(f"\nВ этот же пакет добавлено ещё {len(pending_review)} сообщений.\n")

    if not confirm_batch_send(approved, total_count, text_mode=text_mode):
        return

    for item in approved:
        if not item.get("send", True):
            continue
        try:
            entity = item.get("entity")
            if entity is None:
                entity = await tg.get_entity_by_id(item["chat_id"])
            await tg.send_message(entity, item["reply"])
            try:
                await tg.mark_read(entity)
            except Exception:
                pass
            print(f"Отправлено {item['sender']}: {item['reply'][:50]}")
            # Update history
            chat_id = item["chat_id"]
            hist = claude.get_history(chat_id)
            storage.save_history(chat_id, hist)
        except Exception as e:
            print(f"Ошибка отправки: {e}")


def run(args: Any, config: dict, storage: Any) -> None:
    """Entry point for `tgai listen`."""
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
    if getattr(claude, "provider_name", "") != "yandexgpt":
        print("Автоответы доступны только с подключенным YandexGPT.")
        return

    persona_name = getattr(args, "persona", None) or defaults.get("persona", "default")
    persona = storage.load_persona(persona_name)
    text_mode = getattr(args, "text", False)
    auto_mode = getattr(args, "auto", False)
    batch_interval = getattr(args, "batch", None) or defaults.get("batch_interval", "30m")

    async def main():
        await tg.start()
        try:
            if auto_mode:
                await _listen_auto(tg, claude, storage, persona)
            else:
                interval = _parse_interval(batch_interval)
                await _listen_batch(tg, claude, storage, persona, interval, text_mode)
        finally:
            await tg.stop()

    try:
        asyncio.run(main())
    except SessionLockedError as e:
        print(f"\nОшибка: {e}")
    except KeyboardInterrupt:
        print("\nВыход из listen.")
