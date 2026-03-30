from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration."""

    database_url: str = Field(default="postgresql+psycopg://postgres:postgres@localhost:5432/reviewer", env="DATABASE_URL")
    lm_studio_url: str = Field(default="http://localhost:1234/v1/chat/completions", env="LM_STUDIO_URL")
    lm_model: str = Field(default="openai/gpt-oss-20b", env="LM_MODEL")
    sandbox_code_url: str = Field(default="http://localhost:8001/run_code", env="SANDBOX_CODE_URL")
    sandbox_sql_url: str = Field(default="http://localhost:8002/run_sql", env="SANDBOX_SQL_URL")
    web_search_url: str = Field(default="http://localhost:8003/search", env="WEB_SEARCH_URL")
    minio_endpoint: str = Field(default="localhost:9000", env="MINIO_ENDPOINT")
    minio_access_key: str = Field(default="minioadmin", env="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="minioadmin", env="MINIO_SECRET_KEY")
    minio_bucket: str = Field(default="rag-documents", env="MINIO_BUCKET")
    minio_secure: bool = Field(default=False, env="MINIO_SECURE")
    rag_chunk_size: int = Field(default=900, env="RAG_CHUNK_SIZE")
    rag_chunk_overlap: int = Field(default=150, env="RAG_CHUNK_OVERLAP")
    rag_default_top_k: int = Field(default=5, env="RAG_DEFAULT_TOP_K")
    allow_origins: str = Field(default="*")  # comma-separated origins

    class Config:
        env_file = ".env"


settings = Settings()
