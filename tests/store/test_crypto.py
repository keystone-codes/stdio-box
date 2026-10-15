import os

import pytest

from stdio_box.store.crypto import (
    INFO_AUDIT_SUB,
    INFO_FORM_CSRF,
    INFO_KEY_CHECK,
    INFO_PROFILE_SECRETS,
    derive,
    open_,
    seal,
)
from stdio_box.store.errors import SecretUnreadable


def test_seal_round_trip(store_key: str) -> None:
    salt = os.urandom(16)
    key = derive(store_key, salt, INFO_PROFILE_SECRETS)
    values = {"JIRA_API_TOKEN": "test-token", "note": "kept"}
    nonce, ciphertext = seal(key, "atlassian/shared", 1, values)
    assert len(nonce) == 12
    assert open_(key, "atlassian/shared", 1, nonce, ciphertext) == values


def test_wrong_aad_is_unreadable(store_key: str) -> None:
    salt = os.urandom(16)
    key = derive(store_key, salt, INFO_PROFILE_SECRETS)
    nonce, ciphertext = seal(key, "atlassian/shared", 1, {"JIRA_API_TOKEN": "test-token"})
    with pytest.raises(SecretUnreadable):
        open_(key, "atlassian/other", 1, nonce, ciphertext)
    with pytest.raises(SecretUnreadable):
        open_(key, "atlassian/shared", 2, nonce, ciphertext)


def test_info_strings_derive_different_keys(store_key: str) -> None:
    salt = os.urandom(16)
    keys = [
        derive(store_key, salt, info)
        for info in (INFO_PROFILE_SECRETS, INFO_KEY_CHECK, INFO_FORM_CSRF, INFO_AUDIT_SUB)
    ]
    assert len(set(keys)) == len(keys)
