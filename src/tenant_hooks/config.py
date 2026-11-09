from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOOKS_")

    database_url: str
    secret_key: str
    signature_tolerance_seconds: int = 300
    max_body_bytes: int = 1_048_576


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOOKS_")

    database_url: str
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 60
    worker_batch_size: int = 1
    retry_base_seconds: float = 5.0
    retry_cap_seconds: float = 900.0
    purge_interval_seconds: float = 60.0
