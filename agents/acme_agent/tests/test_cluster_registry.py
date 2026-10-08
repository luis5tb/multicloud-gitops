import hashlib

import pytest

from acme_agent.cluster_registry import (
    ClusterRegistry,
    ClusterURLAmbiguousError,
    ClusterURLError,
    ClusterURLMissingError,
    MAX_CLUSTER_ID_LENGTH,
    canonicalize_api_url,
    derive_cluster_id,
    extract_single_url,
    resolve_cluster_id_from_text,
)


# ---------------------------------------------------------------------------
# Worked example from LIGHTSPEED_DESIGN.md / IMPLEMENTATION_PLAN.md
# ---------------------------------------------------------------------------


def test_worked_example_canonicalization_and_id():
    url = "https://api.bm-cluster.e2e.bos.redhat.com:6443"
    canonical = canonicalize_api_url(url)
    assert canonical == url
    assert derive_cluster_id(canonical) == "api-bm-cluster-e2e-bos-redhat-com-6443"


def test_canonicalize_adds_explicit_default_port():
    assert canonicalize_api_url("https://api.example.com") == "https://api.example.com:443"


def test_canonicalize_lowercases_host():
    assert canonicalize_api_url("https://API.Example.COM:6443") == "https://api.example.com:6443"


def test_canonicalize_accepts_root_path():
    assert (
        canonicalize_api_url("https://api.example.com:6443/")
        == "https://api.example.com:6443"
    )


# ---------------------------------------------------------------------------
# Malformed URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://api.example.com:6443",  # not https
        "ftp://api.example.com:6443",
        "https://",  # no host
        "not-a-url-at-all",
        "https://api.example.com:not-a-port",
        "",
        "   ",
    ],
)
def test_canonicalize_rejects_malformed_urls(bad_url):
    with pytest.raises(ClusterURLError):
        canonicalize_api_url(bad_url)


