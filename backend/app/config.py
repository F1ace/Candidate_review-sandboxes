from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration."""

    database_url: str = Field(default="postgresql+psycopg://postgres:postgres@localhost:5432/reviewer", env="DATABASE_URL")
    lm_studio_url: str = Field(default="http://localhost:1234/v1/chat/completions", env="LM_STUDIO_URL")
    lm_model: str = Field(default="openai/gpt-oss-20b", env="LM_MODEL")
    lm_embedding_model: str = Field(
        default="text-embedding-jina-embeddings-v5-text-small-retrieval",
        env="LM_EMBEDDING_MODEL",
    )
    lm_api_key: str = Field(default="lm-studio", env="LM_API_KEY")
    sandbox_code_url: str = Field(default="http://localhost:8001/run_code", env="SANDBOX_CODE_URL")
    sandbox_sql_url: str = Field(default="http://localhost:8002/run_sql", env="SANDBOX_SQL_URL")
    allow_origins: str = Field(default="*")  # comma-separated origins

    @property
    def lm_studio_api_base(self) -> str:
        url = (self.lm_studio_url or "").rstrip("/")
        suffixes = (
            "/chat/completions",
            "/completions",
            "/responses",
            "/embeddings",
        )
        for suffix in suffixes:
            if url.endswith(suffix):
                return url[: -len(suffix)]
        return url

    class Config:
        env_file = ".env"


settings = Settings()
