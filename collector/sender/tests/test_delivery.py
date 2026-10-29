"""Delivery tests. Transport is MOCKED throughout: nothing here reaches a real
receiver, and none of it is evidence that the hosted endpoint accepts anything.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import send
from send import (EXIT_AUTH, EXIT_NEEDS_RECOVERY, EXIT_OK, EXIT_TRANSIENT,
                  NoRedirects, Outcome, classify, load_state, state_key)

KEY = "lfc_" + "a" * 60
NOW = datetime(2026, 10, 29, 6, 0, 0, tzinfo=timezone.utc)
ENVIRONMENT = {"id": "env-local", "name": "local-box"}


def at(minutes_ago, base=NOW):
    return (base - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def row(hostname="local-box", minutes_ago=1):
    return {
        "collected_at": at(minutes_ago),
        "host": {"hostname": hostname},
        "metrics": {"cpu": {"utilization": 0.2308},
                    "memory": {"used_bytes": 1, "total_bytes": 2}},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.dir, "state.json")
        self.rows = [row()]
        self.posts = []

        send.resolve_environment = lambda source, wanted: ENVIRONMENT
        send.fetch_measurements = lambda source, env, limit: list(self.rows)

    def args(self, **overrides):
        base = dict(
            source="http://localhost:8787", url="https://example.invalid",
            environment="local-box", limit=200, interval=60,
            state_file=self.state_file, once=True, dry_run=False,
            allow_loopback_destination=False, discard_pending=False,
        )
        base.update(overrides)
        return Namespace(**base)

    def run_pass(self, outcome, **overrides):
        """One pass with a mocked transport. Returns (exit code, printed text)."""
        def fake_post(url, key, payload):
            # Captured at the moment of the POST, so the ordering assertion in
            # PendingBeforePost is about what was really on disk by then.
            self.posts.append({
                "url": url, "key": key, "payload": payload,
                "state_on_disk": load_state(self.state_file),
            })
            return outcome() if callable(outcome) else outcome

        send.post_snapshot = fake_post
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = send.run_once(self.args(**overrides), KEY)
        return code, buffer.getvalue()

    def slot(self):
        state = load_state(self.state_file)
        return state[state_key("http://localhost:8787", "https://example.invalid", "env-local")]


# --- 5. pending persisted before posting, reused afterwards ------------------

class PendingBeforePost(Base):
    def test_the_pending_batch_is_on_disk_before_the_post_happens(self):
        self.run_pass(Outcome("transient", "timeout"))
        written = self.posts[0]["state_on_disk"]
        pending = list(written.values())[0]["pending"]
        self.assertIsNotNone(pending, "the payload must be persisted before posting")
        self.assertEqual(pending["payload"]["batch_id"], self.posts[0]["payload"]["batch_id"])

    def test_an_uncertain_response_keeps_the_batch_pending(self):
        code, _ = self.run_pass(Outcome("transient", "no usable response"))
        self.assertEqual(code, EXIT_TRANSIENT)
        self.assertIsNotNone(self.slot()["pending"])
        self.assertIsNone(self.slot()["cursor"])

    def test_a_restart_retries_the_identical_payload_and_batch_id(self):
        self.run_pass(Outcome("transient", "timeout"))
        first = self.posts[0]["payload"]

        # A new process would only have the state file to go on.
        self.rows = [row(minutes_ago=0)]  # newer data exists, but pending wins
        self.run_pass(Outcome("delivered", "accepted"))
        second = self.posts[1]["payload"]

        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first, second)

    def test_the_cursor_advances_only_after_confirmed_delivery(self):
        self.run_pass(Outcome("transient", "timeout"))
        self.assertIsNone(self.slot()["cursor"])
        self.run_pass(Outcome("delivered", "accepted"))
        self.assertEqual(self.slot()["cursor"], at(1))
        self.assertIsNone(self.slot()["pending"])

    def test_dry_run_sends_nothing_and_writes_no_state(self):
        code, _ = self.run_pass(Outcome("delivered", "accepted"), dry_run=True)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.posts, [])
        self.assertFalse(os.path.exists(self.state_file))

    def test_state_is_isolated_by_source_destination_and_environment(self):
        a = state_key("http://localhost:8787", "https://one.example", "env-1")
        for other in (
            state_key("http://localhost:8788", "https://one.example", "env-1"),
            state_key("http://localhost:8787", "https://two.example", "env-1"),
            state_key("http://localhost:8787", "https://one.example", "env-2"),
        ):
            self.assertNotEqual(a, other)

    def test_the_key_is_never_written_to_the_state_file(self):
        self.run_pass(Outcome("transient", "timeout"))
        with open(self.state_file, encoding="utf-8") as fh:
            self.assertNotIn(KEY, fh.read())


# --- 6. what counts as confirmation ------------------------------------------

class Confirmation(unittest.TestCase):
    def test_200_with_accepted_or_duplicate_confirms(self):
        for status in ("accepted", "duplicate"):
            self.assertEqual(classify(200, json.dumps({"status": status})).kind, "delivered")

    def test_202_is_not_confirmation(self):
        outcome = classify(202, json.dumps({"status": "accepted"}))
        self.assertEqual(outcome.kind, "transient")
        self.assertIn("202", outcome.detail)

    def test_200_without_a_status_is_not_confirmation(self):
        for body in ('{"message":"ok"}', '{"ok":true}', "Success!", "{}"):
            self.assertEqual(classify(200, body).kind, "transient")

    def test_200_with_an_unknown_status_is_not_confirmation(self):
        self.assertEqual(classify(200, json.dumps({"status": "queued"})).kind, "transient")


class PendingAfter202(Base):
    def test_a_202_leaves_the_batch_pending_and_the_cursor_untouched(self):
        code, out = self.run_pass(lambda: classify(202, '{"status":"accepted"}'))
        self.assertEqual(code, EXIT_TRANSIENT)
        self.assertIsNotNone(self.slot()["pending"])
        self.assertIsNone(self.slot()["cursor"])
        self.assertIn("202", out)


# --- 7. refusals, rate limits, permanent rejection ---------------------------

class Refusals(Base):
    def test_401_and_403_stop_and_explain(self):
        for status in (401, 403):
            self.setUp()
            code, out = self.run_pass(lambda s=status: classify(s, '{"error":"no"}'))
            self.assertEqual(code, EXIT_AUTH)
            self.assertIn("key", out.lower())
            self.assertIsNotNone(self.slot()["pending"], "nothing should be lost on a refusal")

    def test_429_is_transient_and_carries_retry_after(self):
        class Headers(dict):
            def get(self, key, default=None):
                return dict.get(self, key, default)

        outcome = classify(429, "{}", Headers({"Retry-After": "30"}))
        self.assertEqual(outcome.kind, "transient")
        self.assertEqual(outcome.retry_after, 30.0)

    def test_a_malformed_retry_after_does_not_break_the_pass(self):
        class Headers(dict):
            def get(self, key, default=None):
                return dict.get(self, key, default)

        self.assertIsNone(classify(429, "{}", Headers({"Retry-After": "soon"})).retry_after)

    def test_server_errors_are_transient(self):
        for status in (500, 502, 503):
            self.assertEqual(classify(status, "").kind, "transient")

    def test_a_permanent_rejection_blocks_and_demands_an_explicit_recovery(self):
        code, _ = self.run_pass(lambda: classify(422, '{"error":"unknown field"}'))
        self.assertEqual(code, EXIT_NEEDS_RECOVERY)
        self.assertIn("blocked", self.slot()["pending"])

        # It must not loop: a second pass refuses rather than retrying.
        code, out = self.run_pass(Outcome("delivered", "accepted"))
        self.assertEqual(code, EXIT_NEEDS_RECOVERY)
        self.assertEqual(len(self.posts), 1, "a blocked batch must not be posted again")
        self.assertIn("--discard-pending", out)

    def test_a_pending_batch_that_has_aged_out_blocks_rather_than_retrying_forever(self):
        self.rows = [row(minutes_ago=1)]
        self.run_pass(Outcome("transient", "timeout"))

        # Age the pending payload past the receiver's 24 hour window.
        state = load_state(self.state_file)
        key = list(state)[0]
        payload = state[key]["pending"]["payload"]
        payload["observed_at"] = at(60 * 30)
        send.save_state(self.state_file, state)

        code, out = self.run_pass(Outcome("delivered", "accepted"))
        self.assertEqual(code, EXIT_NEEDS_RECOVERY)
        self.assertIn("24 hour", out)
        self.assertEqual(len(self.posts), 1)


# --- 8. credentials stay put -------------------------------------------------

class Credentials(Base):
    def test_the_key_is_never_printed(self):
        _, out = self.run_pass(Outcome("auth", "HTTP 401"))
        self.assertNotIn(KEY, out)
        self.assertNotIn(KEY[4:], out)
        # Only the prefix, which is what distinguishes lfc_ from lfk_.
        self.assertIn("lfc_", out)

    def test_a_redirect_is_refused_rather_than_followed(self):
        handler = NoRedirects()
        request = urllib.request.Request("https://one.example/api/public/v1/infrastructure")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {},
                                     "https://elsewhere.example/collect")
        self.assertIn("elsewhere.example", str(caught.exception))

    def test_the_key_goes_in_a_header_never_in_the_url(self):
        self.run_pass(Outcome("delivered", "accepted"))
        self.assertNotIn(KEY, self.posts[0]["url"])
        self.assertEqual(self.posts[0]["key"], KEY)


if __name__ == "__main__":
    unittest.main()


class BoundedReads(unittest.TestCase):
    """A body larger than the bound must raise, not return half a document."""

    class Fake:
        def __init__(self, payload):
            self.payload = payload
            self.read_calls = []

        def read(self, n):
            self.read_calls.append(n)
            return self.payload[:n]

    def test_a_body_within_the_bound_is_returned(self):
        body = send.read_bounded(self.Fake(b'{"ok":true}'), limit=1024)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_a_body_over_the_bound_raises_rather_than_truncating(self):
        oversized = b"x" * 2048
        with self.assertRaises(ValueError) as caught:
            send.read_bounded(self.Fake(oversized), limit=1024)
        self.assertIn("not read in full", str(caught.exception))

    def test_the_source_bound_is_larger_than_the_destination_bound(self):
        # A destination reply is a small status object; a source reply is a
        # batch of measurements, which legitimately runs to megabytes.
        self.assertGreater(send.MAX_SOURCE_BYTES, send.MAX_RESPONSE_BYTES)
