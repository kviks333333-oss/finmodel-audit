"""Small GigaChat HTTP client with verified TLS and a synthetic tool probe.

No spreadsheet data is uploaded by this module. Credentials are read from the
environment; neither HTTP bodies nor low-level exceptions enter error messages.
"""

from __future__ import annotations

import json
from http.client import HTTPException
import math
import os
from pathlib import Path
import re
import ssl
import threading
import time
from typing import Any, Literal
from urllib import error, parse, request
import uuid


API_BASE_URL = "https://api.giga.chat/v1"
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_MODEL = "GigaChat-3-Ultra"
DEFAULT_SCOPE = "GIGACHAT_API_PERS"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class GigaChatError(RuntimeError):
    """A diagnostic that never includes credentials or raw response content."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _session_headers(session_id: str | None) -> dict[str, str]:
    """Only accept short opaque identifiers; never workbook names or headers."""
    if session_id is None:
        return {}
    if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", session_id) is None:
        raise GigaChatError("session_id must be a short ASCII identifier without spaces or control characters.")
    return {"X-Session-ID": session_id}


def _reasoning_options(effort: str | None, max_tokens: int | None) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if effort is not None:
        if effort not in ("low", "medium", "high"):
            raise GigaChatError("reasoning_effort must be low, medium, high, or None.")
        options["reasoning_effort"] = effort
    if max_tokens is not None:
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise GigaChatError("reasoning_max_tokens must be an integer from 1 to 8192, or None.")
        options["reasoning_max_tokens"] = max_tokens
    return options


def _without_reasoning_content(value: Any) -> Any:
    """Keep final responses and usage, discarding private reasoning text."""
    if isinstance(value, dict):
        return {key: _without_reasoning_content(item) for key, item in value.items() if key != "reasoning_content"}
    if isinstance(value, list):
        return [_without_reasoning_content(item) for item in value]
    return value


def load_env_file(path: str | os.PathLike[str] = ".env") -> int:
    """Load simple KEY=VALUE lines without interpolation or overwriting env.

    Blank lines, comments, optional ``export`` and single/double quoted values
    are supported. Values are literal: shell commands and escapes are never
    executed or expanded. Missing files are harmless. Returns the added count.
    """
    try:
        contents = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeError):
        raise GigaChatError("Cannot read the environment file.") from None

    pending: dict[str, str] = {}
    for line_number, original in enumerate(contents.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not _ENV_NAME.fullmatch(name):
            raise GigaChatError(
                f"Invalid environment file syntax on line {line_number}."
            )
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (
                value[end + 1 :].strip()
                and not value[end + 1 :].lstrip().startswith("#")
            ):
                raise GigaChatError(
                    f"Invalid environment file syntax on line {line_number}."
                )
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "\x00" in value:
            raise GigaChatError(
                f"Invalid environment file syntax on line {line_number}."
            )
        pending[name] = value

    added = 0
    for name, value in pending.items():
        if name not in os.environ:
            os.environ[name] = value
            added += 1
    return added


class _NoRedirect(request.HTTPRedirectHandler):
    """Do not forward authorization headers to redirect destinations."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GigaChatClient:
    """Synchronous client; calls on one instance are serialized.

    Set GIGACHAT_CREDENTIALS to the authorization key from the API cabinet.
    Optional GIGACHAT_CA_BUNDLE names a PEM trust bundle. TLS verification
    cannot be disabled. No request is sent during construction.
    """

    provider_name = "gigachat"

    def __init__(
        self,
        *,
        model: str | None = None,
        scope: str | None = None,
        timeout: float = 120.0,
        ca_bundle: str | os.PathLike[str] | None = None,
        base_url: str | None = None,
    ) -> None:
        credentials = os.environ.get("GIGACHAT_CREDENTIALS", "").strip()
        if credentials.startswith("Basic "):
            credentials = credentials[6:].strip()
        if not credentials:
            raise GigaChatError("Set GIGACHAT_CREDENTIALS before connecting.")
        if not credentials.isascii() or any(ch.isspace() for ch in credentials):
            raise GigaChatError("GIGACHAT_CREDENTIALS has an invalid format.")
        if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise GigaChatError("The request timeout must be a positive number.")
        selected_model = model if model is not None else os.environ.get("GIGACHAT_MODEL", DEFAULT_MODEL)
        if not isinstance(selected_model, str) or not selected_model.strip():
            raise GigaChatError("A model identifier is required.")
        selected_base_url = base_url if base_url is not None else os.environ.get("GIGACHAT_BASE_URL", API_BASE_URL)
        if not isinstance(selected_base_url, str) or selected_base_url.rstrip("/") != API_BASE_URL:
            raise GigaChatError("GIGACHAT_BASE_URL must be the official HTTPS v1 API endpoint.")
        selected_scope = scope or os.environ.get("GIGACHAT_SCOPE") or DEFAULT_SCOPE
        if selected_scope not in {
            "GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"
        }:
            raise GigaChatError("GIGACHAT_SCOPE is not supported.")
        self.model = selected_model
        self.base_url = selected_base_url.rstrip("/")
        self.scope = selected_scope
        self.timeout = float(timeout)
        self._credentials = credentials
        self._access_token = ""
        self._expires_at = 0.0
        self._lock = threading.RLock()
        ca_file = ca_bundle or os.environ.get("GIGACHAT_CA_BUNDLE") or None
        try:
            self._ssl_context = ssl.create_default_context()
            if ca_file is not None:
                self._ssl_context.load_verify_locations(cafile=ca_file)
            # Explicitly keep both checks, even if the platform default changes.
            self._ssl_context.verify_mode = ssl.CERT_REQUIRED
            self._ssl_context.check_hostname = True
            self._opener = request.build_opener(
                request.HTTPSHandler(context=self._ssl_context), _NoRedirect()
            )
        except (OSError, ValueError, TypeError):
            raise GigaChatError(
                "Cannot configure verified TLS. Check GIGACHAT_CA_BUNDLE."
            ) from None

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
    ) -> dict[str, Any]:
        try:
            req = request.Request(url, data=data, headers=headers, method=method)
            with self._opener.open(req, timeout=self.timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            status = exc.code if isinstance(exc.code, int) else None
            exc.close()
            message = f"GigaChat HTTP error {status}." if status else "GigaChat HTTP error."
            raise GigaChatError(message, status=status) from None
        except (error.URLError, OSError, ValueError, HTTPException):
            raise GigaChatError(
                "GigaChat connection failed. Check the network and trusted TLS certificates."
            ) from None
        if len(body) > _MAX_RESPONSE_BYTES:
            raise GigaChatError("GigaChat response exceeds the client size limit.")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise GigaChatError("GigaChat returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise GigaChatError("GigaChat returned an unexpected response structure.")
        return payload

    def _get_access_token(self) -> str:
        # Callers hold _lock, including while refreshing an expired token.
        if self._access_token and time.time() < self._expires_at - 30:
            return self._access_token
        payload = self._request_json(
            OAUTH_URL,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "RqUID": str(uuid.uuid4()),
                "Authorization": "Basic " + self._credentials,
            },
            data=parse.urlencode({"scope": self.scope}).encode("ascii"),
        )
        token = payload.get("access_token")
        expiration = payload.get("expires_at")
        if (
            not isinstance(token, str) or not token or not token.isascii()
            or any(ch.isspace() for ch in token)
            or isinstance(expiration, bool)
            or not isinstance(expiration, (int, float))
        ):
            raise GigaChatError("GigaChat returned an invalid authorization response.")
        try:
            expires_at = float(expiration)
        except (OverflowError, ValueError):
            raise GigaChatError("GigaChat returned an invalid token expiration.") from None
        if expires_at > 100_000_000_000:
            expires_at /= 1000  # API deployments may return Unix milliseconds.
        if not math.isfinite(expires_at) or expires_at <= time.time():
            raise GigaChatError("GigaChat returned an expired or invalid access token.")
        self._access_token = token
        self._expires_at = expires_at
        return token

    def list_models(self) -> dict[str, Any]:
        """Return the API's GET /models JSON object."""
        with self._lock:
            token = self._get_access_token()
            return self._request_json(
                self.base_url + "/models",
                method="GET",
                headers={"Accept": "application/json", "Authorization": "Bearer " + token},
            )

    def validate_function(self, function: dict[str, Any]) -> dict[str, Any]:
        """Check one public tool description; does not send workbook data."""
        if not isinstance(function, dict):
            raise GigaChatError("The function description must be a JSON object.")
        try:
            data = json.dumps(function, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise GigaChatError("The function description is not valid JSON.") from None
        with self._lock:
            token = self._get_access_token()
            return self._request_json(
                self.base_url + "/functions/validate", method="POST",
                headers={"Accept": "application/json", "Content-Type": "application/json",
                         "Authorization": "Bearer " + token}, data=data,
            )

    def chat(
        self,
        messages: list[dict[str, Any]],
        functions: list[dict[str, Any]] | None = None,
        function_call: str | dict[str, str] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
        reasoning_max_tokens: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send messages; tool execution remains the application's responsibility."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if functions is not None:
            payload["functions"] = functions
        if function_call is not None:
            payload["function_call"] = function_call
        payload.update(_reasoning_options(reasoning_effort, reasoning_max_tokens))
        session_headers = _session_headers(session_id)
        if max_tokens is not None:
            if type(max_tokens) is not int or not 1 <= max_tokens <= 65_536:
                raise GigaChatError("max_tokens must be an integer from 1 to 65536.")
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            if (
                isinstance(temperature, bool) or not isinstance(temperature, (float, int))
                or not math.isfinite(temperature) or not 0 < temperature <= 2
            ):
                raise GigaChatError("temperature must be greater than zero and at most 2.")
            payload["temperature"] = temperature
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise GigaChatError("The chat request is not valid JSON.") from None
        with self._lock:
            token = self._get_access_token()
            result = self._request_json(
                self.base_url + "/chat/completions",
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                    **session_headers,
                },
                data=data,
            )
            return _without_reasoning_content(result)

    @staticmethod
    def _choice(response: dict[str, Any]) -> dict[str, Any]:
        choices = response.get("choices")
        if (
            not isinstance(choices, list) or not choices
            or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get("message"), dict)
        ):
            raise GigaChatError("GigaChat returned an invalid chat response.")
        return choices[0]

    @staticmethod
    def _safe_usage(response: dict[str, Any]) -> dict[str, int]:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return {}
        names = ("prompt_tokens", "completion_tokens", "total_tokens", "precached_prompt_tokens")
        return {
            key: usage[key] for key in names
            if type(usage.get(key)) is int and usage[key] >= 0
        }

    def _redact(self, text: str) -> str:
        for secret in (self._credentials, self._access_token):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def probe_tool_call(self) -> dict[str, Any]:
        """Exercise one allowlisted tool on Demo!B2=42, never a real workbook.

        Returns only the model, validated call, synthetic result, final text,
        and numeric usage. Unexpected function names/arguments are rejected.
        """
        function = {
            "name": "get_test_cell",
            "description": "Читает единственную тестовую ячейку Demo!B2.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string", "enum": ["Demo"]},
                    "cell": {"type": "string", "enum": ["B2"]},
                },
                "required": ["sheet", "cell"],
            },
        }
        messages = [{
            "role": "user",
            "content": "Вызови get_test_cell для ячейки B2 листа Demo, чтобы узнать её значение.",
        }]
        first = self.chat(
            messages, functions=[function], function_call={"name": "get_test_cell"}
        )
        choice = self._choice(first)
        assistant = choice["message"]
        call = assistant.get("function_call")
        if choice.get("finish_reason") != "function_call" or not isinstance(call, dict):
            raise GigaChatError("The tool probe did not receive a function call.")
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                raise GigaChatError("The tool probe received invalid arguments.") from None
        if call.get("name") != "get_test_cell":
            raise GigaChatError("The tool probe rejected a function outside its allowlist.")
        if arguments != {"sheet": "Demo", "cell": "B2"}:
            raise GigaChatError("The tool probe rejected unexpected cell arguments.")
        validated_call = {"name": "get_test_cell", "arguments": arguments}
        assistant_message = {
            "role": "assistant",
            "content": assistant.get("content") if isinstance(assistant.get("content"), str) else "",
            "function_call": validated_call,
        }
        if isinstance(assistant.get("functions_state_id"), str):
            assistant_message["functions_state_id"] = assistant["functions_state_id"]
        result = {"sheet": "Demo", "cell": "B2", "value": 42}
        messages.extend([
            assistant_message,
            {"role": "function", "name": "get_test_cell", "content": json.dumps(result)},
            {"role": "user", "content": "Кратко сообщи значение ячейки по результату функции."},
        ])
        final = self.chat(messages, functions=[function], function_call="none")
        final_choice = self._choice(final)
        final_message = final_choice["message"]
        final_text = final_message.get("content")
        if (
            final_choice.get("finish_reason") != "stop"
            or not isinstance(final_text, str) or not final_text.strip()
            or final_message.get("function_call")
        ):
            raise GigaChatError("The tool probe did not receive a completed text answer.")
        model = final.get("model") or first.get("model") or self.model
        return {
            "model": self._redact(model) if isinstance(model, str) else self.model,
            "function_call": validated_call,
            "tool_result": result,
            "final_text": self._redact(final_text),
            "usage": {
                "tool_selection": self._safe_usage(first),
                "final_answer": self._safe_usage(final),
            },
        }
