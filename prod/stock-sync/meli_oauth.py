"""One-time Mercado Libre OAuth bootstrap with PKCE and state validation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import requests


AUTHORIZATION_URL = "https://auth.mercadolibre.cl/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
CALLBACK_PATH = "/oauth/meli/callback"
PENDING_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class OAuthSettings:
    app_id: str
    client_secret: str
    expected_seller_id: str | None
    public_host: str
    tokens_file: Path
    pending_file: Path
    host: str = "0.0.0.0"
    port: int = 3001

    @property
    def redirect_uri(self) -> str:
        return f"https://{self.public_host}{CALLBACK_PATH}"

    @classmethod
    def from_env(cls) -> "OAuthSettings":
        required = ("MELI_APP_ID", "MELI_CLIENT_SECRET", "STOCK_SYNC_PUBLIC_HOST")
        values = {name: os.environ.get(name, "").strip() for name in required}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        host = values["STOCK_SYNC_PUBLIC_HOST"]
        if urlsplit(f"//{host}").hostname != host or any(character in host for character in "/?#@"):
            raise ValueError("STOCK_SYNC_PUBLIC_HOST must be a hostname without scheme or path")
        expected_seller_id = os.environ.get("MELI_EXPECTED_SELLER_ID", "").strip()
        if expected_seller_id and not expected_seller_id.isdigit():
            raise ValueError("MELI_EXPECTED_SELLER_ID must be a numeric user ID or empty during bootstrap")
        tokens_file = Path(os.environ.get("MELI_TOKENS_FILE", "/data/meli_tokens.json"))
        return cls(
            app_id=values["MELI_APP_ID"],
            client_secret=values["MELI_CLIENT_SECRET"],
            expected_seller_id=expected_seller_id or None,
            public_host=host,
            tokens_file=tokens_file,
            pending_file=tokens_file.with_name("meli_oauth_pending.json"),
            host=os.environ.get("MELI_OAUTH_HOST", "0.0.0.0"),
            port=int(os.environ.get("MELI_OAUTH_PORT", "3001")),
        )


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(payload, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _valid_existing_tokens(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text())
        return (
            isinstance(payload.get("access_token"), str) and bool(payload["access_token"])
            and isinstance(payload.get("refresh_token"), str) and bool(payload["refresh_token"])
            and isinstance(payload.get("expires_at"), (int, float)) and payload["expires_at"] > 0
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def create_authorization(settings: OAuthSettings, *, now: float | None = None) -> str:
    if _valid_existing_tokens(settings.tokens_file):
        raise ValueError("A valid Mercado Libre token file already exists; refusing to replace it")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    _atomic_json(settings.pending_file, {
        "created_at": time.time() if now is None else now,
        "redirect_uri": settings.redirect_uri,
        "state": state,
        "verifier": verifier,
    })
    return f"{AUTHORIZATION_URL}?{urlencode({
        'response_type': 'code',
        'client_id': settings.app_id,
        'redirect_uri': settings.redirect_uri,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })}"


def _pending(settings: OAuthSettings, state: str, now: float) -> dict:
    try:
        pending = json.loads(settings.pending_file.read_text())
        created_at = float(pending["created_at"])
        expected_state = str(pending["state"])
        verifier = str(pending["verifier"])
        redirect_uri = str(pending["redirect_uri"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("No valid pending Mercado Libre authorization exists") from error
    if now < created_at or now - created_at > PENDING_TTL_SECONDS:
        raise ValueError("The Mercado Libre authorization request expired")
    if not hmac.compare_digest(expected_state, state):
        raise ValueError("Invalid Mercado Libre OAuth state")
    if redirect_uri != settings.redirect_uri or not 43 <= len(verifier) <= 128:
        raise ValueError("Invalid Mercado Libre authorization state file")
    return pending


def exchange_code(
    settings: OAuthSettings,
    code: str,
    state: str,
    *,
    now: float | None = None,
    post: Callable = requests.post,
) -> str:
    current_time = time.time() if now is None else now
    pending = _pending(settings, state, current_time)
    try:
        response = post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": settings.app_id,
            "client_secret": settings.client_secret,
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "code_verifier": pending["verifier"],
        }, timeout=30)
    except requests.RequestException as error:
        raise ValueError("Mercado Libre token exchange network failure") from error
    if response.status_code != 200:
        raise ValueError(f"Mercado Libre token exchange failed with HTTP {response.status_code}")
    try:
        payload = response.json()
        access_token = payload["access_token"]
        refresh_token = payload["refresh_token"]
        expires_in = payload["expires_in"]
        seller_id = str(payload["user_id"])
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("access token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("refresh token")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise ValueError("expiry")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Mercado Libre returned an invalid token response") from error
    if settings.expected_seller_id is not None and seller_id != settings.expected_seller_id:
        raise ValueError("Authorized Mercado Libre seller does not match MELI_EXPECTED_SELLER_ID")
    _atomic_json(settings.tokens_file, {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": current_time + expires_in,
        "user_id": seller_id,
    })
    settings.pending_file.unlink(missing_ok=True)
    return seller_id


def callback_parameters(path: str) -> tuple[str, str]:
    parsed = urlsplit(path)
    if parsed.path != CALLBACK_PATH:
        raise ValueError("Unknown callback path")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("error"):
        raise ValueError("Mercado Libre authorization was denied")
    if len(query.get("code", [])) != 1 or len(query.get("state", [])) != 1:
        raise ValueError("Mercado Libre callback is missing code or state")
    code, state = query["code"][0], query["state"][0]
    if not code or not state:
        raise ValueError("Mercado Libre callback contains an empty code or state")
    return code, state


def _page(title: str, message: str) -> bytes:
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{title}</title><h1>{title}</h1><p>{message}</p>"
    ).encode()


def serve(settings: OAuthSettings) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            try:
                code, state = callback_parameters(self.path)
                seller_id = exchange_code(settings, code, state)
            except ValueError as error:
                print(f"OAuth callback failed: {error}", flush=True)
                body = _page("Autorizacion fallida", "No se guardaron tokens. Revise los logs seguros del servicio.")
                self.send_response(400)
            else:
                body = _page(
                    "Autorizacion completada",
                    f"ID numerico del vendedor: {seller_id}. Los tokens se guardaron; puede cerrar esta ventana.",
                )
                print(f"Mercado Libre seller ID authorized: {seller_id}", flush=True)
                self.send_response(200)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    # A single-threaded server serializes callbacks so one state cannot be
    # exchanged concurrently by two requests.
    server = HTTPServer((settings.host, settings.port), Handler)
    print("Mercado Libre OAuth callback ready", flush=True)
    server.serve_forever()
    server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="Create and print a one-time authorization URL")
    commands.add_parser("serve", help="Wait for the HTTPS callback proxied by Nginx")
    args = parser.parse_args(argv)
    try:
        settings = OAuthSettings.from_env()
        if args.command == "start":
            print(create_authorization(settings))
        else:
            serve(settings)
        return 0
    except (OSError, ValueError) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
