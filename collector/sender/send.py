"""Forward collector measurements to the hosted Infrastructure endpoint.

Reads through the local HTTP API, never from SQLite, so collector.db is
untouched. Outbound only: no inbound port is required.

    python send.py --environment local-box --once --dry-run
    python send.py --environment local-box --once
    python send.py --environment local-box

The key is read from LIGHTOS_COLLECTOR_KEY and is never accepted as an
argument, never written to the state file, and never printed. Only its first
four characters are ever shown, to tell an lfc_ Infrastructure key from an lfk_
trace key.

Delivery rules that are easy to get wrong, and are therefore explicit:

  * The pending payload and its batch_id are persisted BEFORE the first POST,
    so a timeout, a dropped connection or a restart retries the identical
    payload rather than minting a new one. A receiver that already stored it
    answers "duplicate", which counts as delivered.
  * Only HTTP 200 whose JSON status is "accepted" or "duplicate" confirms
    delivery. A 202, or a 200 with a cheerful message and no status, does not:
    the batch stays pending.
  * The source cursor advances only after confirmed delivery.
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from snapshot import MAX_BYTES, STALE_AFTER, build, iso, parse_time

INFRASTRUCTURE_PATH = "/api/public/v1/infrastructure"
DEFAULT_SOURCE = "http://localhost:8787"
DEFAULT_STATE = "sender-state.json"
DEFAULT_INTERVAL = 60.0
MIN_INTERVAL = 10.0
CONNECT_TIMEOUT = 15
POST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 64 * 1024          # a destination reply is a small status object
MAX_SOURCE_BYTES = 8 * 1024 * 1024      # a source reply is a batch of measurements
CONFIRMING = ("accepted", "duplicate")
LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")

EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_AUTH = 2
EXIT_NEEDS_RECOVERY = 3
EXIT_USAGE = 4


# --- small helpers -----------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def log(message):
    """Progress goes to stderr so stdout carries only a --dry-run payload."""
    print(f"{iso(now_utc())}  {message}", file=sys.stderr, flush=True)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the Authorization header to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"refused to follow a redirect to {urllib.parse.urlsplit(newurl).netloc} "
            "(the collector key must not be sent to another origin)",
            headers, fp,
        )


OPENER = urllib.request.build_opener(NoRedirects)


def read_bounded(response, limit=MAX_RESPONSE_BYTES):
    """Read at most `limit` bytes, and refuse to return a truncated body.

    Silently truncating would hand half a JSON document to the parser, which
    fails somewhere unrelated and looks like malformed data rather than a body
    that was too big.
    """
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError(
            f"the response exceeded {limit} bytes, so it was not read in full. "
            "Lower --limit, or raise the bound if this is genuinely expected."
        )
    return body.decode("utf-8", "replace")


def as_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


# --- the local source --------------------------------------------------------

def is_loopback(url):
    host = urllib.parse.urlsplit(url).hostname
    return host in ("localhost", "127.0.0.1", "::1")


def get_json(url, timeout=CONNECT_TIMEOUT):
    with OPENER.open(urllib.request.Request(url), timeout=timeout) as response:
        return json.loads(read_bounded(response, MAX_SOURCE_BYTES))


def resolve_environment(source, wanted):
    """Exact id or exact name. An ambiguous match is refused, never guessed."""
    rows = get_json(f"{source.rstrip('/')}/api/environments")
    matches = [r for r in rows if wanted in (r.get("id"), r.get("name"))]
    if not matches:
        names = ", ".join(sorted(str(r.get("name")) for r in rows)) or "none"
        raise SystemExit(f"No environment with id or name {wanted!r}. Available: {names}")
    if len(matches) > 1:
        ids = ", ".join(str(r.get("id")) for r in matches)
        raise SystemExit(f"{wanted!r} matches more than one environment ({ids}). Use the id.")
    return matches[0]


def fetch_measurements(source, environment_id, limit):
    """Newest first. The API has a limit and no offset, so this cannot page."""
    query = urllib.parse.urlencode({"environment": environment_id, "limit": limit})
    return get_json(f"{source.rstrip('/')}/api/measurements?{query}")


# --- state -------------------------------------------------------------------

def state_key(source, destination, environment_id):
    """Isolates state per source, destination and environment. No key material."""
    raw = "\n".join([source.rstrip("/"), destination.rstrip("/"), environment_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    """Atomic: a crash mid-write must not leave a half-written pending payload."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    temporary = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)


def slot(state, key):
    return state.setdefault(key, {"pending": None, "cursor": None})


# --- delivery ----------------------------------------------------------------

class Outcome:
    def __init__(self, kind, detail="", retry_after=None):
        self.kind = kind        # delivered | transient | auth | permanent
        self.detail = detail
        self.retry_after = retry_after


