from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://synara:synara@localhost:5432/synara"
    duckdb_path: str = "./data/synara.duckdb"
    api_key: str = "dev-synara-key"
    api_base_url: str = "http://localhost:8000"
    worker_id: str = "worker-1"
    worker_poll_seconds: float = 1.0
    log_level: str = "INFO"
    seed: int = 42
    outbox_batch_size: int = 25
    outbox_max_attempts: int = 8
    simulation_horizon_hours: int = 48

    @property
    def duckdb_file(self) -> Path:
        return Path(self.duckdb_path).resolve()


def get_settings() -> Settings:
    return Settings()
