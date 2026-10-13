import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

import codex_model_watch as watch


class FakeAPI(BaseHTTPRequestHandler):
    requests = []
    reply = []
    content_type = "text/event-stream"
    status = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        self.end_headers()
        if self.content_type == "text/event-stream":
            for event in self.reply:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(json.dumps(self.reply).encode())


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        FakeAPI.requests = []
        FakeAPI.status = 200
        FakeAPI.content_type = "text/event-stream"
        FakeAPI.reply = [
            {"type": "response.created", "response": {"model": "served-model"}},
            {"type": "response.output_text.delta", "delta": "你好 44"},
            {"type": "response.completed", "response": {
                "model": "served-model", "usage": {"output_tokens_details": {"reasoning_tokens": 123}}}},
        ]
        self.db_patch = patch.object(watch, "APP_DIR", self.home)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def config(self, extra="", root='model_provider = "CodexPlusPlus"\n'):
        Path(self.home, "config.toml").write_text(
            root + '[model_providers.CodexPlusPlus]\n'
            'base_url = "%s"\nwire_api = "responses"\n%s' % (self.base, extra),
            encoding="utf-8")

    def test_auto_pure_api_auth_json(self):
        self.config()
        Path(self.home, "auth.json").write_text('{"OPENAI_API_KEY":"fixture-key"}')
        r = watch.run_probe(self.home, "requested-model")
        self.assertIsNone(r["error"])
        self.assertTrue(r["swapped"])
        path, auth, body = FakeAPI.requests[-1]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(auth, "Bearer fixture-key")
        self.assertIn("input", body)
        self.assertEqual(body["model"], "requested-model")
        conn = watch.db_connect(watch.db_path())
        try:
            row = conn.execute("SELECT * FROM probes").fetchone()
            self.assertEqual(row["served"], "served-model")
            self.assertNotIn("fixture-key", str(tuple(row)))
        finally:
            conn.close()

    def test_bearer_with_official_oauth_present(self):
        self.config('experimental_bearer_token = "provider-key"\n')
        Path(self.home, "auth.json").write_text('{"tokens":{"access_token":"official-secret"}}')
        r = watch._post_ask("m", "q", "high", self.home, want_reasoning=True)
        self.assertIsNone(r["error"])
        self.assertEqual(r["text"], "你好 44")
        self.assertEqual(r["reasoning"], 123)
        self.assertEqual(r["reasoning_unit"], "tokens")
        self.assertEqual(FakeAPI.requests[-1][1], "Bearer provider-key")

    def test_env_key(self):
        self.config('env_key = "TEST_PROVIDER_KEY"\n')
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "environment-key"}):
            self.assertEqual(watch.resolve_api(self.home)[1], "environment-key")

    def test_missing_env_does_not_use_other_credentials(self):
        self.config('env_key = "TEST_MISSING_KEY"\nexperimental_bearer_token = "wrong-key"\n')
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "环境变量"):
                watch.resolve_api(self.home)

    def test_profile_provider(self):
        self.config('experimental_bearer_token = "key"\n',
                    'model_provider = "openai"\nprofile = "third"\n'
                    '[profiles.third]\nmodel_provider = "CodexPlusPlus"\n')
        self.assertEqual(watch.resolve_api(self.home)[0], self.base + "/responses")

    def test_manual_paths(self):
        cases = [
            ("https://api.example", "chat", "https://api.example/v1/chat/completions"),
            ("https://api.example/v1/", "responses", "https://api.example/v1/responses"),
            ("https://api.example/custom/v1", "responses", "https://api.example/custom/v1/responses"),
            ("https://api.example/v1/responses", None, "https://api.example/v1/responses"),
        ]
        for base, fmt, expected in cases:
            with self.subTest(base=base):
                self.assertEqual(watch.resolve_api(self.home, base, "key", fmt)[0], expected)

    def test_chat_stream(self):
        FakeAPI.reply = [
            {"model": "m", "choices": [{"delta": {"content": "hello", "reasoning_content": "think"}}]},
            {"model": "m", "choices": [], "usage": {"completion_tokens_details": {"reasoning_tokens": 9}}},
        ]
        r = watch._post_ask("m", "q", "low", self.home, self.base, "key", True)
        self.assertEqual(r["text"], "hello")
        self.assertEqual(r["reasoning"], 9)
        self.assertEqual(FakeAPI.requests[-1][0], "/v1/chat/completions")
        self.assertIn("messages", FakeAPI.requests[-1][2])

    def test_responses_json(self):
        FakeAPI.content_type = "application/json"
        FakeAPI.reply = {"model": "m", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "44"}]}]}
        r = watch._post_ask("m", "q", "high", self.home, self.base + "/responses", "key")
        self.assertEqual(r["text"], "44")

    def test_chat_json(self):
        FakeAPI.content_type = "application/json"
        FakeAPI.reply = {"model": "m", "choices": [{"message": {"content": "hi"}}]}
        r = watch.run_probe(self.home, "m", self.base, "key")
        self.assertIsNone(r["error"])
        self.assertFalse(r["swapped"])

    def test_missing_model_is_not_consistent(self):
        FakeAPI.reply = [{"choices": [{"delta": {"content": "hi"}}]}]
        r = watch.run_probe(self.home, "m", self.base, "key")
        self.assertIsNotNone(r["error"])
        self.assertFalse(r["swapped"])

    def test_api_error(self):
        FakeAPI.reply = [{"type": "response.failed", "response": {"error": {"message": "secret"}}}]
        r = watch.run_probe(self.home, "m", self.base + "/responses", "key")
        self.assertIsNotNone(r["error"])
        self.assertNotIn("secret", r["error"])

    def test_http_error_does_not_echo_secret(self):
        FakeAPI.status = 401
        FakeAPI.content_type = "application/json"
        FakeAPI.reply = {"error": "fixture-key"}
        r = watch.run_probe(self.home, "m", self.base, "fixture-key")
        self.assertIn("401", r["error"])
        self.assertNotIn("fixture-key", r["error"])

    def test_no_login(self):
        self.assertIn("登录态", watch.run_probe(self.home, "m")["error"])

    def test_dashboard_routes_all_probes(self):
        self.config('experimental_bearer_token = "config-key"\n')
        args = SimpleNamespace(codex_home=self.home, api_base="", api_key="", api_format="")
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(watch, "g_args", args):
                url = "http://127.0.0.1:%d/api/probe" % server.server_port
                req = Request(url, data=json.dumps({"model": "m"}).encode(),
                              headers={"Content-Type": "application/json"})
                with urlopen(req) as response:
                    self.assertIsNone(json.load(response)["error"])
                self.assertEqual(FakeAPI.requests[-1][0], "/v1/responses")
                for flag, fn in (("canary", "run_canary"), ("battery", "run_battery")):
                    with patch.object(watch, fn, return_value={"error": None}) as mock:
                        req = Request(url, data=json.dumps({"model": "m", flag: True}).encode(),
                                      headers={"Content-Type": "application/json"})
                        with urlopen(req) as response:
                            self.assertIsNone(json.load(response)["error"])
                        self.assertEqual(mock.call_args.kwargs["api_base"], self.base + "/responses")
                        self.assertEqual(mock.call_args.kwargs["api_key"], "config-key")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_demo_schema(self):
        conn = watch.db_connect(watch.db_path())
        try:
            watch.seed_demo(conn)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
