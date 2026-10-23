# Multi-OIDC Configuration Guide

This guide explains how to configure the Validated Pattern to support tokens from multiple OIDC issuers (Red Hat SSO Production, Red Hat SSO Stage, and the local Keycloak).

## Architecture Overview

```
┌─────────────────┐
│  ACME Agent     │  Token from Red Hat SSO/Stage/Keycloak
│  (or other)     │
└────────┬────────┘
         │ Token A (from any issuer)
         ▼
┌─────────────────┐
│  Praxis Proxy   │  Validates token against multiple trusted issuers
└────────┬────────┘
         │ Forwards Token A
         ▼
┌─────────────────┐
│  OLS Agent      │  1. Validates Token A (multi-issuer OIDC)
│  (a2a-split)    │  2. Exchanges Token A → Token B (via Keycloak)
└────────┬────────┘
         │ Token B (always from Keycloak, scoped to openshift-mcp)
         ▼
┌─────────────────┐
│  OpenShift MCP  │  Forwards Token B to API server
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  API Server     │  Validates Token B, applies RBAC
└─────────────────┘
```

### Key Points

1. **OLS validates** tokens from multiple issuers (Red Hat SSO prod, Red Hat SSO stage, Keycloak)
2. **Token exchange happens ONLY against Keycloak** (the local instance deployed by the pattern)
3. **Keycloak trusts** the external issuers via Identity Provider configuration
4. **Praxis validates** tokens from all configured issuers (optional - can be disabled to let OLS handle all validation)
5. **ACME requests `openid api.ask_red_hat`** from Red Hat SSO so Keycloak 26.4 can call the issuer's userinfo endpoint during token exchange

## Configuration Steps

### 1. Configure Praxis for Multi-Issuer (Optional)

In `variants/standalone/values-standalone.yaml`, under the `praxis-proxy` application overrides:

```yaml
praxis-proxy:
  identity:
    # Multi-issuer configuration for Praxis
    oidcIssuers:
      - name: red-hat-sso-prod
        issuerUrl: https://sso.redhat.com/auth/realms/redhat-external
        jwksUrl: https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs
        audience: openshift-lightspeed
      - name: red-hat-sso-stage
        issuerUrl: https://sso.stage.redhat.com/auth/realms/redhat-external
        jwksUrl: https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs
        audience: openshift-lightspeed
```

**Note:** When `oidcIssuers` is set, the local Keycloak issuer is automatically included. If you want Praxis to skip validation and let OLS handle everything, leave `oidcIssuers` empty.

### 2. Configure OLS for Multi-Issuer

In `variants/standalone/values-standalone.yaml`, under the `openshift-lightspeed-config` application:

```yaml
openshift-lightspeed-config:
  appServerPatch:
    a2a:
      # Multi-issuer OIDC configuration
      oidcIssuers:
        - issuer_url: https://sso.redhat.com/auth/realms/redhat-external
          audience: openshift-lightspeed
          username_claim: preferred_username  # or 'email' depending on SSO config
          jwks_cache_seconds: 300
        - issuer_url: https://sso.stage.redhat.com/auth/realms/redhat-external
          audience: openshift-lightspeed
          username_claim: preferred_username
          jwks_cache_seconds: 300
```

**Note:** The local Keycloak issuer is automatically appended to this list.

### 3. Configure MAS A2A for Multi-Issuer (Optional)

MAS A2A can accept tokens from multiple issuers directly, without requiring token exchange. When `identity.oidcIssuers` is set, MAS validates tokens from any configured issuer.

In `variants/standalone/values-standalone.yaml`, under the `mao-a2a` application:

```yaml
mao-a2a:
  identity:
    # Multi-issuer OIDC: accept tokens from Red Hat SSO prod, stage, and Keycloak
    # When oidcIssuers is set, providerMode is ignored
    oidcIssuers:
      - issuer_url: https://sso.redhat.com/auth/realms/redhat-external
        jwks_url: https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs
        audience: openshift-lightspeed
        username_claim: preferred_username
      - issuer_url: https://sso.stage.redhat.com/auth/realms/redhat-external
        jwks_url: https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs
        audience: openshift-lightspeed
        username_claim: preferred_username
      # IMPORTANT: Keycloak is NOT auto-appended; include it explicitly
      - issuer_url: https://keycloak.apps.<cluster>/realms/rca
        jwks_url: https://keycloak.apps.<cluster>/realms/rca/protocol/openid-connect/certs
        audience: mas-a2a
        username_claim: preferred_username
    oidcVerifySsl: true
    oidcTimeout: 30
```

