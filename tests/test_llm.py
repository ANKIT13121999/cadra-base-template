from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.llm import LLMClient, LLMError


class FakeResponse:
    def __init__(self, status_code: int, text: str, data: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class LLMRetryTests(unittest.TestCase):
    def test_400_retries_without_reasoning_and_strips_think(self) -> None:
        first = FakeResponse(400, '{"error":"reasoning.enabled is not supported"}')
        second = FakeResponse(
            200,
            "",
            {
                "model": "qwen/qwen3.8-27b",
                "choices": [{"message": {"content": "<think>secret</think>{\"ok\":true}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )
        env = {
            "MODEL_NAME": "qwen/qwen3.8-27b",
            "LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "LLM_API_KEY": "test-key",
            "PRICE_IN_PER_MTOK": "0.60",
            "PRICE_OUT_PER_MTOK": "3.00",
        }
        with patch.dict(os.environ, env, clear=False), patch("src.llm.httpx.post", side_effect=[first, second]) as post:
            result = LLMClient().chat([{"role": "user", "content": "hi"}], json_mode=True)
        self.assertEqual(post.call_count, 2)
        first_payload = post.call_args_list[0].kwargs["json"]
        second_payload = post.call_args_list[1].kwargs["json"]
        self.assertEqual(first_payload["reasoning_effort"], "none")
        self.assertNotIn("reasoning", first_payload)
        self.assertNotIn("reasoning_effort", second_payload)
        self.assertEqual(result.content, '{"ok":true}')
        self.assertEqual((result.input_tokens, result.output_tokens), (5, 2))
        self.assertAlmostEqual(result.cost_usd, 0.000009, places=8)

    def test_error_body_is_included_and_truncated(self) -> None:
        body = "bad parameter: unsupported reasoning.enabled " + ("x" * 400)
        responses = [FakeResponse(400, body), FakeResponse(400, body)]
        env = {
            "MODEL_NAME": "qwen/qwen3.8-27b",
            "LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "LLM_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=False), patch("src.llm.httpx.post", side_effect=responses):
            with self.assertRaises(LLMError) as raised:
                LLMClient().chat([{"role": "user", "content": "hi"}])
        message = str(raised.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("bad parameter", message)
        self.assertNotIn("test-key", message)
        self.assertLessEqual(len(message), 340)

    def test_missing_model_name_is_clear(self) -> None:
        env = {
            "LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "LLM_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MODEL_NAME", None)
            with self.assertRaises(LLMError) as raised:
                LLMClient().chat([{"role": "user", "content": "hi"}])
        self.assertEqual(str(raised.exception), "MODEL_NAME is not set")


if __name__ == "__main__":
    unittest.main()
