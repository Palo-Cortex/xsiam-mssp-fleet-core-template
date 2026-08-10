"""resolve.py — PIN-DRIVEN SENTINEL routing for local-source packs.

Contract under test — routing is decided by the PIN VALUE, not by whether the
pack is in Packs/:

  1. A composed pack pinned to the string "local" (in fleet/pins/<ring>.yml OR
     in tenant.pin_overrides) resolves with source == "local" and version ==
     the pack's pack_metadata.json currentVersion. This requires Packs/<id>/
     source to exist.
  2. A composed pack pinned to a real version "X.Y.Z" resolves with
     source == "upstream" and version == "X.Y.Z" — EVEN IF the pack also exists
     in Packs/. (Presence in Packs/ must NOT force local when the pin is a
     version.)
  3. A composed pack with NO pin and no override raises ResolveError (the
     unpinned invariant is restored). This applies to MSSP packs too now —
     being in Packs/ does NOT exempt a pack from needing a pin.
  4. GUARD: a pack pinned to "local" but with NO source at Packs/<id>/ raises a
     ResolveError whose message mentions the missing local source (distinct from
     the generic "no pin" message).
  5. pin_overrides accepts "local": an override of "local" routes the pack
     source == "local" even if the ring pin is a real version (override wins).

These tests use the REAL Packs/ExamplePack fixture (id "ExamplePack",
currentVersion "1.0.0") as the local pack, and an isolated temp fleet so we
never touch the real fleet/tenants tree.
"""
import pytest

from resolve import resolve, ResolveError, owned_pins

from helpers_local_source import (
    LOCAL_PACK_ID,
    LOCAL_SENTINEL,
    UPSTREAM_PACK_ID,
    local_pack_current_version,
)


# --- helpers ---------------------------------------------------------------
# The contract fixes the marker key as item["source"] ("local" | "upstream").
# No tolerant OR-based helper: we assert the real key exactly.
def _find(items, pack_id):
    matches = [it for it in items if it["id"] == pack_id]
    assert matches, f"pack '{pack_id}' not found in resolved items: {items}"
    assert len(matches) == 1, f"pack '{pack_id}' appears more than once: {matches}"
    return matches[0]


def _all_items(r):
    return r["base"] + r["extras"]


# --- 1. sentinel "local" -> source local + metadata version ----------------
def test_sentinel_local_pin_resolves_source_local_and_metadata_version(temp_fleet):
    """A ring pin of "local" routes the pack to local source, version from
    pack_metadata.json currentVersion (NOT from the pin string)."""
    temp_fleet.pins("dev", {LOCAL_PACK_ID: LOCAL_SENTINEL})
    tenant = temp_fleet.tenant("t_sentinel", ring="dev", extras=[LOCAL_PACK_ID])

    r = resolve(tenant)
    item = _find(_all_items(r), LOCAL_PACK_ID)

    assert item["source"] == "local"
    assert item["version"] == local_pack_current_version()  # "1.0.0"


def test_sentinel_local_pin_works_in_base(temp_fleet):
    """Sentinel routing applies to base composition too, not just extras."""
    temp_fleet.pins("dev", {LOCAL_PACK_ID: LOCAL_SENTINEL})
    tenant = temp_fleet.tenant(
        "t_sentinel_base", ring="dev", base=[LOCAL_PACK_ID], extras=[]
    )

    r = resolve(tenant)
    item = _find(r["base"], LOCAL_PACK_ID)

    assert item["source"] == "local"
    assert item["version"] == local_pack_current_version()


# --- 2. version pin -> source upstream EVEN when pack is in Packs/ ----------
def test_version_pin_routes_upstream_even_when_pack_in_packs(temp_fleet):
    """THE KEY REVERSAL: a real version pin routes ExamplePack to source
    "upstream" and version "X.Y.Z" even though its source lives in Packs/.

    Under the OLD (presence-driven) contract this pack would have resolved
    source=="local" simply because it exists in Packs/. That is now WRONG:
    the PIN VALUE decides routing.
    """
    temp_fleet.pins("dev", {LOCAL_PACK_ID: "3.1.4"})
    tenant = temp_fleet.tenant("t_ver_pin", ring="dev", extras=[LOCAL_PACK_ID])

    r = resolve(tenant)
    item = _find(_all_items(r), LOCAL_PACK_ID)

    assert item["source"] == "upstream", (
        "a version-pinned pack must route upstream even when it exists in "
        "Packs/ (presence must NOT force local)"
    )
    assert item["version"] == "3.1.4"


# --- 3. no pin + no override -> ResolveError (even for Packs/ packs) --------
def test_unpinned_local_source_pack_raises_resolve_error(temp_fleet):
    """RESTORED INVARIANT: being in Packs/ does NOT exempt a pack from needing
    a pin. An MSSP pack composed with no pin and no override is a hard error."""
    temp_fleet.pins("dev", {})  # no entry for ExamplePack
    tenant = temp_fleet.tenant("t_unpinned_local", ring="dev", extras=[LOCAL_PACK_ID])

    with pytest.raises(ResolveError) as exc:
        resolve(tenant)
    assert LOCAL_PACK_ID in str(exc.value)


def test_unpinned_upstream_pack_raises_resolve_error(temp_fleet):
    """Preserved behavior: a non-local pack with no pin is a hard error."""
    temp_fleet.pins("dev", {})  # empty pins -> upstream pack is unpinned
    tenant = temp_fleet.tenant("t_upstream", ring="dev", extras=[UPSTREAM_PACK_ID])

    with pytest.raises(ResolveError) as exc:
        resolve(tenant)
    assert UPSTREAM_PACK_ID in str(exc.value)


