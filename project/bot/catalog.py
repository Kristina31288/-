"""Каталог продуктов. Цены и названия — из brand.md (раздел 2).

Меняете цену в бренд-буке — поменяйте здесь, в knowledge.md и faq.json.
Только стандартная библиотека.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import Config


@dataclass(frozen=True)
class Product:
    key: str
    title: str
    price_rub: int
    description: str
    bundle: tuple[str, ...] = ()   # для набора — ключи входящих разборов

    @property
    def price_text(self) -> str:
        return format_price(self.price_rub)


def format_price(rub: int) -> str:
    # 1500 -> «1 500 ₽» (неразрывный пробел между разрядами)
    return f"{rub:,}".replace(",", " ") + " ₽"


_COMMON = (
    "Внутри: «светофор» — норма это или пора к специалисту, объяснение по возрастам "
    "(5–7, 8–11, 12–16 лет), готовые фразы, план на 7 дней и шпаргалка на холодильник. "
    "PDF 10–14 страниц, удобно читать с телефона."
)

PRODUCTS: dict[str, Product] = {
    "telefon": Product(
        key="telefon",
        title="Разбор 1. Что сказать, когда… ребёнок не отлипает от телефона и игр",
        price_rub=299,
        description=(
            "Для ситуаций, когда ребёнок не может оторваться от гаджета и злится, "
            "когда вы его забираете. " + _COMMON
        ),
    ),
    "podrostok": Product(
        key="podrostok",
        title="Разбор 2. Что сказать, когда… подросток хамит или закрылся",
        price_rub=299,
        description=(
            "Для ситуаций, когда подросток грубит, не разговаривает или отвечает односложно. "
            + _COMMON
        ),
    ),
    "uroki": Product(
        key="uroki",
        title="Разбор 3. Что сказать, когда… ребёнок не хочет учиться, а уроки заканчиваются криком",
        price_rub=299,
        description=(
            "Для вечеров, когда домашка тянется часами и заканчивается ссорой. " + _COMMON
        ),
    ),
    "nabor": Product(
        key="nabor",
        title="Набор из трёх разборов",
        price_rub=690,
        description="Все три разбора: телефон и игры, подросток, уроки. "
                    "Отдельно они стоят 897 ₽.",
        bundle=("telefon", "podrostok", "uroki"),
    ),
}

CONSULTATION_TITLE = "Консультация автора по вашей ситуации, 40 минут онлайн"
CONSULTATION_PRICE = 1500
CONSULTATION_DESCRIPTION = (
    "Разговор с автором один на один: разбираем вашу ситуацию и подбираем фразы и шаги "
    "именно для вашей семьи. Это информационно-просветительская консультация педагога, "
    "а не психологическая или медицинская помощь."
)
LEAD_MAGNET_TITLE = "10 фраз, которые останавливают ссору с ребёнком"
REFUND_RULE = (
    "Если разбор не подошёл, деньги можно вернуть в течение 7 дней после покупки. "
    "Точная формулировка — в оферте."
)

# Синонимы для команды /give
ALIASES = {
    "1": "telefon", "telefon": "telefon", "телефон": "telefon",
    "2": "podrostok", "podrostok": "podrostok", "подросток": "podrostok",
    "3": "uroki", "uroki": "uroki", "уроки": "uroki",
    "nabor": "nabor", "набор": "nabor", "all": "nabor", "все": "nabor",
}


def resolve_product(name: str) -> Product | None:
    key = ALIASES.get((name or "").strip().lower())
    return PRODUCTS.get(key) if key else None


def files_for(product: Product, config: Config) -> list[Path]:
    """Список PDF, которые надо отправить за продукт."""
    keys = product.bundle or (product.key,)
    return [Path(config.files_dir) / config.product_files[k] for k in keys]
