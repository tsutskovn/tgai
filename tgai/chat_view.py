"""Pure helpers for fullscreen chat viewport calculations."""

from __future__ import annotations


CHAT_RESERVED_ROWS = 3
CHAT_BOTTOM_GAP = 2


def visible_lines(terminal_lines: int) -> int:
    """How many message lines fit above the separator/hint/input area."""
    return max(1, terminal_lines - CHAT_RESERVED_ROWS)


def padded_total(total_lines: int) -> int:
    """Virtual line count with a small bottom gap so the latest message is visible."""
    return total_lines + CHAT_BOTTOM_GAP


def max_scroll(total_lines: int, terminal_lines: int) -> int:
    visible = visible_lines(terminal_lines)
    return max(0, padded_total(total_lines) - visible)


def window_start(total_lines: int, scroll_from_bottom: int, terminal_lines: int) -> int:
    visible = visible_lines(terminal_lines)
    total = padded_total(total_lines)
    return max(0, total - visible - scroll_from_bottom)
