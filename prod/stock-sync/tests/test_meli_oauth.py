from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import meli_oauth


@pytest.fixture
def settings(tmp_path: Path) -> meli_oauth.OAuthSettings:
    return meli_oauth.OAuthSettings(
        app_id="123456",
        client_secret="secret",
        expected_seller_id="998877",
        public_host="sync.zipp.cl",
        tokens_file=tmp_path / "meli_tokens.json",
        pending_file=tmp_path / "meli_oauth_pending.json",
    )


def authorization(settings: meli_oauth.OAuthSettings, now: float = 1000) -> tuple[str, dict, dict]:
    url = meli_oauth.create_authorization(settings, now=now)
    query = parse_qs(urlsplit(url).query)
    pending = json.loads(settings.pending_file.read_text())
    return url, query, pending


def test_authorization_url_uses_exact_redirect_pkce_and_private_state_file(settings):
    url, query, pending = authorization(settings)

    assert urlsplit(url)._replace(query="").geturl() == meli_oauth.AUTHORIZATION_URL
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["123456"]
    assert query["redirect_uri"] == ["https://sync.zipp.cl/oauth/meli/callback"]
    assert query["state"] == [pending["state"]]
    assert query["code_challenge_method"] == ["S256"]
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(pending["verifier"].encode()).digest()
    ).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected_challenge]
    assert pending["created_at"] == 1000
    assert settings.pending_file.stat().st_mode & 0o777 == 0o600


def test_authorization_refuses_to_replace_valid_tokens(settings):
    settings.tokens_file.write_text(json.dumps({
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": 9999,
    }))

    with pytest.raises(ValueError, match="refusing to replace"):
        meli_oauth.create_authorization(settings)

    assert not settings.pending_file.exists()


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_exchange_verifies_seller_and_atomically_saves_tokens(settings):
    _, query, pending = authorization(settings)
    request = {}

    def post(url, *, data, timeout):
        request.update(url=url, data=data, timeout=timeout)
        return Response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 21600,
            "user_id": 998877,
        })

    seller_id = meli_oauth.exchange_code(
        settings, "authorization-code", query["state"][0], now=1100, post=post
    )

    assert request == {
        "url": meli_oauth.TOKEN_URL,
        "data": {
            "grant_type": "authorization_code",
            "client_id": "123456",
            "client_secret": "secret",
            "code": "authorization-code",
            "redirect_uri": "https://sync.zipp.cl/oauth/meli/callback",
            "code_verifier": pending["verifier"],
        },
        "timeout": 30,
    }
    assert seller_id == "998877"
    assert json.loads(settings.tokens_file.read_text()) == {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": 22700,
        "user_id": "998877",
    }
    assert settings.tokens_file.stat().st_mode & 0o777 == 0o600
    assert not settings.pending_file.exists()


def test_seller_mismatch_does_not_write_tokens_or_consume_pending_state(settings):
    _, query, _ = authorization(settings)

    def post(*_args, **_kwargs):
        return Response({
            "access_token": "wrong-access",
            "refresh_token": "wrong-refresh",
            "expires_in": 21600,
            "user_id": 1,
        })

    with pytest.raises(ValueError, match="does not match"):
        meli_oauth.exchange_code(settings, "code", query["state"][0], now=1100, post=post)

    assert not settings.tokens_file.exists()
    assert settings.pending_file.exists()


def test_bootstrap_can_capture_seller_id_before_it_is_configured(settings):
    settings = meli_oauth.OAuthSettings(
        **{**settings.__dict__, "expected_seller_id": None}
    )
    _, query, _ = authorization(settings)

    def post(*_args, **_kwargs):
        return Response({
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 21600,
            "user_id": 123456789,
        })

    seller_id = meli_oauth.exchange_code(settings, "code", query["state"][0], now=1100, post=post)

    assert seller_id == "123456789"
    assert json.loads(settings.tokens_file.read_text())["user_id"] == "123456789"


@pytest.mark.parametrize("state,now,message", [
    ("wrong", 1100, "Invalid"),
    (None, 1000 + meli_oauth.PENDING_TTL_SECONDS + 1, "expired"),
])
def test_exchange_rejects_invalid_or_expired_state_without_network(settings, state, now, message):
    _, query, _ = authorization(settings)
    actual_state = query["state"][0] if state is None else state

    def post(*_args, **_kwargs):
        raise AssertionError("network must not be used")

    with pytest.raises(ValueError, match=message):
        meli_oauth.exchange_code(settings, "code", actual_state, now=now, post=post)


@pytest.mark.parametrize("path,message", [
    ("/wrong?code=a&state=b", "Unknown"),
    ("/oauth/meli/callback?error=access_denied&state=b", "denied"),
    ("/oauth/meli/callback?code=a", "missing"),
    ("/oauth/meli/callback?code=&state=b", "empty"),
])
def test_callback_parameter_validation(path, message):
    with pytest.raises(ValueError, match=message):
        meli_oauth.callback_parameters(path)
