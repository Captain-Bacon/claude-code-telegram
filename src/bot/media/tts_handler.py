"""Text-to-speech synthesis via mlx-audio.

Generates speech audio from text using locally-running TTS models.
Default provider is Chatterbox via mlx-audio on Apple Silicon.
The model is configurable via TTS_MODEL so swapping to Kokoro,
Qwen3-TTS, or other mlx-audio-supported models is a config change.
"""

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import structlog

from src.config.settings import Settings

logger = structlog.get_logger(__name__)


@dataclass
class SynthesisResult:
    """Result of TTS synthesis."""

    audio_bytes: bytes
    duration_seconds: float
    text_length: int


class TTSHandler:
    """Synthesise speech from text using mlx-audio TTS models."""

    def __init__(self, config: Settings):
        self.config = config
        self._model: Optional[Any] = None

    async def synthesise(self, text: str) -> SynthesisResult:
        """Generate speech audio from text, return as OGG Opus bytes.

        Args:
            text: Plain text to synthesise (already adapted for speech).

        Returns:
            SynthesisResult with OGG Opus audio data, duration, and text length.
        """
        logger.info(
            "Starting TTS synthesis",
            model=self.config.tts_model,
            text_length=len(text),
        )

        wav_bytes, duration = await self._generate_wav(text)
        ogg_bytes = await self._convert_to_ogg_opus(wav_bytes)

        logger.info(
            "TTS synthesis complete",
            duration_seconds=round(duration, 1),
            audio_size_kb=round(len(ogg_bytes) / 1024, 1),
        )

        return SynthesisResult(
            audio_bytes=ogg_bytes,
            duration_seconds=duration,
            text_length=len(text),
        )

    def _get_model(self) -> Any:
        """Load and cache the TTS model on first use."""
        if self._model is not None:
            return self._model

        try:
            from mlx_audio.tts.utils import load_model
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'mlx-audio' is missing. "
                "Install TTS extras: "
                'pip install "claude-code-telegram[tts]"'
            ) from exc

        logger.info("Loading TTS model", model=self.config.tts_model)
        self._model = load_model(self.config.tts_model)
        return self._model

    async def _generate_wav(self, text: str) -> Tuple[bytes, float]:
        """Generate WAV audio from text (blocking, runs in executor).

        Returns:
            Tuple of (wav_bytes, duration_seconds).
        """
        model = self._get_model()

        def _run_synthesis() -> Tuple[bytes, float]:
            import mlx.core as mx
            from mlx_audio.audio_io import write as audio_write

            results = model.generate(text=text)
            audio_segments = [r.audio for r in results]
            audio = mx.concatenate(audio_segments, axis=0)

            duration = audio.shape[0] / model.sample_rate

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                audio_write(Path(tmp.name), audio, model.sample_rate)
                tmp.seek(0)
                wav_data = tmp.read()

            return wav_data, duration

        try:
            return await asyncio.get_event_loop().run_in_executor(None, _run_synthesis)
        except RuntimeError as exc:
            if "mlx-audio" in str(exc).lower() or "mlx" in str(exc).lower():
                raise
            logger.warning(
                "TTS synthesis failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise RuntimeError("TTS synthesis failed.") from exc
        except Exception as exc:
            logger.warning(
                "TTS synthesis failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise RuntimeError("TTS synthesis failed.") from exc

    async def _convert_to_ogg_opus(self, wav_bytes: bytes) -> bytes:
        """Convert WAV audio to OGG Opus format using ffmpeg.

        Args:
            wav_bytes: Raw WAV audio data.

        Returns:
            OGG Opus encoded audio bytes.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                "-f",
                "ogg",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg is required for speech output. "
                "Install it with: brew install ffmpeg"
            ) from exc

        ogg_bytes, stderr = await proc.communicate(input=wav_bytes)

        if proc.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Audio conversion failed: {error_msg}")

        return ogg_bytes
