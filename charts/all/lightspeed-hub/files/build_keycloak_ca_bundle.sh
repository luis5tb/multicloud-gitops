#!/bin/sh
# Run in the adapter image so the public roots match the application image.
set -eu

fail() {
    echo "build-keycloak-ca-bundle: $*" >&2
    exit 1
}

: "${KEYCLOAK_CA_BUNDLE:?KEYCLOAK_CA_BUNDLE is required}"
: "${COMBINED_CA_BUNDLE:?COMBINED_CA_BUNDLE is required}"

public_bundle=${PUBLIC_CA_BUNDLE:-}
if [ -z "$public_bundle" ]; then
    for candidate in \
        /etc/pki/tls/certs/ca-bundle.crt \
        /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
        /etc/ssl/certs/ca-certificates.crt \
        /etc/pki/tls/cert.pem \
        /etc/ssl/cert.pem; do
        if [ -f "$candidate" ] && [ -s "$candidate" ]; then
            public_bundle=$candidate
            break
        fi
    done
fi

[ -n "$public_bundle" ] && [ -f "$public_bundle" ] && [ -s "$public_bundle" ] ||
    fail "no existing public/system CA bundle found; refusing private-CA-only trust"
[ -f "$KEYCLOAK_CA_BUNDLE" ] && [ -s "$KEYCLOAK_CA_BUNDLE" ] ||
    fail "mounted Keycloak CA bundle is missing or empty"

# Catch absent PEM data without adding an openssl/Python runtime dependency.
# Actual chain/signature/hostname verification remains with Go's TLS client.
for bundle in "$public_bundle" "$KEYCLOAK_CA_BUNDLE"; do
    grep -q -- '-----BEGIN CERTIFICATE-----' "$bundle" &&
        grep -q -- '-----END CERTIFICATE-----' "$bundle" ||
        fail "public or Keycloak CA bundle contains no complete PEM certificates"
done

# No pipeline: set -e must notice a failed read or write of either input.
# A newline between files also handles a source without a trailing newline.
umask 022
cat "$public_bundle" > "$COMBINED_CA_BUNDLE"
printf '\n' >> "$COMBINED_CA_BUNDLE"
cat "$KEYCLOAK_CA_BUNDLE" >> "$COMBINED_CA_BUNDLE"
