import json
import logging
from pathlib import Path

import pytest

from stdio_box.store.repo import Store


def test_sub_hash_is_stable_and_hides_the_subject(
    tmp_path: Path, store_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store.open(tmp_path / "data", store_key, field_names={"JIRA_API_TOKEN"})
    subject = "alice@example.com"
    with caplog.at_level(logging.INFO, logger="stdio_box.audit"):
        first = store.record("admin", sub=subject, outcome="ok")
        second = store.record("admin", sub=subject, outcome="ok")
    assert first is not None
    assert first == second == store.sub_hash(subject)
    assert first != subject
    assert len(first) == 16
    assert subject not in caplog.text
    assert first in caplog.text
    store.close()


def test_detail_rejects_provider_field_names(tmp_path: Path, store_key: str) -> None:
    store = Store.open(tmp_path / "data", store_key, field_names={"JIRA_API_TOKEN", "JIRA_URL"})
    with pytest.raises(ValueError, match="provider field name"):
        store.record("admin", outcome="ok", detail={"JIRA_API_TOKEN": "not-stored"})
    dumped = json.dumps(store.list_audit())
    assert "JIRA_API_TOKEN" not in dumped
    assert "not-stored" not in dumped
    store.close()
