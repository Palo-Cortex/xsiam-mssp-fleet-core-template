"""XSIAM tenant checks for the setup wizard and doctor.

`check_credentials` makes the SAME read-only call converge makes first
(GET /xsoar/public/v1/contentpacks/metadata/installed) with the same headers,
so "the wizard accepted this key" means "converge will be able to use it".
Failures are translated into what to do about them.
"""
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

INSTALLED_PATH = "/xsoar/public/v1/contentpacks/metadata/installed"

HOW_TO_CREATE_KEY = """\
In the XSIAM console of this tenant:
  1. Settings → Configurations → Integrations → API Keys → + New Key
  2. Security Level: Standard      Role: Instance Administrator
     (content upload and pack removal need it; give the key a clear comment,
      e.g. "fleet CI", and an expiry you will track)
  3. Copy the KEY now — XSIAM shows it only once.
  4. In the keys table, note the key's ID number.
  5. Click "Copy API URL" (top right of the API Keys page). It looks like
     https://api-<tenant>.xdr.<region>.paloaltonetworks.com
"""


def normalize_api_url(raw):
    """(url, note): a clean https://host URL, plus a note when we changed
    something the user should know about (e.g. added the api- prefix)."""
    s = raw.strip()
    if not s:
        return "", None
    if "://" not in s:
        s = "https://" + s
    p = urllib.parse.urlparse(s)
    host = (p.hostname or "").lower()
    note = None
    if p.scheme != "https":
        note = "switched to https"
    if host.endswith(".paloaltonetworks.com") and not host.startswith("api-"):
        host = "api-" + host
        note = ("that looked like the console URL — using the API URL "
                f"https://{host} (the API Keys page's \"Copy API URL\" gives this form)")
    return f"https://{host}", note


def tenant_label(url):
    """The tenant part of an API host: https://api-acme-dev.xdr.us... -> acme-dev."""
    host = urllib.parse.urlparse(url).hostname or ""
    first = host.split(".")[0]
    return first[4:] if first.startswith("api-") else first


def suggest_tenant_name(url):
    label = re.sub(r"[^a-z0-9-]", "", tenant_label(url).lower()).strip("-")
    return label[:40] or None


def check_credentials(url, key_id, key, timeout=20, opener=None):
    """(ok, message, installed_count|None). Read-only."""
    req = urllib.request.Request(url.rstrip("/") + INSTALLED_PATH, method="GET", headers={
        "Authorization": key, "x-xdr-auth-id": str(key_id),
        "Content-Type": "application/json", "Accept": "application/json"})
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, _http_reason(e.code), None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLError):
            return False, f"TLS error talking to {url} ({reason}) — is this the API URL?", None
        return False, (f"can't reach {url} ({reason}). Check the URL, your network/VPN, "
                       f"and that this machine may reach the tenant"), None
    except Exception as e:  # noqa: BLE001 — timeouts, resets
        return False, f"request to {url} failed: {e}", None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, ("the URL answered, but not with the content API — use the "
                       "\"Copy API URL\" value (https://api-<tenant>...)"), None
    count = len(data) if isinstance(data, list) else None
    return True, (f"key works — {count} content pack(s) installed" if count is not None
                  else "key works"), count


def _http_reason(code):
    if code == 401:
        return ("HTTP 401 — the key or key ID is wrong, the key was deleted, or it "
                "expired. Re-copy both (the ID is the number in the keys table)")
    if code == 403:
        return ("HTTP 403 — the key is valid but its role can't manage content. "
                "Create it with the Instance Administrator role")
    if code in (404, 405):
        return (f"HTTP {code} — this isn't the tenant's API URL. Use \"Copy API URL\" "
                f"on the API Keys page (https://api-<tenant>.xdr.<region>...)")
    if code == 429:
        return "HTTP 429 — rate limited by the tenant; wait a minute and retry"
    if code >= 500:
        return (f"HTTP {code} — the tenant returned a server error. If it repeats, "
                f"check the URL is the API URL and the tenant is healthy")
    return f"HTTP {code} from the tenant"
