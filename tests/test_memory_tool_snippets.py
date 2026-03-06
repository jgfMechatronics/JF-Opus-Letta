"""Unit tests for memory tool snippet functionality.

Tests the requirement: memory tools should return snippets around edits,
not full block content (token optimization).
"""

from letta.services.tool_executor.core_tool_executor import _compute_snippet

# Simple 10-line fixture: letters A through J
ALPHABET_CONTENT = "\n".join("ABCDEFGHIJ")


class TestComputeSnippet:
    """Tests for the _compute_snippet helper function."""

    def test_edit_in_middle_returns_context_window(self):
        """Edit at line 5 (F) with 3 context lines returns C-I (lines 2-8)."""
        snippet = _compute_snippet(ALPHABET_CONTENT, edit_start_line=5, edit_line_count=1, context_lines=3)
        expected = "\n".join("CDEFGHI")
        assert snippet == expected

    def test_edit_at_start_clips_to_beginning(self):
        """Edit at line 0 (A) doesn't go negative — returns A-D."""
        snippet = _compute_snippet(ALPHABET_CONTENT, edit_start_line=0, edit_line_count=1, context_lines=3)
        expected = "\n".join("ABCD")
        assert snippet == expected

    def test_edit_at_end_clips_to_end(self):
        """Edit at line 9 (J) doesn't exceed bounds — returns G-J."""
        snippet = _compute_snippet(ALPHABET_CONTENT, edit_start_line=9, edit_line_count=1, context_lines=3)
        expected = "\n".join("GHIJ")
        assert snippet == expected

    def test_multiline_edit_includes_full_edit_region(self):
        """Edit spanning lines 4-6 (E-G) with 2 context returns C-I."""
        snippet = _compute_snippet(ALPHABET_CONTENT, edit_start_line=4, edit_line_count=3, context_lines=2)
        expected = "\n".join("CDEFGHI")
        assert snippet == expected

    def test_empty_content_returns_empty(self):
        """Empty content returns empty string."""
        assert _compute_snippet("", edit_start_line=0, edit_line_count=0) == ""

    def test_single_line_content(self):
        """Single line content returns that line."""
        assert _compute_snippet("only", edit_start_line=0, edit_line_count=1) == "only"

    def test_default_context_is_three(self):
        """Default context_lines is 3."""
        default = _compute_snippet(ALPHABET_CONTENT, edit_start_line=5, edit_line_count=1)
        explicit = _compute_snippet(ALPHABET_CONTENT, edit_start_line=5, edit_line_count=1, context_lines=3)
        assert default == explicit
