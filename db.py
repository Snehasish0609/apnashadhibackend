import os
import ssl
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

# Load .env.local ONLY for local development
if os.getenv("ENV") != "production":
    from dotenv import load_dotenv
    load_dotenv(".env.local")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# ── asyncpg does NOT support sslmode / channel_binding as URL query params.
# Strip them and pass ssl via connect_args instead.
def _clean_asyncpg_url(url: str) -> tuple[str, dict]:
    """Remove psycopg2-style SSL params from URL; return cleaned URL + connect_args."""
    # Ensure the scheme is postgresql+asyncpg
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    # Params that asyncpg cannot handle in the URL
    _STRIP = {"sslmode", "channel_binding", "sslrootcert", "sslcert", "sslkey"}
    needs_ssl = "sslmode" in params and params["sslmode"][0] != "disable"

    cleaned_params = {k: v for k, v in params.items() if k not in _STRIP}
    cleaned_query = urlencode(cleaned_params, doseq=True)
    cleaned_url = urlunparse(parsed._replace(query=cleaned_query))

    connect_args = {}
    if needs_ssl:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ssl_ctx

    return cleaned_url, connect_args


_clean_url, _connect_args = _clean_asyncpg_url(DATABASE_URL)

engine = create_async_engine(
    _clean_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()
