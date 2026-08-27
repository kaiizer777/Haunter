from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic_settings import BaseSettings, SettingsConfigDict


def _to_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    parsed = urlparse(url)
    if parsed.query:
        qs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in qs if k not in ("sslmode", "channel_binding")]
        parsed = parsed._replace(query=urlencode(filtered))
        url = urlunparse(parsed)
    return url


class Settings(BaseSettings):
    database_url: str
    database_url_unpooled: str
    better_auth_secret: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def async_database_url(self) -> str:
        return _to_asyncpg_url(self.database_url)

    @property
    def async_database_url_unpooled(self) -> str:
        return _to_asyncpg_url(self.database_url_unpooled)


settings = Settings()
