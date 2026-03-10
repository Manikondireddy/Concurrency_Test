import os


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_csv(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:1234@localhost:5432/data_wallet"
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    if IS_PRODUCTION:
        raise RuntimeError("DATABASE_URL is required in production.")
    DATABASE_URL = DEFAULT_DATABASE_URL

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-only-insecure-secret-change-me-32+")
if IS_PRODUCTION and (not JWT_SECRET_KEY or JWT_SECRET_KEY == "dev-only-insecure-secret-change-me-32+"):
    raise RuntimeError("JWT_SECRET_KEY must be set in production.")
if len(JWT_SECRET_KEY) < 32:
    raise RuntimeError("JWT_SECRET_KEY must be at least 32 characters.")

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = _get_int("JWT_EXPIRE_MINUTES", 60)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
APP_NAME = os.getenv("APP_NAME", "wallet-ledger-api")
SQL_ECHO = _get_bool("SQL_ECHO", False)

DB_POOL_SIZE = _get_int("DB_POOL_SIZE", 10)
DB_MAX_OVERFLOW = _get_int("DB_MAX_OVERFLOW", 20)
DB_POOL_RECYCLE = _get_int("DB_POOL_RECYCLE", 1800)

# Keep false in production; enable only in local/dev setups.
AUTO_CREATE_TABLES = _get_bool("AUTO_CREATE_TABLES", not IS_PRODUCTION)

CORS_ALLOW_ORIGINS = _get_csv("CORS_ALLOW_ORIGINS")
ALLOWED_HOSTS = _get_csv("ALLOWED_HOSTS")
