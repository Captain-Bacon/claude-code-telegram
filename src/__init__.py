"""Claude Code Telegram Bot.

A Telegram bot that provides remote access to Claude Code CLI, allowing developers
to interact with their projects from anywhere through a secure, terminal-like
interface within Telegram.
"""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

# Read version from pyproject.toml when running from source (always current).
# Fall back to installed package metadata for pip installs without source tree.
_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
try:
    with open(_pyproject, "rb") as _f:
        __version__: str = tomllib.load(_f)["project"]["version"]
except Exception:
    try:
        __version__ = _pkg_version("claude-code-telegram")
    except PackageNotFoundError:
        __version__ = "0.0.0-dev"

__author__ = "Richard Atkinson"
__email__ = "richardatk01@gmail.com"
__license__ = "MIT"
__homepage__ = "https://github.com/richardatkinson/claude-code-telegram"


def get_build_info() -> str:
    """Return branch@commit for version identification.

    Example: 'feature/persistent-client-v2@3e238ba'
    Falls back to just the version string if git isn't available.
    """
    import subprocess

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
        ).stdout.strip()
        if branch and commit:
            return f"{branch}@{commit}"
    except Exception:
        pass
    return __version__
