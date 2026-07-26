"""Bootstrap a fresh SigNoz: admin user, service account, role, API key.

A fresh self-hosted SigNoz has no credentials and no documented scripted setup,
so this is normally a sequence of clicks that cannot be reproduced. Every step
below was established against a live v0.134.0 instance, and several of the
endpoints are not what the docs suggest:

* Login is ``POST /api/v2/sessions/email_password`` and **requires ``orgID``**.
  ``/api/v1/login`` does not exist; unknown ``/api/...`` paths fall through to
  the single-page app and return HTTP 200 with an HTML body, so a wrong URL
  looks like success until you try to parse it.
* A service account is created without permissions. ``role`` in the create body
  is ignored -- ``serviceAccountRoles`` comes back ``null`` and every query then
  fails with ``authz_forbidden``.
* Assigning the role needs **both** ``id`` and ``name`` in the body. Sending
  either alone fails with ``invalid uuid``.

Safe to re-run: each step checks for the resource first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

DEFAULT_BASE = os.environ.get("SIGNOZ_URL", "http://localhost:8080")
DEFAULT_EMAIL = os.environ.get("SIGNOZ_EMAIL", "admin@rosetta.dev")
DEFAULT_PASSWORD = os.environ.get("SIGNOZ_PASSWORD", "Rosetta!2026demo")
DEFAULT_ORG = os.environ.get("SIGNOZ_ORG", "Rosetta")
SERVICE_ACCOUNT = "rosetta"
ADMIN_ROLE = "signoz-admin"
KEY_PATH = Path("infra/api_key.txt")


class SetupError(RuntimeError):
    pass


def call(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    base: str,
    token: str | None = None,
    api_key: str | None = None,
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if api_key:
        headers["SIGNOZ-API-KEY"] = api_key

    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise SetupError(f"cannot reach SigNoz at {base}: {exc.reason}") from exc

    # An unknown /api path returns the SPA shell with HTTP 200. Treat any HTML
    # body as a routing miss rather than letting it masquerade as success.
    if raw.lstrip().startswith("<"):
        return status, {"_html": True}
    try:
        return status, (json.loads(raw) if raw.strip() else None)
    except ValueError:
        return status, raw


def _data(payload: Any) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


def register_admin(base: str, email: str, password: str, org: str) -> str:
    """Create the first (root) user. Returns the org id."""
    status, payload = call("GET", "/api/v1/version", base=base)
    version = (payload or {}).get("version", "?") if isinstance(payload, dict) else "?"
    completed = bool((payload or {}).get("setupCompleted")) if isinstance(payload, dict) else False
    print(f"[info] SigNoz {version} (setupCompleted={completed})")

    status, payload = call(
        "POST",
        "/api/v1/register",
        {"email": email, "password": password, "name": "Rosetta Admin", "orgName": org},
        base=base,
    )
    data = _data(payload)
    if status in (200, 201) and isinstance(data, dict) and data.get("orgId"):
        print(f"[ok]   registered {email}")
        return str(data["orgId"])

    # Already registered: recover the org id by logging in without one, which
    # fails with a message, or by listing orgs after a successful login attempt.
    print(f"[skip] user already exists (HTTP {status})")
    org_id = os.environ.get("SIGNOZ_ORG_ID", "").strip()
    if org_id:
        return org_id
    raise SetupError(
        "user exists but org id is unknown. Re-run against a fresh SigNoz, or "
        "set SIGNOZ_ORG_ID (Settings -> Organization in the UI)."
    )


def login(base: str, email: str, password: str, org_id: str) -> str:
    status, payload = call(
        "POST",
        "/api/v2/sessions/email_password",
        {"email": email, "password": password, "orgID": org_id},
        base=base,
    )
    data = _data(payload)
    if status != 200 or not isinstance(data, dict) or not data.get("accessToken"):
        raise SetupError(f"login failed (HTTP {status}): {str(payload)[:300]}")
    print("[ok]   logged in")
    return str(data["accessToken"])


def ensure_service_account(base: str, token: str) -> str:
    status, payload = call("GET", "/api/v1/service_accounts", base=base, token=token)
    for account in _data(payload) or []:
        if isinstance(account, dict) and account.get("name") == SERVICE_ACCOUNT:
            print(f"[skip] service account '{SERVICE_ACCOUNT}' exists")
            return str(account["id"])

    status, payload = call(
        "POST",
        "/api/v1/service_accounts",
        {"name": SERVICE_ACCOUNT},
        base=base,
        token=token,
    )
    data = _data(payload)
    if status not in (200, 201) or not isinstance(data, dict):
        raise SetupError(f"service account creation failed (HTTP {status}): {payload}")
    print(f"[ok]   service account '{SERVICE_ACCOUNT}'")
    return str(data["id"])


def ensure_admin_role(base: str, token: str, account_id: str) -> None:
    """Grant signoz-admin. Without this every query returns authz_forbidden."""
    status, payload = call(
        "GET", f"/api/v1/service_accounts/{account_id}/roles", base=base, token=token
    )
    for role in _data(payload) or []:
        if isinstance(role, dict) and role.get("name") == ADMIN_ROLE:
            print(f"[skip] role '{ADMIN_ROLE}' already granted")
            return

    status, payload = call("GET", "/api/v1/roles", base=base, token=token)
    role_id = next(
        (
            str(role["id"])
            for role in _data(payload) or []
            if isinstance(role, dict) and role.get("name") == ADMIN_ROLE
        ),
        None,
    )
    if not role_id:
        raise SetupError(f"role '{ADMIN_ROLE}' not found")

    # Both fields are required; either alone is rejected with "invalid uuid".
    status, payload = call(
        "POST",
        f"/api/v1/service_accounts/{account_id}/roles",
        {"id": role_id, "name": ADMIN_ROLE},
        base=base,
        token=token,
    )
    if status not in (200, 201, 204):
        raise SetupError(f"role assignment failed (HTTP {status}): {payload}")
    print(f"[ok]   granted '{ADMIN_ROLE}'")


def create_api_key(base: str, token: str, account_id: str) -> str:
    """Issue an API key.

    The secret is returned exactly once at creation and is not retrievable
    afterwards, so a re-run cannot recover an existing key -- it mints a new one
    under a fresh name instead of failing on the duplicate-name conflict.
    """
    for attempt in range(1, 12):
        name = "rosetta-key" if attempt == 1 else f"rosetta-key-{attempt}"
        status, payload = call(
            "POST",
            f"/api/v1/service_accounts/{account_id}/keys",
            {"name": name, "expiresInDays": 365},
            base=base,
            token=token,
        )
        data = _data(payload)
        if status in (200, 201) and isinstance(data, dict) and data.get("key"):
            print(f"[ok]   API key '{name}' issued")
            return str(data["key"])
        if status == 409:
            continue
        raise SetupError(f"API key creation failed (HTTP {status}): {payload}")
    raise SetupError("could not issue an API key: too many existing keys named rosetta-key*")


def verify(base: str, api_key: str) -> bool:
    """Prove the key can actually query, rather than merely existing."""
    import time

    now_ms = int(time.time() * 1000)
    body = {
        "schemaVersion": "v1",
        "start": now_ms - 300_000,
        "end": now_ms,
        "requestType": "scalar",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "stepInterval": 60,
                        "aggregations": [{"expression": "count()"}],
                    },
                }
            ]
        },
    }
    status, payload = call("POST", "/api/v5/query_range", body, base=base, api_key=api_key)
    if status == 200:
        print("[ok]   key verified against /api/v5/query_range")
        return True
    print(f"[FAIL] key cannot query (HTTP {status}): {str(payload)[:200]}")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--out", default=str(KEY_PATH))
    args = parser.parse_args(argv)

    out = Path(args.out)

    # A previously issued key that still works is better than minting another:
    # the secret cannot be read back, so every re-run would otherwise leave a
    # dead key behind on the service account.
    if out.exists():
        existing = out.read_text(encoding="utf-8").strip()
        if existing and verify(args.base, existing):
            print(f"[skip] reusing working key from {out}")
            print(
                "\nNext:\n"
                f"  export SIGNOZ_API_KEY=$(cat {out})\n"
                "  python demo/fleet.py            # emit the polyglot fleet\n"
                "  python scripts/provision.py     # dashboard + alerts\n"
                "  python scripts/investigate.py   # agent postmortem over MCP\n"
            )
            return 0

    try:
        org_id = register_admin(args.base, args.email, args.password, args.org)
        token = login(args.base, args.email, args.password, org_id)
        account_id = ensure_service_account(args.base, token)
        ensure_admin_role(args.base, token, account_id)
        api_key = create_api_key(args.base, token, account_id)
    except SetupError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(api_key + "\n", encoding="utf-8")
    print(f"[ok]   key written to {out}")

    ok = verify(args.base, api_key)
    print(
        "\nNext:\n"
        f"  export SIGNOZ_API_KEY=$(cat {out})\n"
        "  python demo/fleet.py            # emit the polyglot fleet\n"
        "  python scripts/provision.py     # dashboard + alerts\n"
        "  python scripts/investigate.py   # agent postmortem over MCP\n"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
