from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Agentic RAG service."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "agentic-rag-enterprise"
    app_env: str = "development"
    log_level: str = "INFO"

    llm_provider: str = "mock"
    embedding_provider: str = "mock"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None

    max_iterations: int = 3
    max_tool_calls: int = 12
    max_retrieval_top_k: int = 8


settings = Settings()
