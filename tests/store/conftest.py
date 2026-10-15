import secrets

import pytest


@pytest.fixture
def store_key() -> str:
    return secrets.token_urlsafe(32)
