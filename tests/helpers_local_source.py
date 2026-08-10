"""Shared constants/helpers for the local-source (MSSP-authored) pack tests.

Kept in a normal importable module (not conftest.py, which pytest does not
expose as an importable module by name) so both test files can share them.

CONTRACT NOTE (pin-driven sentinel routing):
Routing is decided by the PIN VALUE, not by whether the pack is in Packs/:
  * pin/override value == "local"  -> source=="local" (requires Packs/<id>/)
  * pin/override value == "X.Y.Z"  -> source=="upstream" (EVEN IF in Packs/)
  * no pin and no override          -> ResolveError (unpinned invariant)
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The sentinel pin value that opts a pack into local-source deployment.
LOCAL_SENTINEL = "local"

# ExamplePack is a REAL pack fixture already in the repo at
# Packs/ExamplePack/pack_metadata.json (id "ExamplePack", currentVersion
# "1.0.0"). It has local source, so a pin of "local" routes it source=local and
# a version pin routes it source=upstream even though it lives in Packs/.
LOCAL_PACK_ID = "ExamplePack"
LOCAL_PACK_DIR = REPO_ROOT / "Packs" / LOCAL_PACK_ID
LOCAL_PACK_META = LOCAL_PACK_DIR / "pack_metadata.json"

# A pack that is NOT in Packs/ — a normal upstream pack. Used to prove the
# unpinned-is-a-hard-error behavior and, when pinned to "local", the missing
# local-source GUARD.
UPSTREAM_PACK_ID = "soc-some-upstream-pack"


def local_pack_current_version():
    """The currentVersion declared in the real Packs/ExamplePack metadata."""
    return json.loads(LOCAL_PACK_META.read_text())["currentVersion"]
