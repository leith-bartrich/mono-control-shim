"""Tests for the host-callback broker.

Stdlib ``unittest`` and stdlib ``urllib``, for the same reason as test_cli.py: no
dependency may enter this repo, and a test client is a dependency like any other.

    python -m unittest discover -s tests -t .

These run against a **real server on a real socket** rather than a mocked handler.
The behavior being asserted — that a bad token gets 401 and an unregistered method
gets refused — is only meaningful end-to-end; a unit test of the dispatch dict would
prove nothing about what the socket actually answers.
"""

from __future__ import annotations

import json
import logging
import unittest
import urllib.error
import urllib.request

from mono_control_shim import broker


def setUpModule() -> None:
    """Silence the audit log for the duration of the suite.

    Most of these tests deliberately trigger the WARNING-level events (401s and
    refusals), so leaving the handler attached would bury the test output in the very
    lines being asserted on.
    """
    logging.getLogger("mono_control_shim.broker").addHandler(logging.NullHandler())
    logging.getLogger("mono_control_shim.broker").setLevel(logging.CRITICAL)


class BrokerTestCase(unittest.TestCase):
    """A live broker on an ephemeral loopback port, torn down per test."""

    def setUp(self) -> None:
        self.broker = broker.BrokerServer()
        self.broker.start()
        self.addCleanup(self.broker.stop)
        self.url = f"http://127.0.0.1:{self.broker.port}/"

    def _post(self, body: bytes | str, *, token: str | None = "__valid__") -> tuple[int, dict]:
        """POST *body*, returning (status, decoded JSON).

        ``token`` defaults to the sentinel meaning "the real one"; pass ``None`` for
        no Authorization header at all, or a string to present a wrong one.
        """
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = (
                f"Bearer {self.broker.token if token == '__valid__' else token}"
            )
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _call(self, method: str, **kwargs) -> tuple[int, dict]:
        return self._post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}), **kwargs)


class Verbs(BrokerTestCase):
    """The two things the broker will do in Step 1."""

    def test_ping_returns_pong(self) -> None:
        status, payload = self._call("ping")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"jsonrpc": "2.0", "id": 1, "result": "pong"})

    def test_broker_info_advertises_version_and_verbs(self) -> None:
        _, payload = self._call("broker.info")

        self.assertEqual(payload["result"]["version"], broker.BROKER_VERSION)
        # The exact set, not a subset: a verb appearing here unnoticed is precisely
        # the regression this repo cannot afford.
        self.assertEqual(payload["result"]["methods"], ["broker.info", "ping"])


class Authentication(BrokerTestCase):
    """The token is the security gate; everything else is defense in depth."""

    def test_missing_authorization_is_401(self) -> None:
        status, _ = self._call("ping", token=None)

        self.assertEqual(status, 401)

    def test_wrong_token_is_401(self) -> None:
        status, _ = self._call("ping", token="not-the-token")

        self.assertEqual(status, 401)

    def test_bad_token_is_rejected_before_the_body_is_parsed(self) -> None:
        # Garbage that would be a -32700 if it were ever parsed. An unauthenticated
        # caller must not be able to tell the parser from the auth check, and must not
        # be able to reach the parser at all.
        status, _ = self._post(b"{ not json", token="not-the-token")

        self.assertEqual(status, 401)

    def test_token_prefix_is_not_accepted(self) -> None:
        status, _ = self._call("ping", token=self.broker.token[:-1])

        self.assertEqual(status, 401)

    def test_non_bearer_scheme_is_rejected(self) -> None:
        status, _ = self._call("ping", token=None)  # baseline
        self.assertEqual(status, 401)

        request = urllib.request.Request(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
            headers={"Authorization": f"Basic {self.broker.token}"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_each_broker_gets_its_own_token(self) -> None:
        other = broker.BrokerServer()

        self.assertNotEqual(other.token, self.broker.token)


class Refusal(BrokerTestCase):
    """Everything not registered is refused. This is the whole point of Step 1."""

    def test_unknown_method_is_method_not_found(self) -> None:
        status, payload = self._call("rm -rf /")

        self.assertEqual(status, 200)  # transport fine, call refused
        self.assertEqual(payload["error"]["code"], broker.METHOD_NOT_FOUND)
        self.assertNotIn("result", payload)

    def test_a_plausible_future_verb_is_still_refused_today(self) -> None:
        # Step 2 will add git verbs. Until it does, naming one must get nothing.
        _, payload = self._call("git.clone")

        self.assertEqual(payload["error"]["code"], broker.METHOD_NOT_FOUND)


class Protocol(BrokerTestCase):
    """JSON-RPC error taxonomy, for a caller that is authenticated but wrong."""

    def test_malformed_json_is_parse_error(self) -> None:
        status, payload = self._post(b"{ not json")

        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], broker.PARSE_ERROR)

    def test_empty_body_is_parse_error(self) -> None:
        _, payload = self._post(b"")

        self.assertEqual(payload["error"]["code"], broker.PARSE_ERROR)

    def test_missing_jsonrpc_version_is_invalid_request(self) -> None:
        _, payload = self._post(json.dumps({"id": 1, "method": "ping"}))

        self.assertEqual(payload["error"]["code"], broker.INVALID_REQUEST)

    def test_non_object_request_is_invalid_request(self) -> None:
        _, payload = self._post(json.dumps(["ping"]))

        self.assertEqual(payload["error"]["code"], broker.INVALID_REQUEST)

    def test_non_string_method_is_invalid_request(self) -> None:
        _, payload = self._post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": 7}))

        self.assertEqual(payload["error"]["code"], broker.INVALID_REQUEST)

    def test_positional_params_are_invalid_params(self) -> None:
        # By-name params only; see the handler for why.
        _, payload = self._post(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]})
        )

        self.assertEqual(payload["error"]["code"], broker.INVALID_PARAMS)

    def test_request_id_is_echoed_back_on_error(self) -> None:
        _, payload = self._post(
            json.dumps({"jsonrpc": "2.0", "id": "abc-123", "method": "nope"})
        )

        self.assertEqual(payload["id"], "abc-123")


