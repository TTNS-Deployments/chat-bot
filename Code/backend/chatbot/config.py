"""Settings, read from environment variables.

App Service gets them from the ARM template (app settings). Locally, copy .env.example to .env.
Nothing here is a secret except UPLOAD_API_KEY, which App Service resolves from Key Vault.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


# The deployed package keeps the repository layout (Code/backend/chatbot, Code/frontend, Data/).
REPO_ROOT = PACKAGE_DIR.parents[2]
FRONTEND_DIR = PACKAGE_DIR.parents[1] / "frontend"
SAMPLE_DOCS_DIR = REPO_ROOT / "Data" / "sample_docs"

ALLOWED_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".html", ".htm", ".csv", ".json")


def _secret(value: str | None) -> str:
    """A secret app setting. App Service passes an unresolved Key Vault reference through as the
    literal text "@Microsoft.KeyVault(...)"; treat that as not set, never as the secret."""
    value = (value or "").strip()
    return "" if value.startswith("@Microsoft.KeyVault(") else value


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _float(value: str | None, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    mode: str = "local"  # local | azure

    openai_endpoint: str = ""
    openai_api_version: str = "2025-04-01-preview"
    chat_deployment: str = "chat"
    chat_reasoning_effort: str = "low"  # minimal | low | medium | high, or "none" for non-reasoning models
    chat_temperature: float | None = None  # only sent to non-reasoning models
    chat_max_tokens: int = 2000  # reasoning tokens count towards this limit
    summary_max_tokens: int = 150  # answer tokens for a short summary
    embedding_endpoint: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_deployment: str = "embedding"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    search_endpoint: str = ""
    search_api_key: str = field(default="", repr=False)
    search_datasource_connection: str = field(default="", repr=False)
    search_index: str = "documents-index"
    search_semantic_config: str = ""
    search_api_version: str = "2024-07-01"
    search_top_k: int = 5

    storage_account_url: str = ""
    storage_container: str = "documents"
    storage_resource_id: str = ""

    allow_document_upload: bool = True
    upload_api_key: str = field(default="", repr=False)  # only for deleting documents through the API
    max_upload_mb: int = 16

    auth_mode: str = "none"  # none | password
    demo_password: str = field(default="", repr=False)

    max_history_turns: int = 6
    max_source_chars: int = 12000
    rate_limit_per_minute: int = 20
    trust_forwarded_for: bool = False  # True behind App Service (WEBSITE_SITE_NAME is set) or a proxy you trust
    allow_sensitive_reveal: bool = True  # False: personal information stays hidden even when asked for
    chat_history_dir: Path = REPO_ROOT / ".localdata" / "conversations"
    chat_history_days: int = 7
    azure_client_id: str = ""
    local_data_dir: Path = REPO_ROOT / ".localdata"

    @property
    def is_azure(self) -> bool:
        return self.mode == "azure"

    @property
    def embedding_resource_url(self) -> str:
        return self.embedding_endpoint or self.openai_endpoint

    @property
    def search_datasource(self) -> str:
        return f"{self.search_index}-datasource"

    @property
    def search_skillset(self) -> str:
        return f"{self.search_index}-skillset"

    @property
    def search_indexer(self) -> str:
        return f"{self.search_index}-indexer"

    def missing_azure_settings(self) -> list[str]:
        required = {
            "AZURE_OPENAI_ENDPOINT": self.openai_endpoint,
            "AZURE_SEARCH_ENDPOINT": self.search_endpoint,
            "AZURE_STORAGE_ACCOUNT_URL": self.storage_account_url,
            "AZURE_STORAGE_RESOURCE_ID": self.storage_resource_id,
        }
        return [name for name, value in required.items() if not value]


def settings_from_env(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    local_data_dir = Path(e.get("LOCAL_DATA_DIR") or Settings.local_data_dir)
    mode = (e.get("CHATBOT_MODE") or ("azure" if e.get("AZURE_OPENAI_ENDPOINT") else "local")).lower()
    return Settings(
        mode=mode if mode in ("local", "azure") else "local",
        openai_endpoint=e.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/"),
        openai_api_version=e.get("AZURE_OPENAI_API_VERSION") or Settings.openai_api_version,
        chat_deployment=e.get("AZURE_OPENAI_CHAT_DEPLOYMENT") or Settings.chat_deployment,
        chat_reasoning_effort=(e.get("CHAT_REASONING_EFFORT") or Settings.chat_reasoning_effort).lower(),
        chat_temperature=_float(e.get("CHAT_TEMPERATURE"), Settings.chat_temperature),
        chat_max_tokens=_int(e.get("CHAT_MAX_TOKENS"), Settings.chat_max_tokens),
        summary_max_tokens=max(50, _int(e.get("SUMMARY_MAX_TOKENS"), Settings.summary_max_tokens)),
        embedding_endpoint=e.get("AZURE_OPENAI_EMBEDDING_ENDPOINT", "").rstrip("/"),
        embedding_api_key=_secret(e.get("AZURE_OPENAI_EMBEDDING_KEY")),
        embedding_deployment=e.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT") or Settings.embedding_deployment,
        embedding_model=e.get("AZURE_OPENAI_EMBEDDING_MODEL") or Settings.embedding_model,
        embedding_dimensions=_int(e.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS"), Settings.embedding_dimensions),
        search_endpoint=e.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/"),
        search_api_key=_secret(e.get("AZURE_SEARCH_API_KEY")),
        search_datasource_connection=_secret(e.get("AZURE_SEARCH_DATASOURCE_CONNECTION")),
        search_index=e.get("AZURE_SEARCH_INDEX") or Settings.search_index,
        search_semantic_config=e.get("AZURE_SEARCH_SEMANTIC_CONFIG", ""),
        search_api_version=e.get("AZURE_SEARCH_API_VERSION") or Settings.search_api_version,
        search_top_k=max(1, min(_int(e.get("SEARCH_TOP_K"), Settings.search_top_k), 20)),
        storage_account_url=e.get("AZURE_STORAGE_ACCOUNT_URL", ""),
        storage_container=e.get("AZURE_STORAGE_CONTAINER") or Settings.storage_container,
        storage_resource_id=e.get("AZURE_STORAGE_RESOURCE_ID", ""),
        allow_document_upload=_bool(e.get("ALLOW_DOCUMENT_UPLOAD"), True),
        upload_api_key=e.get("UPLOAD_API_KEY", ""),
        auth_mode=(e.get("AUTH_MODE") or "none").lower(),
        demo_password=_secret(e.get("DEMO_PASSWORD")),
        max_upload_mb=_int(e.get("MAX_UPLOAD_MB"), Settings.max_upload_mb),
        max_history_turns=_int(e.get("MAX_HISTORY_TURNS"), Settings.max_history_turns),
        max_source_chars=_int(e.get("MAX_SOURCE_CHARS"), Settings.max_source_chars),
        rate_limit_per_minute=_int(e.get("RATE_LIMIT_PER_MINUTE"), Settings.rate_limit_per_minute),
        trust_forwarded_for=_bool(e.get("TRUST_FORWARDED_FOR"), bool(e.get("WEBSITE_SITE_NAME"))),
        azure_client_id=e.get("AZURE_CLIENT_ID", ""),
        local_data_dir=local_data_dir,
        allow_sensitive_reveal=_bool(e.get("ALLOW_SENSITIVE_REVEAL"), True),
        # App Service shares /home between workers and instances; wwwroot can be read-only (run from package).
        chat_history_dir=Path(e.get("CHAT_HISTORY_DIR") or ("/home/data/chatbot/conversations" if e.get("WEBSITE_SITE_NAME")
                                                           else local_data_dir / "conversations")),
        chat_history_days=max(1, _int(e.get("CHAT_HISTORY_DAYS"), Settings.chat_history_days)),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return settings_from_env()
