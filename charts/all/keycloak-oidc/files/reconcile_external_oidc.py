"""Reconcile configured external OIDC providers without replacing their aliases."""

import copy
import urllib.parse


def reconcile(call, realm, desired_providers, environ):
    """Apply managed settings and credentials, then converge group mappers."""
    desired_providers = copy.deepcopy(desired_providers)
    for desired in desired_providers:
        for field, env_key in (
            ("clientId", "clientIdEnv"),
            ("clientSecret", "clientSecretEnv"),
        ):
            if env_key in desired:
                value = environ.get(desired[env_key], "")
                if not value:
                    raise ValueError(f"Missing credential {desired[env_key]}")
                desired["config"][field] = value

    base_path = f"/admin/realms/{urllib.parse.quote(realm, safe='')}/identity-provider/instances"
    providers = call("GET", base_path)
    for desired in desired_providers:
        issuer = desired["issuerUrl"].rstrip("/")
        matches = [
            p
            for p in providers
            if p.get("config", {}).get("issuer", "").rstrip("/") == issuer
        ]
        if len(matches) > 1:
            raise ValueError(f"Multiple external IdPs configured for issuer {issuer}")
        provider = (
            matches[0]
            if matches
            else next((p for p in providers if p["alias"] == desired["alias"]), None)
        )
        updated = copy.deepcopy(provider) if provider else {"alias": desired["alias"]}
        updated.update(providerId="oidc", enabled=True)
        if "displayName" in desired:
            updated["displayName"] = desired["displayName"]
        updated.setdefault("config", {}).update(desired["config"])
        provider_path = base_path + "/" + urllib.parse.quote(updated["alias"], safe="")
        if provider is None:
            call("POST", base_path, data=updated)
            providers.append(updated)
            print(f"External IdP {updated['alias']} created.")
        elif updated != provider:
            call("PUT", provider_path, data=updated)
            print(f"External IdP {updated['alias']} reconciled.")
        else:
            print(f"External IdP {updated['alias']} already in the desired state.")

        if not desired["groups"]:
            continue
        mapper_path = provider_path + "/mappers"
        mappers = call("GET", mapper_path)
        for group in desired["groups"]:
            mapper = {
                "name": f"{group.lstrip('/')}-{updated['alias']}",
                "identityProviderAlias": updated["alias"],
                "identityProviderMapper": "oidc-hardcoded-group-idp-mapper",
                "config": {"group": "/" + group.lstrip("/"), "syncMode": "FORCE"},
            }
            existing = next(
                (
                    m
                    for m in mappers
                    if m.get("name") == mapper["name"]
                    or (
                        m.get("identityProviderMapper")
                        == mapper["identityProviderMapper"]
                        and m.get("config", {}).get("group")
                        == mapper["config"]["group"]
                    )
                ),
                None,
            )
            if existing is None:
                call("POST", mapper_path, data=mapper)
                mappers.append(mapper)
                print(f"Mapper {group} on IdP {updated['alias']} created.")
            elif any(existing.get(key) != value for key, value in mapper.items()):
                call(
                    "PUT",
                    mapper_path + "/" + urllib.parse.quote(existing["id"], safe=""),
                    data={**mapper, "id": existing["id"]},
                )
                print(f"Mapper {group} on IdP {updated['alias']} updated.")
