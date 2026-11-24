#!/usr/bin/env python3
import argparse
import urllib.request
import urllib.error
import json
import yaml
from pathlib import Path
import re
from resolve import resolve, ResolveError
from deploy_tenant import load_creds, assert_host_matches, DeployError
import sys

ENDPOINT = "/xsoar/public/v1/contentpacks/metadata/installed"
STATE_FILE = Path("installed_state.yml")
CRED_ENV = ("DEMISTO_BASE_URL", "DEMISTO_API_KEY", "XSIAM_AUTH_ID")

def ensure_installed_state_exists(filepath: str = "installed_state.yml") -> bool:
    """
    Checks if the file exists, creating it (and parent directories) if it does not.
    """

    # Instantiate variable (PurePath) using filepath string 
    path = Path(filepath)

    # Conditional - IF the path/file exists, THEN return True
    if path.exists():
        return True

    # Method call to mkdir to create directory for file
    path.parent.mkdir(parents=True, exist_ok=True)

    # MEthod call to touch to create file
    path.touch()

    # Conditional - IF path/file exists on recheck, THEN return True. ELSE return False
    if path.exists():

        return True

    else:

        return False

def load_installed_state(filepath: str = "installed_state.yml") -> dict:
    """
    Loads installed_state.yml into a Python dictionary.
    Returns an empty dict if the file is empty or missing.
    """

    # Instantiate variable (PurePath) using filepath string
    path = Path(filepath)

    # Conditional - IF path is NOT an existing file, THEN return expty dict object
    if not path.is_file():
        return {}

    # With Statement - open file path with method call to yaml.safe_load()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Return Conditional - return yaml dict IF it IS a dict, ELSE return empty dict
    return data if isinstance(data, dict) else {}    

def main():

    # Define variable (ArgumentParser) with description and args
    parser = argparse.ArgumentParser(
        description="Generate data for installed state for tenant.\n"
        "Adds tenant installed content packs to installed_state.yml.\n"
    )

    # Method call to add tenant arg to parser.
    parser.add_argument("-t", "--tenant", required=True, help="tenant name to update packs installed state")

    # Define variable (NAMESPACE) with arguments form parser.
    args = parser.parse_args()

    # Define variable (str) of tenant argument provided.
    tenant = args.tenant

    # Try Block - Try creds and host assertion checks and ourput Error to stderr and exit if fails.
    try:

        # Define variable (dict) via function call to resolve with tenant (str) as argument
        resolved_tenant = resolve(tenant)

        # Define variable (dict) via function call to load_creds() 
        creds = load_creds()

        print(creds.get("DEMISTO_BASE_URL"))

        # Function call to assert_host_matches() with reolved tenan
        assert_host_matches(resolved_tenant, creds)

    except ResolveError as e:

        print(f"ResolveError: {e}", file=sys.stderr)
        sys.exit(1)

    except DeployError as e:

        print(f"DeployError: {e}", file=sys.stderr)
        sys.exit(1)

    # Define variable (str) of API url and REST API endpoint
    url = creds["DEMISTO_BASE_URL"].rstrip("/") + ENDPOINT

    # Define variable (dict) or headers for api request
    headers = {
        "Authorization": creds["DEMISTO_API_KEY"],
        "x-xdr-auth-id": creds["XSIAM_AUTH_ID"],
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Try block - TODO: comment this
    try:

        # Define variable (Request) via GET method call to make request.
        req = urllib.request.Request(url, method="GET", headers=headers)

        # With Statement - open request and perform work
        with urllib.request.urlopen(req, timeout=30) as response:


            # Define variable (int) with status of response
            status = response.status

            # Define variable (str) with the body of the response decoded to utf-8
            body = response.read().decode("utf-8")
            
            print(f"SUCCESS: HTTP {status}")

            # Define variable (list) via method call to json.loads() to transform string to list of dict
            parsed = json.loads(body)

            # Define variable (list) of (dict) objects containing just id and current version in key: value format
            packs = [{p.get("id"): p.get("currentVersion")} for p in parsed] if isinstance(parsed, list) else []

            # Define variable (dict) of items in list object.
            merged_packs = {k: v for item in packs for k, v in item.items()}

            # Conditional - IF function call to ensure_installed_state_exists() fails to find/create file, THEN raise exception
            if not ensure_installed_state_exists():
                raise FileNotFoundError(f"State file could not be created or accessed at: {STATE_FILE.resolve()}")

            # Define variable (dict) of current content of installed state file. Set as empty dict if no data found
            state = load_installed_state() or {}

            # Define variable (str) based on conditonal. Updated if it will be updated, Added if it does not have entry
            action = "Updated" if tenant in state else "Added"

            # Set variable value for tenant name key with value of merged_packs
            # NOTE: This ensures only the tenant added/modified is changed.
            state[tenant] = merged_packs

            # Define variable (str) with contents of method call to yaml.dump()
            raw_yaml = yaml.dump(state, sort_keys=False, default_flow_style=False)

            # Define variable (str) of return from regex call to replace pattern
            # NOTE: this is just to retain formatting and visibility within .yml file for tenants
            formatted_yaml = re.sub(r"\n(?=\S)", "\n\n", raw_yaml)

            # With Statement - open file and write changes to installed_state.yml
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                f.write(formatted_yaml)

            print(f"{action} data for '{tenant}'")

    # Except HTTPErrors and try to print JSON body for troubleshooting        
    except urllib.error.HTTPError as e:

        # Output base code and reason provided.
        print(f"FAILED: HTTP {e.code} {e.reason}")

        # Try Block - Attempt to make the error output readable, output original on failure.
        error_body = ""
        try:

            # Define variable (str) of error by reading and decoding to utf-8
            error_body = e.read().decode("utf-8")

            # Output readable details by loading to json and dumping with indent 2
            print(f"Error Details: {json.dumps(json.loads(error_body), indent=2)}")

        except Exception:

            print(f"Raw HTML/Text Error Body:\n{error_body}") # output less readible on fail.

        sys.exit(1)  # a failed fetch must fail the job, not look like success

    # Except URLError and output reason
    except urllib.error.URLError as e:

        print(
            "NETWORK ERROR: Could not reach host. "
            f"Verify DEMISTO_BASE_URL used for credentials. ({e.reason})"
        )
        sys.exit(1)

if __name__ == "__main__":
    main()