def post_snapshot(url, key, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        with OPENER.open(request, timeout=POST_TIMEOUT) as response:
            return classify(response.status, read_bounded(response), response.headers)
    except urllib.error.HTTPError as e:
        return classify(e.code, read_bounded(e), e.headers)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        # Uncertain: the receiver may or may not have stored it. Keep the
        # payload and the id so the retry is byte-identical.
        return Outcome("transient", f"no usable response ({getattr(e, 'reason', e)})")


def classify(status, body, headers=None):
    parsed = as_json(body)
    if status == 200:
        state = (parsed or {}).get("status") if isinstance(parsed, dict) else None
        if state in CONFIRMING:
            return Outcome("delivered", str(state))
        return Outcome(
            "transient",
            f"HTTP 200 without a confirming status (got {state!r}); treating as not delivered",
        )
    if status in (401, 403):
        return Outcome("auth", f"HTTP {status}")
    if status == 429:
        retry = None
        if headers is not None:
            raw = headers.get("Retry-After")
            try:
                retry = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                retry = None
        return Outcome("transient", "HTTP 429 rate limited", retry_after=retry)
    if 500 <= status < 600:
        return Outcome("transient", f"HTTP {status}")
    # 202 lands here too: accepted-for-processing is not confirmation.
    if status == 202:
        return Outcome("transient", "HTTP 202 is not confirmation; the batch stays pending")
    return Outcome("permanent", f"HTTP {status} {str(body)[:200]}")


def pending_is_expired(pending, now):
    observed = parse_time((pending or {}).get("payload", {}).get("observed_at"))
    return observed is not None and (now - observed) > STALE_AFTER


# --- one pass ----------------------------------------------------------------

def run_once(args, key):
    source, destination = args.source, args.url
    environment = resolve_environment(source, args.environment)
    key_id = state_key(source, destination or "dry-run", environment["id"])

    state = load_state(args.state_file)
    here = slot(state, key_id)
    now = now_utc()

    pending = here.get("pending")
    if pending and pending.get("blocked"):
        log(f"blocked: {pending.get('blocked')}")
        log("  the same payload will not be retried until you run --discard-pending")
        return EXIT_NEEDS_RECOVERY

    if pending and pending_is_expired(pending, now):
        pending["blocked"] = (
            f"the pending batch observed {pending['payload'].get('observed_at')} is now older "
            "than the 24 hour window the receiver accepts, so it can never be delivered"
        )
        here["pending"] = pending
        save_state(args.state_file, state)
        log(f"blocked: {pending['blocked']}")
        log("  run with --discard-pending to drop it and continue with current data")
        return EXIT_NEEDS_RECOVERY

    if pending:
        payload = pending["payload"]
        log(f"retrying pending batch {payload['batch_id'][:8]}... "
            f"(attempt {pending.get('attempts', 0) + 1})")
    else:
        rows = fetch_measurements(source, environment["id"], args.limit)
        cursor = here.get("cursor")
        fresh = [r for r in rows if not cursor or str(r.get("collected_at") or "") > cursor]
        if not fresh:
            log(f"nothing new for {environment['name']}")
            return EXIT_OK

        payload, report = build(fresh, batch_id=str(uuid.uuid4()), now=now)

        for problem in report["problems"]:
            log(f"  note: {problem}")
        if not payload["entities"]:
            log("no entity carried a usable metric; nothing to send")
            return EXIT_OK
        if report["oversized"]:
            log(f"refusing to send: {report['bytes']} bytes exceeds the {MAX_BYTES} limit")
            return EXIT_NEEDS_RECOVERY
        if report["stale"]:
            log(f"source data is STALE: newest reading is {report['age_seconds'] / 3600:.1f}h old "
                f"({report['newest_source_time']}). The receiver rejects anything over 24h.")
            log("  the agent has probably stopped; forwarding cannot fix that")
        if report["future"]:
            log(f"source data is in the FUTURE ({report['newest_source_time']}); check the clock")
        log(f"built batch {payload['batch_id'][:8]}... "
            f"entities={report['entities']} bytes={report['bytes']} "
            f"observed_at={payload['observed_at']}")

        if args.dry_run:
            print(json.dumps(payload, indent=2))
            log("dry run: nothing was sent and no state was changed")
            return EXIT_OK

        # Persisted BEFORE the first POST, so an uncertain response retries
        # this exact payload and id rather than minting a new batch.
        pending = {
            "payload": payload,
            "attempts": 0,
            "created_at": iso(now),
            "source_cursor": max(str(r.get("collected_at") or "") for r in fresh),
        }
        here["pending"] = pending
        save_state(args.state_file, state)

    if args.dry_run:
        print(json.dumps(pending["payload"], indent=2))
        log("dry run: a pending batch exists; nothing was sent and no state was changed")
        return EXIT_OK

    url = destination.rstrip("/") + INFRASTRUCTURE_PATH
    outcome = post_snapshot(url, key, pending["payload"])
    pending["attempts"] = pending.get("attempts", 0) + 1

    if outcome.kind == "delivered":
        here["cursor"] = pending["source_cursor"]
        here["pending"] = None
        here["last_delivered_at"] = iso(now_utc())
        save_state(args.state_file, state)
        log(f"delivered ({outcome.detail}); cursor now {here['cursor']}")
        return EXIT_OK

    if outcome.kind == "auth":
        here["pending"] = pending
        save_state(args.state_file, state)
        log(f"refused: {outcome.detail}. The key was rejected.")
        log(f"  the key in use starts {key[:4]!r} and belongs to a different environment, "
            "has been revoked, or was never valid here")
        log("  check it in Infrastructure, or create a new environment and key")
        log("  nothing was lost: the batch stays pending and will be retried once the key works")
        return EXIT_AUTH

    if outcome.kind == "permanent":
        pending["blocked"] = f"the receiver rejected this payload outright: {outcome.detail}"
        here["pending"] = pending
        save_state(args.state_file, state)
        log(f"rejected: {outcome.detail}")
        log("  this will not be retried. Run --discard-pending once you have looked at it.")
        return EXIT_NEEDS_RECOVERY

    here["pending"] = pending
    save_state(args.state_file, state)
    log(f"not delivered: {outcome.detail}; keeping the batch pending")
    if outcome.retry_after:
        log(f"  the receiver asked to wait {outcome.retry_after}s")
    return EXIT_TRANSIENT


# --- entry point -------------------------------------------------------------

def check_destination(url, allow_loopback_http):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"):
        if allow_loopback_http:
            return
        raise SystemExit(
            "The destination is loopback HTTP. That is only for developing a receiver; "
            "pass --allow-loopback-destination to confirm you mean it."
        )
    raise SystemExit(f"The destination must be HTTPS. Got {parts.scheme or 'no scheme'!r}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Forward collector measurements to Infrastructure.")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="local collector server")
    parser.add_argument("--url", default=os.environ.get("LIGHTOS_APP_URL", ""),
                        help="hosted origin (or LIGHTOS_APP_URL)")
    parser.add_argument("--environment", required=True, help="source environment id or name")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--state-file", default=DEFAULT_STATE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print a batch; sends nothing, changes no state")
    parser.add_argument("--allow-loopback-destination", action="store_true",
                        help="permit http://localhost as the destination, for receiver development")
    parser.add_argument("--discard-pending", action="store_true",
                        help="drop a blocked pending batch after you have looked at it")
    args = parser.parse_args(argv)

    if not is_loopback(args.source):
        raise SystemExit(f"The source must be loopback. Got {args.source!r}.")

    if args.interval < MIN_INTERVAL:
        raise SystemExit(f"The receiver accepts at most one batch every {MIN_INTERVAL:.0f}s.")

    if args.discard_pending:
        state = load_state(args.state_file)
        environment = resolve_environment(args.source, args.environment)
        here = slot(state, state_key(args.source, args.url or "dry-run", environment["id"]))
        if not here.get("pending"):
            log("there is no pending batch to discard")
            return EXIT_OK
        batch = here["pending"]["payload"]["batch_id"]
        here["pending"] = None
        save_state(args.state_file, state)
        log(f"discarded pending batch {batch[:8]}...; the next pass builds a fresh one")
        return EXIT_OK

    key = os.environ.get("LIGHTOS_COLLECTOR_KEY", "")
    if not args.dry_run:
        if not args.url:
            raise SystemExit("No destination. Pass --url or set LIGHTOS_APP_URL.")
        check_destination(args.url, args.allow_loopback_destination)
        if not key:
            raise SystemExit(
                "LIGHTOS_COLLECTOR_KEY is not set. Set it in the shell, not on the command line."
            )
        if not key.startswith("lfc_"):
            raise SystemExit(
                f"That key starts {key[:4]!r}. Infrastructure collector keys start 'lfc_'; "
                "'lfk_' is an LLM trace key and a login token is neither."
            )

    if args.once:
        return run_once(args, key)

    while True:
        try:
            code = run_once(args, key)
        except SystemExit:
            raise
        except urllib.error.URLError as e:
            log(f"could not reach the local server: {getattr(e, 'reason', e)}")
            code = EXIT_TRANSIENT
        if code == EXIT_AUTH:
            log("stopping: continuous sending will not continue with a rejected key")
            return EXIT_AUTH
        if code == EXIT_NEEDS_RECOVERY:
            log("stopping: this needs you to look at it")
            return EXIT_NEEDS_RECOVERY
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
