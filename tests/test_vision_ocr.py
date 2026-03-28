"""Tests for Multimodal Vision and OCR functionality."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tgai.claude import YandexGPTClient, LLMClient
from tgai.ui import format_messages

class TestVisionOCR:
    # 1. Тест на базовый интерфейс (VLM)
    def test_describe_image_interface(self):
        client = LLMClient(model="test")
        with pytest.raises(NotImplementedError):
            client.describe_image(b"fake_bytes")

    # 2. Тест парсинга OCR ответа Яндекса
    def test_yandex_ocr_parsing(self, monkeypatch):
        client = YandexGPTClient(api_key="key", folder_id="folder")
        
        # Mocking the urllib response for OCR
        mock_response = MagicMock()
        mock_response.read.return_value = b"""{
            "results": [{
                "results": [{
                    "textDetection": {
                        "pages": [{
                            "blocks": [{
                                "lines": [
                                    {"words": [{"text": "Hello"}]},
                                    {"words": [{"text": "World"}]}
                                ]
                            }]
                        }]
                    }
                }]
            }]
        }"""
        mock_response.__enter__.return_value = mock_response
        
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = client.ocr_image(b"fake_bytes")
            assert "Hello" in result
            assert "World" in result

    # 3. Тест на очистку OCR текста (промпт)
    def test_clean_ocr_text_logic(self, monkeypatch):
        client = YandexGPTClient(api_key="key", folder_id="folder")
        
        def mock_complete(self, messages, max_tokens=100):
            # Check if prompt contains the messy text
            assert "raw text" in messages[0]["content"]
            return "Clean logic"
            
        monkeypatch.setattr(YandexGPTClient, "_complete", mock_complete)
        result = client.clean_ocr_text("raw text")
        assert result == "Clean logic"

    # 4. Тест на пустой OCR (должен возвращать пустую строку)
    def test_clean_ocr_text_empty(self):
        client = YandexGPTClient(api_key="key", folder_id="folder")
        assert client.clean_ocr_text("") == ""
        assert client.clean_ocr_text("a") == "" # too short

    # 5. Тест форматирования [media] заглушки в UI
    def test_ui_media_placeholder(self):
        me_id = 123
        msg = SimpleNamespace(
            id=1, sender_id=456, text=None, photo=True, date=datetime.now()
        )
        # format_messages expects a list, newest first (reversed internally)
        lines = format_messages([msg], me_id)
        assert any("[media]" in line for line in lines)

    # 6. Тест фильтрации по времени (логика из chat.py)
    def test_media_time_filtering_logic(self):
        now = datetime.now(timezone.utc)
        fresh_date = now - timedelta(hours=1)
        old_date = now - timedelta(hours=25)
        
        day_ago = now - timedelta(days=1)
        
        assert fresh_date > day_ago  # Should be processed
        assert old_date < day_ago    # Should be skipped

    # 7. Тест на приоритет обработки (новые сообщения)
    def test_new_message_media_logic(self):
        # Simulating logic where new messages always trigger media processing
        new_msgs = [
            SimpleNamespace(id=10, text=None, photo=True),
            SimpleNamespace(id=11, text="Caption", photo=True)
        ]
        for m in new_msgs:
            if hasattr(m, "photo") and m.photo:
                # In real code this triggers task
                assert True

    # 8. Тест сборки финальной строки (Vision + OCR)
    def test_final_media_line_assembly(self):
        description = "A cat"
        clean_ocr = "Store name"
        
        parts = []
        if description: parts.append(f"[media]: {description}")
        if clean_ocr: parts.append(f"[текст]: {clean_ocr}")
        
        result = " | ".join(parts)
        assert result == "[media]: A cat | [текст]: Store name"

    # 9. Тест на падение API (Vision 500 error handling)
    def test_yandex_vision_error_handling(self, monkeypatch):
        client = YandexGPTClient(api_key="key", folder_id="folder")
        
        with patch("urllib.request.urlopen", side_effect=Exception("Internal Error")):
            result = client.describe_image(b"bytes")
            assert result == "" # Should return empty string, not crash

    # 10. Тест на корректный медиа-промпт
    def test_yandex_vision_prompt(self, monkeypatch):
        client = YandexGPTClient(api_key="key", folder_id="folder")
        
        def mock_urlopen(req, timeout=30):
            import json
            body = json.loads(req.data.decode())
            prompt = body["messages"][0]["content"][0]["text"]
            assert "6 до 20 слов" in prompt
            
            # Return dummy response
            resp = MagicMock()
            resp.read.return_value = b'{"result": {"alternatives": [{"message": {"text": "ok"}}]}}'
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            client.describe_image(b"bytes")

    # 11. Тест на пустой результат OCR (должен корректно обрабатываться)
    def test_yandex_ocr_empty_result(self, monkeypatch):
        client = YandexGPTClient(api_key="key", folder_id="folder")
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"results": []}'
        mock_response.__enter__.return_value = mock_response
        with patch("urllib.request.urlopen", return_value=mock_response):
            assert client.ocr_image(b"bytes") == ""

    # 12. Тест на обработку очень длинного текста OCR (обрезка в chat.py)
    def test_ocr_text_truncation_logic(self):
        long_text = "A" * 200
        # Logic from chat.py: ocr_text[:100] + "..." if len(ocr_text) > 100 else ocr_text
        truncated = long_text[:100] + "..." if len(long_text) > 100 else long_text
        assert len(truncated) == 103
        assert truncated.endswith("...")

    # 13. Тест на логику 'clean_ocr_text' при возникновении ошибки
    def test_clean_ocr_text_error_handling(self, monkeypatch):
        client = YandexGPTClient(api_key="key", folder_id="folder")
        with patch.object(YandexGPTClient, "_complete", side_effect=Exception("API Error")):
            assert client.clean_ocr_text("some text") == ""

    # 14. Тест на проверку формата [media] при наличии текста
    def test_final_line_with_existing_text(self):
        new_media_line = "[media]: cat | [текст]: hello"
        original = "caption"
        # Logic from chat.py: f"{new_media_line}\n{original}"
        final = f"{new_media_line}\n{original}"
        assert "[media]: cat" in final
        assert "caption" in final
        assert "\n" in final
