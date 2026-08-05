"""Zero-Touch Automated Zerodha Access Token Generator.

Automates 2FA TOTP login to Zerodha Kite API and generates access_token.
Can run standalone or as an automated pre-market cron job.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def generate_access_token_auto(
    user_id: str,
    password: str,
    totp_seed: str,
    api_key: str,
    api_secret: str,
) -> str:
    """Automate 2FA TOTP login to Zerodha and exchange request_token for access_token."""
    import pyotp
    import requests
    from kiteconnect import KiteConnect

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
    )

    # 1. Login with User ID + Password
    login_resp = sess.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": user_id, "password": password},
        timeout=10,
    )
    res_data = login_resp.json()
    if res_data.get("status") != "success":
        msg = res_data.get("message", "Login failed")
        raise RuntimeError(f"Zerodha login failed: {msg}")

    request_id = res_data["data"]["request_id"]

    # 2. Complete 2FA with TOTP
    totp_code = pyotp.TOTP(totp_seed.replace(" ", "")).now()
    twofa_resp = sess.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
        },
        timeout=10,
    )
    twofa_data = twofa_resp.json()
    if twofa_data.get("status") != "success":
        msg = twofa_data.get("message", "2FA failed")
        raise RuntimeError(f"Zerodha 2FA failed: {msg}")

    # 3. Authorize Kite Connect App
    connect_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    auth_resp = sess.get(connect_url, allow_redirects=False, timeout=10)

    location = auth_resp.headers.get("Location", "")
    if "request_token=" not in location:
        # Retry with redirect follow if location header wasn't set immediately
        auth_resp = sess.get(connect_url, timeout=10)
        location = auth_resp.url

    parsed = urllib.parse.urlparse(location)
    query_params = urllib.parse.parse_qs(parsed.query)
    req_tokens = query_params.get("request_token")

    if not req_tokens:
        raise RuntimeError(f"Could not extract request_token from redirect URL: {location}")

    request_token = req_tokens[0]

    # 4. Exchange request_token for Zerodha access_token
    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    return str(session_data["access_token"])


def main() -> None:
    api_key = os.getenv("ZERODHA_API_KEY", "")
    api_secret = os.getenv("ZERODHA_API_SECRET", "")
    user_id = os.getenv("ZERODHA_USER_ID", "")
    password = os.getenv("ZERODHA_PASSWORD", "")
    totp_seed = os.getenv("ZERODHA_TOTP_SEED", "")

    # Fallback to CLI args if provided
    if len(sys.argv) >= 4:
        api_key = sys.argv[1].strip()
        api_secret = sys.argv[2].strip()
        request_token = sys.argv[3].strip()
        from kiteconnect import KiteConnect

        try:
            kite = KiteConnect(api_key=api_key)
            data = kite.generate_session(request_token, api_secret=api_secret)
            token = data["access_token"]
            print(f"\nZERODHA_ACCESS_TOKEN={token}\n")
            return
        except Exception as exc:
            print(f"Error: {exc}")
            sys.exit(1)

    if not (user_id and password and totp_seed and api_key and api_secret):
        print("\n=======================================================")
        print("ZERO-TOUCH AUTO-LOGIN USAGE:")
        print("Set these in your .env file:")
        print("  ZERODHA_USER_ID=your_client_id")
        print("  ZERODHA_PASSWORD=your_password")
        print("  ZERODHA_TOTP_SEED=your_totp_secret_key")
        print("  ZERODHA_API_KEY=your_api_key")
        print("  ZERODHA_API_SECRET=your_api_secret")
        print("\nOr run manual exchange:")
        print("  python scripts/get_zerodha_token.py <api_key> <api_secret> <request_token>")
        print("=======================================================\n")
        sys.exit(1)

    try:
        print(f"Automating Zerodha 2FA login for user {user_id}...")
        token = generate_access_token_auto(user_id, password, totp_seed, api_key, api_secret)
        print("\n==========================================")
        print("SUCCESS! Generated Zerodha Access Token:")
        print(f"\nZERODHA_ACCESS_TOKEN={token}\n")
        print("==========================================\n")
    except Exception as exc:
        print(f"Auto-login failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
