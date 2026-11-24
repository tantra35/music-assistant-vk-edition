"""
VK ID OAuth2 token receiver.

The legacy oauth.vk.com password-grant tokens (TokenReceiver) are served
by VK as a legacy surface: the catalog method family (catalog.getAudio,
catalog.getSection, catalog.getBlockItems) answers them with
'Access denied: token required', phone validation for official-surface
sessions is disabled and MAX OTP is rejected there.

The only way to get a catalog-capable token is the VK ID OAuth2 flow that
the VK Music Android app itself uses (reversed from com.uma.musicvk):
client_id 51778446 (+ client_secret from the app manifest), PKCE,
authorize on oauth.vk.ru/authorize, token exchange on oauth.vk.ru/token,
redirect_uri https://api.vk.ru/blank.html, v=5.193, API host api.vk.ru.

Usage:
    >>> receiver = VkIdReceiver()
    >>> if receiver.auth():            # opens an automated browser window
    ...     receiver.save_to_config()  # caches the token
    >>> service = Service.parse_config()
"""

import base64
import configparser
import hashlib
import json
import os
import secrets
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs, unquote

from curl_cffi import requests as curl_requests

VK_ID_CLIENT_ID = "51778446"
VK_CLIENT_SECRET = "0MXTzxxS9D8rsEhBulsq"  # com.vk.sdk.CLIENT_SECRET (APK manifest)
VK_ID_AUTHORIZE_URL = "https://oauth.vk.ru/authorize"
VK_ID_TOKEN_URL = "https://oauth.vk.ru/token"
VK_ID_REDIRECT = "vk51778446://vk.ru/blank.html"
VK_API_VERSION = "5.193"
VK_MUSIC_USER_AGENT = "VKMusicAndroidApp"

