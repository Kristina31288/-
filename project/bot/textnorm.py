"""Нормализация русского текста для поиска ключевых слов.

Только стандартная библиотека. Используется в safety.py и faq.py.
"""
from __future__ import annotations

import re
import unicodedata

# Невидимые символы, которыми иногда «разрывают» слова.
_INVISIBLE_RE = re.compile("[­​-‏⁠﻿]")
_NON_WORD_RE = re.compile(r"[^0-9a-zа-я]+")
_SPACES_RE = re.compile(r"\s+")
_HAS_CYR_RE = re.compile(r"[а-я]")
_REPEAT_RE = re.compile(r"(.)\1+")
# 3+ одиночные буквы подряд: «с у и ц и д»
_SPACED_LETTERS_RE = re.compile(r"\b(?:[а-яa-z] ){2,}[а-яa-z]\b")

# Латинские буквы, похожие на кириллические (подмена внутри русских слов).
_LAT2CYR = str.maketrans(
    {
        "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х",
        "y": "у", "k": "к", "m": "м", "t": "т", "h": "н", "b": "в",
        "0": "о",
    }
)


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, без пунктуации, латинские «двойники» → кириллица."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _INVISIBLE_RE.sub("", t)
    t = t.lower().replace("ё", "е")
    t = _NON_WORD_RE.sub(" ", t)
    tokens = []
    for tok in t.split():
        if _HAS_CYR_RE.search(tok):
            tok = tok.translate(_LAT2CYR)
        tokens.append(tok)
    return " ".join(tokens)


def squeeze(text: str) -> str:
    """Схлопывает повторы букв: «жиииить» → «жить»."""
    return _REPEAT_RE.sub(r"\1", text)


def join_spaced_letters(text: str) -> str:
    """«с у и ц и д» → «суицид»."""
    return _SPACED_LETTERS_RE.sub(lambda m: m.group(0).replace(" ", ""), text)


def variants(text: str) -> list[str]:
    """Набор нормализованных вариантов текста для сопоставления с шаблонами."""
    base = normalize(text)
    out = [base]
    for v in (squeeze(base), join_spaced_letters(base), squeeze(join_spaced_letters(base))):
        if v not in out:
            out.append(v)
    return out


def collapse_spaces(text: str) -> str:
    return _SPACES_RE.sub(" ", text).strip()
