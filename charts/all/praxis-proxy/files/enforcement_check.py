#!/usr/bin/env python3
"""Allow-list enforcement check for the Praxis caller allow-list.

Runs as a PostSync hook Job (see templates/enforcement-check-job.yaml).
Guards against a silent fail-open regression: the pinned
ghcr.io/praxis-proxy/ai image has admitted every validly-signed token before
(see charts/all/praxis-proxy/files/policy.yaml's long comments and AGENTS.md),
so this check asserts the proxy DENIES a caller whose azp is not in the
allow-list -- a regression on an image bump fails the sync instead of going
unnoticed.

Two tiers, by what credentials are available:

  * ALWAYS (no credentials needed): an unauthenticated request and a
    bad-token request to a protected path must be rejected (401/403) and must
    NOT be forwarded to the backend (2xx). This proves the authentication step
    is wired at all. It does NOT exercise the historically dangerous
    fail-open (a *validly-signed* token with a non-allow-listed `azp` being
    forwarded), because the jwt-client step rejects an unsigned/garbage token
    before the allow-list predicate ever runs.

  * STRONG (only when MINT_CLIENT_ID + a client secret are provided): mint a
    real, validly-signed Keycloak token via client_credentials for a client
    whose `azp` is NOT in policy.allowedCallers, then assert Praxis denies it
    (401/403). This is the check that actually catches the allow-list
    fail-open. It is opt-in because it needs a non-allow-listed confidential
    client's credentials, which this chart has no self-contained source for.

Any detected fail-open exits non-zero, failing the Job (and thus the sync).
Uses only the Python standard library.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PRAXIS_URL = os.environ["PRAXIS_URL"].rstrip("/")
PROTECTED_PATH = os.environ.get("PROTECTED_PATH", "/")
TIMEOUT = float(os.environ.get("TIMEOUT_SECONDS", "30"))

TOKEN_URL = os.environ.get("KEYCLOAK_TOKEN_URL", "").strip()
MINT_CLIENT_ID = os.environ.get("MINT_CLIENT_ID", "").strip()
MINT_CLIENT_SECRET = os.environ.get("MINT_CLIENT_SECRET", "").strip()
CA_BUNDLE_FILE = os.environ.get("CA_BUNDLE_FILE", "").strip()

DENIED = {401, 403}


def _ssl_context():
    # Extend the default trust store, never replace it (AGENTS.md hard-won
    # lesson): a public-CA-signed Keycloak Route must still verify when no
    # internal bundle is supplied.
    ctx = ssl.create_default_context()
    if CA_BUNDLE_FILE and os.path.exists(CA_BUNDLE_FILE):
        ctx.load_verify_locations(cafile=CA_BUNDLE_FILE)
    return ctx


def _request(url, headers=None, data=None, method="GET", ctx=None):
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _assert_denied(label, status):
    if status in DENIED:
        print(f"  PASS: {label} -> {status} (denied)")
        return True
    if 200 <= status < 300:
        print(
            f"  FAIL-OPEN: {label} -> {status}: request was FORWARDED to the "
            f"backend. The allow-list is not enforcing; see "
            f"charts/all/praxis-proxy/files/policy.yaml."
        )
        return False
    # 404/5xx etc.: not a 2xx forward, but not a clean auth denial either --
    # treat as a failure so a misrouted/misconfigured proxy is not mistaken
    # for a passing test.
    print(
        f"  FAIL: {label} -> {status}: expected 401/403, got neither a denial "
        f"nor a 2xx. Investigate before trusting this result."
    )
    return False


def main():
    ctx = _ssl_context()
    target = f"{PRAXIS_URL}{PROTECTED_PATH}"
    print(f"Enforcement check against protected path: {target}")

    ok = True
    status, _ = _request(target)
    ok &= _assert_denied("unauthenticated request", status)

    status, _ = _request(target, headers={"Authorization": "Bearer not.a.valid.jwt"})
    ok &= _assert_denied("bad-token request", status)

    if MINT_CLIENT_ID and MINT_CLIENT_SECRET and TOKEN_URL:
        print(f"Minting a validly-signed token for non-allow-listed client "
              f"'{MINT_CLIENT_ID}' at {TOKEN_URL}")
        form = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": MINT_CLIENT_ID,
            "client_secret": MINT_CLIENT_SECRET,
        }).encode()
        status, body = _request(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=form,
            method="POST",
            ctx=ctx,
        )
        if status != 200:
            print(f"  ERROR: could not mint token (HTTP {status}): {body[:200]!r}. "
                  f"Check MINT_CLIENT_ID/secret and the realm; failing so a "
                  f"broken strong-test setup is not silently skipped.")
            ok = False
        else:
            token = json.loads(body)["access_token"]
            status, _ = _request(
                target, headers={"Authorization": f"Bearer {token}"}
            )
            ok &= _assert_denied(
                f"validly-signed token, azp='{MINT_CLIENT_ID}' (not allow-listed)",
                status,
            )
    else:
        print(
            "Strong check SKIPPED: no MINT_CLIENT_ID/secret provided, so this "
            "run does not exercise the validly-signed-but-wrong-azp fail-open "
            "(the historically dangerous one). Set "
            "policy.enforcementTest.minter.* to enable it. See chart README."
        )

    if not ok:
        print("ENFORCEMENT CHECK FAILED: Praxis did not deny an unauthorized request.")
        sys.exit(1)
    print("ENFORCEMENT CHECK PASSED.")


if __name__ == "__main__":
    main()
