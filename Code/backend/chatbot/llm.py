"""Chat completion clients: Azure OpenAI (managed identity) and a local stand-in."""
from __future__ import annotations

import logging
import re
from typing import Protocol, Sequence

from .config import Settings
from .errors import ContentFiltered, UpstreamBusy, UpstreamError
from .prompts import NO_SOURCES_NOTE

log = logging.getLogger(__name__)


class ChatModel(Protocol):
    def complete(self, messages: Sequence[dict]) -> str: ...


class AzureChatModel:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self._reasoning = settings.chat_reasoning_effort not in ("", "none")
        self._omit_temperature = self._reasoning or settings.chat_temperature is None
        if client is None:
            from azure.identity import get_bearer_token_provider
            from openai import AzureOpenAI

            from .azure_auth import COGNITIVE_SCOPE, get_credential

            client = AzureOpenAI(
                azure_endpoint=settings.openai_endpoint,
                api_version=settings.openai_api_version,
                azure_ad_token_provider=get_bearer_token_provider(get_credential(), COGNITIVE_SCOPE),
                max_retries=2,
                timeout=60.0,
            )
        self.client = client

    def complete(self, messages: Sequence[dict]) -> str:
        import openai
        from azure.core.exceptions import ClientAuthenticationError

        kwargs = {"model": self.settings.chat_deployment, "messages": list(messages),
                  "max_completion_tokens": self.settings.chat_max_tokens}
        if self._reasoning:
            # Reasoning models (gpt-5 family) take an effort level and reject temperature.
            kwargs["reasoning_effort"] = self.settings.chat_reasoning_effort
        if not self._omit_temperature:
            kwargs["temperature"] = self.settings.chat_temperature
        try:
            try:
                response = self.client.chat.completions.create(**kwargs)
            except openai.BadRequestError as exc:
                # Reasoning models only accept the default temperature; retry once without it.
                if "temperature" not in str(exc) or "temperature" not in kwargs:
                    raise
                log.info("Chat model rejected temperature; continuing without it")
                self._omit_temperature = True
                kwargs.pop("temperature")
                response = self.client.chat.completions.create(**kwargs)
        except openai.RateLimitError as exc:
            raise UpstreamBusy("The language model is busy right now. Please try again in a moment.") from exc
        except openai.BadRequestError as exc:
            if "content_filter" in str(exc) or "content management policy" in str(exc):
                raise ContentFiltered("The question was blocked by the content filter. Please rephrase it.") from exc
            raise UpstreamError(f"The language model rejected the request: {exc}") from exc
        except openai.APIError as exc:
            raise UpstreamError(f"The language model call failed: {exc}") from exc
        except ClientAuthenticationError as exc:
            raise UpstreamError(f"Could not get a Microsoft Entra token for Azure OpenAI: {exc}") from exc

        try:
            choice = response.choices[0]
            finish_reason, content = choice.finish_reason, choice.message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise UpstreamError(f"The language model returned an unexpected response: {str(response)[:200]}") from exc
        if finish_reason == "content_filter":
            raise ContentFiltered("The answer was blocked by the content filter. Please rephrase the question.")
        return (content or "").strip()


class LocalChatModel:
    """Answers with the most relevant sentences of the top sources. For local development only."""

    def complete(self, messages: Sequence[dict]) -> str:
        prompt = messages[-1]["content"]
        if NO_SOURCES_NOTE in prompt:
            return ("I could not find this in the available documents. Try rephrasing the question, "
                    "or add a document that covers it. (Local mode: answers are extracts, not generated.)")
        sources_part, _, question = prompt.rpartition("\n\nQuestion: ")
        sources_part = sources_part.split("\n\nScope: ")[0]  # drop the scope and privacy notes
        wanted = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 3}
        lines = []
        for number, body in re.findall(r"\[(\d+)\] [^\n]*\n(.*?)(?=\n\n\[\d+\] |\Z)", sources_part, flags=re.S)[:2]:
            text = " ".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
            sentences = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
            best = max(sentences, key=lambda s: sum(w in s.lower() for w in wanted))
            lines.append(f"{best.strip()} [{number}]")
        return " ".join(lines) + "\n\n(Local mode: answers are extracts from the sources, not generated.)"
