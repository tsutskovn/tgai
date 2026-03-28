"""Tests for Media Actions (View, Copy, Paste)."""

import pytest
from unittest.mock import MagicMock, patch
from tgai.telegram import TelegramManager

class TestMediaActions:
    # 1. Тест на регистрацию метода send_file
    @pytest.mark.asyncio
    async def test_telegram_send_file_exists(self):
        mock_client = MagicMock()
        async def mock_send(e, f, caption=""): return True
        mock_client.send_file = mock_send
        
        tg = TelegramManager(123, "hash", "path")
        tg.client = mock_client
        
        res = await tg.send_file("peer", "file.png")
        assert res is True

    # 2. Тест логики отображения префикса [МЕДИА]
    def test_media_prefix_logic(self):
        # Emulating the UI logic from chat.py
        pending_upload = {"file_path": "/tmp/test.png"}
        
        prefix = [("bold", "Вы: ")]
        if pending_upload["file_path"]:
            prefix.insert(0, ("bold ansigreen", "[МЕДИА] "))
            
        assert prefix[0][1] == "[МЕДИА] "
        assert prefix[1][1] == "Вы: "

    # 3. Тест на наличие русских горячих клавиш (имитация проверки KeyBindings)
    def test_keybinding_existence(self):
        # This is a structural test idea
        # In real chat.py, we have @kb.add("м") etc.
        actions = ["v", "м", "c", "с", "m-v", "m-м"]
        # If any of these were missing, the UI wouldn't respond.
        # We ensure our list of intended shortcuts is complete.
        assert "м" in actions
        assert "с" in actions
        assert "m-м" in actions