# --- 4. GUARD: "local" pin without source at Packs/<id>/ -------------------
def test_local_pin_without_source_raises_missing_source_guard(temp_fleet):
    """A pin of "local" for a pack that has NO source at Packs/<id>/ must raise
    a ResolveError that mentions the missing local source — distinct from the
    generic "no pin" error."""
    # UPSTREAM_PACK_ID is deliberately NOT in Packs/.
    temp_fleet.pins("dev", {UPSTREAM_PACK_ID: LOCAL_SENTINEL})
    tenant = temp_fleet.tenant("t_guard", ring="dev", extras=[UPSTREAM_PACK_ID])

    with pytest.raises(ResolveError) as exc:
        resolve(tenant)
    msg = str(exc.value).lower()
    assert UPSTREAM_PACK_ID in str(exc.value)
    # The guard message must reference the missing local source (Packs/ / source /
    # local), distinguishing it from the generic unpinned message.
    assert ("source" in msg) or ("packs/" in msg) or ("local" in msg), (
        f"guard error must mention the missing local source, got: {exc.value!r}"
    )


def test_combined_unpinned_and_bad_local_raises_and_reports_unpinned(temp_fleet):
    """DOCUMENTS CURRENT BEHAVIOR: when a tenant composes BOTH an unpinned pack
    AND a pack pinned to "local" that has no Packs/ source, resolve() raises a
    ResolveError. The `missing` (unpinned) check runs first, so the raised error
    is the generic unpinned message naming the unpinned pack.

    We use two packs that are NOT in Packs/: one left unpinned, one pinned to the
    "local" sentinel (so it would trip the missing-source guard were it reached).
    """
    unpinned_id = "soc-unpinned-pack"
    bad_local_id = "soc-bad-local-pack"  # pinned "local" but not in Packs/
    temp_fleet.pins("dev", {bad_local_id: LOCAL_SENTINEL})  # unpinned_id absent
    tenant = temp_fleet.tenant(
        "t_combined", ring="dev", extras=[unpinned_id, bad_local_id]
    )

    with pytest.raises(ResolveError) as exc:
        resolve(tenant)
    # Current behavior: the unpinned check fires first and names the unpinned pack.
    assert unpinned_id in str(exc.value)


# --- 5. pin_override "local" wins routing over a version ring pin -----------
def test_pin_override_local_wins_over_version_ring_pin(temp_fleet):
    """An override of "local" routes the pack source=="local" even when the ring
    pin is a real version (override wins)."""
    temp_fleet.pins("dev", {LOCAL_PACK_ID: "9.9.9"})  # ring says a version
    tenant = temp_fleet.tenant(
        "t_override_local",
        ring="dev",
        extras=[LOCAL_PACK_ID],
        pin_overrides={LOCAL_PACK_ID: LOCAL_SENTINEL},  # override says local
    )

    r = resolve(tenant)
    item = _find(_all_items(r), LOCAL_PACK_ID)

    assert item["source"] == "local", "pin_override 'local' must win over the ring version pin"
    assert item["version"] == local_pack_current_version()  # "1.0.0" from metadata


def test_pin_override_version_for_local_source_pack_routes_upstream(temp_fleet):
    """The mirror of the above: a version pin_override for a pack that lives in
    Packs/ routes it source=="upstream" at the override version — presence in
    Packs/ does not force local when the override is a version."""
    temp_fleet.pins("dev", {})  # no ring pin
    tenant = temp_fleet.tenant(
        "t_override_ver",
        ring="dev",
        extras=[LOCAL_PACK_ID],
        pin_overrides={LOCAL_PACK_ID: "2.5.0"},
    )

    r = resolve(tenant)
    item = _find(_all_items(r), LOCAL_PACK_ID)

    assert item["source"] == "upstream"
    assert item["version"] == "2.5.0"


# --- mixed composition -----------------------------------------------------
def test_mixed_sentinel_local_and_version_pinned_upstream_resolve(temp_fleet):
    """A sentinel-local pack and a version-pinned upstream pack resolve together
    with the correct, distinct sources."""
    temp_fleet.pins(
        "dev", {LOCAL_PACK_ID: LOCAL_SENTINEL, UPSTREAM_PACK_ID: "9.9.9"}
    )
    tenant = temp_fleet.tenant(
        "t_mixed", ring="dev", extras=[LOCAL_PACK_ID, UPSTREAM_PACK_ID]
    )

    r = resolve(tenant)
    items = _all_items(r)

    local = _find(items, LOCAL_PACK_ID)
    upstream = _find(items, UPSTREAM_PACK_ID)

    assert local["source"] == "local"
    assert local["version"] == local_pack_current_version()

    assert upstream["source"] == "upstream"
    assert upstream["version"] == "9.9.9"


# --- owned_pins ------------------------------------------------------------
def test_owned_pins_includes_sentinel_local_pack_at_metadata_version(temp_fleet):
    """owned_pins() (used downstream to compute the install set) includes a
    sentinel-local pack at its metadata currentVersion."""
    temp_fleet.pins("dev", {LOCAL_PACK_ID: LOCAL_SENTINEL})
    tenant = temp_fleet.tenant("t_owned", ring="dev", extras=[LOCAL_PACK_ID])

    r = resolve(tenant)
    owned = owned_pins(r)

    assert owned.get(LOCAL_PACK_ID) == local_pack_current_version()
