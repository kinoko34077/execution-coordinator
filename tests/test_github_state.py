from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from execution_coordinator.github_state import GitHubApiError, GitHubStateStore


class _Handler(BaseHTTPRequestHandler):
    body = "initial body"
    updated_at: object = "2026-09-27T12:00:00Z"
    comments: list[str] = []
    fail_patch = False

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _json(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/repos/owner/repo/issues/9":
            self._json(404, {"message": "not found"})
            return
        payload: dict[str, object] = {"number": 9, "body": type(self).body}
        if type(self).updated_at is not None:
            payload["updated_at"] = type(self).updated_at
        self._json(200, payload)

    def do_PATCH(self) -> None:  # noqa: N802
        if self.path != "/repos/owner/repo/issues/9":
            self._json(404, {"message": "not found"})
            return
        if type(self).fail_patch:
            self._json(500, {"message": "synthetic failure"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).body = payload["body"]
        self._json(200, {"number": 9, "body": type(self).body})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/repos/owner/repo/issues/9/comments":
            self._json(404, {"message": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).comments.append(payload["body"])
        self._json(201, {"id": len(type(self).comments), "body": payload["body"]})


class GitHubStateStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self) -> None:
        _Handler.body = "initial body"
        _Handler.updated_at = "2026-09-27T12:00:00Z"
        _Handler.comments = []
        _Handler.fail_patch = False
        self.store = GitHubStateStore(
            token="test-token",
            repository="owner/repo",
            issue_number=9,
            api_base_url=self.base_url,
        )

    def test_load_returns_issue_body(self) -> None:
        self.assertEqual("initial body", self.store.load_body())

    def test_metadata_read_returns_body_and_exact_updated_at(self) -> None:
        load_with_metadata = getattr(self.store, "load_body_with_metadata", None)
        self.assertIsNotNone(load_with_metadata, "metadata-bearing state read must exist")

        result = load_with_metadata()

        self.assertEqual(result.body, "initial body")
        self.assertEqual(result.updated_at, "2026-09-27T12:00:00Z")

    def test_metadata_read_fails_closed_on_missing_or_malformed_updated_at(self) -> None:
        load_with_metadata = getattr(self.store, "load_body_with_metadata", None)
        self.assertIsNotNone(load_with_metadata, "metadata-bearing state read must exist")

        for updated_at in (None, "not-a-timestamp", 123):
            with self.subTest(updated_at=updated_at):
                _Handler.updated_at = updated_at
                with self.assertRaises(GitHubApiError):
                    load_with_metadata()

    def test_save_replaces_issue_body(self) -> None:
        self.store.save_body("replacement")
        self.assertEqual("replacement", self.store.load_body())

    def test_comment_posts_durable_event(self) -> None:
        self.store.add_comment("CLAIMED example")
        self.assertEqual(["CLAIMED example"], _Handler.comments)

    def test_non_2xx_patch_raises_api_error(self) -> None:
        _Handler.fail_patch = True
        with self.assertRaises(GitHubApiError):
            self.store.save_body("must-not-succeed")
        self.assertEqual("initial body", _Handler.body)

    def test_token_is_never_exposed_in_api_error(self) -> None:
        bad = GitHubStateStore(
            token="super-secret-token",
            repository="owner/repo",
            issue_number=999,
            api_base_url=self.base_url,
        )
        with self.assertRaises(GitHubApiError) as caught:
            bad.load_body()
        self.assertNotIn("super-secret-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
