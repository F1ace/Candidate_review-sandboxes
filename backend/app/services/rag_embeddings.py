from __future__ import annotations

from functools import cached_property

from langchain_openai import OpenAIEmbeddings

from ..config import settings


class EmbeddingServiceError(RuntimeError):
    pass


class LMStudioEmbeddingService:
    @property
    def model_name(self) -> str:
        return settings.lm_embedding_model

    @property
    def backend_name(self) -> str:
        return "langchain_inmemory_vectorstore"

    @cached_property
    def _client(self) -> OpenAIEmbeddings:
        return OpenAIEmbeddings(
            model=settings.lm_embedding_model,
            base_url=settings.lm_studio_api_base,
            api_key=settings.lm_api_key,
            tiktoken_enabled=False,
            check_embedding_ctx_length=False,
            chunk_size=256,
            max_retries=1,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._client.embed_documents(texts)
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingServiceError(
                "LM Studio embeddings request failed while indexing document chunks. "
                f"Model={settings.lm_embedding_model!r}, base_url={settings.lm_studio_api_base!r}. "
                f"Load the embedding model in LM Studio and retry. Original error: {exc}"
            ) from exc

    def embed_query(self, text: str) -> list[float]:
        try:
            return self._client.embed_query(text)
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingServiceError(
                "LM Studio embeddings request failed while embedding the retrieval query. "
                f"Model={settings.lm_embedding_model!r}, base_url={settings.lm_studio_api_base!r}. "
                f"Load the embedding model in LM Studio and retry. Original error: {exc}"
            ) from exc


embedding_service = LMStudioEmbeddingService()
