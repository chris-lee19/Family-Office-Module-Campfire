"""Symmetric encryption for secrets stored in the database (OAuth tokens and the like)."""

from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    # MultiFernet encrypts with the first key and decrypts with any, so keys can be rotated.
    return MultiFernet([Fernet(key.strip().encode()) for key in settings.FIELD_ENCRYPTION_KEYS])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
