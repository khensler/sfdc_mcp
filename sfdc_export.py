#!/usr/bin/env python3
"""
SFDC Data Exporter
Exports Salesforce data via SOQL using a browser session cookie (sid).

Setup:
  python sfdc_export.py --login --instance-url https://myorg.my.salesforce.com
  (opens a browser; sign in and the sid cookie is captured automatically)

  Or by hand:
  1. Log into Salesforce in your browser
  2. Open DevTools -> Application -> Cookies -> select your SF domain
  3. Copy the value of the 'sid' cookie
  4. Find your instance URL (e.g. https://myorg.my.salesforce.com)

Usage:
  python sfdc_export.py --test
  python sfdc_export.py --query "SELECT Id, Name FROM Account LIMIT 10"
  python sfdc_export.py --query "SELECT Id, Name, Email FROM Contact" --format json --output contacts.json
  python sfdc_export.py --objects          # list all available objects
  python sfdc_export.py --describe Account  # show fields for an object
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
import urllib.request
import urllib.error

API_VERSION = "v61.0"

# Persistent browser profile for --login, so SSO is remembered between sign-ins.
BROWSER_PROFILE_DIR = Path.home() / ".sfdc-mcp" / "browser-profile"


class SessionExpiredError(RuntimeError):
    """The API rejected the session id (HTTP 401)."""


def _api_url(instance_url: str) -> str:
    # The Lightning UI domain 302-redirects API calls to my.salesforce.com;
    # urllib turns a redirected POST into a GET and refuses to follow PATCH,
    # so writes silently fail unless we call the API domain directly.
    return instance_url.rstrip("/").replace(".lightning.force.com", ".my.salesforce.com")


def load_config() -> dict:
    """Load session config from env vars or config file."""
    config_path = os.path.join(os.path.dirname(__file__), ".sfdc_config.json")
    config = {}

    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)

    # env vars override file
    if "SFDC_SESSION_ID" in os.environ:
        config["session_id"] = os.environ["SFDC_SESSION_ID"]
    if "SFDC_INSTANCE_URL" in os.environ:
        config["instance_url"] = os.environ["SFDC_INSTANCE_URL"].rstrip("/")

    return config


def write_config(session_id: str, instance_url: str) -> str:
    """Write the session to .sfdc_config.json and return its path. Prints nothing,
    because the MCP server calls this and its stdout is the protocol stream."""
    config_path = os.path.join(os.path.dirname(__file__), ".sfdc_config.json")
    with open(config_path, "w") as f:
        json.dump({"session_id": session_id, "instance_url": instance_url.rstrip("/")}, f, indent=2)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        # Best effort — mode bits are not enforced on all platforms (e.g. Windows).
        pass
    return config_path


def save_config(session_id: str, instance_url: str):
    config_path = write_config(session_id, instance_url)
    print(f"Saved config to {config_path}")


def _session_works(session_id: str, instance_url: str) -> bool:
    try:
        SalesforceClient(session_id, instance_url).test_connection()
        return True
    except (RuntimeError, OSError):
        return False


def browser_login(instance_url: str, timeout_seconds: int = 300, channel: str | None = None) -> str:
    """Open a browser on instance_url, wait for the user to sign in, and return a
    working sid. The session is saved to .sfdc_config.json.

    Salesforce sets a sid cookie on several domains (my.salesforce.com,
    lightning.force.com, file.force.com, ...) and only some of them are accepted
    by the REST API, so each candidate is tried against the API, starting with
    the one on the API host.
    """
    from playwright.sync_api import sync_playwright

    instance_url = instance_url.rstrip("/")
    api_host = urlparse(_api_url(instance_url)).hostname or ""
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    launch_kwargs = {
        "user_data_dir": str(BROWSER_PROFILE_DIR),
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
        "viewport": {"width": 1280, "height": 900},
    }
    if channel:
        launch_kwargs["channel"] = channel

    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            raise RuntimeError(
                f"Could not start the login browser: {e}. Close any open login window, or "
                "install a browser with: python -m playwright install chromium"
            ) from e
        try:
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(context)
            except ImportError:
                pass

            page = context.pages[0] if context.pages else context.new_page()
            page.goto(instance_url, wait_until="domcontentloaded")

            rejected: set[str] = set()
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                try:
                    cookies = context.cookies()
                except Exception as e:  # window closed by the user
                    raise RuntimeError("The login browser was closed before sign-in completed.") from e

                candidates = [c for c in cookies if c["name"] == "sid" and c["value"] not in rejected]
                candidates.sort(key=lambda c: c["domain"].lstrip(".") != api_host)
                for c in candidates:
                    if _session_works(c["value"], instance_url):
                        write_config(c["value"], instance_url)
                        return c["value"]
                    rejected.add(c["value"])
                time.sleep(2)

            raise RuntimeError(f"Sign-in was not completed within {timeout_seconds}s.")
        finally:
            try:
                context.close()
            except Exception:
                pass


class SalesforceClient:
    def __init__(self, session_id: str, instance_url: str):
        self.session_id = session_id
        self.instance_url = _api_url(instance_url)
        self.base_url = f"{self.instance_url}/services/data/{API_VERSION}"

    def _request(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.session_id}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 401:
                raise SessionExpiredError(f"Salesforce API error 401: {body[:300]}")
            try:
                errors = json.loads(body)
                if isinstance(errors, list) and errors:
                    raise RuntimeError(f"Salesforce API error {e.code}: {errors[0].get('message', body)}")
            except (json.JSONDecodeError, KeyError):
                pass
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}")

    def _mutate(self, method: str, path: str, body: dict | None = None) -> dict | None:
        """Send a POST, PATCH, or DELETE request. Returns parsed JSON or None for 204."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.session_id}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body_str = e.read().decode()
            if e.code == 401:
                raise SessionExpiredError(f"Salesforce API error 401: {body_str[:300]}")
            try:
                errors = json.loads(body_str)
                if isinstance(errors, list) and errors:
                    raise RuntimeError(f"Salesforce API error {e.code}: {errors[0].get('message', body_str)}")
            except (json.JSONDecodeError, KeyError):
                pass
            raise RuntimeError(f"HTTP {e.code}: {body_str[:300]}")

    def _request_url(self, url: str) -> dict:
        """Fetch an absolute URL (used for pagination nextRecordsUrl)."""
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.session_id}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_connection(self) -> dict:
        """Verify the session is valid and return org info."""
        return self._request("/")

    def list_objects(self) -> list[dict]:
        """List all SObjects accessible to this user."""
        result = self._request("/sobjects/")
        return result.get("sobjects", [])

    def describe_object(self, obj_name: str) -> dict:
        """Describe fields for a given SObject."""
        return self._request(f"/sobjects/{obj_name}/describe/")

    def search(self, sosl: str) -> list[dict]:
        """Run a SOSL search and return all matched records across objects."""
        result = self._request("/search/", {"q": sosl})
        records = result.get("searchRecords", [])
        return records

    def create_record(self, object_name: str, fields: dict) -> dict:
        """Create a new SObject record. Returns {"id": "...", "success": True}."""
        result = self._mutate("POST", f"/sobjects/{object_name}/", fields)
        return result

    def update_record(self, object_name: str, record_id: str, fields: dict) -> None:
        """Update fields on an existing record by Id. Returns None on success (HTTP 204)."""
        self._mutate("PATCH", f"/sobjects/{object_name}/{record_id}", fields)

    def delete_record(self, object_name: str, record_id: str) -> None:
        """Delete a record by Id. Returns None on success (HTTP 204)."""
        self._mutate("DELETE", f"/sobjects/{object_name}/{record_id}")

    def query(self, soql: str) -> list[dict]:
        """Run a SOQL query and return all records (handles pagination)."""
        records = []
        result = self._request("/query/", {"q": soql})
        records.extend(result.get("records", []))

        total = result.get("totalSize", 0)
        done = result.get("done", True)
        next_url = result.get("nextRecordsUrl")

        if total > 0:
            print(f"  Total records: {total}", file=sys.stderr)

        while not done and next_url:
            url = f"{self.instance_url}{next_url}"
            result = self._request_url(url)
            batch = result.get("records", [])
            records.extend(batch)
            done = result.get("done", True)
            next_url = result.get("nextRecordsUrl")
            print(f"  Fetched {len(records)}/{total}...", file=sys.stderr)

        return records


