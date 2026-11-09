from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOOKS_")

    database_url: str
    secret_key: str
    signature_tolerance_seconds: int = 300
    max_body_bytes: int = 1_048_576
