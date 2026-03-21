"""Adapt Claude's markdown responses for text-to-speech.

Converts markdown formatting to plain text suitable for spoken delivery.
Code blocks and tables are replaced with spoken notes; formatting markers
are stripped; bullet lists become flowing prose.
"""

import re
from typing import List

# --- Regex patterns (mirror html_format.py where applicable) ---

_FENCED_CODE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
_TABLE_SEP = re.compile(r"^\|?[\s:]*-{2,}[\s:|-]*\|", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADER = re.compile(r"^(#{1,6})\s+(.*)", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<!\w)\*(.+?)\*(?!\w)|(?<!\w)_(.+?)_(?!\w)")
_STRIKETHROUGH = re.compile(r"~~(.+?)~~")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BARE_URL = re.compile(r"(?<!\()https?://\S+")
_BULLET = re.compile(r"^[ \t]*[-*]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^[ \t]*\d+\.\s+", re.MULTILINE)

# Language display names for code block notes
_LANG_NAMES = {
    "py": "Python",
    "python": "Python",
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "rb": "Ruby",
    "ruby": "Ruby",
    "rs": "Rust",
    "rust": "Rust",
    "go": "Go",
    "java": "Java",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "sql": "SQL",
    "html": "HTML",
    "css": "CSS",
    "json": "JSON",
    "yaml": "YAML",
    "yml": "YAML",
    "toml": "TOML",
    "xml": "XML",
    "md": "Markdown",
    "markdown": "Markdown",
    "c": "C",
    "cpp": "C++",
    "cs": "C#",
    "swift": "Swift",
    "kt": "Kotlin",
    "kotlin": "Kotlin",
    "php": "PHP",
    "r": "R",
    "scala": "Scala",
    "dart": "Dart",
    "lua": "Lua",
    "perl": "Perl",
    "dockerfile": "Dockerfile",
}


def adapt_for_speech(text: str, max_length: int = 4000) -> str:
    """Convert markdown text to plain text suitable for TTS.

    Args:
        text: Claude's markdown response.
        max_length: Truncate adapted text to this many characters.

    Returns:
        Plain text suitable for speech synthesis, or empty string
        if the response is entirely code/tables.
    """
    result = text

    # 1. Replace fenced code blocks with spoken notes
    result = _replace_code_blocks(result)

    # 2. Replace tables with spoken notes
    result = _replace_tables(result)

    # 3. Strip inline code backticks (keep text)
    result = _INLINE_CODE.sub(r"\1", result)

    # 4. Convert headers — strip markers, add pause before
    result = _HEADER.sub(r"\n\2.", result)

    # 5. Convert links — keep text, drop URL
    result = _LINK.sub(r"\1", result)

    # 6. Replace bare URLs
    result = _BARE_URL.sub("a link in the written response", result)

    # 7. Strip bold/italic/strikethrough markers
    result = _BOLD.sub(lambda m: m.group(1) or m.group(2), result)
    result = _ITALIC.sub(lambda m: m.group(1) or m.group(2), result)
    result = _STRIKETHROUGH.sub(r"\1", result)

    # 8. Convert bullet lists to prose
    result = _convert_bullet_lists(result)

    # 9. Strip numbered list markers
    result = _NUMBERED.sub("", result)

    # 10. Collapse excessive whitespace
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip()

    # 11. Truncate if needed
    if len(result) > max_length:
        result = _truncate_at_sentence(result, max_length)

    return result


def _replace_code_blocks(text: str) -> str:
    """Replace fenced code blocks with spoken notes."""
    blocks: List[re.Match] = list(_FENCED_CODE.finditer(text))  # type: ignore[type-arg]
    if not blocks:
        return text

    use_numbers = len(blocks) > 1
    result = text

    # Process in reverse to preserve positions
    for i, match in enumerate(reversed(blocks)):
        idx = len(blocks) - i
        lang = match.group(1)
        lang_name = _LANG_NAMES.get(lang, lang) if lang else None

        if use_numbers:
            ordinal = _ordinal(idx)
            if lang_name:
                note = f"There's a {ordinal} code example in {lang_name} in the written response."
            else:
                note = f"There's a {ordinal} code example in the written response."
        else:
            if lang_name:
                note = f"There's a code example in {lang_name} in the written response."
            else:
                note = "There's a code example in the written response."

        result = result[: match.start()] + note + result[match.end() :]

    return result


def _ordinal(n: int) -> str:
    """Return ordinal string for a number (1st, 2nd, 3rd, etc.)."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _replace_tables(text: str) -> str:
    """Replace markdown tables with spoken notes."""
    result = text
    count = 0

    while True:
        sep_match = _TABLE_SEP.search(result)
        if not sep_match:
            break
        count += 1

        # Find start of separator line
        line_start = result.rfind("\n", 0, sep_match.start())
        line_start = line_start + 1 if line_start != -1 else 0

        # Walk back for header rows (lines containing |)
        block_start = line_start
        while block_start > 0:
            prev_nl = result.rfind("\n", 0, block_start - 1)
            prev_line_start = prev_nl + 1 if prev_nl != -1 else 0
            prev_line = result[prev_line_start : block_start - 1]
            if "|" in prev_line:
                block_start = prev_line_start
            else:
                break

        # Walk forward for data rows
        block_end = sep_match.end()
        while block_end < len(result):
            nl = result.find("\n", block_end)
            if nl == -1:
                block_end = len(result)
                break
            next_line = result[block_end:nl].strip() if block_end < nl else ""
            if next_line.startswith("|") or (
                "|" in next_line and next_line.endswith("|")
            ):
                block_end = nl + 1
            else:
                block_end = nl
                break

        note = "There's a table in the written response."
        result = result[:block_start] + note + result[block_end:]

    return result


def _convert_bullet_lists(text: str) -> str:
    """Convert bullet lists to flowing prose."""
    lines = text.split("\n")
    result_lines: List[str] = []
    bullet_buffer: List[str] = []

    def flush_bullets() -> None:
        if not bullet_buffer:
            return
        # Short, simple items -> comma-separated prose
        if len(bullet_buffer) <= 5 and all(
            len(item) < 80 and "\n" not in item for item in bullet_buffer
        ):
            if len(bullet_buffer) == 1:
                result_lines.append(bullet_buffer[0] + ".")
            elif len(bullet_buffer) == 2:
                result_lines.append(f"{bullet_buffer[0]} and {bullet_buffer[1]}.")
            else:
                joined = ", ".join(bullet_buffer[:-1])
                result_lines.append(f"{joined}, and {bullet_buffer[-1]}.")
        else:
            # Complex items -> separate sentences
            for item in bullet_buffer:
                sentence = item.rstrip(".")
                result_lines.append(sentence + ".")
        bullet_buffer.clear()

    for line in lines:
        bullet_match = _BULLET.match(line)
        if bullet_match:
            item = line[bullet_match.end() :].strip()
            bullet_buffer.append(item)
        else:
            flush_bullets()
            result_lines.append(line)

    flush_bullets()
    return "\n".join(result_lines)


def _truncate_at_sentence(text: str, max_length: int) -> str:
    """Truncate text at the last sentence boundary before max_length."""
    truncated = text[:max_length]

    # Find last sentence-ending punctuation
    for end_char in [".", "!", "?"]:
        last_pos = truncated.rfind(end_char)
        if last_pos > max_length // 2:
            return (
                truncated[: last_pos + 1]
                + " The full response continues in the written message."
            )

    # No good boundary — hard truncate
    return (
        truncated.rstrip() + "... The full response continues in the written message."
    )
