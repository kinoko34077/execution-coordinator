from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import execution_coordinator.github_state as github_state
from execution_coordinator.github_state import GitHubApiError, GitHubStateStore


class _Handler(BaseHTTPRequestHandler):
    body = "initial body"
    updated_at: object = "2026-09-27T12:00:00Z"
    comments: list[str] = []
    fail_patch = False
    get_redirect_location: str | None = None
    comment_redirect_location: str | None = None
    get_response_repository = "owner/repo"
    get_response_number = 9
    patch_response_repository = "owner/repo"
    patch_response_number = 9
    comment_issue_repository = "owner/repo"
    comment_issue_number = 9

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _json(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _api_base_url(self) -> str:
        return f"http://{self.headers['Host']}"

    def _issue_payload(
        self,
        *,
        repository: str,
        number: int,
        body: str,
        include_updated_at: bool,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": 1000 + number,
            "number": number,
            "body": body,
            "html_url": f"https://github.com/{repository}/issues/{number}",
            "repository_url": f"{self._api_base_url()}/repos/{repository}",
        }
        if include_updated_at and type(self).updated_at is not None:
            payload["updated_at"] = type(self).updated_at
        return payload

    def _parse_issue_path(self) -> tuple[str, int] | None:
        parts = self.path.strip("/").split("/")
        if len(parts) != 5 or parts[0] != "repos" or parts[3] != "issues":
            return None
        try:
            number = int(parts[4])
        except ValueError:
            return None
        return f"{parts[1]}/{parts[2]}", number

    def do_GET(self) -> None:  # noqa: N802
        expected_path = "/repos/owner/repo/issues/9"
        if self.path == expected_path and type(self).get_redirect_location is not None:
            self._redirect(type(self).get_redirect_location)
            return

        parsed = self._parse_issue_path()
        if parsed is None:
            self._json(404, {"message": "not found"})
            return

        if self.path == expected_path:
            repository = type(self).get_response_repository
            number = type(self).get_response_number
            body = type(self).body
        else:
            repository, number = parsed
            body = "foreign body"

        self._json(
            200,
            self._issue_payload(
                repository=repository,
                number=number,
                body=body,
                include_updated_at=True,
            ),
        )

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
        self._json(
            200,
            self._issue_payload(
                repository=type(self).patch_response_repository,
                number=type(self).patch_response_number,
                body=type(self).body,
                include_updated_at=False,
            ),
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/repos/owner/repo/issues/9/comments":
            self._json(404, {"message": "not found"})
            return
        if type(self).comment_redirect_location is not None:
            self._redirect(type(self).comment_redirect_location)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).comments.append(payload["body"])
        comment_id = len(type(self).comments)
        repository = type(self).comment_issue_repository
        number = type(self).comment_issue_number
        self._json(
            201,
            {
                "id": comment_id,
                "body": payload["body"],
                "issue_url": f"{self._api_base_url()}/repos/{repository}/issues/{number}",
                "html_url": (
                    f"https://github.com/{repository}/issues/{number}"
                    f"#issuecomment-{comment_id}"
                ),
            },
        )


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
        _Handler.get_redirect_location = None
        _Handler.comment_redirect_location = None
        _Handler.get_response_repository = "owner/repo"
        _Handler.get_response_number = 9
        _Handler.patch_response_repository = "owner/repo"
        _Handler.patch_response_number = 9
        _Handler.comment_issue_repository = "owner/repo"
        _Handler.comment_issue_number = 9
        self.store = GitHubStateStore(
            token="test-token",
            repository="owner/repo",
            issue_number=9,
            api_base_url=self.base_url,
        )

    def _state_store_error_type(self):
        return getattr(github_state, "StateStoreError", GitHubApiError)

    def test_state_store_error_type_is_available(self) -> None:
        self.assertTrue(hasattr(github_state, "StateStoreError"))

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

    def test_load_fails_closed_on_redirect_to_another_issue(self) -> None:
        _Handler.get_redirect_location = "/repos/other/repo/issues/10"

        with self.assertRaises(self._state_store_error_type()):
            self.store.load_body_with_metadata()

    def test_load_fails_closed_on_repository_identity_mismatch(self) -> None:
        _Handler.get_response_repository = "other/repo"

        with self.assertRaises(self._state_store_error_type()):
            self.store.load_body_with_metadata()

    def test_load_fails_closed_on_issue_number_mismatch(self) -> None:
        _Handler.get_response_number = 10

        with self.assertRaises(self._state_store_error_type()):
            self.store.load_body_with_metadata()

    def test_save_replaces_issue_body(self) -> None:
        self.store.save_body("replacement")
        self.assertEqual("replacement", self.store.load_body())

    def test_save_fails_closed_on_response_identity_mismatch(self) -> None:
        _Handler.patch_response_repository = "other/repo"

        with self.assertRaises(self._state_store_error_type()):
            self.store.save_body("must-not-be-trusted")

    def test_comment_posts_durable_event(self) -> None:
        self.store.add_comment("CLAIMED example")
        self.assertEqual(["CLAIMED example"], _Handler.comments)

    def test_comment_fails_closed_on_redirect_to_another_issue(self) -> None:
        _Handler.comment_redirect_location = "/repos/other/repo/issues/10"

        with self.assertRaises(self._state_store_error_type()):
            self.store.add_comment("must-not-be-trusted")
        self.assertEqual([], _Handler.comments)

    def test_comment_fails_closed_on_issue_identity_mismatch(self) -> None:
        _Handler.comment_issue_repository = "other/repo"

        with self.assertRaises(self._state_store_error_type()):
            self.store.add_comment("must-not-be-trusted")

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
