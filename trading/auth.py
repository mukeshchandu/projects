#auth.py
from __future__ import annotations

import os

from dotenv import load_dotenv


def get_session() -> tuple[str, str]:
    """Returns (user_id, session_token). Raises if token not set."""
    load_dotenv()
    user_id = os.getenv("FLATTRADE_USER_ID", "")
    token   = os.getenv("FLATTRADE_SESSION_TOKEN", "")
    if not user_id or not token:
        raise RuntimeError(
            "No session token found in .env.\n"
            "Run: python generate_token.py"
        )
    return user_id, token