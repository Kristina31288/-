"""Выдача после оплаты.

СЕЙЧАС (старт): платёжный сервис сам отправляет файл покупателю на e-mail,
а при проблемах владелица выдаёт PDF в Telegram командой /give.

ПОТОМ: вебхук Продамус/ЮKassa → `grant()`. Сервер для вебхука здесь НЕ реализован
сознательно: форматы и проверка подписи должны быть сверены с актуальной
документацией сервиса (см. README, раздел «Автоматическая выдача»). Ниже —
интерфейс, в который такой обработчик нужно встроить.

Только стандартная библиотека.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol

from catalog import PRODUCTS, Product
from storage import Storage


@dataclass(frozen=True)
class PaymentEvent:
    provider: str          # prodamus | yookassa
    external_id: str       # id платежа в сервисе — для защиты от повторной выдачи
    user_id: int           # Telegram ID покупателя (передаётся в ссылке на оплату)
    product_key: str       # telefon | podrostok | uroki | nabor
    amount_rub: int


class PaymentProvider(Protocol):
    name: str

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> PaymentEvent | None:
        """Проверить подпись и вернуть событие успешной оплаты (или None, если это не оплата).
        При неверной подписи — бросить ValueError."""
        ...


class ProdamusProvider:
    """TODO: реализовать после сверки с документацией Продамус.

    Что нужно уточнить в документации/личном кабинете:
    - как передать Telegram ID покупателя в ссылку оплаты (доп. параметр ссылки);
    - формат уведомления об оплате и заголовок с подписью (HMAC секретным ключом);
    - поле со статусом успешной оплаты и id заказа.
    """
    name = "prodamus"

    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> PaymentEvent | None:
        raise NotImplementedError("Сверить формат вебхука Продамус и проверку подписи")


class YooKassaProvider:
    """TODO: реализовать после сверки с документацией ЮKassa.

    Что нужно уточнить:
    - событие об успешной оплате (уведомления HTTP в личном кабинете);
    - передача Telegram ID и ключа продукта в metadata платежа;
    - проверка подлинности уведомления (рекомендуется перезапросить платёж по id через API
      и сверить статус и сумму, а также ограничить IP-адреса отправителя).
    """
    name = "yookassa"

    def __init__(self, shop_id: str, secret_key: str) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> PaymentEvent | None:
        raise NotImplementedError("Сверить формат уведомлений ЮKassa и способ проверки")


class GrantError(ValueError):
    pass


SendDocuments = Callable[[int, Product], Awaitable[None]]


async def grant(event: PaymentEvent, storage: Storage, send_documents: SendDocuments) -> bool:
    """Учесть оплату и отправить PDF. False — если этот платёж уже был выдан (повтор)."""
    product = PRODUCTS.get(event.product_key)
    if product is None:
        raise GrantError(f"Неизвестный продукт: {event.product_key}")
    if event.amount_rub < product.price_rub:
        raise GrantError(f"Сумма {event.amount_rub} меньше цены {product.price_rub}")
    if storage.get_user(event.user_id) is None:
        raise GrantError("Пользователь не запускал бота — выдайте файл по e-mail")
    if not storage.add_purchase(event.user_id, product.key, event.provider, event.external_id):
        return False
    await send_documents(event.user_id, product)
    return True
