"""Fetches the Velocity "modern" forwarding secret from folia-nexa-mgmt.

Called by run-velocity.sh (bin/run-velocity.sh) on every proxy startup,
never as a standalone tool. Prints the secret to stdout on success and
nothing on any failure (unreachable mgmt, bad token, etc.) — the caller
treats empty output as "leave the existing forwarding.secret alone" per
run-velocity.sh's own comment.

Requires FOLIA_MGMT_URL and FOLIA_MGMT_API_TOKEN in the environment
(sourced from config.env by run-velocity.sh before this runs).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    mgmt_url = os.environ.get("FOLIA_MGMT_URL")
    api_token = os.environ.get("FOLIA_MGMT_API_TOKEN")
    if not mgmt_url or not api_token:
        return 1

    req = urllib.request.Request(
        mgmt_url.rstrip("/") + "/api/v1/routes/forwarding-secret",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            secret = json.load(resp)["secret"]
    except Exception:
        return 1

    print(secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())
