from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


class Settings(BaseSettings):
    APP_NAME: str = "GameHub"
    SECRET_KEY: str
    DATABASE_URL: str
    REDIS_URL: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080
    COOKIE_SECURE: bool = False

    ADMIN_EMAIL: str = "admin@gamehub.local"
    ADMIN_PASSWORD: str = ""

    MODERATOR_EMAIL: str = "moderator@gamehub.local"
    MODERATOR_PASSWORD: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
