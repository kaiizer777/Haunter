"""
Shared rate limiter instance.

Centralised here so auth.py and other routers can import @limiter.limit()
without a circular import (main.py imports auth.py, so limiter must live
in a module that neither imports from main.py).

slowapi = express-rate-limit equivalent for FastAPI.
Key function: per-IP (get_remote_address). Limits are per-decorator.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=[])
