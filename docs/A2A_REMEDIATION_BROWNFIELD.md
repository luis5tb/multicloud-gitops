# Remediation execution path: brownfield upgrade

**Greenfield deployments do not need this page**: follow the
[deployment runbook](lightspeed-hub-deployment-runbook.md), where one
`values-standalone.yaml` and the realm import create everything.

Use this only for an environment deployed **before** the execution path
existed (such as the current standalone cluster). Two things differ there:

1. **The realm was imported once and is never re-imported**, so the new
   Keycloak client, roles, group, scopes and mappers must be added to the
   live realm through the Admin API before the values that depend on them
   sync (the keycloak-oidc PostSync hook fails on a missing client).
2. **ACME's token-debug feature may have been on**, so tokens may have been
   copied. ACME's service account must join the remediation group only
   after debug is off and copied tokens have expired.

Both are handled by `scripts/remediation/keycloak_execution_brownfield.py`
(stdlib, idempotent; `--dry-run` shows the plan). Its defaults mirror
`variants/standalone/values-standalone.yaml`; pass different flags if your
values differ. It reads the master-realm admin credentials in memory from
the `keycloak-admin-user` Secret via `oc` and trusts the hub ingress CA.

## Steps

```bash
export KC_URL=https://keycloak.apps.bm-cluster.e2e.bos.redhat.com   # your hub
cd ~/git_repos/multicloud-gitops
```

1. **Build and push the images** exactly as in runbook A1 (all four with the
   tag pinned in the standalone file; pin a tested sandbox digest).
2. **Vault:** add the `praxis-spoke-gateway-exec` entry from
   `values-secret.yaml.template` to your existing local secrets file (do not
   regenerate other entries) and run `./pattern.sh make load-secrets`.
3. **Prepare the live realm (no membership yet):**

   ```bash
   python3 scripts/remediation/keycloak_execution_brownfield.py --keycloak-url "$KC_URL" --dry-run
   python3 scripts/remediation/keycloak_execution_brownfield.py --keycloak-url "$KC_URL"
   ```

4. **Push the committed standalone values** (one push: new images, ACME
   token debug/override off, execution exchanger, execution gateways, Hub
   execution path). Wait for every Application to sync, then confirm ACME
   rolled out with debug off:

   ```bash
   oc -n acme-agent rollout status deployment/acme-agent --timeout=10m
   oc -n acme-agent get deploy acme-agent \
     -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
     | grep -E '^(AUTH_TOKEN_DEBUG_ENABLED|ACME_ALLOW_TOKEN_OVERRIDE)='   # both =false
   ```

   The Hub path is enabled now, but nobody can approve yet: ACME's account
   is not in the remediation group.
5. **Wait at least 30 minutes** (longer than ACME's Token A and the 1800 s
   run token), then grant the shared principal and verify the claims:

   ```bash
   python3 scripts/remediation/keycloak_execution_brownfield.py --keycloak-url "$KC_URL" --grant-acme-membership
   python3 scripts/remediation/keycloak_execution_brownfield.py --keycloak-url "$KC_URL" --verify
   ```

6. **Continue with the runbook** from A4 (d) (prepare the spoke writer
   RBAC, do not apply it yet), A6 (checks), B1 (check or re-save the MAS
   blueprint with `hitl_mode: ask`), and B3 (token gate, spoke writer
   RBAC, reject/approve tests).

## Rollback

Remove the spoke writer RBAC and ACME's membership in
`acme-remediation-mario-ipi` (Admin Console, or the Admin API), then disable
the execution keys in all three charts together in
`values-standalone.yaml` and push. The other Keycloak objects created in
step 3 are harmless without the membership.
