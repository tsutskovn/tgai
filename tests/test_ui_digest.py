import pytest
from unittest.mock import patch, MagicMock
from tgai.ui import _word_wrap, digest_viewer
import shutil

class TestUIDigest:
    def test_word_wrap_basic(self):
        res = _word_wrap("hello world", 50)
        assert res == ["hello world"]

    def test_word_wrap_long_text(self):
        text = "this is a very long string that should be wrapped to multiple lines"
        res = _word_wrap(text, 20)
        assert len(res) > 1
        assert "this is a very long" in res[0]

    def test_word_wrap_with_ansi(self):
        text = "start \033[32m[media]\033[0m end"
        # Length of visible text "start [media] end" is 17.
        res = _word_wrap(text, 20)
        assert len(res) == 1

    def test_digest_viewer_handles_empty(self):
        assert digest_viewer([]) is None
