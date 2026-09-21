#!/usr/bin/env python3
"""
SFDC MCP Server
Exposes Salesforce data export as MCP tools for Claude.

Setup (one-time):
  pip install mcp
  python sfdc_export.py --setup   # save your browser session cookie

Add to Claude Code MCP config:
  {
    "mcpServers": {
      "sfdc": {
        "command": "python",
        "args": ["/path/to/sfdc/sfdc_mcp_server.py"],
        "env": {
          "SFDC_SESSION_ID": "your_sid_here",
          "SFDC_INSTANCE_URL": "https://yourorg.my.salesforce.com"
        }
      }
    }
  }

Or omit "env" to use the saved .sfdc_config.json from sfdc_export.py --setup.
"""

import io
import json
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Reuse client logic from sfdc_export.py in the same directory
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sfdc_export import SalesforceClient, flatten_record, load_config

mcp = FastMCP("sfdc", instructions=(
    "Tools for querying, searching, and writing Salesforce data. "
    "READ: sfdc_list_objects to discover objects, sfdc_describe_object to see fields, "
    "sfdc_query for SOQL (SELECT ... FROM ... WHERE ...), "
    "sfdc_search for SOSL full-text search across multiple objects "
    "(FIND {term} IN ALL FIELDS RETURNING Account(Id, Name), Contact(Id, Name)). "
    "Use SOQL when you know the object and need filtering/aggregation. "
    "Use SOSL when searching by name or keyword across multiple objects. "
    "sfdc_opportunity_history to see who changed which Opportunity fields and when "
    "(reads OpportunityFieldHistory). "
    "WRITE: sfdc_create_record to insert a new record, "
    "sfdc_update_record to modify fields on an existing record by Id. "
    "Always confirm the record Id with a query before writing."
))


def _client() -> SalesforceClient:
    config = load_config()
    session_id = config.get("session_id")
    instance_url = config.get("instance_url")
    if not session_id or not instance_url:
        raise RuntimeError(
            "No Salesforce credentials found. "
            "Run: python sfdc_export.py --setup   "
            "or set SFDC_SESSION_ID and SFDC_INSTANCE_URL env vars."
        )
    return SalesforceClient(session_id, instance_url)


@mcp.tool()
def sfdc_test_connection() -> str:
    """Test the Salesforce connection and confirm the session is valid."""
    client = _client()
    info = client.test_connection()
    return f"Connected to {client.instance_url} — {len(info)} API resources available (API {client.base_url.split('/')[-1]})"


@mcp.tool()
def sfdc_list_objects(search: Optional[str] = None) -> str:
    """
    List all Salesforce objects (SObjects) queryable by this user.

    Args:
        search: Optional substring to filter object names (case-insensitive).
    """
    client = _client()
    objects = client.list_objects()
    queryable = [o for o in objects if o.get("queryable")]

    if search:
        queryable = [o for o in queryable if search.lower() in o["name"].lower() or search.lower() in o["label"].lower()]

    lines = [f"{'Name':<45} {'Label'}", "-" * 75]
    for o in sorted(queryable, key=lambda x: x["name"]):
        lines.append(f"{o['name']:<45} {o['label']}")
    lines.append(f"\n{len(queryable)} queryable objects" + (f" matching '{search}'" if search else ""))
    return "\n".join(lines)


@mcp.tool()
def sfdc_describe_object(object_name: str) -> str:
    """
    Show all fields available on a Salesforce object.

    Args:
        object_name: API name of the SObject (e.g. Account, Contact, Opportunity).
    """
    client = _client()
    desc = client.describe_object(object_name)
    fields = desc.get("fields", [])

    lines = [
        f"Object: {desc['name']} ({desc['label']})",
        f"{'Field Name':<45} {'Type':<20} {'Label'}",
        "-" * 95,
    ]
    for f in fields:
        lines.append(f"{f['name']:<45} {f['type']:<20} {f['label']}")
    lines.append(f"\n{len(fields)} fields")
    return "\n".join(lines)


