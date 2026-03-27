"""Tests for fullscreen chat viewport calculations."""

from tgai.chat_view import (
    visible_lines,
    max_scroll,
    window_start,
)


class TestChatViewport:
    def test_visible_lines_reserve_footer(self):
        assert visible_lines(24) == 21

    def test_max_scroll_includes_bottom_gap(self):
        assert max_scroll(total_lines=30, terminal_lines=24) == 11

    def test_window_start_at_bottom_keeps_gap(self):
        assert window_start(total_lines=30, scroll_from_bottom=0, terminal_lines=24) == 11

    def test_window_start_moves_up_when_scrolled(self):
        assert window_start(total_lines=30, scroll_from_bottom=4, terminal_lines=24) == 7