**Note:** Unlike Praxis and OLS, MAS A2A does **not** automatically append the local Keycloak issuer. If you want to accept Keycloak tokens, add it explicitly to `oidcIssuers`.

**Building the image:** Multi-issuer support requires code changes in the `uie-mas-hosted` repository (see "MAS A2A Code Changes" section below). Build and push a custom image, then set the tag in the values above.

### 4. Configure Keycloak to Trust External Issuers (REQUIRED for token exchange)

**This is critical**: When OLS exchanges a Red Hat SSO token for a Keycloak token (RFC 8693), Keycloak must validate the incoming `subject_token`. Without this configuration, token exchange will fail with "unknown issuer".

In `variants/standalone/values-standalone.yaml`, under the `keycloak-oidc` application:

```yaml
keycloak-oidc:
  keycloak:
    # External OIDC providers that Keycloak trusts for token exchange.
    # When OLS calls Keycloak's token endpoint with a Red Hat SSO token as
    # subject_token, Keycloak validates it against these providers before
    # issuing Token B.
    externalOidcProviders:
      - alias: red-hat-sso-prod
        displayName: "Red Hat SSO Production"
        issuerUrl: https://sso.redhat.com/auth/realms/redhat-external
        jwksUrl: https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs
        tokenIntrospectionUrl: https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect
        userInfoUrl: https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo
        clientId: <red-hat-sso-client-id>
        isAccessTokenJWT: "true"
        allowTokenExchange: true
      - alias: red-hat-sso-stage
        displayName: "Red Hat SSO Stage"
        issuerUrl: https://sso.stage.redhat.com/auth/realms/redhat-external
        jwksUrl: https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs
        tokenIntrospectionUrl: https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect
        userInfoUrl: https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo
        clientId: <red-hat-sso-client-id>
        isAccessTokenJWT: "true"
        allowTokenExchange: true
```

This configuration tells Keycloak to:
- Trust tokens from these external issuers **when used as subject_token in RFC 8693 token exchange**
- Validate their signatures using the provided JWKS URLs
- Validate the subject token via introspection and userinfo on Keycloak 26.4
- Allow the exchange to proceed (issue Token B with Keycloak as issuer)

**Note**: This is separate from browser-based "Login with Red Hat SSO" federation. This configuration is purely for validating externally-issued tokens during programmatic token exchange.

## How It Works

### Token Validation Flow

1. **User obtains token from Red Hat SSO** (production or stage)
2. **User calls OLS A2A endpoint** with `Authorization: Bearer <sso-token>`
3. **Praxis validates token** (if `oidcIssuers` configured) against all trusted issuers
4. **OLS validates token** using multi-issuer OIDC configuration
5. **OLS performs token exchange:**
   ```
   POST https://keycloak.../realms/rca/protocol/openid-connect/token
   grant_type=urn:ietf:params:oauth:grant-type:token-exchange
   subject_token=<sso-token>
   client_assertion=<ols-spiffe-jwt-svid>
   audience=openshift-mcp
   ```
6. **Keycloak validates the subject_token**:
   - Checks it against configured `externalOidcProviders`
   - Verifies signature using the external issuer's JWKS
   - Issues Token B with `sub` from the SSO token, `azp=lightspeed-mcp`
7. **OLS uses Token B** to call OpenShift MCP
8. **API server validates Token B** and applies RBAC

### Important Notes

- **The `audience` field**: All external tokens must have `audience: openshift-lightspeed` (or whatever you configure). This must match across:
  - Praxis `oidcIssuers[].audience`
  - OLS `oidcIssuers[].audience`
  - The actual token from Red Hat SSO

- **Token exchange always goes through Keycloak**: Even though OLS validates tokens from multiple issuers, the RFC 8693 exchange always happens against the local Keycloak instance (`A2A_KEYCLOAK_ISSUER_URL`).

- **Keycloak must trust the external issuer**: This is configured via `externalOidcProviders` in the Keycloak realm.

## Testing

1. **Obtain a token from Red Hat SSO**:
   ```bash
   TOKEN=$(curl -X POST "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token" \
     -d "client_id=<your-client>" \
     -d "client_secret=<your-secret>" \
     -d "grant_type=client_credentials" \
     | jq -r '.access_token')
   ```

2. **Decode and verify the token**:
   ```bash
   echo $TOKEN | cut -d. -f2 | base64 -d | jq .
   # Verify: iss, aud, azp
   ```

