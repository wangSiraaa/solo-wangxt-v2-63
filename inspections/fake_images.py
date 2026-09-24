"""Programmatically generated mock inspection photos (Pillow only).

The generator draws recognizable "problems" (overflowing bin, road stain,
illegal dump) with a deterministic seed, plus controlled perturbations:

* ``rephoto``        : same content, slightly shifted/rotated/recompressed -
                       simulates the same spot from another angle. pHash stays
                       within the Hamming threshold.
* ``same_picture_far_away`` : byte-identical image claimed at a far-away grid
                       (the mis-upload case). pHash distance is 0 but the
                       location is kilometers off.
"""
from __future__ import annotations

import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 256

SCENES = {
    "garbage_overflow": (90, 110, 80),
    "road_stain": (110, 105, 100),
    "illegal_dump": (120, 95, 80),
}


def _rng(seed: str) -> random.Random:
    return random.Random(seed)


def _base_scene(kind: str, seed: str) -> Image.Image:
    bg = SCENES[kind]
    rng = _rng(seed)
    img = Image.new("RGB", (SIZE, SIZE), bg)
    draw = ImageDraw.Draw(img, "RGBA")

    # road surface texture (deterministic speckle)
    for _ in range(1400):
        x, y = rng.randrange(SIZE), rng.randrange(SIZE)
        shade = rng.randrange(-18, 18)
        draw.point((x, y), fill=tuple(max(0, min(255, c + shade)) for c in bg))

    # lane marking
    draw.rectangle([SIZE // 2 - 4, 0, SIZE // 2 + 4, SIZE],
                   fill=(220, 210, 90, 160))

    if kind == "garbage_overflow":
        # green bin tipped over with black bags spilling out
        draw.rectangle([70, 140, 150, 205], fill=(60, 130, 70), outline=(20, 60, 30), width=3)
        for _ in range(7):
            cx = rng.randrange(60, 190)
            cy = rng.randrange(120, 230)
            r = rng.randrange(12, 22)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(30, 30, 34, 240))
            draw.arc([cx - r, cy - r, cx + r, cy + r], 10, 160,
                     fill=(70, 70, 75), width=2)
    elif kind == "road_stain":
        cx, cy = 128, 150
        for i in range(10):
            r = 60 - i * 4
            offset = (rng.randrange(-6, 6), rng.randrange(-6, 6))
            alpha = 40 + i * 18
            draw.ellipse([cx - r + offset[0], cy - r + offset[1],
                          cx + r + offset[0], cy + r + offset[1]],
                         fill=(50, 35, 25, alpha))
    elif kind == "illegal_dump":
        for _ in range(9):
            cx, cy = rng.randrange(60, 200), rng.randrange(100, 210)
            w, h = rng.randrange(26, 46), rng.randrange(20, 40)
            color = rng.choice([(140, 70, 60), (160, 140, 70), (90, 90, 100),
                                (110, 130, 80)])
            draw.rectangle([cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2],
                           fill=color, outline=(20, 20, 20), width=2)

    img = img.filter(ImageFilter.GaussianBlur(0.6))
    return img


def make_photo(kind: str, seed: str = "default") -> bytes:
    """Original photo for a scene/seed, encoded as JPEG bytes."""
    img = _base_scene(kind, seed)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def make_rephoto(kind: str, seed: str = "default", angle: float = 2.5,
                 shift: int = 4, quality: int = 80) -> bytes:
    """Perturbed re-shoot of the same scene: rotated/shifted/recompressed.

    The low-frequency content (hence pHash) stays essentially identical even
    though bytes and pixels differ.
    """
    img = _base_scene(kind, seed)
    img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=SCENES[kind])
    img = img.transform(
        (SIZE, SIZE), Image.AFFINE,
        (1, 0, shift, 0, 1, -shift), resample=Image.BICUBIC,
        fillcolor=SCENES[kind],
    )
    img = img.filter(ImageFilter.GaussianBlur(0.4))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