@mcp.tool()
def sfdc_query(soql: str, format: str = "csv", limit_preview: int = 0) -> str:
    """
    Run a SOQL query and return results as CSV or JSON.

    Args:
        soql: Full SOQL query string. Example: "SELECT Id, Name, Industry FROM Account WHERE Industry = 'Technology'"
        format: Output format — "csv" (default) or "json".
        limit_preview: If > 0, return only this many rows in the response (useful for previewing large result sets).
                       The full result count is still reported.
    """
    if format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    client = _client()
    records = client.query(soql)
    total = len(records)

    if not records:
        return "Query returned 0 records."

    preview_note = ""
    if limit_preview > 0 and limit_preview < total:
        records = records[:limit_preview]
        preview_note = f"\n[Preview: showing {limit_preview} of {total} records]"

    flat = [flatten_record(r) for r in records]

    if format == "json":
        return json.dumps(flat, indent=2, default=str) + preview_note

    # CSV
    import csv as csv_mod
    buf = io.StringIO()
    fields = list(flat[0].keys())
    writer = csv_mod.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(flat)
    return buf.getvalue() + preview_note


@mcp.tool()
def sfdc_search(sosl: str, format: str = "json") -> str:
    """
    Run a SOSL (Salesforce Object Search Language) full-text search across one or more objects.
    Use this to search for a name, keyword, or phrase when you don't know which object holds the data.

    Args:
        sosl: Full SOSL search string.
              Example: "FIND {Jane Smith} IN ALL FIELDS RETURNING Contact(Id, Name, Email), Lead(Id, Name)"
              Search groups: ALL FIELDS, NAME FIELDS, EMAIL FIELDS, PHONE FIELDS, SIDEBAR FIELDS.
              Wildcards: * (zero or more chars), ? (one char). Phrase search: use double quotes inside braces.
        format: Output format — "json" (default) or "csv".
    """
    if format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    client = _client()
    records = client.search(sosl)

    if not records:
        return "Search returned 0 records."

    flat = [flatten_record(r) for r in records]

    if format == "json":
        return json.dumps(flat, indent=2, default=str)

    import csv as csv_mod
    buf = io.StringIO()
    fields = list(flat[0].keys())
    writer = csv_mod.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(flat)
    return buf.getvalue()


@mcp.tool()
def sfdc_search_to_file(sosl: str, output_path: str, format: str = "csv") -> str:
    """
    Run a SOSL full-text search and write results to a file. Use for large result sets.

    Args:
        sosl: Full SOSL search string.
              Example: "FIND {Acme} IN ALL FIELDS RETURNING Account(Id, Name), Opportunity(Id, Name, Amount)"
        output_path: Absolute path to the output file (e.g. ./exports/results.csv).
        format: Output format — "csv" (default) or "json".
    """
    if format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    client = _client()
    records = client.search(sosl)

    if not records:
        return "Search returned 0 records. File not written."

    flat = [flatten_record(r) for r in records]

    if format == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(flat, f, indent=2, default=str)
    else:
        import csv as csv_mod
        fields = list(flat[0].keys())
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat)

    return f"Wrote {len(records)} records to {output_path}"


@mcp.tool()
def sfdc_query_to_file(soql: str, output_path: str, format: str = "csv") -> str:
    """
    Run a SOQL query and write the full results to a file. Use this for large exports.

    Args:
        soql: Full SOQL query string.
        output_path: Absolute path to the output file (e.g. ./exports/accounts.csv).
        format: Output format — "csv" (default) or "json".
    """
    if format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    client = _client()
    records = client.query(soql)

    if not records:
        return "Query returned 0 records. File not written."

    flat = [flatten_record(r) for r in records]

    if format == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(flat, f, indent=2, default=str)
    else:
        import csv as csv_mod
        fields = list(flat[0].keys())
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat)

    return f"Wrote {len(records)} records to {output_path}"


