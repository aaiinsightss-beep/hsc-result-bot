# -*- coding: utf-8 -*-
"""Captcha OCR — ddddocr ensemble with per-transform voters.

Portal captchas are 4 digits painted white over noisy landscape photos.
Single reads drop strokes (med filter eats thin '1's) or hallucinate CJK
glyphs from cloud texture, so decode() runs several model/transform
combos and returns the first read with exactly 4 digits; otherwise the
most common / longest digit string. Callers regrab a fresh image when
decode() is unsatisfied — image noise differs per grab.
"""
import io
import re
from collections import Counter

from PIL import Image, ImageFilter

try:
    import ddddocr
except Exception:
    ddddocr = None

_ENGINES = None  # lazy [(label, engine)] — model init is ~seconds


def _engines():
    global _ENGINES
    if _ENGINES is not None:
        return _ENGINES
    out = []
    if ddddocr is not None:
        for label, kw in (("std", {}), ("beta", {"beta": True}), ("old", {"old": True})):
            try:
                out.append((label, ddddocr.DdddOcr(show_ad=False, **kw)))
            except Exception:
                pass
    _ENGINES = out
    return out


def _prep(png: bytes, thr: int, scale: int, med: int) -> bytes:
    """White digits on photo noise -> black-on-white enlarged binary."""
    img = Image.open(io.BytesIO(png)).convert("L")
    img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    img = img.point(lambda p: 0 if p >= thr else 255)
    if med:
        img = img.filter(ImageFilter.MedianFilter(med))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _digits_only(text: str) -> str:
    return "".join(c for c in (text or "") if c.isdigit())


def _candidates(png: bytes):
    """All digit strings the voters produce, with a frequency vote."""
    reads = []
    for _, eng in _engines():
        try:
            reads.append(_digits_only(eng.classification(png)))
        except Exception:
            pass
    for thr, med in ((170, 5), (200, 5), (230, 3)):
        data = _prep(png, thr, 3, med)
        for _, eng in _engines():
            try:
                reads.append(_digits_only(eng.classification(data)))
            except Exception:
                pass
    return [r for r in reads if r]


def _eval_math(text: str):
    m = re.fullmatch(r"\s*(\d+)\s*([+\-*xX])\s*(\d+)\s*[?=\s]*", text or "")
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if op == "+":
        return str(a + b)
    if op in ("-",):
        return str(a - b)
    return str(a * b)


def decode(png: bytes) -> str:
    """bytes -> captcha answer. Raises RuntimeError when nothing credible.

    Portal-style captchas are pure 4-digit codes; legacy word/math forms
    fall through to the old classifier path.
    """
    votes = _candidates(png)
    if not votes:
        raise RuntimeError("captcha OCR returned nothing")

    # exact 4-digit hit — the portal's canonical shape
    four = [v for v in votes if len(v) == 4]
    if four:
        # earliest voter wins ties: reads are ordered raw -> prep
        return four[0]

    # math captcha arc ("3+2=?")
    math_ans = _eval_math(max(votes, key=len))
    if math_ans:
        return math_ans

    # fall back: longest read, then most frequent
    best = max(votes, key=len)
    if len(best) < 3:
        common = Counter(votes).most_common(1)
        if common and len(common[0][0]) >= len(best):
            best = common[0][0]
    return best


def confident(png: bytes) -> bool:
    """True when some voter produced the canonical 4-digit read."""
    return any(len(v) == 4 for v in _candidates(png))
