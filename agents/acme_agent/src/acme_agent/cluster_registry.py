"""OpenShift cluster API URL canonicalization, allow-list validation, and
``X-OLS-Cluster`` id derivation.

This module is the server-side enforcement described in the Lightspeed
migration plan: ACME's system prompt (see ``agent.py``'s instruction) asks a
user for the target OpenShift cluster API URL, but a prompt alone cannot be
trusted to keep the model from guessing, defaulting, or inferring a cluster
from a resource name. Every inbound message is independently validated here,
against a GitOps-configured allow-list, before any downstream A2A RPC is
permitted to fire (see ``routing.py``).

The id-derivation algorithm matches the frozen interface decision in
LIGHTSPEED_DESIGN.md / LIGHTSPEED_IMPLEMENTATION_PLAN.md: canonicalize
the URL (https only; lowercase/IDNA host; explicit port; no credentials,
path, query, or fragment), then derive the id as lowercase ``host-port`` with
``.``/``:`` replaced by ``-``; if that exceeds 63 characters or collides with
an already-known id, truncate and append a short stable SHA-256 suffix of the
canonical URL. Example::

    https://api.bm-cluster.e2e.bos.redhat.com:6443
      -> api-bm-cluster-e2e-bos-redhat-com-6443
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# DNS label / Kubernetes name length cap the derived id must respect.
MAX_CLUSTER_ID_LENGTH = 63
# Hex characters of the SHA-256 digest appended when truncating or
# disambiguating a collision. Short, but long enough that two different
# canonical URLs truncating to the same prefix essentially never collide.
_HASH_SUFFIX_LENGTH = 10

_DEFAULT_HTTPS_PORT = 443

_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+")
_TRAILING_PUNCTUATION = ").,;:!?'\""


class ClusterURLError(ValueError):
    """Base class for any rejected cluster URL/id. Fail closed on this."""


class ClusterURLMissingError(ClusterURLError):
    """Raised when no candidate cluster URL is present in the request."""


class ClusterURLAmbiguousError(ClusterURLError):
    """Raised when more than one distinct candidate cluster URL is present."""


def canonicalize_api_url(raw_url: str) -> str:
    """Canonicalize an OpenShift API URL per the frozen interface algorithm.

    Accepts only an ``https`` URL with no userinfo, path (other than an
    empty/root path), query string, or fragment. Returns
    ``"https://<lowercase-idna-host>:<port>"`` with an explicit port (443
    when the input omits one).

    Raises:
        ClusterURLError: if the URL is malformed or violates any of the
            above constraints.
    """

    if not isinstance(raw_url, str):
        raise ClusterURLError("cluster API URL must be a string")
    candidate = raw_url.strip()
    if not candidate:
        raise ClusterURLError("cluster API URL must not be empty")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise ClusterURLError(f"cluster API URL could not be parsed: {exc}") from exc

    if parts.scheme.lower() != "https":
        raise ClusterURLError("cluster API URL must use https")
    if parts.username is not None or parts.password is not None:
        raise ClusterURLError("cluster API URL must not contain credentials")
    if parts.path not in ("", "/"):
        raise ClusterURLError("cluster API URL must not contain a path")
    if parts.query:
        raise ClusterURLError("cluster API URL must not contain a query string")
    if parts.fragment:
        raise ClusterURLError("cluster API URL must not contain a fragment")

    try:
        hostname = parts.hostname
    except ValueError as exc:
        raise ClusterURLError(f"cluster API URL has an invalid host: {exc}") from exc
    if not hostname:
        raise ClusterURLError("cluster API URL must contain a host")

    try:
        host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ClusterURLError(
            "cluster API URL host is not a valid IDNA hostname"
        ) from exc
    if not host:
        raise ClusterURLError("cluster API URL must contain a host")

    try:
        port = parts.port
    except ValueError as exc:
        raise ClusterURLError(f"cluster API URL has an invalid port: {exc}") from exc
    if port is None:
        port = _DEFAULT_HTTPS_PORT

    return f"https://{host}:{port}"


def derive_cluster_id(
    canonical_url: str, *, known_ids: frozenset[str] = frozenset()
) -> str:
    """Derive the DNS-safe ``X-OLS-Cluster`` id for a canonicalized URL.

    ``canonical_url`` must already be the output of ``canonicalize_api_url``.
    The base id is ``lower(host):port`` with ``.``/``:`` replaced by ``-``.
    When that exceeds ``MAX_CLUSTER_ID_LENGTH`` characters, or collides with
    an id already present in ``known_ids``, the base is truncated and a
    short stable SHA-256 suffix of ``canonical_url`` is appended instead so
    the result stays unique and within the length cap.
    """

    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").lower()
    port = parts.port or _DEFAULT_HTTPS_PORT
    base = f"{host}:{port}".replace(".", "-").replace(":", "-")

    if len(base) <= MAX_CLUSTER_ID_LENGTH and base not in known_ids:
        return base

    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[
        :_HASH_SUFFIX_LENGTH
    ]
    suffix = f"-{digest}"
    truncated = base[: max(MAX_CLUSTER_ID_LENGTH - len(suffix), 0)]
    candidate = f"{truncated}{suffix}"

    # An adversarial/unlucky base could still collide post-truncation; keep
    # extending the digest window deterministically rather than ever return
    # a non-unique id silently.
    extra = 0
    while candidate in known_ids:
        extra += 1
        digest = hashlib.sha256(
            f"{canonical_url}#{extra}".encode("utf-8")
        ).hexdigest()[:_HASH_SUFFIX_LENGTH]
        suffix = f"-{digest}"
        truncated = base[: max(MAX_CLUSTER_ID_LENGTH - len(suffix), 0)]
        candidate = f"{truncated}{suffix}"

    return candidate


def extract_single_url(text: str) -> str:
    """Extract exactly one candidate ``https://`` URL from free-form text.

    Raises:
        ClusterURLMissingError: if no ``https://``/``http://`` URL-looking
            token is present.
        ClusterURLAmbiguousError: if more than one distinct candidate is
            present -- the caller must ask a clarifying question rather
            than guess which one is the intended target.
    """

    candidates = {
        match.rstrip(_TRAILING_PUNCTUATION) for match in _URL_PATTERN.findall(text or "")
    }
    if not candidates:
        raise ClusterURLMissingError(
            "no OpenShift cluster API URL was found in the request"
        )
    if len(candidates) > 1:
        raise ClusterURLAmbiguousError(
            "more than one candidate OpenShift cluster API URL was found in "
            "the request; state exactly one target cluster API URL"
        )
    return next(iter(candidates))


@dataclass(frozen=True)
class ClusterRegistry:
    """Allow-list of cluster ids mapped to their canonical API URL.

    ACME only ever holds ``{id: apiURL}`` -- never the Praxis-only upstream
    host/port/SNI fields from the shared ``global.olsClusters`` registry.
    """

    entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: dict[str, str]) -> "ClusterRegistry":
        """Build a registry from ``{id: apiURL}``, validating each entry.

        Each configured id must equal the id this module's own algorithm
        derives for its (canonicalized) apiURL -- this catches a
        GitOps/config mistake (e.g. a stale or hand-typed id) at startup
        rather than silently accepting an id that does not actually match
        its URL.
        """

        entries: dict[str, str] = {}
        known_ids: set[str] = set()
        for raw_id, raw_url in mapping.items():
            cluster_id = str(raw_id).strip()
            if not cluster_id:
                raise ClusterURLError("olsClusters keys must not be empty")
            canonical = canonicalize_api_url(str(raw_url))
            expected_id = derive_cluster_id(canonical, known_ids=frozenset(known_ids))
            if expected_id != cluster_id:
                raise ClusterURLError(
                    f"configured cluster id {cluster_id!r} does not match the "
                    f"id {expected_id!r} derived from its apiURL {canonical!r}"
                )
            entries[cluster_id] = canonical
            known_ids.add(cluster_id)
        return cls(entries=entries)

    @classmethod
    def from_env(cls, var_name: str = "OLS_CLUSTERS_JSON") -> "ClusterRegistry":
        """Load the registry from a JSON object env var of ``{id: apiURL}``.

        An unset/empty value yields an empty registry (every URL is then
        rejected as unregistered) rather than raising, so the process can
        still start -- and fail closed on every request -- before the
        chart's registry value is configured.
        """

        raw = os.getenv(var_name, "").strip()
        if not raw:
            return cls(entries={})
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClusterURLError(f"{var_name} must contain valid JSON") from exc
        if not isinstance(mapping, dict):
            raise ClusterURLError(f"{var_name} must be a JSON object of id -> apiURL")
        return cls.from_mapping(mapping)

    def resolve(self, raw_url: str) -> str:
        """Validate ``raw_url`` against the allow-list, returning its id.

        Raises:
            ClusterURLError: for any malformed, credentialed, non-https, or
                unregistered URL. Never falls back to a default cluster.
        """

        canonical = canonicalize_api_url(raw_url)
        for cluster_id, registered in self.entries.items():
            if registered == canonical:
                return cluster_id
        raise ClusterURLError(
            f"{raw_url!r} is not a registered OpenShift cluster API URL"
        )


def resolve_cluster_id_from_text(registry: ClusterRegistry, text: str) -> str:
    """Extract, canonicalize, and validate the single cluster URL in ``text``.

    This is the single entry point ``routing.py`` calls before allowing a
    message to be dispatched. It raises ``ClusterURLError`` (or a subclass)
    for any missing, ambiguous, malformed, or unregistered URL; the caller
    must not dispatch downstream when this raises.
    """

    url = extract_single_url(text)
    return registry.resolve(url)
