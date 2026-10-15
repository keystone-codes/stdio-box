"""Signed identity: audience, expiry, algorithm, type, target, and jti replay."""

import hashlib
import json
import logging
import time
from pathlib import Path

import jwt
import pytest
from tests.support.facade import make_config, make_token

from stdio_box._vendor import keystone_identity
from stdio_box.identity import CallRejected, ReplayCache, verify_call

VECTOR_PATH = Path(__file__).resolve().parent / "data" / "test-vector.json"
VENDOR_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "stdio_box" / "_vendor" / "keystone_identity.py"
)
# Body below the vendored header. Same digest as the source file at that commit.
VENDOR_BODY_SHA256 = "50058263846b41b5c9a88e303b6e9ac6768297d8f7f6564f4a4c0da247b4429f"


def _headers(token: str | None = None, **extra: str) -> dict[str, str]:
    headers = {key.lower(): value for key, value in extra.items()}
    if token is not None:
        headers["x-keystone-identity"] = token
    return headers


def _rejected(
    headers: dict[str, str], tool: str = "who", cache: ReplayCache | None = None
) -> CallRejected:
    with pytest.raises(CallRejected) as caught:
        verify_call(headers, tool, make_config(), cache or ReplayCache())
    return caught.value


def test_vendor_file_is_the_pinned_source() -> None:
    text = VENDOR_PATH.read_text(encoding="utf-8")
    assert "6b04a198101dac22815b17d5bcf55b0e1c6463d8" in text
    body = text[text.index('"""Reference verifier') :]
    assert hashlib.sha256(body.encode()).hexdigest() == VENDOR_BODY_SHA256


def test_rejects_bad_assertions() -> None:
    now = int(time.time())
    cases = {
        "audience": make_token(tool="who", audience="other-box"),
        "expired": make_token(tool="who", iat=now - 180, exp=now - 120),
        "algorithm": make_token(tool="who", alg="HS384", secret="h" * 48),
        "typ": make_token(tool="who", typ="JWT"),
        "target": make_token(tool="other"),
    }
    for label, token in cases.items():
        rejected = _rejected(_headers(token), tool="who")
        assert rejected.refusal.code == "identity_rejected", label
        assert rejected.refusal.message.startswith("identity rejected:"), label
        assert token not in rejected.refusal.message

    author = _rejected(_headers(None, **{"X-Author-Email": "boss@example.com"}))
    assert author.refusal.code == "identity_rejected"
    assert "boss@example.com" not in author.refusal.message


def test_accepts_human_and_service_account() -> None:
    human = verify_call(_headers(make_token(tool="who")), "who", make_config(), ReplayCache())
    assert human.sub == "ann@example.com"
    assert human.sub_type == "human"

    service = verify_call(
        _headers(make_token(tool="who", sub="svc:backend", sub_type="service")),
        "who",
        make_config(),
        ReplayCache(),
    )
    assert service.sub == "svc:backend"
    assert service.sub_type == "service"


def test_jti_replay_and_hold_until_expiry() -> None:
    cache = ReplayCache()
    token = make_token(tool="who", jti="once")
    verify_call(_headers(token), "who", make_config(), cache)
    again = _rejected(_headers(token), cache=cache)
    assert again.refusal.code == "identity_rejected"
    assert token not in again.refusal.message

    start = time.time()
    clock = {"now": start}

    def now() -> float:
        return clock["now"]

    held = ReplayCache(clock=now)
    exp = int(start) + 20 * 60
    live = make_token(tool="who", jti="held", iat=int(start), exp=exp)
    verify_call(_headers(live), "who", make_config(), held)
    clock["now"] = start + 15 * 60
    replayed = _rejected(_headers(live), cache=held)
    assert replayed.refusal.code == "identity_rejected"
    assert "already used" in replayed.refusal.message


def test_full_replay_cache_rejects_without_evicting(caplog: pytest.LogCaptureFixture) -> None:
    cache = ReplayCache(limit=3)
    for index in range(3):
        verify_call(
            _headers(make_token(tool="who", jti=f"jti-{index}")),
            "who",
            make_config(),
            cache,
        )
    with caplog.at_level(logging.WARNING, logger="stdio_box.identity"):
        rejected = _rejected(_headers(make_token(tool="who", jti="jti-new")), cache=cache)
    assert rejected.refusal.code == "identity_rejected"
    assert any(
        record.message == "replay_cache_full" and record.levelno == logging.WARNING
        for record in caplog.records
    )
    # The first assertion is still stored: a full cache does not drop it.
    still = _rejected(_headers(make_token(tool="who", jti="jti-0")), cache=cache)
    assert "already used" in still.refusal.message


def test_reference_vector() -> None:
    vector = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    header = jwt.get_unverified_header(vector["token"])
    assert header["typ"] == keystone_identity.IDENTITY_TYP
    assert header["alg"] == "HS256"
    claims = jwt.decode(
        vector["token"],
        vector["secret"],
        algorithms=["HS256"],
        audience=vector["audience"],
        issuer=vector["issuer"],
        options={"verify_exp": False},
    )
    assert claims == vector["claims"]
    identity = keystone_identity.verify_keystone_identity(
        vector["token"],
        secrets=[vector["secret"]],
        audience=vector["audience"],
        issuer=vector["issuer"],
        leeway=10**12,
    )
    assert identity.sub == vector["claims"]["sub"]
    assert identity.sub_type == vector["claims"]["sub_type"]
    assert identity.mcp_target == vector["claims"]["mcp_target"]
    assert identity.jti == vector["claims"]["jti"]