def flatten_record(record: dict, prefix: str = "") -> dict:
    """Flatten nested SF records (e.g. relationship fields) into dot-notation keys."""
    flat = {}
    for key, value in record.items():
        if key == "attributes":
            continue
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict) and "attributes" in value:
            # nested relationship object
            flat.update(flatten_record(value, prefix=f"{full_key}."))
        else:
            flat[full_key] = value
    return flat


def export_csv(records: list[dict], output_path: str | None):
    if not records:
        print("No records to export.")
        return

    flat = [flatten_record(r) for r in records]
    fields = list(flat[0].keys())

    dest = open(output_path, "w", newline="", encoding="utf-8") if output_path else sys.stdout
    try:
        writer = csv.DictWriter(dest, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)
    finally:
        if output_path:
            dest.close()


def export_json(records: list[dict], output_path: str | None):
    if not records:
        print("[]")
        return

    flat = [flatten_record(r) for r in records]
    content = json.dumps(flat, indent=2, default=str)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
    else:
        print(content)


def cmd_setup(args):
    print("SFDC session setup")
    print("------------------")
    print("1. Log into Salesforce in your browser")
    print("2. Open DevTools (F12) -> Application -> Cookies -> select your SF domain")
    print("3. Copy the 'sid' cookie value\n")

    session_id = input("Paste your sid cookie value: ").strip()
    instance_url = input("Paste your instance URL (e.g. https://myorg.my.salesforce.com): ").strip()

    if not session_id or not instance_url:
        print("Both values are required.", file=sys.stderr)
        sys.exit(1)

    save_config(session_id, instance_url)

    client = SalesforceClient(session_id, instance_url)
    print("\nTesting connection...")
    try:
        info = client.test_connection()
        print(f"Connected! API version: {API_VERSION}")
        print(f"Available resources: {len(info)} endpoints")
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_login(args):
    instance_url = args.instance_url or load_config().get("instance_url")
    if not instance_url:
        print("No instance URL. Pass --instance-url https://myorg.my.salesforce.com", file=sys.stderr)
        sys.exit(1)
    print(f"Opening {instance_url}")
    print("Sign in in the browser window; it closes automatically once the session is captured.\n")
    try:
        browser_login(instance_url, timeout_seconds=args.timeout, channel="chrome" if args.chrome else None)
    except RuntimeError as e:
        print(f"Sign-in failed: {e}", file=sys.stderr)
        if not args.chrome:
            print("If SSO blocked the automated browser, retry with --chrome.", file=sys.stderr)
        sys.exit(1)
    print("Signed in. Session saved to .sfdc_config.json")


