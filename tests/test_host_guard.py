import pytest

from deploy_tenant import assert_host_matches, DeployError


def test_matching_host_passes():
    r = {"tenant": "qacanary01", "host_token": "qacanary01"}
    creds = {
        "DEMISTO_BASE_URL": "https://api-qacanary01.xdr.us.paloaltonetworks.com"
    }
    # Should not raise.
    assert assert_host_matches(r, creds) is None


def test_mismatched_host_hard_fails():
    # The failure mode this guard exists for: a swapped secret pointing the
    # converge at a different tenant's host must hard-fail before any write.
    r = {"tenant": "qacanary01", "host_token": "qacanary01"}
    creds = {
        "DEMISTO_BASE_URL": "https://api-devlab01.xdr.us.paloaltonetworks.com"
    }
    with pytest.raises(DeployError) as exc:
        assert_host_matches(r, creds)
    msg = str(exc.value)
    assert "qacanary01" in msg
    assert "devlab01" in msg


def test_override_host_token_used():
    # Fleet name differs from the subdomain; host_token override must be honored.
    r = {"tenant": "acme-prod", "host_token": "acmecorp01"}
    ok = {
        "DEMISTO_BASE_URL": "https://api-acmecorp01.xdr.us.paloaltonetworks.com"
    }
    assert assert_host_matches(r, ok) is None

    bad = {
        "DEMISTO_BASE_URL": "https://api-acmecorp02.xdr.us.paloaltonetworks.com"
    }
    with pytest.raises(DeployError):
        assert_host_matches(r, bad)


def test_empty_base_url_fails_closed():
    r = {"tenant": "qacanary01", "host_token": "qacanary01"}
    creds = {"DEMISTO_BASE_URL": ""}
    with pytest.raises(DeployError):
        assert_host_matches(r, creds)


def test_no_host_token_fails_closed():
    r = {"tenant": "x", "host_token": ""}
    creds = {
        "DEMISTO_BASE_URL": "https://api-something.xdr.us.paloaltonetworks.com"
    }
    with pytest.raises(DeployError):
        assert_host_matches(r, creds)
