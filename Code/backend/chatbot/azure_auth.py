"""Credential helper. Every Azure call uses Microsoft Entra ID: the web app's managed identity on
App Service, and your `az login` identity locally. No service keys are used."""
from __future__ import annotations

from functools import lru_cache

from .config import get_settings

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
SEARCH_SCOPE = "https://search.azure.com/.default"


@lru_cache(maxsize=1)
def get_credential():
    from azure.identity import DefaultAzureCredential

    client_id = get_settings().azure_client_id or None
    return DefaultAzureCredential(managed_identity_client_id=client_id)


def bearer_token(scope: str) -> str:
    return get_credential().get_token(scope).token
