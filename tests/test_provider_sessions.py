import unittest

from finmodel_audit.providers import ProviderAdapter, ProviderError


class ProviderSessionTests(unittest.TestCase):
    def test_optional_session_is_forwarded_only_to_capable_providers(self):
        class Capable:
            model = "test"

            def chat(self, messages, functions=None, function_call=None, session_id=None):
                return {"session": session_id}

        class Minimal:
            model = "test"

            def chat(self, messages, functions=None, function_call=None):
                return {"ok": True}

        self.assertEqual(ProviderAdapter(Capable()).chat([], session_id="opaque-1"), {"session": "opaque-1"})
        self.assertEqual(ProviderAdapter(Capable()).chat([]), {"session": None})
        self.assertEqual(ProviderAdapter(Minimal()).chat([], session_id="opaque-2"), {"ok": True})

    def test_invalid_session_is_rejected_without_calling_provider(self):
        class NeverCalled:
            model = "test"

            def chat(self, *args, **kwargs):
                raise AssertionError("Network must not be called")

        with self.assertRaisesRegex(ProviderError, "session_id"):
            ProviderAdapter(NeverCalled()).chat([], session_id="bad\nheader")
