import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Neo4j settings
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # LLM & Embedding settings
    google_api_key: str = ""
    llm_model: str = "gemini-flash-lite-latest"
    small_llm_model: str = "gemini-flash-lite-latest"
    embedding_model: str = "gemini-embedding-001"

    # Application settings
    app_env: str = "development"
    log_level: str = "INFO"


settings = Settings()
