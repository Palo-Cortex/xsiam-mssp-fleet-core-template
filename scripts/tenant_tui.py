#!/usr/bin/env python3
"""
tenant_tui.py — a visual editor for fleet/tenants/*.yml (Textual TUI).

The runbook's "Add a new managed tenant" is a checklist of hand-edited YAML and
resolve.py error messages. This tool makes the same workflow visual:

  • list every tenant with its ring and whether it resolves cleanly
  • create / edit / rename / delete a tenant profile with live validation —
    the same checks scripts/resolve.py and namespace_lint.py enforce
  • a live YAML preview of the file that will be written
  • on save, offer to add missing pins to fleet/pins/<ring>.yml (the one manual
    step that trips everyone up), with ring-gate-aware warnings
  • remove pins no tenant in the ring composes anymore — the runbook's
    step-2 cleanup, the inverse of the add-missing-pins flow

It is a thin UI over scripts/resolve.py: validation runs the REAL resolver
against a temp copy of the fleet tree with the candidate tenant swapped in, so
what the TUI calls "clean" is exactly what check.sh / ring-gate / converge see.

The TUI writes working-tree files only. Nothing here bypasses the promotion
chain: pin edits are plain file edits you review and merge like any other, and
ring-gate still judges them on the PR.

Usage:
    python scripts/tenant_tui.py            # tenant list
    python scripts/tenant_tui.py <tenant>   # open that tenant's editor directly
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from rich.text import Text

from textual.app import App, ComposeResult
from textual.containers import Horizontal, HorizontalGroup, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.suggester import SuggestFromList
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

import resolve


class TuiError(Exception):
    """A user-facing error (bad input, write failure)."""


# Define variable (compiled regex) matching a valid tenant name
NAME_RE = re.compile(r"^[a-z0-9-]+$")

# Define variable (compiled regex) matching a dotted version like 1.0.2
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")

# House key order for the tenant file: identity block, then composition block.
KEY_ORDER = (
    "tenant", "display_name", "ring", "credentials_ref",
    "groups", "extras", "pin_overrides", "custom",
    "customer_namespace", "host_token",
)

# Define variable (dict) of ring name to Textual color for the list
RING_COLORS = {"dev": "green", "qa": "yellow", "prod": "red", "mssp": "magenta"}

# Define variable (str) placeholder shown when a tenant has no namespace
NO_NAMESPACE = "(none)"

def _dedupe(ids):
    """Preserve first-seen order, drop duplicates."""
    
    # Define variables (set, list) tracking seen ids and the deduped result
    seen, out = set(), []
    # For Loop - Iterates through each id checking for duplicates
    for i in ids:
        # Conditional - IF the id has not been seen, THEN add it. ELSE, skip
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _scalar(value):
    """YAML scalar text for a pin value, quoted when it would parse as another
    type (the version '1.0' must stay a string, not a float)."""
    
    # safe_dump appends a '...' document-end marker for bare scalars; drop it.
    return yaml.safe_dump(str(value)).strip().split("\n", 1)[0]


def available_rings():
    """Rings that exist = every pin file in fleet/pins/."""
    return sorted(p.stem for p in (resolve.FLEET / "pins").glob("*.yml"))


def available_groups():
    """{name: description} for every fleet/groups/*.yml."""
    
    # Define variable (dict) of group name to description
    out = {}
    # For Loop - Iterates through each group file building the name-to-description map
    for f in sorted((resolve.FLEET / "groups").glob("*.yml")):
        
        # Define variable (dict) of the group file, empty when unreadable
        doc = resolve.load(f) or {}
        out[f.stem] = " ".join(str(doc.get("description") or "").split())
    return out


def registered_prefixes():
    """Customer namespace prefixes registered in defaults.yml."""
    
    # Define variable (dict) of defaults.yml, empty when missing
    doc = resolve.load(resolve.FLEET / "defaults.yml") or {}
    
    return doc.get("customer_namespaces") or []


def pack_suggestions():
    """Pack ids the operator is likely to type: every ring pin, every group
    extra, and every MSSP-authored pack under Packs/."""
    
    # Define variable (set) of pack ids to suggest
    ids = set()
    
    # For Loop - Iterates through each pin file collecting pack ids
    for p in (resolve.FLEET / "pins").glob("*.yml"):
        ids.update((resolve.load(p).get("pins") or {}).keys())
    
    # For Loop - Iterates through each group file collecting extra pack ids
    for g in (resolve.FLEET / "groups").glob("*.yml"):
        ids.update((resolve.load(g).get("extras") or []))
    
    ids.update(resolve.local_packs().keys())
    
    return sorted(ids)


def list_tenants():
    """One row per tenant: identity fields plus a resolve status."""
    
    # Define variable (list) of one dict per tenant for the table
    rows = []
    
    # For Loop - Iterates through each tenant building a table row
    for name in resolve.all_tenants():
        # Try Block - Try loading the manifest and fall back to an unreadable row on failure.
        try:
            # Define variable (dict) of the manifest, empty when unreadable
            m = resolve.load_tenant(name) or {}
        except Exception as e:  # noqa: BLE001 — a broken file is a row, not a crash
            rows.append({"name": name, "display": "?", "ring": "?", "creds": "?",
                         "groups": 0, "extras": 0, "overrides": 0,
                         "status": f"unreadable: {e}", "ok": False})
            continue
    
        # Define variable (str) of the ring, '?' when unset
        ring = str(m.get("ring") or "?")
        # Try Block - Try resolving the tenant and capture the error message on failure.
        try:
            resolve.resolve(name)
            status, ok = "✓ resolves", True
        except resolve.ResolveError as e:
            status, ok = str(e), False
        rows.append({
            "name": name,
            "display": str(m.get("display_name") or name),
            "ring": ring,
            "creds": str(m.get("credentials_ref") or "—"),
            "groups": len(m.get("groups") or []),
            "extras": len(m.get("extras") or []),
            "overrides": len(m.get("pin_overrides") or {}),
            "status": status,
            "ok": ok,
        })
    return rows


def _candidate_tree(manifest, name):
    """A temp fleet tree: the real fleet with the candidate tenant swapped in.

    resolve.resolve() reads module-level paths, so this is the same trick the
    test suite uses (point resolve.FLEET at a temp tree) — the path is restored
    by the caller in the same call.
    """
    
    # Define variable (Path) of a temp dir to hold the candidate fleet tree
    tmp = Path(tempfile.mkdtemp(prefix="tenant-tui-"))
    
    # Define variable (Path) of the fleet root inside the temp dir
    tree = tmp / "fleet"
    
    # Define variable (Path) of the real fleet root to copy from
    src = resolve.FLEET
    # For Loop - Iterates through each fleet subdirectory to copy into the temp tree
    for sub in ("pins", "groups", "config_overlays"):
        # Conditional - IF the subdirectory exists, THEN copy it. ELSE, skip it
        if (src / sub).is_dir():
            shutil.copytree(src / sub, tree / sub)
    shutil.copy2(src / "defaults.yml", tree / "defaults.yml")
    (tree / "tenants").mkdir(parents=True)
    (tree / "tenants" / f"{name}.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return tree


def validate_manifest(manifest, name, exists=False):
    """Validate a candidate tenant manifest. Returns (errors, warnings).

    `exists` is True when editing a file already on disk (so the duplicate-name
    check doesn't flag the tenant itself). Format checks run first; when they
    pass, the real resolver runs against a temp fleet tree with the candidate
    swapped in — so the composition/pin errors are resolve.py's own words.
    """
    
    # Define variables (list, list) of validation errors and warnings
    errors, warnings = [], []
    
    # Define variable (str) of the tenant field from the manifest
    tname = str(manifest.get("tenant") or "").strip()
    
    # Conditional - IF the name does not match the pattern, THEN append an error. 
    if not NAME_RE.match(name or ""):
        errors.append(f"tenant name '{name}' must be lowercase alphanumeric "
                      f"(hyphens allowed)")
    # Conditional - IF the tenant field does not equal the file name, THEN append an error. 
    if tname != name:
        errors.append(f"the `tenant:` field ('{tname}') must equal the file "
                      f"name ('{name}')")
    # Conditional - IF the tenant is new and the name is already taken, THEN append an error. 
    if not exists and name and name in resolve.all_tenants():
        errors.append(f"tenant '{name}' already exists")
    
    # Define variable (str) of the ring, '' when unset
    ring = str(manifest.get("ring") or "").strip()
    
    # Conditional - IF no ring is set, THEN append an error. ELSE, check the ring exists
    if not ring:
        errors.append("no ring selected")
    
    # Conditional - IF the ring has no pin file, THEN append an error. 
    elif ring not in available_rings():
        errors.append(f"no pin file for ring '{ring}' (fleet/pins/{ring}.yml)")
    
    # Define variable (str) of the credentials ref, '' when unset
    creds = str(manifest.get("credentials_ref") or "").strip()
    # Conditional - IF credentials_ref is empty, THEN append an error. ELSE, check the ref format
    if not creds:
        errors.append("credentials_ref is required (it names the <REF>_CREDS "
                      "CI secret)")
    
    # Conditional - IF the ref does not match the uppercased tenant name, THEN append a warning. 
    elif tname and creds != tname.upper():
        warnings.append(f"credentials_ref '{creds}' is not the tenant name "
                        "uppercased")
    
    # Define variable (str) of the customer namespace, None when unset
    ns = manifest.get("customer_namespace")
    
    # Conditional - IF a namespace is set but not registered, THEN append an error. 
    if ns and ns not in registered_prefixes():
        errors.append(f"customer_namespace '{ns}' is not registered in "
                      "defaults.yml › customer_namespaces")
    
    # For Loop - Iterates through each custom pack checking its prefix
    for c in manifest.get("custom") or []:
        cid = str(c.get("id") or "")
        # Conditional - IF the pack id has no registered prefix, THEN append an error. 
        if not any(cid.startswith(p) for p in registered_prefixes()):
            errors.append(f"custom pack '{cid}' carries no registered "
                          "customer prefix")
    
    # Conditional - IF a host token is set, THEN append a warning. 
    if manifest.get("host_token"):
        warnings.append(f"host guard will match '{manifest['host_token']}' as "
                        "a substring of the tenant's XSIAM host")
    # Conditional - IF any errors were found, THEN return early. ELSE, run the resolver
    if errors:
        return errors, warnings
    
    # Define variable (Path) of the temp fleet tree with the candidate in
    tree = _candidate_tree(manifest, name)
    
    # Try Block - Try resolving against the temp tree and restore the fleet path in finally.
    try:
        """ 
        NOTE: resolve reads the module-level FLEET path, so swap it to temp
        tree for the duration of the call and restore it in the finally block
        """
        old = resolve.FLEET
        resolve.FLEET = tree
        # Try Block - Try resolving the candidate and capture the error message on failure.
        try:
            resolve.resolve(name)
        except resolve.ResolveError as e:
            errors.append(str(e))
    
    finally:
        resolve.FLEET = old
        shutil.rmtree(tree.parent, ignore_errors=True)
    
    return errors, warnings


def _composed_with_source(manifest):
    """Yield (pack_id, source) for every MSSP-owned pack a manifest composes.

    source is "base", "group <name>", or "extras". Custom packs are
    customer-owned and never pinned by the fleet, so they are excluded.
    """
    
    # Define variable (dict) of defaults.yml, for the base packs
    defaults = resolve.load(resolve.FLEET / "defaults.yml") or {}
    
    # For Loop - Iterates through each base pack yielding it with its source
    for pid in _dedupe(defaults.get("base") or []):
        yield pid, "base"
    
    # For Loop - Iterates through each group loading its extras
    for g in manifest.get("groups") or []:
        # Try Block - Try loading the group file and skip it on failure.
        try:
            
            # Define variable (dict) of the group file, skipped when unreadable
            gdoc = resolve.load_group(g)
        
        except resolve.ResolveError:
            continue
        
        # For Loop - Iterates through each group extra yielding it with its source
        for pid in _dedupe((gdoc or {}).get("extras") or []):
            yield pid, f"group {g}"
    
    # For Loop - Iterates through each tenant extra yielding it with its source
    for pid in _dedupe(manifest.get("extras") or []):
        yield pid, "extras"


def missing_pins(manifest):
    """Composed packs (base ⊕ groups ⊕ extras) with no ring pin and no override.

    Returns [(pack_id, source)] where source says where the pack came from
    ("base", "group <name>", or "extras") — shown in the add-pins dialog.
    """
    
    # Define variable (str) of the ring, '' when unset
    ring = str(manifest.get("ring") or "").strip()
    
    # Conditional - IF no ring is set, THEN return empty. ELSE, continue checking
    if not ring:
        
        return []
    
    # Try Block - Try loading the ring pins and return empty when no pin file exists.
    try:
        # Define variable (dict) of the ring's pins, {} when no pin file
        pins = resolve.load_pins(ring)
    
    except resolve.ResolveError:
        return []
    
    # Define variable (dict) of the tenant's pin overrides
    overrides = manifest.get("pin_overrides") or {}
    
    # Define variable (list) of (pack, source) tuples for the missing pins
    out = []

    def consider(pid, source):
        # Conditional - IF the pack has no pin and no override and is not already listed, THEN add it. ELSE, skip
        if pid not in pins and pid not in overrides \
                and all(p != pid for p, _ in out):
            out.append((pid, source))

    # For Loop - Iterates through each composed pack checking if it is missing
    for pid, source in _composed_with_source(manifest):
        consider(pid, source)
    
    return out


def unused_pins(ring, editing=None):
    """Pins in fleet/pins/<ring>.yml that no tenant in the ring still needs.

    A pin is needed while some tenant in the ring composes the pack without a
    pin_override for it. `editing` is an optional (tenant_name, manifest) pair
    for a tenant the TUI is editing: its not-yet-saved composition stands in
    for the file on disk, and it counts even when it is not saved yet. Returns
    [(pack_id, version)] — the runbook's step-2 cleanup set ("Remove a pack
    from a tenant/ring").
    """
    # Try Block - Try loading the ring pins and return empty when no pin file exists.
    try:
        # Define variable (dict) of the ring's pins, {} when no pin file
        pins = resolve.load_pins(ring)
    
    except resolve.ResolveError:
        
        return []
    
    # Define variable (list) of the saved tenant names in this ring
    names = [t for t in resolve.all_tenants()
             if (resolve.load_tenant(t) or {}).get("ring") == ring]
    
    # Conditional - IF a tenant is being edited, THEN include it in the name list. 
    if editing is not None:
        
        # Define variables (str, dict) of the edited tenant and its manifest
        edit_name, edit_manifest = editing
        
        # Conditional - IF the edited tenant is not already listed, THEN add it. 
        if edit_name and edit_name not in names:
            names.append(edit_name)
    
    # Define variable (set) of pack ids still composed by a tenant in the ring
    needed = set()
    
    # For Loop - Iterates through each tenant in the ring collecting needed packs
    for t in names:
        
        # Conditional - IF this is the tenant being edited, THEN use the in-progress manifest. ELSE, load the saved one
        if editing is not None and t == editing[0]:
            
            # Define variable (dict) of the in-progress manifest for the tenant
            m = editing[1]
        
        else:            
            # Define variable (dict) of the saved manifest for the tenant
            m = resolve.load_tenant(t)
        
        # Define variable (dict) of the tenant's pin overrides
        overrides = m.get("pin_overrides") or {}
        # For Loop - Iterates through each composed pack checking if it is overridden
        for pid, _src in _composed_with_source(m):
            
            # Conditional - IF the pack has no override, THEN mark it as needed. ELSE, skip
            if pid not in overrides:
                needed.add(pid)
    
    return [(pid, pins[pid]) for pid in pins if pid not in needed]


def render_yaml(manifest):
    """Canonical YAML for the tenant file: house key order plus a header comment.

    Key order matches the example tenants: identity block, blank line, then the
    composition block. groups/extras/pin_overrides/custom are always written
    (even empty) so the shape is stable; other empty optional fields are omitted.
    """
    
    # Define variable (tuple) of keys always written, even when empty
    always = ("groups", "extras", "pin_overrides", "custom")
    
    # Define variable (dict) of the manifest in house key order
    ordered = {}
    
    # For Loop - Iterates through each key in house order building the ordered dict
    for key in KEY_ORDER:
        
        value = manifest.get(key)
        
        # Conditional - IF the key is always written, THEN include it even when empty. ELSE, check for a value
        if key in always:
            ordered[key] = value if value is not None else (
                {} if key == "pin_overrides" else [])
        
        # Conditional - IF the value is set, THEN include the key. ELSE, skip it
        elif value not in (None, ""):
            ordered[key] = value
    
    # For Loop - Iterates through any remaining keys appending unknowns last
    for key, value in manifest.items():  # forward-compat: unknown keys last
        
        # Conditional - IF the key is new and has a value, THEN add it. ELSE, skip
        if key not in ordered and value not in (None, ""):
            ordered[key] = value
    
    # Define variable (str) of the dumped YAML body
    body = yaml.safe_dump(ordered, sort_keys=False, width=100)
    
    # Define variable (list) of the body lines, to splice a blank line into
    lines = body.splitlines()
    
    # Conditional - IF credentials_ref is present, THEN insert a blank line after it. 
    if "credentials_ref" in ordered:
        
        # NOTE: find the credentials_ref line so the blank line lands after
        # it, matching the example tenants' identity/composition split
        i = next(i for i, ln in enumerate(lines)
                 if ln.startswith("credentials_ref:"))
        lines.insert(i + 1, "")
    
    # Define variable (str) of the ring for the header comment, '?' when unset
    ring = manifest.get("ring") or "?"
    
    # Define variable (str) of the tenant name for the header comment
    tname = manifest.get("tenant") or "?"
    
    # Define variable (str) of the file header comment
    header = (
        f"# Tenant: {tname}\n"
        f"# Managed by scripts/tenant_tui.py (or edit by hand — keep `tenant:` equal\n"
        f"# to the file name). Every composed pack needs a pin in fleet/pins/{ring}.yml\n"
        f"# or pin_overrides; verify with `python scripts/resolve.py {tname}`.\n"
    )
    
    return header + "\n".join(lines) + "\n"


def tenant_path(name):
    """Return the fleet path for a tenant file.

    Args:
        name (str): tenant name (the file stem)

    Returns:
        Path: fleet/tenants/<name>.yml
    """
    
    return resolve.FLEET / "tenants" / f"{name}.yml"


def save_tenant(name, manifest):
    """Write the tenant file (canonical form). Returns the path."""
    
    # Define variable (Path) of the tenant file to write
    path = tenant_path(name)
    
    path.parent.mkdir(parents=True, exist_ok=True)
    
    path.write_text(render_yaml(manifest), encoding="utf-8")
    
    return path


def delete_tenant(name):
    """Delete a tenant file from the working tree.

    Args:
        name (str): tenant name (the file stem)

    Raises:
        TuiError: no tenant file at the path
    """
    
    # Define variable (Path) of the tenant file to delete
    path = tenant_path(name)
    
    # Conditional - IF the tenant file does not exist, THEN raise an error. ELSE, delete it
    if not path.exists():
        raise TuiError(f"no tenant file at {path}")
    
    path.unlink()


def rename_tenant(old, new):
    """Move the tenant file and update the `tenant:` field."""
    
    # Conditional - IF the new name does not match the pattern, THEN raise an error. ELSE, continue
    if not NAME_RE.match(new or ""):
        raise TuiError(f"tenant name '{new}' must be lowercase alphanumeric "
                       f"(hyphens allowed)")
    
    # Conditional - IF the new name is already taken, THEN raise an error. ELSE, continue
    if new in resolve.all_tenants():
        raise TuiError(f"tenant '{new}' already exists")
    
    # Define variable (Path) of the old tenant file
    src = tenant_path(old)
    
    # Conditional - IF the old tenant file does not exist, THEN raise an error. ELSE, continue
    if not src.exists():
        raise TuiError(f"no tenant file at {src}")
    
    # Define variable (dict) of the manifest, rewritten under the new name
    manifest = resolve.load(src) or {}
    manifest["tenant"] = new
    save_tenant(new, manifest)
    src.unlink()


def is_last_in_ring(name):
    """True when deleting this tenant would leave its ring empty (resolve
    --ring and check.sh error on an empty ring)."""
    
    # Try Block - Try reading the tenant's ring and return False when the file is unreadable.
    try:        
        # Define variable (str) of the tenant's ring, None when unset
        ring = (resolve.load_tenant(name) or {}).get("ring")
    
    except Exception:  # noqa: BLE001
        
        return False
    
    # Conditional - IF no ring is set, THEN return False. ELSE, count ring members
    if not ring:
        
        return False
    
    # Define variable (list) of the tenants sharing the ring
    members = [t for t in resolve.all_tenants()
               if (resolve.load_tenant(t) or {}).get("ring") == ring]
    
    return len(members) <= 1


def add_pins(ring, new_pins):
    """Append pins to fleet/pins/<ring>.yml, preserving the file's comments.

    Surgical text append into the existing `pins:` block (the house format is a
    `ring:` line plus a `pins:` mapping) — a full re-dump would drop the file's
    header and inline comments. Re-parses afterwards to self-verify.
    """
    
    # Define variable (Path) of the ring's pin file
    path = resolve.FLEET / "pins" / f"{ring}.yml"
    
    # Conditional - IF the pin file does not exist, THEN raise an error. ELSE, continue
    if not path.exists():
        
        raise TuiError(f"no pin file for ring '{ring}' ({path})")
    
    # Define variable (dict) of the current pins, to reject duplicates
    existing = resolve.load_pins(ring)
    
    # For Loop - Iterates through each new pin checking for duplicates
    for pid in new_pins:
        
        # Conditional - IF the pin already exists, THEN raise an error. ELSE, continue
        if pid in existing:
            
            raise TuiError(f"{pid} is already pinned in {path.name} "
                           f"({existing[pid]}) — change it there instead")
    
    # Define variable (list) of the pin file lines, to splice the new pins into
    lines = path.read_text(encoding="utf-8").splitlines()
    
    # Define variable (int) of the 'pins:' line index, None when not found
    pins_idx = None
    
    # For Loop - Iterates through each line finding the pins block
    for i, ln in enumerate(lines):
        
        # Conditional - IF the line is the pins header, THEN record its index and stop. ELSE, continue
        if re.match(r"^pins:\s*(\{.*\})?\s*(#.*)?$", ln):
            
            pins_idx = i
            
            break
    
    # Conditional - IF no pins block was found, THEN raise an error. ELSE, continue
    if pins_idx is None:
        
        raise TuiError(f"no 'pins:' block found in {path} — edit it by hand")
    
    # Define variable (match) of an inline 'pins: {...}' mapping, else None
    inline = re.match(r"^pins:\s*(\{.*\})", lines[pins_idx])
    
    # Conditional - IF the pins block uses an inline mapping, THEN normalize it. 
    if inline:
        
        # Conditional - IF the inline mapping is not empty, THEN raise an error. ELSE, replace with bare pins
        if inline.group(1).strip() != "{}":
            
            raise TuiError(f"'pins:' in {path} uses an inline mapping — "
                           "edit it by hand")
        
        lines[pins_idx] = "pins:"
    
    # Define variable (int) of the first line after the pins block
    end_idx = len(lines)
    
    # For Loop - Iterates through lines after the pins header finding where the block ends
    for i in range(pins_idx + 1, len(lines)):
        
        ln = lines[i]
        # NOTE: the block ends at the first non-indented line; the house format
        # has nothing after pins:, so this is usually the end of the file
        if ln.strip() and not ln[:1] in (" ", "\t"):
            end_idx = i
            break
    
    lines[end_idx:end_idx] = [f"  {_scalar(pid)}: {_scalar(ver)}"
                              for pid, ver in new_pins.items()]
    
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    check = resolve.load_pins(ring)
    
    # For Loop - Iterates through each new pin verifying it was written correctly
    for pid, ver in new_pins.items():
        
        # Conditional - IF the re-parsed value does not match, THEN raise a self-verify error. ELSE, continue
        if str(check.get(pid)) != str(ver):
            raise TuiError(f"self-verify failed after writing {path} — "
                           "check the file")


def remove_pins(ring, pack_ids):
    """Remove pins from fleet/pins/<ring>.yml, preserving the file's comments.

    The inverse of add_pins: surgical line deletion from the existing `pins:`
    block (a full re-dump would drop the file's header and inline comments).
    Only remove pins no tenant in the ring needs (unused_pins) — a
    composed-but-unpinned pack is a hard resolve error. Re-parses afterwards
    to self-verify.
    """
    
    # Define variable (Path) of the ring's pin file
    path = resolve.FLEET / "pins" / f"{ring}.yml"
    
    # Conditional - IF the pin file does not exist, THEN raise an error. ELSE, continue
    if not path.exists():
        raise TuiError(f"no pin file for ring '{ring}' ({path})")
    
    # Define variable (dict) of the current pins, to reject unknown packs
    existing = resolve.load_pins(ring)
    
    # For Loop - Iterates through each pack id checking it is currently pinned
    for pid in pack_ids:
        
        # Conditional - IF the pack is not pinned, THEN raise an error. ELSE, continue
        if pid not in existing:
            
            raise TuiError(f"{pid} is not pinned in {path.name} — "
                           "nothing to remove")
    
    # Define variable (list) of the pin file lines, to filter the pins out of
    lines = path.read_text(encoding="utf-8").splitlines()
    
    # Define variable (int) of the 'pins:' line index, None when not found
    pins_idx = None
    
    # For Loop - Iterates through each line finding the pins block
    for i, ln in enumerate(lines):
        
        # Conditional - IF the line is the pins header, THEN record its index and stop. ELSE, continue
        if re.match(r"^pins:\s*(\{.*\})?\s*(#.*)?$", ln):
            
            pins_idx = i
            break
    
    # Conditional - IF no pins block was found, THEN raise an error. ELSE, continue
    if pins_idx is None:
        
        raise TuiError(f"no 'pins:' block found in {path} — edit it by hand")
    
    # Conditional - IF the pins block uses an inline mapping, THEN raise an error. ELSE, continue
    if re.match(r"^pins:\s*\{.*\}", lines[pins_idx]):
        
        raise TuiError(f"'pins:' in {path} uses an inline mapping — "
                       "edit it by hand")
    
    # Define variable (set) of the pack ids to remove
    wanted = set(pack_ids)
    
    # Define variable (set) of the pack ids actually removed, for self-verify
    removed = set()
    
    # Define variable (list) of the filtered lines to write back
    out = []
    
    # For Loop - Iterates through each line filtering out the pins to remove
    for i, ln in enumerate(lines):
        
        # Conditional - IF the line is under the pins block, THEN check for a match. ELSE, keep it
        if i > pins_idx:            
            # Define variable (match) of a pin line under the block, else None
            m = re.match(r"^\s+([^:\s]+):", ln)
            
            # Conditional - IF the line is a pin to remove, THEN skip it. ELSE, keep it
            if m and m.group(1) in wanted:
                
                removed.add(m.group(1))
                
                continue
        
        out.append(ln)
    
    # For Loop - Iterates through each pack id verifying it was removed
    for pid in pack_ids:
        
        # Conditional - IF the pack was not found and removed, THEN raise an error. ELSE, continue
        if pid not in removed:
            
            raise TuiError(f"no line for {pid} under 'pins:' in {path} — "
                           "edit it by hand")
    
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    
    check = resolve.load_pins(ring)
    
    # For Loop - Iterates through each pack id verifying it is gone from the re-parsed file
    for pid in pack_ids:
        
        # Conditional - IF the pack still appears after removal, THEN raise a self-verify error. ELSE, continue
        if pid in check:
            
            raise TuiError(f"self-verify failed after removing {pid} from "
                           f"{path} — check the file")


def pin_warnings(ring, new_pins):
    """Ring-gate advisories for pins about to enter `ring`.

    Informational only — the TUI never bypasses the gate; it just says what the
    PR check will say (no-skip, local-in-gated-ring, change window).
    """
    
    # Define variable (list) of ring-gate advisory strings
    warnings = []
    
    # Try Block - Try loading the policies file and return empty when it is missing.
    try:        
        # Define variable (dict) of policies.yml, empty when missing
        pol = resolve.load(resolve.FLEET / "policies.yml") or {}
    
    except Exception:  # noqa: BLE001 — no policies file: nothing to advise
        
        return warnings
    
    # Define variable (dict) of the ring's policy, empty when unset
    ring_pol = (pol.get("rings") or {}).get(ring) or {}
    
    # Define variable (str) of the source ring, None for a head ring
    source = ring_pol.get("source")
    
    # Conditional - IF the ring has a source ring, THEN check pins against it. ELSE, skip
    if source:
        
        # Try Block - Try loading the source ring pins and fall back to empty on failure.
        try:
            # Define variable (dict) of the source ring's pins
            src_pins = resolve.load_pins(source)
        
        except resolve.ResolveError:
            
            src_pins = {}
        
        # For Loop - Iterates through each new pin checking ring-gate compliance
        for pid, ver in new_pins.items():
            
            # Conditional - IF the version is local, THEN warn it is only allowed in head rings. ELSE, check source match
            if str(ver) == "local":
                
                warnings.append(f"{pid}: 'local' is only allowed in head rings "
                                f"— ring-gate will reject it in {ring}")
            
            # Conditional - IF the version does not match the source ring, THEN warn about no-skip. 
            elif str(src_pins.get(pid)) != str(ver):
                # Define variable (str) of the source version, None when absent
                have = src_pins.get(pid)
                
                warnings.append(
                    f"{pid}: {ver} is not pinned in source ring '{source}'"
                    + (f" (has {have})" if have is not None else "")
                    + f" — ring-gate no-skip will fail; pin it in "
                      f"fleet/pins/{source}.yml first and promote")
    
    # Conditional - IF the ring requires a change window, THEN append a warning. 
    if ring_pol.get("require_change_window"):
        
        # Define variable (str) of the change windows, for the advisory text
        windows = ", ".join(f"{w.get('start', '?')}–{w.get('end', '?')}"
                            for w in ring_pol.get("change_windows") or [])
        
        warnings.append(f"{ring}: pin changes may only merge inside a change "
                        f"window ({windows} UTC)")
    
    return warnings


def default_version_for(pack, ring):
    """Best guess for a new pin's version: the source ring's pin, else the
    Packs/ currentVersion for an MSSP-authored pack, else '' (operator types it)."""
    
    # Try Block - Try looking up the version from the source ring and pass on any failure.
    try:
        
        # Define variable (dict) of policies.yml, empty when missing
        pol = resolve.load(resolve.FLEET / "policies.yml") or {}
        
        # Define variable (str) of the source ring, None for a head ring
        source = ((pol.get("rings") or {}).get(ring) or {}).get("source")
        
        # Conditional - IF the ring has a source ring, THEN look up the pack there. ELSE, skip
        if source:
            # Define variable (dict) of the source ring's pins
            src = resolve.load_pins(source)
            # Conditional - IF the pack is pinned in the source ring, THEN return that version. ELSE, fall through
            if pack in src:
                return str(src[pack])
    
    except Exception:  # noqa: BLE001
        pass
    
    # Define variable (dict) of the MSSP-authored packs under Packs/
    local = resolve.local_packs()
    
    # Conditional - IF the pack is MSSP-authored with a version, THEN return it. ELSE, return empty
    if pack in local and local[pack].get("version"):
        
        return str(local[pack]["version"])
    
    return ""


class TenantTUIApp(App):
    """Fleet tenant editor: list, create, edit, rename, delete."""

    # Define variable (str) of the app title
    TITLE = "Fleet Tenant TUI"
    CSS = """
    #tenants {
        height: 1fr;
    }
    .subtitle {
        color: $text-muted;
        padding: 0 1;
    }
    .hints {
        text-align: center;
        color: $text-muted;
        padding: 0 1;
    }
    #edit-body {
        height: 1fr;
        padding: 0 1;
    }
    #form-pane {
        width: 58%;
        border: round $primary;
        padding: 0 1;
    }
    
    /* Input's default CSS is width: 100%, which breaks the HorizontalGroup
       rows — the row inputs share the width fractionally instead. */

    #form-pane HorizontalGroup Input {
        width: 1fr;
    }
    #custom-owner {
        width: 12;
    }
    #side-pane {
        width: 42%;
        padding: 0 1;
    }
    .section {
        text-style: bold;
        color: $primary;
        margin-top: 1;
    }
    #validation {
        min-height: 5;
        border: round $secondary;
        padding: 1;
        text-wrap: wrap;
    }
    #preview {
        height: 1fr;
        border: round $secondary;
        padding: 1;
        text-wrap: wrap;
    }
    #edit-footer {
        height: 3;
        content-align: center middle;
        padding: 0 1;
    }
    .modal-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .modal-sub {
        color: $text-muted;
    }
    .warn {
        color: $error;
    }
    .modal-actions {
        height: 3;
        content-align: center middle;
        margin-top: 1;
    }
    .pin-pack {
        width: 45;
        padding: 0 1;
    }
    .pin-ver {
        width: 1fr;
    }
    """
    
    # Define variable (list) of the app key bindings
    BINDINGS = [("q", "quit", "Quit")]


    def __init__(self, initial_tenant=None):
        super().__init__()
        # Set variable initial_tenant (str) to the tenant to open, else None
        self.initial_tenant = initial_tenant


    def on_mount(self):

        # Conditional - IF an initial tenant was given and exists, THEN open its edit screen. ELSE, show the list
        if self.initial_tenant and self.initial_tenant in resolve.all_tenants():
            self.push_screen(TenantEditScreen(name=self.initial_tenant))

        else:

            # NOTE: an unknown tenant name falls back to the list instead of
            # crashing, with a notice so the typo is visible
            # Conditional - IF an initial tenant was given but is unknown, THEN notify the error. 
            if self.initial_tenant:
                self.notify(f"no tenant '{self.initial_tenant}'",
                            severity="error")

            self.push_screen(TenantListScreen())


class TenantListScreen(Screen):
    """The default screen: every tenant with its ring and resolve status."""


    # Define variable (list) of the screen key bindings
    BINDINGS = [
        ("n", "new", "New tenant"),
        ("e", "edit", "Edit"),
        ("d", "delete", "Delete"),
        ("r", "refresh", "Refresh"),
    ]


    def compose(self):
        yield Header()
        yield Static("", id="list-subtitle", classes="subtitle")
        with VerticalScroll(id="table-box"):
            yield DataTable(id="tenants")
        yield Static("n new · e edit · d delete · r refresh · q quit",
                     classes="hints")
        yield Footer()


    def on_mount(self):
        # Set variable _names (list) to the tenant names in table order
        self._names = []
        # Define variable (DataTable) of the tenant table
        table = self.query_one("#tenants", DataTable)
        table.add_columns("tenant", "display", "ring", "creds",
                          "groups", "extras", "overrides", "status")
        table.cursor_type = "row"
        self.refresh_table()


    def refresh_table(self):
        # Define variable (DataTable) of the tenant table
        table = self.query_one("#tenants", DataTable)
        table.clear()
        # Define variable (list) of the tenant rows to display
        rows = list_tenants()
        # Set variable _names (list) to the tenant names in table order
        self._names = [row["name"] for row in rows]
        # For Loop - Iterates through each tenant row adding it to the table
        for row in rows:
            # Define variable (Text) of the ring cell, colored by ring
            ring = Text(row["ring"],
                        style=f"bold {RING_COLORS.get(row['ring'], 'white')}")
            # Conditional - IF the tenant resolves cleanly, THEN show green status. ELSE, show the error in red
            if row["ok"]:
                # Define variable (Text) of the status cell, green when clean
                status = Text("✓ resolves", style="green")
            else:
                status = Text(row["status"], style="red")
            table.add_row(row["name"], row["display"], ring, row["creds"],
                          row["groups"], row["extras"], row["overrides"],
                          status, key=row["name"])
        self.query_one("#list-subtitle", Static).update(
            f"fleet/tenants/ — {table.row_count} tenant(s) · "
            f"rings: {', '.join(available_rings()) or '—'}")


    def selected_name(self):

        # Define variable (int) of the cursor row, -1 when none is selected
        i = self.query_one("#tenants", DataTable).cursor_row
        # Conditional - IF the cursor is on a valid row, THEN return that name. ELSE, return None
        if 0 <= i < len(self._names):
            return self._names[i]
        return None


    def action_new(self):
        self.app.push_screen(TenantEditScreen(name=None))


    def action_edit(self):
        name = self.selected_name()
        # Conditional - IF no tenant is selected, THEN notify and return. ELSE, open the edit screen
        if not name:
            self.notify("select a tenant first", severity="warning")
            return
        self.app.push_screen(TenantEditScreen(name=name))


    def action_delete(self):
        name = self.selected_name()
        # Conditional - IF no tenant is selected, THEN notify and return. ELSE, proceed with delete
        if not name:
            self.notify("select a tenant first", severity="warning")
            return
        # Set variable _pending_delete (str) to the tenant to delete on confirm
        self._pending_delete = name
        # Try Block - Try checking if this is the ring's last tenant and default to False on failure.
        try:
            # Define variable (bool) of whether this is the ring's last tenant
            last = is_last_in_ring(name)
        except Exception:  # noqa: BLE001
            last = False
        self.app.push_screen(ConfirmDeleteModal(name, last),
                             callback=self._on_delete_confirmed)


    def _on_delete_confirmed(self, result):

        # Define variable (str) of the tenant to delete, from the pending flag
        name = self._pending_delete
        self._pending_delete = None

        # Conditional - IF the user cancelled, THEN return. ELSE, delete the tenant
        if not result:
            return

        # Try Block - Try deleting the tenant file and notify the error on failure.
        try:
            delete_tenant(name)

        except TuiError as e:
            self.notify(str(e), severity="error")
            return

        self.notify(f"deleted fleet/tenants/{name}.yml")
        self.refresh_table()


    def action_refresh(self):
        self.refresh_table()


class TenantEditScreen(Screen):
    """Create (name=None) or edit an existing tenant profile.

    Left pane: the manifest form. Right pane: live validation (the real
    resolver's own errors) and a live YAML preview of the file to be written.
    """


    def __init__(self, name):
        super().__init__()
        # (not `self.name` — Widget.name is a read-only property in Textual)
        self.tenant_name = name
        # Set variable editing (bool) to True when editing an existing tenant
        self.editing = name is not None
        # Set variable _creds_touched (bool) to True once the ref is edited
        self._creds_touched = False
        # Set variable _validate_timer (Timer) to the pending validation
        self._validate_timer = None
        # Set variable _pending_save (dict) to the manifest to save
        self._pending_save = None


    def compose(self):

        # Define variable (list) of the available rings for the select
        rings = available_rings()

        # Define variable (list) of the registered customer namespace prefixes
        prefixes = registered_prefixes()

        # Define variable (SuggestFromList) of pack id autocompletion
        suggester = SuggestFromList(pack_suggestions(), case_sensitive=False)

        yield Header()

        with Horizontal(id="edit-body"):

            with VerticalScroll(id="form-pane"):

                yield Label("Identity", classes="section")

                yield Input(
                    placeholder="lowercase alphanumeric — becomes the file name",
                    id="tenant-name", disabled=self.editing)

                yield Input(
                    placeholder="human name (defaults to the tenant name)",
                    id="display-name")

                yield Select.from_values(
                    rings, prompt="ring (fleet/pins/<ring>.yml)",
                    allow_blank=not rings, id="ring")

                yield Input(
                    placeholder="env-var prefix for the <REF>_CREDS CI secret",
                    id="creds-ref")

                yield Input(
                    placeholder="host guard override (default: the tenant name)",
                    id="host-token")

                yield Select.from_values(
                    [NO_NAMESPACE] + prefixes, prompt="customer namespace",
                    value=NO_NAMESPACE, allow_blank=False, id="customer-ns")

                yield Label("Groups — bundles from fleet/groups/",
                            classes="section")

                with Vertical(id="groups-box"):

                    # For Loop - Iterates through each group building a checkbox
                    for gname, gdesc in available_groups().items():

                        # Define variable (str) of the label, truncated
                        label = f"{gname} — {gdesc}" if gdesc else gname

                        # Conditional - IF the label is too long, THEN truncate it. ELSE, keep it as is
                        if len(label) > 78:
                            label = label[:77] + "…"

                        yield Checkbox(label, id=f"group-{gname}")

                # Conditional - IF no groups are defined, THEN show a placeholder. 
                if not available_groups():

                    yield Static("  (no groups defined in fleet/groups/)",
                                 classes="subtitle")

                yield Label("Extras — vendor pack ids", classes="section")

                with HorizontalGroup():

                    yield Input(
                        placeholder="pack id — type to autocomplete",
                        id="extra-input",
                        suggester=suggester)

                    yield Button("+", id="extra-add")

                    yield Button("−", id="extra-remove")

                yield DataTable(id="extras")

                yield Label("Pin overrides — hold this tenant at a version",
                            classes="section")

                with HorizontalGroup():

                    yield Input(placeholder="pack id", id="override-pack",
                                suggester=suggester)

                    yield Input(placeholder="version or local",
                                id="override-ver")

                    yield Button("+", id="override-add")

                    yield Button("−", id="override-remove")

                yield DataTable(id="overrides")

                yield Label("Custom — customer-owned, referenced only",
                            classes="section")

                with HorizontalGroup():

                    yield Input(
                        placeholder="pack id (registered prefix)",
                        id="custom-id",
                        suggester=suggester)

                    yield Input(value="customer", id="custom-owner")

                    yield Button("+", id="custom-add")

                    yield Button("−", id="custom-remove")

                yield DataTable(id="custom")

            with Vertical(id="side-pane"):

                yield Label("Validation", classes="section")

                yield Static("…", id="validation")

                yield Button("Add missing pins…", id="fix-pins",
                             variant="warning", disabled=True)

                yield Button("Remove unused pins…", id="prune-pins",
                             variant="error", disabled=True)

                yield Label("YAML preview", classes="section")

                yield Static("", id="preview")

        with Horizontal(id="edit-footer"):

            # Conditional - IF editing an existing tenant, THEN show the rename button. 
            if self.editing:
                yield Button("Rename…", id="rename")

            yield Button("Cancel", id="cancel")

            yield Button("Save", id="save", variant="success")

        yield Footer()


    def on_mount(self):

        self.query_one("#extras", DataTable).add_columns("pack")
        self.query_one("#overrides", DataTable).add_columns("pack", "version")
        self.query_one("#custom", DataTable).add_columns("id", "owner")

        # For Loop - Iterates through each table setting row cursor type
        for sel in ("#extras", "#overrides", "#custom"):
            self.query_one(sel, DataTable).cursor_type = "row"

        # Conditional - IF editing an existing tenant, THEN populate the form. 
        if self.editing:

            # NOTE: a broken saved file shows an error but still opens,
            # so the operator can fix and re-save instead of losing the screen
            try:

                self._populate(resolve.load_tenant(self.tenant_name) or {})

            except resolve.ResolveError as e:

                self.notify(str(e), severity="error")

        self._schedule_validate()

    def _populate(self, m):

        self.query_one("#tenant-name", Input).value = str(
            m.get("tenant") or self.tenant_name)

        self.query_one("#display-name", Input).value = str(
            m.get("display_name") or "")

        # Define variable (str) of the ring to select, None when unset
        ring = m.get("ring")

        # Conditional - IF a ring is set, THEN select it in the dropdown. 
        if ring:

            # Try Block - Try setting the ring select and pass when the ring has no pin file.
            try:

                self.query_one("#ring", Select).value = ring

            except Exception:  # noqa: BLE001 — ring has no pin file; validation says

                pass

        self.query_one("#creds-ref", Input).value = str(
            m.get("credentials_ref") or "")

        self.query_one("#host-token", Input).value = str(
            m.get("host_token") or "")

        # Define variable (str) of the customer namespace to select, else None
        ns = m.get("customer_namespace")

        # Conditional - IF a namespace is set, THEN select it in the dropdown. 
        if ns:

            # Try Block - Try setting the namespace select and pass when the prefix is unregistered.
            try:

                self.query_one("#customer-ns", Select).value = ns

            except Exception:  # noqa: BLE001 — unregistered prefix; validation says

                pass

        # For Loop - Iterates through each group checkbox setting its checked state
        for cb in self.query("#groups-box Checkbox"):

            cb.value = cb.id.removeprefix("group-") in (m.get("groups") or [])

        self._fill_table("#extras", [(p,) for p in m.get("extras") or []])

        self._fill_table(
            "#overrides",
            [(p, str(v)) for p, v in (m.get("pin_overrides") or {}).items()])

        self._fill_table(
            "#custom",
            [(str(c.get("id") or ""), str(c.get("owner") or "customer"))
             for c in m.get("custom") or []])


    def _fill_table(self, sel, rows):

        # Define variable (DataTable) of the table to fill
        table = self.query_one(sel, DataTable)
        table.clear()

        # Define variable (set) of row keys already added, to skip duplicates
        seen = set()

        # For Loop - Iterates through each row adding it to the table
        for r in rows:

            # Conditional - IF the row key was already added, THEN skip it. ELSE, add the row
            if r[0] in seen:

                continue

            seen.add(r[0])

            table.add_row(*r, key=str(r[0]))


    def _table_rows(self, sel):

        # Define variable (DataTable) of the table to read
        table = self.query_one(sel, DataTable)

        return [table.get_row_at(i) for i in range(table.row_count)]

    def _collect(self):

        # Define variable (str) of the selected ring, None when blank
        ring = self.query_one("#ring", Select).selection

        # Define variable (str) of the selected customer namespace
        ns = self.query_one("#customer-ns", Select).selection

        # Define variable (dict) of the manifest collected from the form
        m = {
            "tenant": self.query_one("#tenant-name", Input).value.strip(),
            "display_name": self.query_one("#display-name", Input).value.strip(),
            "ring": ring or "",
            "credentials_ref": self.query_one("#creds-ref", Input).value.strip(),
            "groups": [cb.id.removeprefix("group-")
                       for cb in self.query("#groups-box Checkbox") if cb.value],
            "extras": [str(c[0]) for c in self._table_rows("#extras")],
            "pin_overrides": {str(c[0]): str(c[1])
                              for c in self._table_rows("#overrides")},
            "custom": [{"id": str(c[0]), "owner": str(c[1])}
                       for c in self._table_rows("#custom")],
        }

        # Define variable (str) of the host token, '' when unset
        host = self.query_one("#host-token", Input).value.strip()

        # Conditional - IF a host token is set, THEN add it to the manifest. 
        if host:
            m["host_token"] = host

        # Conditional - IF a namespace is selected and is not the default, THEN add it to the manifest. 
        if ns and ns != NO_NAMESPACE:
            m["customer_namespace"] = ns

        return m


    # ------------------------------------------------------------- validation

    def _schedule_validate(self):

        # Conditional - IF a validation timer is pending, THEN stop it. 
        if self._validate_timer is not None:
            self._validate_timer.stop()

        # NOTE: debounced 0.25s so typing a name doesn't run the resolver on
        # every keystroke; the resolver copies the fleet tree, so it is slow
        self._validate_timer = self.set_timer(0.25, self._validate)

    def _validate(self):

        # Set variable _validate_timer (Timer) to None, validation is running
        self._validate_timer = None

        # Define variable (dict) of the manifest collected from the form
        m = self._collect()

        # Define variables (list, list) of validation errors and warnings
        errors, warnings = validate_manifest(m, m["tenant"],
                                             exists=self.editing)

        # Define variable (list) of composed packs with no pin or override
        missing = missing_pins(m)

        # Define variable (Text) of the validation panel content
        text = Text()

        # For Loop - Iterates through each error appending it to the panel
        for e in errors:
            text.append(f"✗ {e}\n", style="red")

        # For Loop - Iterates through each warning appending it to the panel
        for w in warnings:
            text.append(f"⚠ {w}\n", style="yellow")

        # Conditional - IF there are missing pins, THEN append a hint. 
        if missing:
            text.append(
                f"→ {len(missing)} composed pack(s) have no pin in "
                f"fleet/pins/{m['ring']}.yml — use [Add missing pins…]\n",
                style="dim")

        # Conditional - IF there are no errors, THEN show the clean message. 
        if not errors:
            text.append("✓ resolves cleanly", style="bold green")

        self.query_one("#validation", Static).update(text)
        self.query_one("#preview", Static).update(Text(render_yaml(m)))
        self.query_one("#save", Button).disabled = bool(errors)
        self.query_one("#fix-pins", Button).disabled = not missing

        # Define variable (list) of pins no tenant in the ring still needs
        unused = unused_pins(m["ring"], editing=(m["tenant"], m))

        self.query_one("#prune-pins", Button).disabled = not unused


    def on_input_changed(self, event):

        # Conditional - IF the tenant name changed on a new tenant, THEN sync the creds ref. ELSE, check for creds edits
        if event.input.id == "tenant-name" and not self.editing:

            # Keep credentials_ref in sync with the tenant name until the
            # operator types a custom ref (the CI secret is <REF>_CREDS).
            if not self._creds_touched:
                self.query_one("#creds-ref", Input).value = (
                    event.value.strip().upper())

        # Conditional - IF the creds ref changed, THEN check if it diverged from the tenant name. 
        elif event.input.id == "creds-ref":

            # Define variable (str) of the tenant name, to compare the ref
            tenant = self.query_one("#tenant-name", Input).value.strip()

            # Conditional - IF the ref no longer matches the tenant name, THEN mark it as touched. 
            if event.value.strip() != tenant.upper():
                self._creds_touched = True

        self._schedule_validate()


    def on_select_changed(self, event):
        self._schedule_validate()


    def on_checkbox_changed(self, event):
        self._schedule_validate()

    ### actions ###

    def _add_row(self, sel, input_ids):

        # Define variable (list) of the input values to add as a row
        values = [self.query_one(i, Input).value.strip() for i in input_ids]

        # Conditional - IF any field is empty, THEN notify and return. ELSE, add the row
        if not all(values):
            self.notify("fill in all fields first", severity="warning")
            return

        # Define variable (DataTable) of the table to add the row to
        table = self.query_one(sel, DataTable)

        # Try Block - Try looking up the row to detect duplicates and pass when it is not present.
        try:

            table.get_row_index(values[0])

            self.notify(f"{values[0]} is already listed", severity="warning")

            return

        except Exception:  # noqa: BLE001 — RowDoesNotExist: not present, good

            pass

        table.add_row(*values, key=values[0])

        # For Loop - Iterates through each input clearing its value
        for i in input_ids:
            self.query_one(i, Input).value = ""

        self._schedule_validate()


    def _remove_cursor_row(self, sel):

        # Define variable (DataTable) of the table to remove the row from
        table = self.query_one(sel, DataTable)

        # Define variable (int) of the cursor row, -1 when none is selected
        i = table.cursor_row

        # Conditional - IF the cursor is on a valid row, THEN remove it. 
        if 0 <= i < table.row_count:
            table.remove_row(str(table.get_row_at(i)[0]))
            self._schedule_validate()


    async def on_button_pressed(self, event):

        # Define variable (str) of the pressed button id
        bid = event.button.id

        # Conditional - IF the extra-add button was pressed, THEN add an extra row. ELSE, check the next button
        if bid == "extra-add":
            self._add_row("#extras", ("#extra-input",))

        # Conditional - IF the override-add button was pressed, THEN add an override row. ELSE, check the next button
        elif bid == "override-add":
            self._add_row("#overrides", ("#override-pack", "#override-ver"))

        # Conditional - IF the custom-add button was pressed, THEN add a custom row. ELSE, check the next button
        elif bid == "custom-add":
            self._add_row("#custom", ("#custom-id", "#custom-owner"))

        # Conditional - IF a remove button was pressed, THEN remove the cursor row. ELSE, check the next button
        elif bid in ("extra-remove", "override-remove", "custom-remove"):
            self._remove_cursor_row(
                {"extra": "#extras", "override": "#overrides",
                 "custom": "#custom"}[bid.split("-")[0]])

        # Conditional - IF the cancel button was pressed, THEN pop the screen. ELSE, check the next button
        elif bid == "cancel":
            self.app.pop_screen()

        # Conditional - IF the save button was pressed, THEN save the tenant. ELSE, check the next button
        elif bid == "save":
            await self._save()

        # Conditional - IF the fix-pins button was pressed, THEN open the add-pins modal. ELSE, check the next button
        elif bid == "fix-pins":

            # Define variable (dict) of the manifest collected from the form
            m = self._collect()

            # Define variable (list) of composed packs with no pin or override
            missing = missing_pins(m)

            # Conditional - IF there are missing pins, THEN open the modal. 
            if missing:

                # Set variable _pending_save (dict) to None, standalone fix
                self._pending_save = None

                await self.app.push_screen(
                    AddPinsModal(m["ring"], missing),
                    callback=lambda _r: self._schedule_validate())

        # Conditional - IF the prune-pins button was pressed, THEN open the remove-pins modal. ELSE, check the next button
        elif bid == "prune-pins":

            # Define variable (dict) of the manifest collected from the form
            m = self._collect()

            # Define variable (list) of pins no tenant in the ring still needs
            unused = unused_pins(m["ring"], editing=(m["tenant"], m))

            # Conditional - IF there are unused pins, THEN open the modal. 
            if unused:

                await self.app.push_screen(
                    RemovePinsModal(m["ring"], unused),
                    callback=lambda _r: self._schedule_validate())

        # Conditional - IF the rename button was pressed, THEN open the rename modal. 
        elif bid == "rename":
            self.app.push_screen(RenameModal(self.tenant_name),
                                 callback=self._after_rename)


    async def _save(self):

        # Define variable (dict) of the manifest collected from the form
        m = self._collect()

        # Define variable (list) of validation errors, warnings ignored here
        errors, _ = validate_manifest(m, m["tenant"], exists=self.editing)

        # Conditional - IF there are validation errors, THEN notify and return. ELSE, continue saving
        if errors:
            self.notify("fix the validation errors first", severity="error")
            return

        # Define variable (list) of composed packs with no pin or override
        missing = missing_pins(m)

        # Conditional - IF there are missing pins, THEN open the add-pins modal. ELSE, save directly
        if missing:

            # Set variable _pending_save (dict) to the manifest to save
            self._pending_save = m

            await self.app.push_screen(
                AddPinsModal(m["ring"], missing), callback=self._after_pins)

            return

        self._do_save(m)


    def _after_pins(self, result):

        self._schedule_validate()

        # Conditional - IF the user confirmed and a save is pending, THEN check and save. 
        if result and self._pending_save is not None:

            # Define variable (dict) of the manifest to save
            m = self._pending_save
            self._pending_save = None

            # NOTE: re-check after the pins landed; the operator may have
            # cancelled the modal or left a version blank
            if not missing_pins(m):
                self._do_save(m)


    def _do_save(self, m):


        # Try Block - Try saving the tenant file and notify the error on failure.
        try:

            # Define variable (Path) of the saved tenant file
            path = save_tenant(m["tenant"], m)

        except Exception as e:  # noqa: BLE001
            self.notify(str(e), severity="error")
            return

        # Try Block - Try making the path relative and fall back to the absolute path on failure.
        try:

            # Define variable (Path) of the path relative to the repo
            rel = path.relative_to(resolve.REPO)

        except ValueError:

            rel = path

        self.notify(f"saved {rel}")
        self.app.pop_screen()


    def _after_rename(self, result):

        # Conditional - IF the rename was confirmed, THEN swap to a fresh edit screen. 
        if result:

            # NOTE: swap to a fresh edit screen for the new name, since the
            # tenant name field is disabled on an editing screen
            self.app.pop_screen()
            self.app.push_screen(TenantEditScreen(name=result))


class AddPinsModal(ModalScreen[bool]):
    """Prompt for versions of composed packs that have no pin in the ring."""


    def __init__(self, ring, missing):
        super().__init__()

        # Set variable ring (str) to the ring the pins go into
        self.ring = ring
        self.missing = missing  # [(pack, source)]


    def compose(self):


        yield Static(f"Add missing pins to fleet/pins/{self.ring}.yml",
                     classes="modal-title")

        yield Static("These packs are composed by the tenant but have no pin "
                     "in the ring's pin file (and no pin_override).",
                     classes="modal-sub")


        # For Loop - Iterates through each missing pack building an input row
        for i, (pack, source) in enumerate(self.missing):


            with HorizontalGroup():

                yield Static(f"{pack}  ({source})", classes="pin-pack")

                yield Input(placeholder="version or local", id=f"pin-ver-{i}",
                            classes="pin-ver",
                            value=default_version_for(pack, self.ring))

        yield Static("", id="pin-warnings")


        with Horizontal(classes="modal-actions"):


            yield Button("Cancel", id="pin-cancel")
            yield Button("Add pins", id="pin-confirm", variant="success")


    def on_mount(self):
        self._refresh_warnings()


    def on_input_changed(self, event):
        # Conditional - IF a version input changed, THEN refresh the warnings. 
        if str(event.input.id or "").startswith("pin-ver-"):
            self._refresh_warnings()


    def _versions(self):

        # Define variable (dict) of pack to version, read from the inputs
        return {pack: self.query_one(f"#pin-ver-{i}", Input).value.strip()
                for i, (pack, _) in enumerate(self.missing)}


    def _refresh_warnings(self):

        # Define variable (Text) of the warnings panel content
        text = Text()


        # For Loop - Iterates through each warning appending it to the panel
        for w in pin_warnings(self.ring, self._versions()):
            text.append(f"⚠ {w}\n", style="yellow")

        self.query_one("#pin-warnings", Static).update(text)

    async def on_button_pressed(self, event):

        # Conditional - IF the cancel button was pressed, THEN dismiss. ELSE, check for confirm
        if event.button.id == "pin-cancel":
            self.dismiss(False)

        # Conditional - ELSE IF the confirm button was pressed, THEN validate and add the pins. 
        elif event.button.id == "pin-confirm":

            # Define variable (dict) of pack to version to add
            versions = self._versions()


            # For Loop - Iterates through each version validating its format
            for pack, ver in versions.items():

                # Conditional - IF the version is empty, THEN notify and return. ELSE, check the format
                if not ver:

                    self.notify(f"version for {pack} is empty",
                                severity="error")

                    return

                # Conditional - IF the version is not local and does not match the pattern, THEN notify and return. ELSE, continue
                if ver != "local" and not VERSION_RE.match(ver):

                    self.notify(f"'{ver}' for {pack} doesn't look like a "
                                "version (x.y.z) or 'local'",
                                severity="error")

                    return

            # Try Block - Try adding the pins and keep the modal open on failure.
            try:

                add_pins(self.ring, versions)

            except TuiError as e:

                self.notify(str(e), severity="error")

                return

            self.notify(f"added {len(versions)} pin(s) to "
                        f"fleet/pins/{self.ring}.yml")

            self.dismiss(True)


class RemovePinsModal(ModalScreen[bool]):
    """Remove pins no tenant in the ring composes (runbook step 2)."""


    def __init__(self, ring, unused):
        super().__init__()

        # Set variable ring (str) to the ring the pins come from
        self.ring = ring
        self.unused = unused  # [(pack, version)]

    def compose(self):


        yield Static(f"Remove unused pins from fleet/pins/{self.ring}.yml",
                     classes="modal-title")


        yield Static("No tenant in this ring composes these packs (given your "
                     "current edit), so the pins deploy nothing. This is the "
                     "runbook's step-2 cleanup.", classes="modal-sub")


        # For Loop - Iterates through each unused pin displaying it
        for pack, ver in self.unused:

            yield Static(f"  {pack}  ({ver})")

        yield Static("⚠ Save this tenant's changes afterwards: if you cancel "
                     "the edit, the saved tenant may still compose these "
                     "packs and resolve will fail until the pins are back.",
                     classes="warn")

        yield Static("The packs drop off the tenants at the next converge "
                     "(bounded deletable set).", classes="modal-sub")

        with Horizontal(classes="modal-actions"):

            yield Button("Cancel", id="prune-cancel")
            yield Button("Remove pins", id="prune-confirm", variant="error")


    async def on_button_pressed(self, event):

        # Conditional - IF the cancel button was pressed, THEN dismiss. ELSE, check for confirm
        if event.button.id == "prune-cancel":

            self.dismiss(False)

        # Conditional - IF the confirm button was pressed, THEN remove the pins. 
        elif event.button.id == "prune-confirm":

            # NOTE: the modal keeps the app open on a TuiError so the operator
            # sees why the removal failed instead of losing the list of pins

            # Try Block - Try removing the pins and keep the modal open on failure.
            try:

                remove_pins(self.ring, [p for p, _ in self.unused])

            except TuiError as e:

                self.notify(str(e), severity="error")

                return

            self.notify(f"removed {len(self.unused)} unused pin(s) from "
                        f"fleet/pins/{self.ring}.yml")

            self.dismiss(True)


class ConfirmDeleteModal(ModalScreen[bool]):
    """Delete confirmation, with a warning when the tenant is its ring's last."""


    def __init__(self, name, last_in_ring):
        super().__init__()

        # (not `self.name` — Widget.name is a read-only property in Textual)
        self.tenant_name = name

        # Set variable last (bool) to True when this is the ring's last tenant
        self.last = last_in_ring


    def compose(self):

        yield Static(f"Delete tenant '{self.tenant_name}'?",
                     classes="modal-title")

        yield Static(f"This removes fleet/tenants/{self.tenant_name}.yml "
                     "from the working tree.")

        # Conditional - IF this is the ring's last tenant, THEN show the warning. 
        if self.last:

            yield Static("⚠ This is the last tenant in its ring — "
                         "resolve --ring and check.sh will fail until you add "
                         "another tenant or drop the ring.", classes="warn")

        with Horizontal(classes="modal-actions"):

            yield Button("Cancel", id="del-cancel")
            yield Button("Delete", id="del-confirm", variant="error")


    async def on_button_pressed(self, event):
        self.dismiss(event.button.id == "del-confirm")


class RenameModal(ModalScreen[str]):
    """Rename: moves the file and updates the `tenant:` field."""


    def __init__(self, old):
        super().__init__()

        # Set variable old (str) to the current tenant name
        self.old = old


    def compose(self):

        yield Static(f"Rename tenant '{self.old}'", classes="modal-title")

        yield Static("Moves the file and updates the `tenant:` field. The "
                     "XSIAM host must still match the new name (host guard).",
                     classes="modal-sub")

        yield Input(value=self.old, id="rename-input")

        with Horizontal(classes="modal-actions"):

            yield Button("Cancel", id="rename-cancel")

            yield Button("Rename", id="rename-confirm", variant="success")


    async def on_button_pressed(self, event):

        # Conditional - IF the cancel button was pressed, THEN dismiss. ELSE, check for confirm
        if event.button.id == "rename-cancel":

            self.dismiss("")

        # Conditional - IF the confirm button was pressed, THEN rename the tenant. 
        elif event.button.id == "rename-confirm":

            # Define variable (str) of the new tenant name
            new = self.query_one("#rename-input", Input).value.strip()

            # Try Block - Try renaming the tenant and keep the modal open on failure.
            try:

                rename_tenant(self.old, new)

            except TuiError as e:

                self.notify(str(e), severity="error")

                return

            self.notify(f"renamed {self.old} → {new}")
            self.dismiss(new)


def main():
    """Run the TUI, opening the tenant from argv[1] when given."""

    # Define variable (str) of the tenant to open from argv, None for the list
    initial = sys.argv[1] if len(sys.argv) > 1 else None

    TenantTUIApp(initial_tenant=initial).run()


if __name__ == "__main__":
    main()



