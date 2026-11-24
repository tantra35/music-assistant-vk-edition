"""VK ID OAuth2 authorization for the Music Assistant setup flow.

Legacy oauth.vk.com password-grant tokens (the previous interactive flow)
are served by VK as a legacy surface: the catalog method family
(catalog.getAudio / getSection / getBlockItems) answers them with
'Access denied'. The only catalog-capable token comes from the VK ID
OAuth2 flow used by the VK Music Android app itself (reversed from
com.uma.musicvk): PKCE + authorization code on oauth.vk.ru, token
exchange on oauth.vk.ru/token, v=5.193, API host api.vk.ru.

Music Assistant hosts have no browser, so the setup flow shows the login
URL for the user's own browser and asks them to paste the final redirect
address back (api.vk.ru/blank.html#payload=...).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

VK_ID_CLIENT_ID = "51778446"
VK_CLIENT_SECRET = "0MXTzxxS9D8rsEhBulsq"  # com.vk.sdk.CLIENT_SECRET (APK manifest)
VK_ID_AUTHORIZE_URL = "https://oauth.vk.ru/authorize"
VK_APP_REDIRECT = "vk51778446://vk.ru/blank.html"
VK_SCOPE = "audio%20vkid.personal_info"
VK_OAUTH_STATE = "reversed"

# VK served the exchange from several endpoints over time; try them all
# with every plausible redirect_uri, as the app's own clients do.
_TOKEN_URLS = (
    "https://oauth.vk.ru/token",
    "https://id.vk.ru/oauth2/auth",
    "https://oauth.vk.ru/oauth2/auth",
    "https://api.vk.ru/oauth2/auth",
)
_REDIRECT_CANDIDATES = (
    VK_APP_REDIRECT,
    "https://api.vk.ru/blank.html",
    "https://vk.ru/blank.html",
)


class VKAuthError(Exception):
    """A terminal VK authorization error (message is user-facing)."""


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) — PKCE S256."""
    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def build_authorize_url(code_challenge: str) -> str:
    """Build the VK ID login page URL the user opens in a normal browser."""
    query = "&".join(
        (
            "response_type=code",
            f"client_id={VK_ID_CLIENT_ID}",
            f"redirect_uri={VK_APP_REDIRECT}",
            f"scope={VK_SCOPE}",
            f"code_challenge={code_challenge}",
            "code_challenge_method=s256",
            f"state={VK_OAUTH_STATE}",
        )
    )
    return f"{VK_ID_AUTHORIZE_URL}?{query}"


def extract_auth_code(redirect_text: str) -> tuple[str, str | None, str]:
    """Parse the pasted redirect address into (code, device_id, redirect_base).

    VK lands the browser on api.vk.ru/blank.html#payload=<urlencoded JSON>
    with the code at payload['oauth']['code'] and a trailing &state=... after
    the JSON; Chrome partially decodes the fragment, so the payload substring
    must be taken whole (raw_decode) and never re-parsed with parse_qs first.
    A plain ?code=... query shape is accepted as well.
    redirect_base is the landing URL without query/fragment — VK expects it
    as redirect_uri during the exchange.
    """
    text = (redirect_text or "").strip().strip('"')
    if not text:
        raise VKAuthError("Вставьте адрес из адресной строки браузера после входа.")

    parsed = urlparse(text if "://" in text else f"https://x{text}")
    redirect_base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    sources = (parsed.fragment, parsed.query, text)

    def code_from_payload(value: str) -> tuple[str, str | None] | None:
        for candidate in (value, unquote(value)):
            try:
                doc, _ = json.JSONDecoder().raw_decode(candidate)
            except ValueError:
                continue
            if not isinstance(doc, dict):
                continue
            oauth = doc.get("oauth") if isinstance(doc.get("oauth"), dict) else doc
            code = oauth.get("code") if isinstance(oauth, dict) else None
            if code:
                return str(code), doc.get("device_id")
        return None

    for source in sources:
        if not source:
            continue
        if "payload=" in source:
            found = code_from_payload(source.split("payload=", 1)[1])
            if found:
                return found[0], found[1], redirect_base
        pairs = parse_qs(source)
        payload = pairs.get("payload", [None])[0]
        if payload:
            found = code_from_payload(payload)
            if found:
                return found[0], found[1], redirect_base
        if pairs.get("code"):
            device_id = (pairs.get("device_id") or [None])[0]
            return pairs["code"][0], device_id, redirect_base

    raise VKAuthError(
        "В адресе нет кода авторизации. Скопируйте ПОЛНЫЙ адрес из адресной "
        "строки браузера сразу после входа (api.vk.ru/blank.html#payload=...)."
    )


async def exchange_auth_code(
    code: str,
    code_verifier: str,
    device_id: str | None,
    redirect_base: str | None,
) -> dict[str, Any]:
    """Exchange the authorization code for an access token.

    Tries oauth.vk.ru/token first and then the alternate endpoints VK has
    used, each with the observed redirect base plus the canonical app
    redirects. Returns the raw response dict (must hold access_token).
    """
    candidates: list[str] = []
    for candidate in (redirect_base, *_REDIRECT_CANDIDATES):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    base_form = [
        ("grant_type", "authorization_code"),
        ("code", code),
        ("code_verifier", code_verifier),
        ("client_id", VK_ID_CLIENT_ID),
        ("client_secret", VK_CLIENT_SECRET),
        ("device_id", device_id or secrets.token_hex(16)),
        ("state", VK_OAUTH_STATE),
    ]

    last_error = "кончились варианты endpoint'ов обмена"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession() as http:
        for token_url in _TOKEN_URLS:
            for redirect in candidates:
                try:
                    async with http.post(
                        token_url,
                        data=base_form + [("redirect_uri", redirect)],
                        timeout=timeout,
                    ) as resp:
                        try:
                            data = await resp.json(content_type=None)
                        except (ValueError, aiohttp.ClientError):
                            last_error = f"{token_url}: HTTP {resp.status}, не JSON"
                            continue
                except aiohttp.ClientError as err:
                    last_error = f"{token_url}: {type(err).__name__}"
                    continue
                if isinstance(data, dict) and data.get("access_token"):
                    return data
                if isinstance(data, dict):
                    last_error = str(
                        data.get("error_description") or data.get("error") or data
                    )
    raise VKAuthError(f"VK не выпустил токен: {last_error}")
