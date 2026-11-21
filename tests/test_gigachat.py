"""Offline checks for authentication, transport safety, and the tool cycle."""

import io
from http.client import BadStatusLine
import json
import os
from pathlib import Path
import ssl
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
import uuid

from finmodel_audit.gigachat import (
    API_BASE_URL,
    DEFAULT_MODEL,
    GigaChatClient,
    GigaChatError,
    OAUTH_URL,
    _NoRedirect,
    load_env_file,
)


NOW = 1_800_000_000.0
CREDENTIALS = "example-authorization-secret"
TOKEN = "example-access-token-secret"


def response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def authorization(*, expires_at=NOW + 1800, token=TOKEN):
    return response({"access_token": token, "expires_at": expires_at})


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / ".env"

    def test_literal_values_comments_and_existing_environment(self):
        os.environ["ALREADY_SET"] = "keep-original"
        self.path.write_text(
            "# ignored\nALREADY_SET=replace-me\n"
            "export GIGACHAT_CREDENTIALS='literal$HOME$(cmd)\\n' # comment\n"
            'DOUBLE="literal ${NAME}"\nUNQUOTED=a=b#c # comment\n',
            encoding="utf-8",
        )
        self.assertEqual(load_env_file(self.path), 3)
        self.assertEqual(os.environ["ALREADY_SET"], "keep-original")
        self.assertEqual(os.environ["GIGACHAT_CREDENTIALS"], "literal$HOME$(cmd)\\n")
        self.assertEqual(os.environ["DOUBLE"], "literal ${NAME}")
        self.assertEqual(os.environ["UNQUOTED"], "a=b#c")

    def test_missing_file_is_harmless(self):
        self.assertEqual(load_env_file(self.path), 0)

    def test_bad_syntax_does_not_disclose_value_or_partially_load(self):
        self.path.write_text("FIRST=ok\nBROKEN='secret-not-to-print\n", encoding="utf-8")
        with self.assertRaises(GigaChatError) as caught:
            load_env_file(self.path)
        self.assertNotIn("secret-not-to-print", str(caught.exception))
        self.assertNotIn("FIRST", os.environ)

    def test_quotes_and_trailing_content_are_checked(self):
        self.path.write_text("VALUE='abc'execute-this\n", encoding="utf-8")
        with self.assertRaises(GigaChatError):
            load_env_file(self.path)


class ClientTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GIGACHAT_CREDENTIALS": CREDENTIALS}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        clock = patch("finmodel_audit.gigachat.time.time", return_value=NOW)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        transport = patch("finmodel_audit.gigachat.request.build_opener")
        self.build_opener = transport.start()
        self.addCleanup(transport.stop)
        self.opener = self.build_opener.return_value
        self.client = GigaChatClient()

    def test_function_validation_uses_public_schema_without_chat_payload(self):
        description = {"name": "read_demo", "parameters": {"type": "object", "properties": {}}}
        self.opener.open.side_effect = [authorization(), response({"message": "Function is valid"})]
        self.assertEqual(self.client.validate_function(description)["message"], "Function is valid")
        sent = self.opener.open.call_args_list[-1].args[0]
        self.assertEqual(sent.full_url, API_BASE_URL + "/functions/validate")
        self.assertEqual(json.loads(sent.data), description)
        self.assertNotIn("messages", json.loads(sent.data))
        self.assertNotIn("X-session-id", sent.headers)

    def test_function_validation_rejects_bad_json_before_authorization(self):
        for invalid in ([], {"secret": object()}, {"value": float("nan")}):
            with self.assertRaises(GigaChatError):
                self.client.validate_function(invalid)
        self.opener.open.assert_not_called()

    def test_constructor_performs_no_request_and_keeps_tls_verification(self):
        self.opener.open.assert_not_called()
        self.assertEqual(self.client._ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(self.client._ssl_context.check_hostname)
        handlers = self.build_opener.call_args.args
        self.assertIs(handlers[0]._context, self.client._ssl_context)
        self.assertIsInstance(handlers[1], _NoRedirect)
        self.assertIsNone(handlers[1].redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_custom_ca_bundle_is_passed_to_verified_context(self):
        context = Mock(spec=ssl.SSLContext)
        with patch.dict(os.environ, {"GIGACHAT_CA_BUNDLE": "/trusted/custom-ca.pem"}):
            with patch("finmodel_audit.gigachat.ssl.create_default_context", return_value=context) as create:
                client = GigaChatClient()
        create.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile="/trusted/custom-ca.pem")
        self.assertTrue(client._ssl_context.check_hostname)
        self.assertEqual(client._ssl_context.verify_mode, ssl.CERT_REQUIRED)

    def test_bad_ca_path_error_is_safe(self):
        with patch("finmodel_audit.gigachat.ssl.create_default_context", side_effect=OSError(CREDENTIALS)):
            with self.assertRaises(GigaChatError) as caught:
                GigaChatClient(ca_bundle="missing.pem")
        self.assertNotIn(CREDENTIALS, str(caught.exception))

    def test_authentication_headers_and_token_cache(self):
        self.opener.open.side_effect = [
            authorization(), response({"data": []}), response({"data": []})
        ]
        self.assertEqual(self.client.list_models(), {"data": []})
        self.assertEqual(self.client.list_models(), {"data": []})
        self.assertEqual(self.opener.open.call_count, 3)
        auth_request = self.opener.open.call_args_list[0].args[0]
        self.assertEqual(auth_request.full_url, OAUTH_URL)
        self.assertEqual(auth_request.method, "POST")
        self.assertEqual(auth_request.get_header("Authorization"), "Basic " + CREDENTIALS)
        self.assertEqual(auth_request.data, b"scope=GIGACHAT_API_PERS")
        self.assertEqual(uuid.UUID(auth_request.get_header("Rquid")).version, 4)
        models_request = self.opener.open.call_args_list[1].args[0]
        self.assertEqual(models_request.full_url, API_BASE_URL + "/models")
        self.assertEqual(models_request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(CREDENTIALS, repr(self.client))

    def test_millisecond_expiration_and_refresh_before_expiry(self):
        self.opener.open.side_effect = [
            authorization(expires_at=(NOW + 120) * 1000),
            response({"data": []}),
            response({"data": []}),
            authorization(expires_at=(NOW + 1800) * 1000, token="new-access-token"),
            response({"data": []}),
        ]
        self.client.list_models()
        self.clock.return_value = NOW + 60
        self.client.list_models()
        self.clock.return_value = NOW + 100
        self.client.list_models()
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertEqual(
            self.opener.open.call_args.args[0].get_header("Authorization"),
            "Bearer new-access-token",
        )

    def test_expired_token_response_is_rejected(self):
        self.opener.open.side_effect = [authorization(expires_at=NOW - 1)]
        with self.assertRaises(GigaChatError):
            self.client.list_models()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_missing_credentials_are_actionable_without_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(GigaChatError, "GIGACHAT_CREDENTIALS"):
                GigaChatClient()
        self.opener.open.assert_not_called()

    def test_http_error_never_discloses_url_reason_body_or_secrets(self):
        exception = HTTPError(
            "https://example.invalid/" + CREDENTIALS,
            401,
            "Authorization: Bearer " + TOKEN,
            {},
            io.BytesIO((CREDENTIALS + TOKEN).encode()),
        )
        self.opener.open.side_effect = exception
        try:
            self.client.list_models()
        except GigaChatError as caught:
            self.assertEqual(caught.status, 401)
            display = "".join(traceback.format_exception(type(caught), caught, caught.__traceback__))
            self.assertNotIn(CREDENTIALS, display)
            self.assertNotIn(TOKEN, display)
            self.assertNotIn("example.invalid", display)
        else:
            self.fail("Expected a safe HTTP error")

    def test_network_and_json_errors_are_safe(self):
        for bad_response in (
            URLError(CREDENTIALS),
            BadStatusLine(TOKEN),
            io.BytesIO((TOKEN + " invalid-json").encode()),
        ):
            with self.subTest(kind=type(bad_response).__name__):
                self.opener.open.side_effect = [bad_response]
                with self.assertRaises(GigaChatError) as caught:
                    self.client.list_models()
                self.assertNotIn(CREDENTIALS, str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_chat_serialization_and_optional_tool_fields(self):
        self.opener.open.side_effect = [authorization(), response({"choices": []})]
        messages = [{"role": "user", "content": "Привет"}]
        functions = [{"name": "get_test_cell", "parameters": {"type": "object"}}]
        self.client.chat(messages, functions=functions, function_call={"name": "get_test_cell"})
        sent = self.opener.open.call_args.args[0]
        payload = json.loads(sent.data)
        self.assertEqual(sent.full_url, API_BASE_URL + "/chat/completions")
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["functions"], functions)
        self.assertEqual(payload["function_call"], {"name": "get_test_cell"})
        self.assertFalse(payload["stream"])

    def test_model_and_official_base_url_from_environment(self):
        with patch.dict(os.environ, {
            "GIGACHAT_MODEL": "GigaChat-2-Pro",
            "GIGACHAT_BASE_URL": API_BASE_URL + "/",
        }):
            client = GigaChatClient()
            self.assertEqual(client.model, "GigaChat-2-Pro")
            self.assertEqual(client.base_url, API_BASE_URL)
            self.assertEqual(GigaChatClient(model=DEFAULT_MODEL).model, DEFAULT_MODEL)
        self.opener.open.assert_not_called()

    def test_base_url_rejects_http_and_non_allowlisted_origins(self):
        for url in (
            "http://api.giga.chat/v1",
            "https://attacker.invalid/v1",
            "https://api.giga.chat.attacker.invalid/v1",
            "https://api.giga.chat@attacker.invalid/v1",
            "https://api.giga.chat/v1?token=" + TOKEN,
        ):
            with self.subTest(url=url):
                with patch.dict(os.environ, {"GIGACHAT_BASE_URL": url}):
                    with self.assertRaises(GigaChatError) as caught:
                        GigaChatClient()
                self.assertNotIn(TOKEN, str(caught.exception))
        self.opener.open.assert_not_called()

    def test_non_json_request_is_rejected_before_authentication(self):
        with self.assertRaises(GigaChatError):
            self.client.chat([{"role": "user", "content": object()}])
        self.opener.open.assert_not_called()

    def test_generation_limits_are_serialized_and_invalid_values_rejected(self):
        self.opener.open.side_effect = [authorization(), response({"choices": []})]
        self.client.chat([{"role": "user", "content": "test"}], max_tokens=600, temperature=0.1)
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(payload["max_tokens"], 600)
        self.assertEqual(payload["temperature"], 0.1)
        for kwargs in ({"max_tokens": 0}, {"max_tokens": True}, {"temperature": 0}, {"temperature": 3}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(GigaChatError):
                    self.client.chat([], **kwargs)

    def test_optional_reasoning_is_serialized_and_private_content_removed(self):
        self.opener.open.side_effect = [authorization(), response({
            "choices": [{"message": {"content": "Final answer", "reasoning_content": "private-analysis"}}],
            "usage": {"total_tokens": 1200, "reasoning_tokens": 1024},
        }), response({"choices": []})]
        result = self.client.chat([], max_tokens=3500, reasoning_effort="high", reasoning_max_tokens=1024)
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["reasoning_max_tokens"], 1024)
        self.assertEqual(payload["max_tokens"], 3500)
        self.assertNotIn("private-analysis", json.dumps(result))
        self.assertEqual(result["choices"][0]["message"]["content"], "Final answer")
        self.assertEqual(result["usage"]["reasoning_tokens"], 1024)
        self.client.chat([])
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("reasoning_max_tokens", payload)

    def test_reasoning_invalid_values_are_rejected_before_authentication(self):
        for kwargs in ({"reasoning_effort": "private-invalid"}, {"reasoning_effort": True},
                       {"reasoning_max_tokens": 0}, {"reasoning_max_tokens": 8193},
                       {"reasoning_max_tokens": True}, {"reasoning_max_tokens": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(GigaChatError) as caught:
                self.client.chat([], **kwargs)
            self.assertNotIn("private-invalid", str(caught.exception))
        self.opener.open.assert_not_called()

    def test_session_cache_header_is_per_request_and_never_sent_to_oauth(self):
        self.opener.open.side_effect = [authorization(), response({"choices": []}), response({"choices": []})]
        self.client.chat([], session_id="audit:anonymous-fingerprint")
        oauth, chat = [entry.args[0] for entry in self.opener.open.call_args_list]
        self.assertIsNone(oauth.get_header("X-session-id"))
        self.assertEqual(chat.get_header("X-session-id"), "audit:anonymous-fingerprint")
        self.assertNotIn("session_id", json.loads(chat.data))
        self.client.chat([])
        self.assertIsNone(self.opener.open.call_args.args[0].get_header("X-session-id"))

    def test_session_cache_rejects_header_injection_before_network(self):
        for value in ("", "x\r\nAuthorization: private-token", "file name.xlsx", "я", True, "a" * 129):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(GigaChatError) as caught:
                self.client.chat([], session_id=value)
            self.assertNotIn("private-token", str(caught.exception))
        self.opener.open.assert_not_called()

    def test_probe_round_trip_preserves_state_and_only_uses_synthetic_data(self):
        first = {
            "model": DEFAULT_MODEL,
            "choices": [{
                "finish_reason": "function_call",
                "message": {
                    "role": "assistant", "content": "",
                    "functions_state_id": "synthetic-function-state",
                    "function_call": {
                        "name": "get_test_cell",
                        "arguments": {"sheet": "Demo", "cell": "B2"},
                    },
                },
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        final = {
            "model": DEFAULT_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Значение: 42."}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
        }
        self.opener.open.side_effect = [authorization(), response(first), response(final)]
        result = self.client.probe_tool_call()
        self.assertEqual(result["tool_result"], {"sheet": "Demo", "cell": "B2", "value": 42})
        self.assertEqual(result["final_text"], "Значение: 42.")
        self.assertEqual(result["usage"]["tool_selection"]["total_tokens"], 30)
        calls = self.opener.open.call_args_list
        first_payload = json.loads(calls[1].args[0].data)
        self.assertEqual(first_payload["function_call"], {"name": "get_test_cell"})
        second_payload = json.loads(calls[2].args[0].data)
        self.assertEqual(second_payload["function_call"], "none")
        history = second_payload["messages"]
        self.assertEqual(history[1]["functions_state_id"], "synthetic-function-state")
        self.assertEqual(history[2]["role"], "function")
        self.assertEqual(history[2]["name"], "get_test_cell")
        self.assertEqual(json.loads(history[2]["content"])["value"], 42)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(CREDENTIALS, json.dumps(result))

    def test_probe_rejects_unknown_tool_and_unexpected_arguments(self):
        for name, arguments in (
            ("execute_python", {"sheet": "Demo", "cell": "B2"}),
            ("get_test_cell", {"sheet": "Production", "cell": "B2"}),
            ("get_test_cell", {"sheet": "Demo", "cell": "B2", "code": "print('bad')"}),
        ):
            with self.subTest(name=name, arguments=arguments):
                first = {"choices": [{
                    "finish_reason": "function_call",
                    "message": {"function_call": {"name": name, "arguments": arguments}},
                }]}
                with patch.object(self.client, "chat", return_value=first) as chat:
                    with self.assertRaises(GigaChatError):
                        self.client.probe_tool_call()
                    self.assertEqual(chat.call_count, 1)

    def test_probe_redacts_echoed_secrets_and_filters_usage(self):
        self.client._access_token = TOKEN
        first = {"choices": [{
            "finish_reason": "function_call",
            "message": {"function_call": {
                "name": "get_test_cell", "arguments": '{"sheet":"Demo","cell":"B2"}'
            }},
        }]}
        final = {
            "model": "model-" + TOKEN,
            "choices": [{"finish_reason": "stop", "message": {"content": "42 " + CREDENTIALS + TOKEN}}],
            "usage": {"total_tokens": 4, "secret": TOKEN, "prompt_tokens": TOKEN},
        }
        with patch.object(self.client, "chat", side_effect=[first, final]):
            result = self.client.probe_tool_call()
        serialized = json.dumps(result)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn(CREDENTIALS, serialized)
        self.assertEqual(result["usage"]["final_answer"], {"total_tokens": 4})


if __name__ == "__main__":
    unittest.main()
