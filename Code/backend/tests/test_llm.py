from types import SimpleNamespace

import httpx
import openai
import pytest

from chatbot.config import Settings
from chatbot.errors import ContentFiltered, UpstreamBusy, UpstreamError
from chatbot.llm import AzureChatModel, LocalChatModel
from chatbot.prompts import build_messages
from chatbot.retrieval import Passage

REQUEST = httpx.Request("POST", "https://aoai.openai.azure.com/openai/deployments/chat/chat/completions")


def response(content="Answer [1]", finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])


class FakeCompletions:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def model(*outcomes, **settings):
    completions = FakeCompletions(*outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return AzureChatModel(Settings(mode="azure", **settings), client=client), completions


def bad_request(message):
    return openai.BadRequestError(message, response=httpx.Response(400, request=REQUEST), body=None)


def test_reasoning_model_gets_effort_and_no_temperature():
    m, calls = model(response())
    assert m.complete([{"role": "user", "content": "hi"}]) == "Answer [1]"
    sent = calls.calls[0]
    assert sent["model"] == "chat" and sent["reasoning_effort"] == "low" and sent["max_completion_tokens"] == 2000
    assert "temperature" not in sent


def test_non_reasoning_model_gets_temperature():
    m, calls = model(response(), chat_reasoning_effort="none", chat_temperature=0.2)
    m.complete([])
    assert calls.calls[0]["temperature"] == 0.2 and "reasoning_effort" not in calls.calls[0]


def test_retries_without_temperature_when_rejected():
    m, calls = model(bad_request("Unsupported value: 'temperature' does not support 0.2"), response("ok"), response("again"),
                     chat_reasoning_effort="none", chat_temperature=0.2)
    assert m.complete([]) == "ok"
    assert "temperature" not in calls.calls[1]
    m.complete([])
    assert "temperature" not in calls.calls[2]  # remembered


def test_rate_limit_maps_to_busy():
    error = openai.RateLimitError("slow down", response=httpx.Response(429, request=REQUEST), body=None)
    with pytest.raises(UpstreamBusy):
        model(error)[0].complete([])


def test_content_filter_on_prompt_and_on_answer():
    with pytest.raises(ContentFiltered):
        model(bad_request("The response was filtered: content_filter"))[0].complete([])
    with pytest.raises(ContentFiltered):
        model(response("", finish="content_filter"))[0].complete([])


def test_other_errors_are_upstream_errors():
    with pytest.raises(UpstreamError):
        model(bad_request("context length exceeded"))[0].complete([])
    with pytest.raises(UpstreamError):
        model(openai.APIConnectionError(request=REQUEST))[0].complete([])


def test_local_model_cites_sources_and_admits_ignorance():
    ps = [Passage("1", "a.md", "# Heading\nRefunds take 5 to 7 working days. Pickup is free."), Passage("2", "b.md", "Support opens at 8 am.")]
    answer = LocalChatModel().complete(build_messages("How long do refunds take?", [], ps, 3))
    assert "Refunds take 5 to 7 working days. [1]" in answer and "# Heading" not in answer
    assert "could not find" in LocalChatModel().complete(build_messages("x", [], [], 3))


def test_credential_failure_is_an_upstream_error():
    from azure.core.exceptions import ClientAuthenticationError

    with pytest.raises(UpstreamError, match="Microsoft Entra token"):
        model(ClientAuthenticationError("DefaultAzureCredential failed"))[0].complete([])
