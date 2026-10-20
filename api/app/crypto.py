"""Прозрачное шифрование free-text ПДн через pgcrypto (text-mode PGP).

F1 (152-ФЗ art.10; ПП РФ №1119 УЗ-2; ФСТЭК №21): спецкатегория в покое должна быть
зашифрована. EncryptedText — SQLAlchemy TypeDecorator: в БД хранится bytea
(pgp_sym_encrypt), при чтении возвращается расшифрованный str (pgp_sym_decrypt,
text-mode → сразу str). Сервисный слой работает с обычными str.

impl=LargeBinary даёт bytea DDL; process_bind_param кодирует str→bytes для бинда;
bind_expression кастует bytes→text и шифрует text-mode pgp_sym_encrypt (так decrypt
возвращает text, без Python-декода bytes). Ключ — settings.data_key (CC_DATA_KEY),
параметризованный bind (не в тексте SQL → не светится в pg_stat_activity). Пусто →
демо-ключ (прод-режим запретит через config._check_secrets). Продакшн-путь:
KMS (Yandex/VK Cloud) + LUKS-том + TDE, pgcrypto — уровень приложения для демо/Stage.
"""
from __future__ import annotations

import json

from sqlalchemy import func
from sqlalchemy.types import Text, TypeDecorator

from app.config import settings

# Стабильный демо-ключ: данные переживают рестарт контейнера. НЕ для прод (CC_PROD_MODE).
_DEMO_DATA_KEY = "compass-demo-data-key-do-not-use-in-prod-0123456789"


def data_key() -> str:
    k = settings.data_key
    return k if k else _DEMO_DATA_KEY


class EncryptedText(TypeDecorator):
    """str ↔ bytea(pgp_sym_encrypt/pgp_sym_decrypt, text-mode). Nullable-safe.

    impl=String: бинд str проходит напрямую и bind_expression оборачивает в
    pgp_sym_encrypt (LargeBinary impl обходил bind_expression → данные писались
    plaintext). DDL-колонка реально bytea (Alembic-миграция), impl влияет только на
    Python-side обработку и create_all (не используется — схема из миграций).
    """
    impl = Text
    cache_ok = True

    def bind_expression(self, bindvalue):
        if bindvalue is None:
            return None
        return func.pgp_sym_encrypt(bindvalue, data_key())

    def column_expression(self, col):
        return func.pgp_sym_decrypt(col, data_key())

    def process_result_value(self, value, dialect):
        # pgp_sym_decrypt возвращает text (str); bytes — страховка.
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8")
        return value
