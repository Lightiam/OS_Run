"""Forward collector measurements to the Infrastructure endpoint.

Reads from the local server rather than from the agent, so the local SQLite
history stays exactly as it is while this is being tested. Nothing here writes
to it.

    LIGHTOS_APP_URL=https://<published-address> \
    LIGHTOS_COLLECTOR_KEY=lfc_... \
    python send.py --from http://localhost:8787 --environment local-box --once

The key is read from the environment and never accepted as an argument, so it
cannot end up in shell history or in `ps` output. Use --dry-run to see exactly
what would be sent without sending it, and without needing a key at all.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from snapshot import snapshot

INFRASTRUCTURE_PATH = "/api/public/v1/infrastructure"
CURSOR_FILE = "sent-cursor.json"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _get(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def environments(base):
    return _get(f"{base.rstrip('/')}/api/environments")


def measurements(base, environment_id, limit):
    return _get(
        f"{base.rstrip('/')}/api/measurements"
        f"?environment={environment_id}&limit={limit}"
    )


def resolve(base, wanted):
    """Accept either an environment id or its name."""
    rows = environments(base)
    for row in rows:
        if wanted in (row.get("id"), row.get("name")):
            return row
    names = ", ".join(sorted(str(r.get("name")) for r in rows)) or "none"
    raise SystemExit(f"No environment matching {wanted!r}. Available: {names}")


def load_cursor(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cursor(path, cursor):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cursor, fh, indent=2)


def post(url, key, body, timeout=30):
    """POST one snapshot. Returns (status, parsed body or raw text)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            # A collector key, not a user session token: this runs unattended.
            "Authorization": "Bearer " + key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, _maybe_json(raw)
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read().decode("utf-8", "replace"))


def _maybe_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def build(rows, platform):
    """Oldest first, so a batch reads in the order things happened."""
    ordered = sorted(rows, key=lambda r: str(r.get("collected_at") or ""))
    return snapshot(
        ordered,
        batch_id=str(uuid.uuid4()),
        observed_at=_now(),
        platform=platform,
    )


def unsent(rows, since):
    if not since:
        return rows
    return [r for r in rows if str(r.get("collected_at") or "") > since]


def run_once(args, key):
    environment = resolve(args.source, args.environment)
    rows = measurements(args.source, environment["id"], args.limit)

    cursor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CURSOR_FILE)
    cursor = load_cursor(cursor_path)
    fresh = unsent(rows, cursor.get(environment["id"]))

    if not fresh:
        print(f"nothing new for {environment['name']}")
        return 0

    body = build(fresh, args.platform)

    if args.dry_run:
        print(json.dumps(body, indent=2))
        print(f"\n-- dry run: {len(body['entities'])} entities, not sent", file=sys.stderr)
        return 0

    url = args.url.rstrip("/") + INFRASTRUCTURE_PATH
    status, reply = post(url, key, body)
    print(f"{_now()}  POST {url} -> {status}  entities={len(body['entities'])}")

    if status in (200, 201, 202):
        # Only advance once it is actually accepted, so a failure re-sends
        # rather than losing the window.
        cursor[environment["id"]] = max(str(r.get("collected_at") or "") for r in fresh)
        save_cursor(cursor_path, cursor)
        return 0

    if status == 401:
        print(
            "  rejected: the key was refused. If it was revoked this is expected, "
            "and nothing was lost -- the measurements stay in the local server.",
            file=sys.stderr,
        )
        return 2

    print(f"  not accepted: {reply}", file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", default="http://localhost:8787",
                        help="local collector server to read from")
    parser.add_argument("--url", default=os.environ.get("LIGHTOS_APP_URL", ""),
                        help="published address to send to (or LIGHTOS_APP_URL)")
    parser.add_argument("--environment", required=True, help="environment id or name")
    parser.add_argument("--platform", default="linux")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the snapshot instead of sending it")
    args = parser.parse_args(argv)

    key = os.environ.get("LIGHTOS_COLLECTOR_KEY", "")
    if not args.dry_run:
        if not key:
            raise SystemExit("LIGHTOS_COLLECTOR_KEY is not set. Put it in an env file, not on the command line.")
        if not args.url:
            raise SystemExit("No target address. Pass --url or set LIGHTOS_APP_URL.")

    if args.once:
        return run_once(args, key)

    while True:
        try:
            run_once(args, key)
        except urllib.error.URLError as e:
            print(f"{_now()}  could not reach a server: {e.reason}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
