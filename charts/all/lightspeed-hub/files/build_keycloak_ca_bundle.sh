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

# The adapter image is intentionally minimal; do not assume grep or cat are
# installed. Actual chain/signature/hostname verification remains with Go's
# TLS client.
has_pem_markers() {
    file=$1
    has_begin=0
    has_end=0

    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *"-----BEGIN CERTIFICATE-----"*) has_begin=1 ;;
            *"-----END CERTIFICATE-----"*) has_end=1 ;;
        esac
    done < "$file"

    [ "$has_begin" -eq 1 ] && [ "$has_end" -eq 1 ]
}

for bundle in "$public_bundle" "$KEYCLOAK_CA_BUNDLE"; do
    has_pem_markers "$bundle" ||
        fail "public or Keycloak CA bundle contains no complete PEM certificates"
done

append_file() {
    file=$1
    while :; do
        if IFS= read -r line; then
            printf '%s\n' "$line"
        else
            if [ -n "$line" ]; then
                printf '%s' "$line"
            fi
            break
        fi
    done < "$file"
}

# Preserve each input exactly and insert a newline between them, including
# when the public bundle has no trailing newline.
umask 022
{
    append_file "$public_bundle"
    printf '\n'
    append_file "$KEYCLOAK_CA_BUNDLE"
} > "$COMBINED_CA_BUNDLE"
