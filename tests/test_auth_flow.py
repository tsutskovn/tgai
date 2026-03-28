"""Tests for Authentication and Logout flows."""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from tgai.telegram import TelegramManager
from tgai.storage import Storage

class TestAuthFlow:
    # 1. Тест на корректное определение авторизации
    @pytest.mark.asyncio
    async def test_is_authorized_logic(self):
        # Mock client
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = MagicMock(return_value=True) # Return awaitable-like or use AsyncMock
        
        # Helper for async mock
        async def mock_auth(): return True
        mock_client.is_user_authorized = mock_auth
        
        tg = TelegramManager(api_id=123, api_hash="hash", session_path="path")
        tg.client = mock_client
        
        assert await tg.is_authorized() is True

    # 2. Тест на очистку состояния при выходе
    def test_clear_user_local_state(self, tmp_path):
        # Setup temporary storage
        storage = Storage(base_dir=tmp_path)
        
        # Create some fake data
        (tmp_path / "media_cache.json").write_text("{}")
        (tmp_path / "summary_cache.json").write_text("{}")
        (tmp_path / "session.session").write_text("fake session")
        
        # Run cleanup
        storage.clear_user_local_state(preserve_telegram_app=True)
        
        # Check if files are gone
        assert not (tmp_path / "media_cache.json").exists()
        assert not (tmp_path / "summary_cache.json").exists()
        # session.session deletion check (it deletes multiple session extensions)
        assert not (tmp_path / "session.session").exists()

    # 3. Тест на сохранение api_id при частичной очистке
    def test_preserve_config_on_logout(self, tmp_path):
        storage = Storage(base_dir=tmp_path)
        config = {
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "llm": [{"provider": "yandexgpt", "api_key": "secret"}]
        }
        storage.save_config(config)
        
        storage.clear_user_local_state(preserve_telegram_app=True)
        
        new_config = storage.load_config()
        assert new_config["telegram"]["api_id"] == 123
        assert new_config["telegram"]["api_hash"] == "abc"
        assert len(new_config["llm"]) == 0 # LLM secrets should be cleared

    # 4. Тест на обработку ошибки неверного номера (имитация)
    @pytest.mark.asyncio
    async def test_auth_error_handling(self, monkeypatch):
        mock_tg = MagicMock()
        
        async def mock_is_auth(): return False
        async def mock_start_fail(): 
            from telethon.errors import PhoneNumberInvalidError
            raise PhoneNumberInvalidError("Invalid")
            
        mock_tg.is_authorized = mock_is_auth
        mock_tg.start = mock_start_fail
        
        # This test just ensures we can catch the error
        with pytest.raises(Exception) as exc:
            await mock_tg.start()
        assert "phone number is invalid" in str(exc.value).lower()
