from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Slack Leave Bot"
    app_env: str = "development"
    admin_api_key: str = ""
    database_url: str = "sqlite:///./leavebot.db"
    leave_policy_path: str = "config/leave_policy.json"
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    groq_api_key: str = ""
    groq_model: str = "qwen/qwen3.6-27b"
    agentspan_server_url: str = ""

    job_worker_enabled: bool = True
    job_poll_interval_seconds: float = 0.5
    job_lock_timeout_seconds: int = 300
    job_max_attempts: int = 8
    db_pool_size: int = 5
    db_max_overflow: int = 5

    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    s3_bucket_name: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
