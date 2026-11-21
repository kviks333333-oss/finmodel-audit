"""Provider boundary for the audit harness, independent of a particular SDK."""

from __future__ import annotations

import inspect
from typing import Any, Literal, Protocol

from .gigachat import GigaChatClient, GigaChatError, _reasoning_options, _session_headers, _without_reasoning_content, load_env_file


class ChatClient(Protocol):
    model: str

    def chat(self, messages: list[dict], functions=None, function_call=None, **kwargs) -> dict:
        ...


class ProviderError(RuntimeError):
    """Safe provider diagnostic with no raw responses, request data, or secrets."""


class ProviderAdapter:
    """Adapt the minimal chat protocol; optional generation settings are detected."""

    def __init__(self, client: ChatClient) -> None:
        self.client = client
        self.name = str(getattr(client, "provider_name", type(client).__name__))
        self.model = str(getattr(client, "model", "unspecified"))
        self.model_version = getattr(client, "model_version", None)
        if not self.model_version and ":" in self.model:
            self.model_version = self.model
        try:
            parameters = inspect.signature(client.chat).parameters
        except (TypeError, ValueError):
            parameters = {}
        self._keyword_args = any(p.kind == p.VAR_KEYWORD for p in parameters.values())
        self._parameters = parameters

    def identity(self, version: str) -> dict[str, str]:
        return {"provider": self.name, "model": self.model, "version": version}

    def chat(
        self, messages: list[dict[str, Any]], *, functions=None, function_call=None,
        max_tokens: int = 2500, temperature: float = 0.1,
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
        reasoning_max_tokens: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        kwargs = {"functions": functions, "function_call": function_call}
        for name, value in (("max_tokens", max_tokens), ("temperature", temperature)):
            if self._keyword_args or name in self._parameters:
                kwargs[name] = value
        try:
            reasoning = _reasoning_options(reasoning_effort, reasoning_max_tokens)
            _session_headers(session_id)
        except GigaChatError as exc:
            raise ProviderError(str(exc)) from None
        for name, value in reasoning.items():
            parameter = self._parameters.get(name)
            if not self._keyword_args and (parameter is None or parameter.kind == parameter.POSITIONAL_ONLY):
                raise ProviderError(f"The selected provider does not support {name}.")
            kwargs[name] = value
        # Session caching is an optional optimization. Providers with the
        # minimal chat protocol remain usable without cache-specific options.
        parameter = self._parameters.get("session_id")
        if session_id is not None and (self._keyword_args or (
            parameter is not None and parameter.kind != parameter.POSITIONAL_ONLY
        )):
            kwargs["session_id"] = session_id
        try:
            result = self.client.chat(messages, **kwargs)
        except GigaChatError as exc:
            raise ProviderError(str(exc)) from None
        except Exception:
            raise ProviderError("The language-model request failed.") from None
        if not isinstance(result, dict):
            raise ProviderError("The language model returned an invalid response structure.")
        return _without_reasoning_content(result)


def get_provider(client: ChatClient | None = None) -> ProviderAdapter:
    if client is None:
        load_env_file()
        client = GigaChatClient()
    return ProviderAdapter(client)
