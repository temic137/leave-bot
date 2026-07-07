from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Slack Leave Bot"
    database_url: str = "sqlite:///./leavebot.db"
    leave_policy_path: str = "config/leave_policy.json"
    manager_mapping_csv: str = "config/manager_mapping.sample.csv"

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    openai_api_key: str = ""
    agentspan_api_key: str = ""

    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    s3_bucket_name: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

