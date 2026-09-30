from resolve import normalize_token, resolve


def test_normalize_token_lowercases_and_strips():
    assert normalize_token("QACANARY01") == "qacanary01"
    assert normalize_token("acme-prod") == "acme-prod"
    assert normalize_token("api_host.01") == "apihost01"


def test_normalize_token_empty():
    assert normalize_token("") == ""
    assert normalize_token(None) == ""


def test_resolve_derives_host_token_from_tenant_name():
    r = resolve("qacanary01")
    assert r["host_token"] == "qacanary01"