class InternalError(BrokerTestCase):
    """A verb that raises must not take the daemon down, nor leak host internals."""

    def setUp(self) -> None:
        super().setUp()

        @broker.verb("test.boom")
        def _boom(params):  # noqa: ANN001, ANN202
            raise RuntimeError("secret host detail: C:/Users/someone/.ssh/id_ed25519")

        self.addCleanup(broker._VERBS.pop, "test.boom", None)

    def test_raising_verb_is_internal_error(self) -> None:
        status, payload = self._call("test.boom")

        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], broker.INTERNAL_ERROR)

    def test_the_exception_text_does_not_cross_the_seam(self) -> None:
        _, payload = self._call("test.boom")

        # The traceback belongs in the host's log; the container gets a flat message.
        self.assertEqual(payload["error"]["message"], "internal error")
        self.assertNotIn("id_ed25519", json.dumps(payload))

    def test_the_server_survives_and_still_serves(self) -> None:
        self._call("test.boom")
        status, payload = self._call("ping")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "pong")


class BodyLimit(BrokerTestCase):
    """`Content-Length` is a number the caller chooses; we must not simply trust it."""

    def _raw_post(self, headers: str, body: bytes = b"") -> bytes:
        """Speak HTTP by hand, so we can declare a length we do not send."""
        import socket

        with socket.create_connection(("127.0.0.1", self.broker.port), timeout=5) as sock:
            sock.sendall(headers.encode("ascii") + body)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def test_an_absurd_declared_length_is_refused_not_allocated(self) -> None:
        # 4 GiB claimed, zero bytes sent. The point is that this returns promptly
        # rather than the host reserving 4 GiB for a body that will never arrive.
        response = self._raw_post(
            "POST / HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Authorization: Bearer {self.broker.token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {4 * (1 << 30)}\r\n"
            "\r\n"
        )

        self.assertIn(b"413", response.split(b"\r\n")[0])

    def test_an_oversized_body_is_refused_before_auth_is_bypassed(self) -> None:
        # An unauthenticated caller must still hit the 401, not the size check:
        # auth is the outer gate and nothing gets to reorder that.
        response = self._raw_post(
            "POST / HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Authorization: Bearer wrong\r\n"
            f"Content-Length: {4 * (1 << 30)}\r\n"
            "\r\n"
        )

        self.assertIn(b"401", response.split(b"\r\n")[0])

    def test_a_normal_body_is_unaffected(self) -> None:
        _, payload = self._call("ping")

        self.assertEqual(payload["result"], "pong")

    def test_a_rejected_request_closes_the_connection(self) -> None:
        # We answer 401/413 without draining the body, so the connection cannot be
        # reused: leftover bytes would otherwise be parsed as the next request line.
        response = self._raw_post(
            "POST / HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Authorization: Bearer wrong\r\n"
            "Content-Length: 5\r\n"
            "\r\n",
            b"12345",
        )

        # recv() returned b"" (loop exited) => the server hung up, as it must.
        self.assertIn(b"401", response.split(b"\r\n")[0])
        self.assertIn(b"Connection: close", response)


class Lifecycle(unittest.TestCase):
    """Start/stop, and the port contract."""

    def test_port_is_ephemeral_and_readable_after_start(self) -> None:
        with broker.BrokerServer() as running:
            self.assertGreater(running.port, 0)

    def test_port_before_start_is_an_error_not_a_lie(self) -> None:
        with self.assertRaises(RuntimeError):
            broker.BrokerServer().port

    def test_context_manager_releases_the_port(self) -> None:
        with broker.BrokerServer() as running:
            port = running.port
            url = f"http://127.0.0.1:{port}/"
        # After exit the listener is gone: the socket refuses rather than hanging.
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(url, data=b"{}", timeout=5)

    def test_stop_is_idempotent(self) -> None:
        server = broker.BrokerServer()
        server.start()
        server.stop()
        server.stop()  # must not raise


if __name__ == "__main__":
    unittest.main()
