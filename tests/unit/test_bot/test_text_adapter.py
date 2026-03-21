"""Tests for text_adapter — markdown-to-speech conversion."""

from src.bot.media.text_adapter import adapt_for_speech


class TestCodeBlocks:
    """Fenced code block replacement."""

    def test_single_code_block_no_language(self) -> None:
        text = "Here's an example:\n```\nprint('hello')\n```\nThat's it."
        result = adapt_for_speech(text)
        assert "There's a code example in the written response." in result
        assert "print" not in result

    def test_single_code_block_with_language(self) -> None:
        text = "Example:\n```python\ndef foo():\n    pass\n```\nDone."
        result = adapt_for_speech(text)
        assert "code example in Python" in result
        assert "def foo" not in result

    def test_multiple_code_blocks_numbered(self) -> None:
        text = (
            "First:\n```python\nx = 1\n```\n"
            "Second:\n```js\nlet y = 2;\n```\n"
            "Third:\n```\nraw\n```"
        )
        result = adapt_for_speech(text)
        assert "1st code example" in result
        assert "2nd code example" in result
        assert "3rd code example" in result

    def test_code_block_with_short_lang_alias(self) -> None:
        text = "```py\nprint(1)\n```"
        result = adapt_for_speech(text)
        assert "Python" in result

    def test_all_code_returns_nearly_empty(self) -> None:
        text = "```python\ndef main():\n    pass\n```"
        result = adapt_for_speech(text)
        # Only the spoken note remains, no meaningful content
        assert "code example" in result
        assert "def main" not in result


class TestTables:
    """Markdown table replacement."""

    def test_simple_table(self) -> None:
        text = (
            "Here's a comparison:\n\n"
            "| Model | Speed | Quality |\n"
            "|-------|-------|---------|\n"
            "| Kokoro | Fast | Good |\n"
            "| Chatterbox | Medium | Great |\n\n"
            "That's the summary."
        )
        result = adapt_for_speech(text)
        assert "There's a table in the written response." in result
        assert "Kokoro" not in result
        assert "That's the summary." in result

    def test_table_with_alignment(self) -> None:
        text = "| Left | Center | Right |\n|:-----|:------:|------:|\n| a | b | c |"
        result = adapt_for_speech(text)
        assert "table in the written response" in result


class TestInlineCode:
    """Inline code backtick stripping."""

    def test_inline_code_stripped(self) -> None:
        text = "Use the `calculate_total` function."
        result = adapt_for_speech(text)
        assert result == "Use the calculate_total function."

    def test_multiple_inline_code(self) -> None:
        text = "Call `foo()` then `bar()`."
        result = adapt_for_speech(text)
        assert result == "Call foo() then bar()."


class TestHeaders:
    """Header marker stripping."""

    def test_h1_stripped(self) -> None:
        text = "# Main Title\n\nSome content."
        result = adapt_for_speech(text)
        assert "#" not in result
        assert "Main Title" in result

    def test_h3_stripped(self) -> None:
        text = "### Sub Section\n\nDetails here."
        result = adapt_for_speech(text)
        assert "###" not in result
        assert "Sub Section" in result


class TestFormatting:
    """Bold, italic, strikethrough stripping."""

    def test_bold_double_asterisk(self) -> None:
        text = "This is **important** text."
        result = adapt_for_speech(text)
        assert result == "This is important text."

    def test_bold_double_underscore(self) -> None:
        text = "This is __important__ text."
        result = adapt_for_speech(text)
        assert result == "This is important text."

    def test_italic_single_asterisk(self) -> None:
        text = "This is *emphasised* text."
        result = adapt_for_speech(text)
        assert result == "This is emphasised text."

    def test_strikethrough(self) -> None:
        text = "This is ~~wrong~~ correct."
        result = adapt_for_speech(text)
        assert result == "This is wrong correct."


class TestLinks:
    """Link and URL handling."""

    def test_markdown_link_keeps_text(self) -> None:
        text = "Check [the documentation](https://docs.example.com) for details."
        result = adapt_for_speech(text)
        assert result == "Check the documentation for details."

    def test_bare_url_replaced(self) -> None:
        text = "Visit https://example.com/path for more."
        result = adapt_for_speech(text)
        assert "a link in the written response" in result
        assert "https://" not in result


class TestBulletLists:
    """Bullet list conversion to prose."""

    def test_short_bullet_list_joined(self) -> None:
        text = "Available options:\n- apples\n- bananas\n- oranges"
        result = adapt_for_speech(text)
        assert "apples, bananas, and oranges." in result

    def test_two_item_list(self) -> None:
        text = "Choose:\n- red\n- blue"
        result = adapt_for_speech(text)
        assert "red and blue." in result

    def test_single_item_list(self) -> None:
        text = "Note:\n- just this one"
        result = adapt_for_speech(text)
        assert "just this one." in result

    def test_long_bullet_items_become_sentences(self) -> None:
        text = (
            "Key points:\n"
            "- This is a very long bullet point that contains a full sentence explaining something important about the system\n"
            "- Another equally verbose bullet point that goes into considerable detail about a second topic"
        )
        result = adapt_for_speech(text)
        # Long items should become separate sentences, not comma-joined
        assert ", and" not in result

    def test_asterisk_bullets(self) -> None:
        text = "Items:\n* foo\n* bar\n* baz"
        result = adapt_for_speech(text)
        assert "foo, bar, and baz." in result


class TestWhitespace:
    """Whitespace collapsing."""

    def test_excessive_newlines_collapsed(self) -> None:
        text = "First paragraph.\n\n\n\n\nSecond paragraph."
        result = adapt_for_speech(text)
        assert "\n\n\n" not in result
        assert "First paragraph." in result
        assert "Second paragraph." in result


class TestTruncation:
    """Length truncation at sentence boundaries."""

    def test_truncation_at_sentence_boundary(self) -> None:
        # Build text that exceeds max_length
        sentences = ["This is sentence number {}.".format(i) for i in range(200)]
        text = " ".join(sentences)
        result = adapt_for_speech(text, max_length=500)
        assert len(result) < 600  # Allow for the appended note
        assert "The full response continues in the written message." in result

    def test_no_truncation_when_short(self) -> None:
        text = "Short response."
        result = adapt_for_speech(text, max_length=4000)
        assert result == "Short response."
        assert "continues" not in result


class TestMixedContent:
    """Realistic Claude responses with multiple element types."""

    def test_mixed_response(self) -> None:
        text = (
            "# Summary\n\n"
            "Here's what I found about **Chatterbox TTS**:\n\n"
            "- Fast inference\n"
            "- Good quality\n"
            "- Easy setup\n\n"
            "```python\nfrom chatterbox import TTS\nmodel = TTS()\n```\n\n"
            "Check [the docs](https://example.com) for the full API.\n\n"
            "| Feature | Status |\n"
            "|---------|--------|\n"
            "| Speed | Fast |\n"
            "| Quality | High |\n"
        )
        result = adapt_for_speech(text)
        # Headers stripped
        assert "#" not in result
        assert "Summary" in result
        # Bold stripped
        assert "**" not in result
        assert "Chatterbox TTS" in result
        # Bullets joined
        assert "Fast inference" in result
        # Code replaced
        assert "code example in Python" in result
        assert "from chatterbox" not in result
        # Link text kept
        assert "the docs" in result
        assert "https://" not in result
        # Table replaced
        assert "table in the written response" in result
        assert "Speed" not in result or "table" in result
