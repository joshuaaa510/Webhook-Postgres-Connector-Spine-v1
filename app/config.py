from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Database
    database_url: str = "postgresql://postgres:postgres@db:5432/webhook_connector"
    
    # Redis (for Arq background jobs)
    redis_url: str = "redis://redis:6379"
    
    # Processing
    max_retry_attempts: int = 5
    initial_retry_delay: int = 1  # seconds
    max_retry_delay: int = 60  # seconds
    
    # Mock API
    mock_api_url: str = "http://api:8001"
    mock_api_failure_rate: float = 0.5  # 50% chance of failure for testing
    
    # Logging
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """Cached settings instance"""
    return Settings()
