"""Perceptual frame hashing (GitHub Skills Programme, Lane 2, 2026-11-25).

In-house dHash + pHash over numpy + Pillow — the lane evaluated the
ImageHash library and rejected it: it drags scipy + PyWavelets into
the image solely for a DCT/wavelet, while pHash's DCT at 32x32 is a
single precomputed matrix multiply and dHash needs no transform at
all. Zero new dependencies.

What this is FOR: near-duplicate / visual-diversity evidence over
sampled frames — e.g. a "video" asset whose sampled frames are all
the same image, or two candidate assets that are the same footage.
Hashes are RECORDED evidence (frame_verification attaches them);
promotion to any rejection is a separate, measured decision under
the standing calibration rule.

Hashes are 64-bit, rendered as 16 hex chars. Hamming distance is
the comparison: 0 = identical, <=5 near-identical for dHash on
natural frames, 64 = opposite.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

_HASH_BITS = 64


def _gray_array(image, size):
    gray = image.convert("L").resize(size, Image.LANCZOS)
    return np.asarray(gray, dtype=np.float64)


def dhash(image) -> str:
    """Difference hash: 9x8 greyscale, left<right comparisons."""
    arr = _gray_array(image, (9, 8))
    diff = arr[:, 1:] > arr[:, :-1]
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _dct_matrix(n=32):
    """Orthonormal DCT-II basis matrix (no scipy needed at 32x32)."""
    k = np.arange(n)[:, None]
    x = np.arange(n)[None, :]
    basis = np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    basis[0, :] *= 1.0 / np.sqrt(2.0)
    return basis * np.sqrt(2.0 / n)


_DCT32 = _dct_matrix(32)


def phash(image) -> str:
    """Perceptual hash: 32x32 DCT, low-frequency 8x8 vs its median."""
    arr = _gray_array(image, (32, 32))
    dct = _DCT32 @ arr @ _DCT32.T
    low = dct[:8, :8]
    median = np.median(low)
    value = 0
    for bit in (low > median).flatten():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hamming(hash_a: str, hash_b: str) -> int:
    """Bit distance between two 64-bit hex hashes."""
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


def frame_hashes(images) -> dict:
    """Hash a list of PIL frames and summarise their diversity.

    Returns {count, dhash: [...], phash: [...], min_pairwise_dhash,
    max_pairwise_dhash} — the pairwise extremes are the recorded
    diversity signal: a genuinely moving video's sampled frames sit
    far apart; a static-image-masquerading-as-video sits at ~0.
    """
    dhashes = [dhash(img) for img in images]
    phashes = [phash(img) for img in images]
    distances = [
        hamming(dhashes[i], dhashes[j])
        for i in range(len(dhashes))
        for j in range(i + 1, len(dhashes))
    ]
    return {
        "count": len(images),
        "dhash": dhashes,
        "phash": phashes,
        "min_pairwise_dhash": min(distances) if distances else None,
        "max_pairwise_dhash": max(distances) if distances else None,
    }
