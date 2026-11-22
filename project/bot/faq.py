"""Ответы по ключевым словам из faq.json — режим без ИИ (и запасной при сбое ИИ).

Только стандартная библиотека.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from textnorm import normalize

DEFAULT_FAQ_PATH = Path(__file__).resolve().parent / "faq.json"


@dataclass(frozen=True)
class FaqItem:
    id: str
    question: str
    keywords: tuple[str, ...]
    answer: str


@dataclass(frozen=True)
class FaqMatch:
    item: FaqItem | None
    score: int

    @property
    def found(self) -> bool:
        return self.item is not None


class FaqBase:
    def __init__(self, items: list[FaqItem], fallback: str) -> None:
        self.items = items
        self.fallback = fallback

    @classmethod
    def load(cls, path: str | Path = DEFAULT_FAQ_PATH) -> "FaqBase":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = [
            FaqItem(
                id=raw["id"],
                question=raw["question"],
                keywords=tuple(normalize(k) for k in raw["keywords"] if normalize(k)),
                answer=raw["answer"],
            )
            for raw in data["items"]
        ]
        return cls(items, data.get("fallback", "Этого я не знаю, передам вопрос автору."))

    @staticmethod
    def _keyword_hits(keyword: str, text: str, words: list[str]) -> bool:
        if " " in keyword:
            # фраза: последнее слово — по началу («что входит» ~ «что входить»)
            return (" " + text).find(" " + keyword) != -1
        return any(w.startswith(keyword) for w in words)

    def match(self, text: str) -> FaqMatch:
        norm = normalize(text)
        words = norm.split()
        best: FaqItem | None = None
        best_score = 0
        for item in self.items:
            score = 0
            for kw in item.keywords:
                if self._keyword_hits(kw, norm, words):
                    score += 2 if " " in kw else 1
            if score > best_score:
                best, best_score = item, score
        return FaqMatch(best, best_score)

    def answer(self, text: str) -> tuple[str, bool]:
        """(ответ, найден_ли). Если не найден — текст «передам автору»."""
        m = self.match(text)
        if m.found:
            return m.item.answer, True
        return self.fallback, False
