from __future__ import annotations

import hashlib
import io
import logging

from PIL import Image

from app.ai import AIOrchestrator
from app.database import Database

logger = logging.getLogger(__name__)
MAX_IMAGE_VARIANTS = 5


class ImageCache:
    def __init__(self, database: Database, ai: AIOrchestrator) -> None:
        self.database = database
        self.ai = ai

    @staticmethod
    def hash_tag(tag: str) -> str:
        return hashlib.sha256(tag.encode("utf-8")).hexdigest()

    @staticmethod
    def task_image_tag(prompt: str) -> str:
        return f"plan-task1:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _image_hashes(image_bytes: bytes) -> tuple[str, str]:
        image_key = hashlib.sha256(image_bytes).hexdigest()
        with Image.open(io.BytesIO(image_bytes)) as image:
            grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(grayscale.tobytes())
        difference_bits = 0
        for row in range(8):
            for column in range(8):
                difference_bits = (difference_bits << 1) | int(
                    pixels[row * 9 + column] > pixels[row * 9 + column + 1]
                )
        return image_key, f"{difference_bits:016x}"

    def _image_seen(self, telegram_id: int, image_bytes: bytes) -> bool:
        image_key, difference_hash = self._image_hashes(image_bytes)
        return self.database.has_seen_image(
            telegram_id, image_key, difference_hash
        )

    def get_cached_image(self, tag: str) -> tuple[bytes, str | None, str] | None:
        for variant in range(MAX_IMAGE_VARIANTS):
            variant_tag = tag if variant == 0 else f"{tag}::variant:{variant}"
            tag_hash = self.hash_tag(variant_tag)
            cached = self.database.get_image(tag_hash)
            if cached is not None:
                return cached["image_bytes"], cached["telegram_file_id"], tag_hash
        return None

    async def get_unique_stimulus(
        self, telegram_id: int, kind: str, variation_key: str
    ) -> tuple[bytes, str | None, str]:
        from app.printables import make_stimulus

        for variant in range(MAX_IMAGE_VARIANTS):
            tag = f"stimulus:{kind}:{variation_key}:{variant}"
            tag_hash = self.hash_tag(tag)
            cached = self.database.get_image(tag_hash)
            if cached:
                image_bytes = cached["image_bytes"]
                file_id = cached["telegram_file_id"]
            else:
                image_bytes = make_stimulus(
                    kind, variation_key=f"{variation_key}:{variant}"
                )
                file_id = None
                self.database.save_image(tag_hash, tag, image_bytes)
            if not self._image_seen(telegram_id, image_bytes):
                return image_bytes, file_id, tag_hash
        raise RuntimeError("No unseen stimulus image could be created.")

    async def get_or_generate(
        self,
        tag: str,
        prompt: str,
        telegram_id: int | None = None,
        *,
        request_traces: list[dict[str, object]] | None = None,
    ) -> tuple[bytes, str | None, str]:
        for variant in range(MAX_IMAGE_VARIANTS):
            variant_tag = tag if variant == 0 else f"{tag}::variant:{variant}"
            tag_hash = self.hash_tag(variant_tag)
            cached = self.database.get_image(tag_hash)
            if cached:
                image_bytes = cached["image_bytes"]
                file_id = cached["telegram_file_id"]
            else:
                variation = (
                    ""
                    if variant == 0
                    else f" Create a distinct composition variation number {variant}: "
                    "change object placement, viewing angle, and background spacing."
                )
                if request_traces is None:
                    image_bytes = await self.ai.generate_image(
                        prompt + variation,
                        telegram_id=telegram_id,
                        image_cache_tag_hash=tag_hash,
                    )
                else:
                    image_bytes = await self.ai.generate_image(
                        prompt + variation,
                        telegram_id=telegram_id,
                        image_cache_tag_hash=tag_hash,
                        request_traces=request_traces,
                    )
                self.database.save_image(tag_hash, variant_tag, image_bytes)
                file_id = None
            if telegram_id is None or not self._image_seen(telegram_id, image_bytes):
                return image_bytes, file_id, tag_hash
            logger.info("Skipping previously sent image variant for user %s.", telegram_id)
        raise RuntimeError("No unseen AI image could be generated.")

    def remember_telegram_file(
        self, tag_hash: str, file_id: str, telegram_id: int | None = None
    ) -> None:
        self.database.save_telegram_file_id(tag_hash, file_id)
        if telegram_id is None:
            return
        cached = self.database.get_image(tag_hash)
        if cached is None:
            raise RuntimeError("Cannot record a Telegram file ID for a missing cached image.")
        image_key, difference_hash = self._image_hashes(cached["image_bytes"])
        image_content = {
            "tag": cached["tag"],
            "sha256": image_key,
            "difference_hash": difference_hash,
        }
        for image_identity in (f"sha256:{image_key}", f"dhash:{difference_hash}"):
            self.database.mark_content_used(
                telegram_id,
                "image",
                image_identity,
                image_identity,
                image_content,
            )
