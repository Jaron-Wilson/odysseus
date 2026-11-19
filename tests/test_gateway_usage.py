import json
import os
import tempfile
import unittest
from unittest import mock

from src import gateway_usage as gu


def _sse(model, delta=None, usage=None):
    j = {"id": "x", "object": "chat.completion.chunk", "model": model,
         "choices": [{"index": 0, "delta": delta or {}}] if usage is None else [], }
    if usage:
        j["usage"] = usage
    return ("data: " + json.dumps(j) + "\n\n").encode()


class RewriteTests(unittest.TestCase):
    def test_completion_model_is_the_resolved_one(self):
        body = json.dumps({"model": "upstream", "choices": [{"message": {"content": "hello"}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
        out, tok, chars = gu.rewrite_completion(body, "Lab/qwen")
        self.assertEqual(json.loads(out)["model"], "Lab/qwen")
        self.assertEqual((tok["prompt_tokens"], tok["total_tokens"], chars), (5, 7, 5))

    def test_explicit_model_is_untouched(self):
        body = json.dumps({"model": "upstream", "choices": []}).encode()
        out, _, _ = gu.rewrite_completion(body, None)
        self.assertEqual(out, body)

    def test_not_json_passes_through(self):
        self.assertEqual(gu.rewrite_completion(b"nope", "m")[0], b"nope")


class TapTests(unittest.TestCase):
    def test_stream_rewritten_across_split_chunks(self):
        raw = _sse("up", {"content": "hi"}) + _sse("up", usage={"prompt_tokens": 3, "completion_tokens": 1}) + b"data: [DONE]\n\n"
        for cut in (1, 17, len(raw) // 2, len(raw) - 3):
            tap = gu.SseTap("Lab/qwen")
            out = tap.feed(raw[:cut]) + tap.feed(raw[cut:]) + tap.flush()
            lines = [l for l in out.decode().split("\n") if l.startswith("data: {")]
            self.assertTrue(all(json.loads(l[6:])["model"] == "Lab/qwen" for l in lines))
            self.assertIn(b"data: [DONE]", out)
            self.assertEqual(tap.tokens["prompt_tokens"], 3)
            self.assertEqual(tap.chars, 2)

    def test_no_resolved_leaves_stream_alone(self):
        raw = _sse("up", {"content": "hi"})
        tap2 = gu.SseTap(None)
        self.assertEqual((tap2.feed(raw) + tap2.flush()).decode().strip(), raw.decode().strip())


class LogTests(unittest.TestCase):
    def test_record_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "u.db")
            with mock.patch.object(gu, "db_path", return_value=p):
                gu.record(caller="t / opencode", requested="auto", resolved="A/b", stream=1, status=200, ok=1,
                          prompt_tokens=10, completion_tokens=5, total_tokens=15, cached_tokens=0, estimated=0, latency_ms=100)
                gu.record(caller="t / ask", requested="A/b", resolved="A/b", stream=0, status=502, ok=0, error="x",
                          prompt_tokens=1, completion_tokens=0, total_tokens=1, cached_tokens=0, estimated=1, latency_ms=5)
                s = gu.summary(7)
            self.assertEqual(s["total"]["requests"], 2)
            self.assertEqual(s["total"]["total_tokens"], 16)
            self.assertEqual({r["requested"] for r in s["by_requested"]}, {"auto", "A/b"})
            self.assertIn("A/b", gu.render_html(s))


if __name__ == "__main__":
    unittest.main()
