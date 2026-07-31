"""OAuth 2.0 providers for IMAP/SMTP — Microsoft 365 and Google.

Stdlib-only. Exposes a common OAuthProvider interface used by both
server.py (authorize/callback endpoints) and mail_scanner.py (token refresh).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("sietch.oauth")

# ── Env var keys ─────────────────────────────────────────────────────────────
MICROSOFT_CLIENT_ID = os.environ.get("OAUTH_MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("OAUTH_MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT = os.environ.get("OAUTH_MICROSOFT_TENANT", "common")

GOOGLE_CLIENT_ID = os.environ.get("OAUTH_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET", "")

OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")


def _post_json(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.error("OAuth HTTP %s from %s: %s", e.code, url, err_body)
        raise
    except Exception as e:
        logger.error("OAuth request to %s failed: %s", url, e)
        raise


class OAuthProvider:
    """Base for OAuth 2.0 providers with auth code flow."""

    name: str = ""

    @classmethod
    def authorize_url(cls, state: str) -> str:
        raise NotImplementedError

    @classmethod
    def exchange_code(cls, code: str) -> dict[str, Any]:
        """Exchange authorization code for tokens. Returns {access_token, refresh_token, expires_in, email}."""
        raise NotImplementedError

    @classmethod
    def refresh_token(cls, refresh_token: str) -> dict[str, Any]:
        """Refresh an access token. Returns {access_token, expires_in}."""
        raise NotImplementedError

    @classmethod
    def imap_settings(cls) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def smtp_settings(cls) -> dict[str, Any]:
        raise NotImplementedError


class MicrosoftProvider(OAuthProvider):
    name = "microsoft"

    _AUTHORIZE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
    _TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    _SCOPE = "offline_access openid email https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send"

    @classmethod
    def _tenant(cls) -> str:
        return MICROSOFT_TENANT or "common"

    @classmethod
    def authorize_url(cls, state: str) -> str:
        params = urllib.parse.urlencode({
            "client_id": MICROSOFT_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": cls._SCOPE,
            "state": state,
            "response_mode": "query",
        })
        url = cls._AUTHORIZE.format(tenant=cls._tenant())
        return f"{url}?{params}"

    @classmethod
    def exchange_code(cls, code: str) -> dict[str, Any]:
        data = {
            "client_id": MICROSOFT_CLIENT_ID,
            "client_secret": MICROSOFT_CLIENT_SECRET,
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
        result = _post_json(cls._TOKEN.format(tenant=cls._tenant()), data)
        email = ""
        # Try id_token first
        id_token = result.get("id_token", "")
        if id_token and isinstance(id_token, str):
            parts = id_token.split(".")
            if len(parts) == 3:
                try:
                    import base64
                    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(padded))
                    # Prefer the verified email claim; MSA id_tokens use preferred_username like "live.com#user@outlook.com"
                    email = payload.get("email") or payload.get("preferred_username") or ""
                    if email and "#" in email:
                        email = email.split("#")[-1]
                except Exception:
                    pass
        # Fallback: fetch email from Microsoft Graph API
        if not email and result.get("access_token"):
            try:
                req = urllib.request.Request(
                    "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName",
                    headers={"Authorization": f"Bearer {result['access_token']}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    me = json.loads(resp.read())
                    email = me.get("mail") or me.get("userPrincipalName") or ""
            except Exception:
                pass
        return {
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", ""),
            "expires_in": result.get("expires_in", 3600),
            "email": email,
        }

    @classmethod
    def refresh_token(cls, refresh_token: str) -> dict[str, Any]:
        data = {
            "client_id": MICROSOFT_CLIENT_ID,
            "client_secret": MICROSOFT_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": cls._SCOPE,
        }
        result = _post_json(cls._TOKEN.format(tenant=cls._tenant()), data)
        return {
            "access_token": result.get("access_token", ""),
            "expires_in": result.get("expires_in", 3600),
            "refresh_token": result.get("refresh_token", refresh_token),
        }

    @classmethod
    def imap_settings(cls) -> dict[str, Any]:
        return {"host": "outlook.office365.com", "port": 993}

    @classmethod
    def smtp_settings(cls) -> dict[str, Any]:
        return {"host": "smtp.office365.com", "port": 587}


class GoogleProvider(OAuthProvider):
    name = "google"

    _AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
    _TOKEN = "https://oauth2.googleapis.com/token"
    _SCOPE = "https://mail.google.com/"

    @classmethod
    def authorize_url(cls, state: str) -> str:
        params = urllib.parse.urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": cls._SCOPE,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        })
        return f"{cls._AUTHORIZE}?{params}"

    @classmethod
    def exchange_code(cls, code: str) -> dict[str, Any]:
        data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
        result = _post_json(cls._TOKEN, data)
        email = result.get("id_token", "")
        if email and isinstance(email, str):
            parts = email.split(".")
            if len(parts) == 3:
                try:
                    import base64
                    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(padded))
                    email = payload.get("email") or ""
                except Exception:
                    pass
            else:
                email = ""
        return {
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", ""),
            "expires_in": result.get("expires_in", 3600),
            "email": email,
        }

    @classmethod
    def refresh_token(cls, refresh_token: str) -> dict[str, Any]:
        data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        result = _post_json(cls._TOKEN, data)
        return {
            "access_token": result.get("access_token", ""),
            "expires_in": result.get("expires_in", 3600),
            "refresh_token": result.get("refresh_token", refresh_token),
        }

    @classmethod
    def imap_settings(cls) -> dict[str, Any]:
        return {"host": "imap.gmail.com", "port": 993}

    @classmethod
    def smtp_settings(cls) -> dict[str, Any]:
        return {"host": "smtp.gmail.com", "port": 587}


# ── Resolver ──────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, type[OAuthProvider]] = {
    "microsoft": MicrosoftProvider,
    "google": GoogleProvider,
}


def get_provider(name: str) -> type[OAuthProvider] | None:
    return PROVIDERS.get(name)


def generate_state() -> str:
    return secrets.token_urlsafe(32)