@mcp.tool()
def sfdc_opportunity_history(
    opportunity_id: str,
    format: str = "table",
    field: Optional[str] = None,
    order: str = "desc",
    limit: int = 0,
) -> str:
    """
    Show the field-change history for an Opportunity — what changed, who changed it, and when.

    Reads the standard OpportunityFieldHistory object, which records one row per tracked
    field change (OldValue -> NewValue) with the user and timestamp. Only fields that have
    Salesforce "field history tracking" enabled appear here (max 20 fields per object).

    Args:
        opportunity_id: The 15- or 18-character Opportunity Id (starts with 006).
        format: "table" (default, human-readable timeline), "csv", or "json".
        field: Optional single field API name to filter to (e.g. "StageName", "Amount", "Owner").
        order: "desc" (newest first, default) or "asc" (oldest first).
        limit: If > 0, cap the number of change rows returned.
    """
    if format not in ("table", "csv", "json"):
        raise ValueError("format must be 'table', 'csv', or 'json'")
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")

    # Escape single quotes to keep the SOQL well-formed.
    safe_id = opportunity_id.replace("'", "\\'")
    where = f"OpportunityId = '{safe_id}'"
    if field:
        safe_field = field.replace("'", "\\'")
        where += f" AND Field = '{safe_field}'"

    soql = (
        "SELECT CreatedDate, CreatedBy.Name, Field, DataType, OldValue, NewValue "
        f"FROM OpportunityFieldHistory WHERE {where} "
        f"ORDER BY CreatedDate {order.upper()}"
    )
    if limit > 0:
        soql += f" LIMIT {int(limit)}"

    client = _client()
    records = client.query(soql)
    total = len(records)

    if not records:
        return (
            f"No tracked field changes found for Opportunity {opportunity_id}. "
            "Either the record has no history, or field history tracking is not enabled "
            "for its fields in Salesforce Setup."
        )

    flat = [flatten_record(r) for r in records]

    if format == "json":
        return json.dumps(flat, indent=2, default=str)

    if format == "csv":
        import csv as csv_mod
        buf = io.StringIO()
        fields = list(flat[0].keys())
        writer = csv_mod.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(flat)
        return buf.getvalue()

    # table: readable timeline
    lines = [f"Field-change history for Opportunity {opportunity_id} — {total} change(s)", ""]
    lines.append(f"{'When (UTC)':<22} {'Who':<22} {'Field':<32} {'Change'}")
    lines.append("-" * 110)
    for r in flat:
        when = str(r.get("CreatedDate", ""))[:19].replace("T", " ")
        who = str(r.get("CreatedBy.Name", "") or "")[:21]
        fld = str(r.get("Field", "") or "")[:31]
        old = r.get("OldValue")
        new = r.get("NewValue")
        old_s = "(blank)" if old in (None, "") else str(old)
        new_s = "(blank)" if new in (None, "") else str(new)
        lines.append(f"{when:<22} {who:<22} {fld:<32} {old_s} -> {new_s}")
    return "\n".join(lines)


@mcp.tool()
def sfdc_create_record(object_name: str, fields: dict) -> str:
    """
    Create a new Salesforce record.

    Args:
        object_name: API name of the SObject to create (e.g. Account, Contact, Task).
        fields: Dict of field API names to values.
                Example: {"LastName": "Smith", "FirstName": "Jane", "Email": "jane@example.com"}
    """
    client = _client()
    result = client.create_record(object_name, fields)
    record_id = result.get("id", "unknown")
    return f"Created {object_name} with Id {record_id}"


@mcp.tool()
def sfdc_update_record(object_name: str, record_id: str, fields: dict) -> str:
    """
    Update one or more fields on an existing Salesforce record.

    Args:
        object_name: API name of the SObject (e.g. Opportunity, Account, Contact).
        record_id: The 15- or 18-character Salesforce record Id.
        fields: Dict of field API names to their new values. Only include fields you want to change.
                Example: {"StageName": "4. Negotiating", "CloseDate": "2026-09-30"}
    """
    client = _client()
    client.update_record(object_name, record_id, fields)
    updated = ", ".join(f"{k}={v!r}" for k, v in fields.items())
    return f"Updated {object_name} {record_id}: {updated}"


# sfdc_delete_record is intentionally disabled.
# The underlying client method (client.delete_record) still exists if needed via the CLI.


if __name__ == "__main__":
    mcp.run(transport="stdio")
