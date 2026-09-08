"""Configuration loaded from the environment and .env.

A malformed .env used to crash the whole application at import with an opaque
error from deep inside python-dotenv. That is a bad failure: the app never
starts, and the message says nothing about which file is wrong or how to fix
it. Loading is now guarded so a broken .env degrades to environment variables
and a clear warning instead.

The usual cause on Windows is PowerShell redirection: `echo x >> .env` writes
UTF-16, and the null bytes make python-dotenv raise "embedded null character".
Write .env as UTF-8.
"""

import logging
import os

from dotenv import load_dotenv

logger = logging.getLogger("config")


def _load_env() -> None:
    """Load .env, warning clearly rather than crashing if it is unreadable."""
    try:
        load_dotenv()
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning(
            "Could not read .env (%s). Falling back to environment variables. "
            "If the file was written with PowerShell redirection it is likely "
            "UTF-16; rewrite it as UTF-8.",
            exc,
        )


_load_env()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "database": os.getenv("DB_NAME", "epialert"),
    "user": os.getenv("DB_USER", "root"),
    # Never a default password in source. Unset means unset.
    "password": os.getenv("DB_PASSWORD", ""),
}

# Phone numbers the demo assigns to the 3,000 synthetic people.
#
# These defaults are deliberately unreachable placeholders. Real numbers belong
# in .env, which is not committed -- a real number in source is a real person
# who can be called by anyone who clones the repository.
#
# Override with a comma-separated list:
#     ALERT_PHONE_NUMBERS=+911111111111,+912222222222
_DEFAULT_PHONE_NUMBERS = "+910000000001,+910000000002,+910000000003"

ALERT_PHONE_NUMBERS = [
    n.strip()
    for n in os.getenv("ALERT_PHONE_NUMBERS", _DEFAULT_PHONE_NUMBERS).split(",")
    if n.strip()
]