3. **Call OLS A2A endpoint**:
   ```bash
   curl -X POST "https://openshift-lightspeed.apps.<cluster>/a2a" \
     -H "Authorization: Bearer $TOKEN" \
     -H "X-OLS-Cluster: <cluster-id>" \
     -H "Content-Type: application/json" \
     -d '{
       "jsonrpc": "2.0",
       "method": "agent/sendMessage",
       "params": {
         "task": {...},
         "message": {...}
       },
       "id": 1
     }'
   ```

4. **Check logs**:
   ```bash
   # OLS validation and exchange
   oc logs -n a2a-lightspeed -l app.kubernetes.io/component=application-server | grep a2a_audit

   # Praxis policy validation
   oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy | grep jwt
   ```

## Troubleshooting

### OLS rejects token with "Invalid issuer"
- Verify `oidcIssuers` includes the issuer URL exactly as it appears in the token's `iss` claim
- Check OLS logs for the actual issuer it received

### Keycloak token exchange fails with 401 or "Invalid token issuer"
- **Most common cause**: `keycloak.externalOidcProviders` not configured in `values-standalone.yaml`
- Verify the external issuer is in `externalOidcProviders` (see step 3 above)
- Check Keycloak Admin Console → Identity Providers to see if they were created
  - Should see "red-hat-sso-prod" and "red-hat-sso-stage" providers
- Verify `allowTokenExchange: true` is set for each provider
- Check OLS logs for the actual exchange error:
  ```bash
  oc logs -n a2a-lightspeed -l app.kubernetes.io/component=application-server | grep token_exchange
  ```

### Praxis denies request
- If using `oidcIssuers` in Praxis config, verify the issuer URL and audience match the token
- Check Praxis logs for policy evaluation details
- Verify `claim.azp` is in `policy.allowedCallers`

### MCP/API server rejects token
- This is Token B, not the original SSO token
- Verify the `groups` claim survived the exchange (should be `keycloak:acme-agent-rca`)
- Check `oc auth can-i get pods --as-group=keycloak:acme-agent-rca -n <namespace>`

## Reference: Environment Variables (OLS patch_appserver.py)

The multi-issuer configuration is passed to the patch Job via:

- **`A2A_OIDC_ISSUERS_JSON`**: JSON array of issuer configs (when multi-issuer mode is enabled)
  ```json
  [
    {
      "issuer_url": "https://sso.redhat.com/auth/realms/redhat-external",
      "audience": "openshift-lightspeed",
      "username_claim": "preferred_username",
      "jwks_cache_seconds": 300
    },
    {
      "issuer_url": "https://keycloak.apps.<cluster>/realms/rca",
      "audience": "lightspeed-a2a",
      "username_claim": "preferred_username"
    }
  ]
  ```

- **`A2A_KEYCLOAK_ISSUER_URL`**: Always points to the local Keycloak (for token exchange)

## MAS A2A Code Changes

To enable multi-issuer OIDC support in MAS A2A, apply these changes to the `uie-mas-hosted` repository (branch `a2a-inbound-support`):

### New Files
- `adapters/outbound/auth/multi_issuer_validator.py` - Multi-issuer token validator
- `tests/unit/adapters/test_multi_issuer_validator.py` - Unit tests

### Modified Files

**`config/app_config.py`** - Add multi-issuer configuration fields:
```python
# Multi-issuer OIDC mode
multi_issuer_oidc_issuers_json: str = ""
multi_issuer_oidc_verify_ssl: bool = True
multi_issuer_oidc_timeout: int = 30
```

**`bootstrap/container.py`** - Update `_build_token_validator()` to check for multi-issuer mode before single-issuer modes (see the implementation in the uie-mas-hosted repository).

After making these changes:
```bash
cd /home/ltomasbo/git_repos/uie-mas-hosted
podman build -t quay.io/ltomasbo/mas-api:$(git rev-parse --short HEAD) .
podman push quay.io/ltomasbo/mas-api:$(git rev-parse --short HEAD)
```

Update the image tag in `variants/standalone/values-standalone.yaml`:
```yaml
- name: image.tag
  value: "<git-short-sha-with-multi-issuer>"
```

## Limitations

- **One-shot realm import**: Adding `externalOidcProviders` after the realm already exists requires manual configuration in Keycloak Admin Console or a realm re-import
- **Token exchange direction**: Only external → Keycloak exchange is supported (not Keycloak → external)
- **No `act` claim**: Standard Keycloak V2 token exchange doesn't populate RFC 8693 `act` (actor) claim; audit trail is in logs only
- **MAS A2A custom image**: Multi-issuer support for MAS A2A requires a custom-built image from the `uie-mas-hosted` repository with the changes described above