def get_client(args) -> SalesforceClient:
    config = load_config()

    session_id = getattr(args, "session_id", None) or config.get("session_id")
    instance_url = getattr(args, "instance_url", None) or config.get("instance_url")

    if not session_id or not instance_url:
        print(
            "No session found. Run: python sfdc_export.py --login (or --setup)\n"
            "Or set SFDC_SESSION_ID and SFDC_INSTANCE_URL env vars.",
            file=sys.stderr,
        )
        sys.exit(1)

    return SalesforceClient(session_id, instance_url)


def main():
    parser = argparse.ArgumentParser(description="SFDC Data Exporter")
    parser.add_argument("--setup", action="store_true", help="Interactive setup: save session cookie")
    parser.add_argument("--login", action="store_true", help="Sign in through a browser and capture the sid cookie automatically")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait for --login sign-in (default: 300)")
    parser.add_argument("--chrome", action="store_true", help="Use installed Google Chrome for --login instead of bundled Chromium")
    parser.add_argument("--test", action="store_true", help="Test the current session")
    parser.add_argument("--objects", action="store_true", help="List all available SObjects")
    parser.add_argument("--describe", metavar="OBJECT", help="Show fields for an SObject")
    parser.add_argument("--query", "-q", metavar="SOQL", help="SOQL query to run")
    parser.add_argument("--search", "-s", metavar="SOSL", help="SOSL search to run. Example: \"FIND {Acme} IN ALL FIELDS RETURNING Account(Id, Name), Contact(Id, Name)\"")
    parser.add_argument("--format", choices=["csv", "json"], default="csv", help="Output format (default: csv)")
    parser.add_argument("--output", "-o", metavar="FILE", help="Output file (default: stdout)")
    parser.add_argument("--session-id", metavar="SID", help="Override session ID")
    parser.add_argument("--instance-url", metavar="URL", help="Override instance URL")
    args = parser.parse_args()

    if args.setup:
        cmd_setup(args)
        return

    if args.login:
        cmd_login(args)
        return

    if not any([args.test, args.objects, args.describe, args.query, args.search]):
        parser.print_help()
        return

    client = get_client(args)

    if args.test:
        try:
            info = client.test_connection()
            print(f"Connection OK — {len(info)} API resources available at {client.instance_url}")
        except Exception as e:
            print(f"Connection failed: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.objects:
        objs = client.list_objects()
        queryable = [o for o in objs if o.get("queryable")]
        print(f"{'Name':<40} {'Label'}")
        print("-" * 70)
        for o in sorted(queryable, key=lambda x: x["name"]):
            print(f"{o['name']:<40} {o['label']}")
        print(f"\n{len(queryable)} queryable objects")

    elif args.describe:
        desc = client.describe_object(args.describe)
        fields = desc.get("fields", [])
        print(f"Object: {desc['name']} ({desc['label']})")
        print(f"{'Field Name':<40} {'Type':<20} {'Label'}")
        print("-" * 90)
        for f in fields:
            print(f"{f['name']:<40} {f['type']:<20} {f['label']}")
        print(f"\n{len(fields)} fields")

    elif args.query:
        print(f"Running query: {args.query[:80]}{'...' if len(args.query) > 80 else ''}", file=sys.stderr)
        records = client.query(args.query)
        print(f"Exporting {len(records)} records as {args.format}...", file=sys.stderr)

        if args.format == "json":
            export_json(records, args.output)
        else:
            export_csv(records, args.output)

        if args.output:
            print(f"Written to {args.output}", file=sys.stderr)

    elif args.search:
        print(f"Running search: {args.search[:80]}{'...' if len(args.search) > 80 else ''}", file=sys.stderr)
        records = client.search(args.search)
        print(f"Exporting {len(records)} records as {args.format}...", file=sys.stderr)

        if args.format == "json":
            export_json(records, args.output)
        else:
            export_csv(records, args.output)

        if args.output:
            print(f"Written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
