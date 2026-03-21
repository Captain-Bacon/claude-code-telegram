"""Media handlers for voice, image, and TTS processing."""

from .image_handler import ImageHandler, ProcessedImage
from .text_adapter import adapt_for_speech
from .tts_handler import SynthesisResult, TTSHandler
from .voice_handler import ProcessedVoice, VoiceHandler

__all__ = [
    "ImageHandler",
    "ProcessedImage",
    "VoiceHandler",
    "ProcessedVoice",
    "TTSHandler",
    "SynthesisResult",
    "adapt_for_speech",
]
