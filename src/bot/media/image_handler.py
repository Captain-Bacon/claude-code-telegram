"""Handle image uploads — download, encode, detect format."""

import base64
from dataclasses import dataclass
from typing import Optional

from telegram import PhotoSize

from src.config import Settings


@dataclass
class ProcessedImage:
    """Processed image ready for Claude."""

    prompt: str
    base64_data: str
    media_type: str
    size: int


_MAGIC_TO_MIME = [
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _detect_media_type(image_bytes: bytes) -> str:
    """Detect MIME type from magic bytes."""
    for magic, mime in _MAGIC_TO_MIME:
        if image_bytes.startswith(magic):
            return mime
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:12]:
        return "image/webp"
    return "image/png"  # safe fallback — Claude handles it


class ImageHandler:
    """Download and encode Telegram photos for Claude."""

    def __init__(self, config: Settings):
        self.config = config

    async def process_image(
        self, photo: PhotoSize, caption: Optional[str] = None
    ) -> ProcessedImage:
        """Download photo and prepare for multimodal send."""
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()
        media_type = _detect_media_type(bytes(image_bytes))
        base64_data = base64.b64encode(image_bytes).decode("utf-8")

        prompt = caption or "The user is sharing an image."

        return ProcessedImage(
            prompt=prompt,
            base64_data=base64_data,
            media_type=media_type,
            size=len(image_bytes),
        )
