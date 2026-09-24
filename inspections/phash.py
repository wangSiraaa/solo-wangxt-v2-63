"""Perceptual hash (pHash) for duplicate *candidate* generation.

Pure-Pillow implementation of the classic DCT average hash:

1. decode / EXIF-orient the image, convert to grayscale;
2. resize to 32x32;
3. compute a 2D type-II DCT;
4. take the top-left 8x8 low-frequency block;
5. bit each cell against the block median (excluding the DC term).

A Hamming distance <= ``PHASH_HAMMING_THRESHOLD`` only yields a *suspected*
duplicate.  It never merges events by itself: spatial, temporal and human
confirmation are always required downstream.
"""
from __future__ import annotations

import io
import math
from typing import List, Tuple

from django.conf import settings
from PIL import Image, ImageOps

HASH_SIZE = 8
DCT_SIZE = 32


def _dct_1d(vector: List[float]) -> List[float]:
    n = len(vector)
    out: List[float] = []
    for k in range(n):
        total = 0.0
        for i, x in enumerate(vector):
            total += x * math.cos(math.pi * (2 * i + 1) * k / (2 * n))
        out.append(total * math.sqrt(2.0 / n))
    return out


def dct2d(matrix: List[List[float]]) -> List[List[float]]:
    rows = [_dct_1d(row) for row in matrix]
    cols_in = [[rows[r][c] for r in range(len(rows))] for c in range(len(rows[0]))]
    cols = [_dct_1d(col) for col in cols_in]
    return [[cols[c][r] for c in range(len(cols))] for r in range(len(rows))]


def phash_from_image(image: Image.Image) -> str:
    image = ImageOps.exif_transpose(image).convert("L").resize(
        (DCT_SIZE, DCT_SIZE), Image.LANCZOS
    )
    pixels = [[float(image.getpixel((x, y))) for x in range(DCT_SIZE)]
              for y in range(DCT_SIZE)]
    coeffs = dct2d(pixels)
    block = [coeffs[y][x] for y in range(HASH_SIZE) for x in range(HASH_SIZE)]
    # Median of AC coefficients - the DC term (block[0]) is excluded.
    median = sorted(block[1:])[len(block) // 2]
    bits = 0
    for value in block:
        bits = (bits << 1) | (1 if value >= median else 0)
    return f"{bits:064x}"


def phash_from_bytes(data: bytes) -> str:
    image = Image.open(io.BytesIO(data))
    image.load()
    return phash_from_image(image)


def hamming_distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def looks_similar(a: str, b: str) -> bool:
    return hamming_distance(a, b) <= settings.PHASH_HAMMING_THRESHOLD
