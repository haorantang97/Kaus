"""Hook command a native CLI runs to tell Kaus it is working, waiting or done.

Stdlib only. Reads the hook payload from stdin, posts ``{event, message}`` to the
launch-scoped Kaus endpoint with the launch token, prints nothing and always
exits 0: a status report must never block, approve or deny anything in the agent.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

URL_ENV = "KAUS_STATUS_URL"
TOKEN_ENV = "KAUS_STATUS_TOKEN"
TOKEN_HEADER = "X-Kaus-Status-Token"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    url, token = os.environ.get(URL_ENV), os.environ.get(TOKEN_ENV)
    if not args or not url or not token:
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    message = payload.get("message") if isinstance(payload, dict) else None
    body = json.dumps({"event": args[0], "message": message if isinstance(message, str) else None}).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", TOKEN_HEADER: token})
    try:
        urllib.request.urlopen(request, timeout=2).close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
