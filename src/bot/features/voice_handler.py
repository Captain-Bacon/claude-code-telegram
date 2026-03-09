"""Handle voice message transcription via Mistral, OpenAI, or Parakeet MLX."""

import asyncio
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram import Voice

from src.config.settings import Settings

logger = structlog.get_logger(__name__)


@dataclass
class ProcessedVoice:
    """Result of voice message processing."""

    prompt: str
    transcription: str
    duration: int


class VoiceHandler:
    """Transcribe Telegram voice messages using Mistral, OpenAI, or Parakeet MLX."""

    def __init__(self, config: Settings):
        self.config = config
        self._mistral_client: Optional[Any] = None
        self._openai_client: Optional[Any] = None
        self._parakeet_available: Optional[bool] = None

    def _ensure_allowed_file_size(self, file_size: Optional[int]) -> None:
        """Reject files that exceed the configured max size."""
        if (
            isinstance(file_size, int)
            and file_size > self.config.voice_max_file_size_bytes
        ):
            raise ValueError(
                "Voice message too large "
                f"({file_size / 1024 / 1024:.1f}MB). "
                f"Max allowed: {self.config.voice_max_file_size_mb}MB. "
                "Adjust VOICE_MAX_FILE_SIZE_MB if needed."
            )

    async def process_voice_message(
        self, voice: Voice, caption: Optional[str] = None
    ) -> ProcessedVoice:
        """Download and transcribe a voice message.

        1. Download .ogg bytes from Telegram
        2. Call the configured transcription provider (Mistral, OpenAI, or Parakeet)
        3. Build a prompt combining caption + transcription
        """
        initial_file_size = getattr(voice, "file_size", None)
        self._ensure_allowed_file_size(initial_file_size)

        # Resolve Telegram file metadata before downloading bytes.
        file = await voice.get_file()
        resolved_file_size = getattr(file, "file_size", None)
        self._ensure_allowed_file_size(resolved_file_size)

        # Refuse unknown-size payloads to avoid unbounded downloads.
        if not isinstance(initial_file_size, int) and not isinstance(
            resolved_file_size, int
        ):
            raise ValueError(
                "Unable to determine voice message size before download. "
                "Please retry with a smaller voice message."
            )

        # Download voice data
        voice_bytes = bytes(await file.download_as_bytearray())
        self._ensure_allowed_file_size(len(voice_bytes))

        logger.info(
            "Transcribing voice message",
            provider=self.config.voice_provider,
            duration=voice.duration,
            file_size=initial_file_size or resolved_file_size or len(voice_bytes),
        )

        if self.config.voice_provider == "parakeet":
            transcription = await self._transcribe_parakeet(voice_bytes)
        elif self.config.voice_provider == "openai":
            transcription = await self._transcribe_openai(voice_bytes)
        else:
            transcription = await self._transcribe_mistral(voice_bytes)

        logger.info(
            "Voice transcription complete",
            transcription_length=len(transcription),
            duration=voice.duration,
        )

        # Build prompt
        label = caption if caption else "Voice message transcription:"
        prompt = f"{label}\n\n{transcription}"

        dur = voice.duration
        duration_secs = int(dur.total_seconds()) if isinstance(dur, timedelta) else dur

        return ProcessedVoice(
            prompt=prompt,
            transcription=transcription,
            duration=duration_secs,
        )

    # -- Mistral provider ---------------------------------------------------

    async def _transcribe_mistral(self, voice_bytes: bytes) -> str:
        """Transcribe audio using the Mistral API (Voxtral)."""
        client = self._get_mistral_client()
        try:
            response = await client.audio.transcriptions.complete_async(
                model=self.config.resolved_voice_model,
                file={
                    "content": voice_bytes,
                    "file_name": "voice.ogg",
                },
            )
        except Exception as exc:
            logger.warning(
                "Mistral transcription request failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("Mistral transcription request failed.") from exc

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("Mistral transcription returned an empty response.")
        return text

    def _get_mistral_client(self) -> Any:
        """Create and cache a Mistral client on first use."""
        if self._mistral_client is not None:
            return self._mistral_client

        try:
            from mistralai import Mistral
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'mistralai' is missing for voice transcription. "
                "Install voice extras: "
                'pip install "claude-code-telegram[voice]"'
            ) from exc

        api_key = self.config.mistral_api_key_str
        if not api_key:
            raise RuntimeError("Mistral API key is not configured.")

        self._mistral_client = Mistral(api_key=api_key)
        return self._mistral_client

    # -- OpenAI provider ----------------------------------------------------

    async def _transcribe_openai(self, voice_bytes: bytes) -> str:
        """Transcribe audio using the OpenAI Whisper API."""
        client = self._get_openai_client()
        try:
            response = await client.audio.transcriptions.create(
                model=self.config.resolved_voice_model,
                file=("voice.ogg", voice_bytes),
            )
        except Exception as exc:
            logger.warning(
                "OpenAI transcription request failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("OpenAI transcription request failed.") from exc

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("OpenAI transcription returned an empty response.")
        return text

    def _get_openai_client(self) -> Any:
        """Create and cache an OpenAI client on first use."""
        if self._openai_client is not None:
            return self._openai_client

        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'openai' is missing for voice transcription. "
                "Install voice extras: "
                'pip install "claude-code-telegram[voice]"'
            ) from exc

        api_key = self.config.openai_api_key_str
        if not api_key:
            raise RuntimeError("OpenAI API key is not configured.")

        self._openai_client = AsyncOpenAI(api_key=api_key)
        return self._openai_client

    # -- Parakeet MLX provider (local, Mac-only) ----------------------------

    async def _transcribe_parakeet(self, voice_bytes: bytes) -> str:
        """Transcribe audio locally using Parakeet MLX.

        Parakeet runs on-device via Apple MLX -- no cloud API, no API key.
        Requires ffmpeg for OGG-to-WAV conversion and the parakeet-mlx CLI.
        """
        self._check_parakeet_available()

        tmp_dir = Path(tempfile.mkdtemp(prefix="parakeet_"))
        ogg_path = tmp_dir / "voice.ogg"
        wav_path = tmp_dir / "voice.wav"
        txt_path = tmp_dir / "voice.txt"

        try:
            # Write OGG bytes to disk (Telegram sends voice as OGG/Opus)
            ogg_path.write_bytes(voice_bytes)

            # Convert OGG -> WAV via ffmpeg (Parakeet needs WAV input)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(ogg_path),
                            "-ar",
                            "16000",
                            "-ac",
                            "1",
                            str(wav_path),
                        ],
                        check=True,
                        capture_output=True,
                    ),
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "ffmpeg is required for Parakeet voice transcription but was not "
                    "found. Install it via: brew install ffmpeg (macOS) or "
                    "apt install ffmpeg (Linux)"
                ) from exc
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "ffmpeg OGG-to-WAV conversion failed",
                    error=getattr(exc, "stderr", b"").decode(errors="replace"),
                )
                raise RuntimeError(
                    "Failed to convert voice message to WAV format."
                ) from exc
            finally:
                ogg_path.unlink(missing_ok=True)

            # Run parakeet-mlx CLI in executor (blocking ML inference)
            model = self.config.resolved_voice_model
            logger.info("Running Parakeet MLX transcription", model=model)

            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        [
                            "parakeet-mlx",
                            str(wav_path),
                            "--model",
                            model,
                            "--output-format",
                            "txt",
                            "--output-dir",
                            str(tmp_dir),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ),
                )
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "Parakeet MLX transcription failed",
                    error=getattr(exc, "stderr", ""),
                )
                raise RuntimeError(
                    "Parakeet MLX transcription failed."
                ) from exc

            text = txt_path.read_text().strip() if txt_path.exists() else ""
            if not text:
                raise ValueError(
                    "Parakeet MLX transcription returned an empty response."
                )
            return text

        finally:
            # Clean up temp files
            for p in (ogg_path, wav_path, txt_path):
                p.unlink(missing_ok=True)
            try:
                tmp_dir.rmdir()
            except OSError:
                pass

    def _check_parakeet_available(self) -> None:
        """Verify that parakeet-mlx CLI and ffmpeg are available."""
        if self._parakeet_available is True:
            return

        # Check ffmpeg
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                check=True,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            self._parakeet_available = False
            raise RuntimeError(
                "ffmpeg is required for Parakeet voice transcription but was not "
                "found. Install it via: brew install ffmpeg (macOS) or "
                "apt install ffmpeg (Linux)"
            ) from exc

        # Check parakeet-mlx
        try:
            subprocess.run(
                ["parakeet-mlx", "--help"],
                check=True,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            self._parakeet_available = False
            raise RuntimeError(
                "parakeet-mlx is not installed. Install it via: "
                'pip install "claude-code-telegram[voice-local]" '
                "or: pip install parakeet-mlx"
            ) from exc

        self._parakeet_available = True
        logger.info("Parakeet MLX voice provider available")