def test_canonicalize_rejects_non_string():
    with pytest.raises(ClusterURLError):
        canonicalize_api_url(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Credentials, paths, query, fragment
# ---------------------------------------------------------------------------


def test_canonicalize_rejects_credentials():
    with pytest.raises(ClusterURLError):
        canonicalize_api_url("https://user:pass@api.example.com:6443")


def test_canonicalize_rejects_username_only():
    with pytest.raises(ClusterURLError):
        canonicalize_api_url("https://user@api.example.com:6443")


def test_canonicalize_rejects_path():
    with pytest.raises(ClusterURLError):
        canonicalize_api_url("https://api.example.com:6443/some/path")


def test_canonicalize_rejects_query():
    with pytest.raises(ClusterURLError):
        canonicalize_api_url("https://api.example.com:6443?foo=bar")


def test_canonicalize_rejects_fragment():
    with pytest.raises(ClusterURLError):
        canonicalize_api_url("https://api.example.com:6443#frag")


# ---------------------------------------------------------------------------
# 63-char cap / collision truncation+hash behavior
# ---------------------------------------------------------------------------


def _long_but_valid_host(label_prefix: str, label_count: int) -> str:
    """A hostname with many short (DNS-valid, <=63 char) labels whose total
    length still exceeds the 63-char cluster-id cap once combined with a
    port -- a single 80-char label is itself an invalid IDNA hostname, so
    length has to come from label *count*, not one label's size.
    """

    labels = [f"{label_prefix}{i}" for i in range(label_count)]
    return ".".join(labels) + ".example.com"


def test_derive_cluster_id_truncates_long_host_with_hash_suffix():
    long_host = _long_but_valid_host("segment", 10)
    url = f"https://{long_host}:6443"
    canonical = canonicalize_api_url(url)
    assert len(canonical) > MAX_CLUSTER_ID_LENGTH  # sanity: the input is actually long
    cluster_id = derive_cluster_id(canonical)

    assert len(cluster_id) <= MAX_CLUSTER_ID_LENGTH
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    assert cluster_id.endswith(f"-{digest}")


def test_derive_cluster_id_is_deterministic():
    long_host = _long_but_valid_host("part", 10)
    canonical = canonicalize_api_url(f"https://{long_host}:6443")
    assert derive_cluster_id(canonical) == derive_cluster_id(canonical)


def test_derive_cluster_id_collision_gets_disambiguated():
    url = "https://api.cluster-x.example.com:6443"
    canonical = canonicalize_api_url(url)
    natural_id = derive_cluster_id(canonical)

    # Force the scenario where this URL's natural (un-truncated) id already
    # exists in the registry -- the algorithm must fall back to the
    # truncate+hash form instead of returning a duplicate id.
    forced_id = derive_cluster_id(canonical, known_ids=frozenset({natural_id}))

    assert forced_id != natural_id
    assert len(forced_id) <= MAX_CLUSTER_ID_LENGTH
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    assert forced_id.endswith(f"-{digest}")


def test_derive_cluster_id_short_host_under_cap_has_no_suffix():
    canonical = canonicalize_api_url("https://api.example.com:6443")
    cluster_id = derive_cluster_id(canonical)
    assert cluster_id == "api-example-com-6443"


# ---------------------------------------------------------------------------
# extract_single_url
# ---------------------------------------------------------------------------


def test_extract_single_url_from_sentence():
    text = "Investigate pods in payments on https://api.bm-cluster.e2e.bos.redhat.com:6443 please."
    assert (
        extract_single_url(text)
        == "https://api.bm-cluster.e2e.bos.redhat.com:6443"
    )


def test_extract_single_url_strips_trailing_punctuation():
    text = "Target is https://api.example.com:6443."
    assert extract_single_url(text) == "https://api.example.com:6443"


def test_extract_single_url_missing_raises():
    with pytest.raises(ClusterURLMissingError):
        extract_single_url("Please check the payments namespace for crashing pods.")


def test_extract_single_url_ambiguous_raises():
    text = (
        "Maybe https://api.cluster-a.example.com:6443 or "
        "https://api.cluster-b.example.com:6443?"
    )
    with pytest.raises(ClusterURLAmbiguousError):
        extract_single_url(text)


def test_extract_single_url_repeated_same_url_is_not_ambiguous():
    text = (
        "Use https://api.example.com:6443 -- yes, "
        "https://api.example.com:6443 is correct."
    )
    assert extract_single_url(text) == "https://api.example.com:6443"


# ---------------------------------------------------------------------------
# ClusterRegistry
# ---------------------------------------------------------------------------


def test_registry_from_mapping_resolves_registered_url():
    registry = ClusterRegistry.from_mapping(
        {
            "api-bm-cluster-e2e-bos-redhat-com-6443": (
                "https://api.bm-cluster.e2e.bos.redhat.com:6443"
            )
        }
    )
    assert (
        registry.resolve("https://api.bm-cluster.e2e.bos.redhat.com:6443")
        == "api-bm-cluster-e2e-bos-redhat-com-6443"
    )


def test_registry_resolve_is_canonicalization_tolerant():
    registry = ClusterRegistry.from_mapping(
        {"api-example-com-6443": "https://api.example.com:6443"}
    )
    # Mixed case and an explicit default port omission still match.
    assert registry.resolve("https://API.Example.com:6443") == "api-example-com-6443"


def test_registry_rejects_unknown_url():
    registry = ClusterRegistry.from_mapping(
        {"api-example-com-6443": "https://api.example.com:6443"}
    )
    with pytest.raises(ClusterURLError):
        registry.resolve("https://api.other-cluster.com:6443")


def test_registry_rejects_credentialed_url_even_if_host_matches():
    registry = ClusterRegistry.from_mapping(
        {"api-example-com-6443": "https://api.example.com:6443"}
    )
    with pytest.raises(ClusterURLError):
        registry.resolve("https://attacker:pw@api.example.com:6443")


def test_registry_from_mapping_rejects_mismatched_id():
    with pytest.raises(ClusterURLError):
        ClusterRegistry.from_mapping(
            {"totally-wrong-id": "https://api.example.com:6443"}
        )


def test_registry_from_env_missing_rejects_everything(monkeypatch):
    monkeypatch.delenv("OLS_CLUSTERS_JSON", raising=False)
    registry = ClusterRegistry.from_env()
    with pytest.raises(ClusterURLError):
        registry.resolve("https://api.example.com:6443")


def test_registry_from_env_parses_json(monkeypatch):
    monkeypatch.setenv(
        "OLS_CLUSTERS_JSON",
        '{"api-example-com-6443": "https://api.example.com:6443"}',
    )
    registry = ClusterRegistry.from_env()
    assert registry.resolve("https://api.example.com:6443") == "api-example-com-6443"


def test_registry_from_env_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("OLS_CLUSTERS_JSON", "not json")
    with pytest.raises(ClusterURLError):
        ClusterRegistry.from_env()


# ---------------------------------------------------------------------------
# resolve_cluster_id_from_text (end-to-end helper used by routing.py)
# ---------------------------------------------------------------------------


def test_resolve_cluster_id_from_text_end_to_end():
    registry = ClusterRegistry.from_mapping(
        {
            "api-bm-cluster-e2e-bos-redhat-com-6443": (
                "https://api.bm-cluster.e2e.bos.redhat.com:6443"
            )
        }
    )
    text = "Pods crashing on https://api.bm-cluster.e2e.bos.redhat.com:6443 in payments"
    assert (
        resolve_cluster_id_from_text(registry, text)
        == "api-bm-cluster-e2e-bos-redhat-com-6443"
    )


def test_resolve_cluster_id_from_text_rejects_unknown_url():
    registry = ClusterRegistry.from_mapping(
        {"api-example-com-6443": "https://api.example.com:6443"}
    )
    with pytest.raises(ClusterURLError):
        resolve_cluster_id_from_text(registry, "https://api.unregistered.com:6443 please")


def test_resolve_cluster_id_from_text_rejects_missing_url():
    registry = ClusterRegistry.from_mapping(
        {"api-example-com-6443": "https://api.example.com:6443"}
    )
    with pytest.raises(ClusterURLMissingError):
        resolve_cluster_id_from_text(registry, "no url here")