_TOKEN_EXCHANGE_FALLBACKS = [
    "https://id.vk.ru/oauth2/auth",
    "https://oauth.vk.ru/oauth2/auth",
    "https://api.vk.ru/oauth2/auth",
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_pkce() -> Tuple[str, str]:
    """Returns (code_verifier, code_challenge) — PKCE S256."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class VkIdReceiver:
    """Interactive VK ID token receiver for the VK Music client surface."""

    def __init__(
        self,
        client_id: str = VK_ID_CLIENT_ID,
        client_secret: str = VK_CLIENT_SECRET,
        filename: str = "config_vk.ini",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.filename = filename
        self._token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self.user_agent = VK_MUSIC_USER_AGENT

    # -- interactive browser flow -------------------------------------------

    def _auth_selenium_grab(self, auth_url: str) -> dict:
        """Open the login page in an automated Chrome and return the redirect
        parts as soon as VK navigates to blank.html."""
        import time

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )
        try:
            driver.get(auth_url)
            deadline = time.time() + 600
            while time.time() < deadline:
                url = driver.current_url
                if "blank.html" in url and ("payload=" in url or "code=" in url):
                    raw_hash = driver.execute_script("return window.location.hash")
                    raw_query = driver.execute_script("return window.location.search")
                    return {
                        "url": url,
                        "hash": raw_hash or "",
                        "query": raw_query or "",
                    }
                time.sleep(1)
            raise TimeoutError("VK did not redirect within 10 minutes")
        finally:
            driver.quit()

    def _manual_grab(self, auth_url: str) -> str:
        import webbrowser

        print("Opening VK ID login in your browser...")
        print(auth_url)
        webbrowser.open(auth_url)
        print()
        print("After login the browser will land on api.vk.ru/blank.html#payload=...")
        print("— copy the FULL address from the address bar.")
        return input("Paste the redirect URL here: ").strip()

    def auth(self, use_selenium: bool = False) -> bool:
        """Interactive login: True on success. The token is kept in memory
        (get_token/get_refresh_token); call save_to_config() to cache it.

        By default the login page opens in the user's NORMAL browser and the
        redirect URL must be pasted back (VK redirects to
        api.vk.ru/blank.html#payload=... — copy the full address).
        use_selenium=True drives an automated Chrome window instead and
        grabs the redirect automatically (requires Chrome + webdriver).
        """
        verifier, challenge = make_pkce()
        auth_url = (
            VK_ID_AUTHORIZE_URL
            + "?response_type=code"
            + "&client_id=" + self.client_id
            + "&redirect_uri=" + VK_ID_REDIRECT
            + "&scope=audio%20vkid.personal_info"
            + "&code_challenge=" + challenge
            + "&code_challenge_method=s256"
            + "&state=reversed"
        )

        redirect = None
        if use_selenium:
            try:
                print("Opening VK ID login in an automated browser window...")
                print("(log in there as usual — password, MAX code; the window closes itself)")
                redirect = self._auth_selenium_grab(auth_url)
            except Exception as e:
                print("Automated browser failed:", e)
        if redirect is None:
            redirect = {"url": self._manual_grab(auth_url), "hash": "", "query": ""}

        code, device_id, redirect_uri = self._extract_code(redirect)
        data = self._exchange(code, verifier, redirect_uri, device_id)
        self._token = str(data["access_token"])
        refresh = data.get("refresh_token")
        self._refresh_token = str(refresh) if refresh else None
        self.user_agent = VK_MUSIC_USER_AGENT
        return True

    # -- token exchange ------------------------------------------------------

    def _exchange(
        self, code: str, verifier: str, redirect_uri: Optional[str], device_id: Optional[str]
    ) -> dict:
        """POST the authorization code to oauth.vk.ru/token (with fallback
        endpoints) and return the raw response dict (must hold access_token)."""
        session = curl_requests.Session()
        from urllib.parse import urlparse

        candidates = []
        if redirect_uri:
            p = urlparse(redirect_uri)
            candidates.append(p.scheme + "://" + p.netloc + p.path)
        candidates += [VK_ID_REDIRECT, "https://api.vk.ru/blank.html", "https://vk.ru/blank.html"]
        seen = set()
        data: dict = {}
        for token_url in [VK_ID_TOKEN_URL] + _TOKEN_EXCHANGE_FALLBACKS:
            for redirect in candidates:
                if redirect in seen:
                    continue
                seen.add(redirect)
                try:
                    response = session.post(
                        token_url,
                        data=[
                            ("grant_type", "authorization_code"),
                            ("code", code),
                            ("code_verifier", verifier),
                            ("client_id", self.client_id),
                            ("client_secret", self.client_secret),
                            ("redirect_uri", redirect),
                            ("device_id", device_id or secrets.token_hex(16)),
                            ("state", "reversed"),
                        ],
                        timeout=30,
                    )
                except Exception as e:
                    print("Exchange %s [%s]: request error %s" % (token_url, redirect, e))
                    continue
                try:
                    data = response.json()
                except Exception:
                    print(
                        "Exchange %s [%s]: HTTP %s, non-JSON body"
                        % (token_url, redirect, response.status_code)
                    )
                    continue
                if data.get("access_token"):
                    print("Exchange %s [%s]: OK" % (token_url, redirect))
                    return data
                print("Exchange %s [%s]: %s" % (token_url, redirect, data))
        raise RuntimeError("Token exchange failed on all endpoints: %s" % data)

    @staticmethod
    def _extract_code(redirect):
        """Returns (code, device_id, redirect_uri) from the redirect.

        Accepts the Selenium dict {'url', 'hash', 'query'} or a full URL.
        VK lands on api.vk.ru/blank.html#payload=<urlencoded JSON> (code at
        payload['oauth']['code'], a trailing &state=... after the JSON, and
        Chrome partially decodes the fragment — see raw_decode usage).
        """
        if isinstance(redirect, dict):
            full_url = redirect.get("url") or ""
            raw_hash = redirect.get("hash") or ""
            raw_query = redirect.get("query") or ""
            url_for_dump = full_url or (raw_hash + raw_query)
            sources = [raw_hash, raw_query, full_url]
        else:
            url_for_dump = redirect
            parsed = urlparse(redirect)
            sources = [parsed.fragment, parsed.query, redirect]

        def try_payload(value: str, base: str):
            for cand in (value, unquote(value)):
                try:
                    doc, _ = json.JSONDecoder().raw_decode(cand)
                except Exception:
                    continue
                oauth = doc.get("oauth") or {}
                if oauth.get("code"):
                    return str(oauth["code"]), doc.get("device_id"), base
                if doc.get("code"):
                    return str(doc["code"]), doc.get("device_id"), base
            return None

        base = ""
        p = urlparse(url_for_dump if "://" in url_for_dump else ("https://x" + url_for_dump))
        base = p.scheme + "://" + p.netloc + p.path

        for source in sources:
            if not source:
                continue
            if "payload=" in source:
                got = try_payload(source.split("payload=", 1)[1], base)
                if got:
                    return got
            qs = parse_qs(source)
            payload = qs.get("payload", [None])[0]
            if payload:
                got = try_payload(payload, base)
                if got:
                    return got
            if "code" in qs:
                return qs["code"][0], qs.get("device_id", [None])[0], base
        raise RuntimeError(
            "No 'code' in the redirect. Full redirect: " + url_for_dump[:300]
        )

    # -- results / caching ----------------------------------------------------

    def get_token(self) -> Optional[str]:
        """The access token received by auth()."""
        return self._token

    def get_refresh_token(self) -> Optional[str]:
        """The refresh token, if VK returned one (often absent)."""
        return self._refresh_token

    def save_to_config(self, filename: Optional[str] = None) -> None:
        """Cache the token in config_vk.ini format readable by
        Service.parse_config(). Filename is resolved relative to the
        package directory (same behaviour as TokenReceiver.save_to_config).
        """
        if not self._token:
            raise RuntimeError("No token: call auth() first")
        filename = filename or self.filename
        if os.path.dirname(filename):
            full_fp = filename
        else:
            full_fp = os.path.join(os.path.dirname(__file__), filename)
        config = configparser.ConfigParser()
        config["VK"] = {
            "user_agent": self.user_agent,
            "token_for_audio": self._token,
        }
        if self._refresh_token:
            config["VK"]["refresh_token"] = self._refresh_token
        os.makedirs(os.path.dirname(full_fp), exist_ok=True)
        with open(full_fp, "w", encoding="utf-8") as f:
            config.write(f)
        print("Token was saved: " + full_fp)
