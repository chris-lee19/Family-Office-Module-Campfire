from typing import Any

from django.db import models

from core.crypto import decrypt, encrypt


class EncryptedTextField(models.TextField):
    """Text encrypted at rest. Not queryable by value; use it only for secrets."""

    def from_db_value(self, value: str | None, expression: Any, connection: Any) -> str | None:
        if value in (None, ""):
            return value
        return decrypt(value)

    def get_prep_value(self, value: Any) -> Any:
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        return encrypt(value)
