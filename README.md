# SFDC Tools

A Salesforce data exporter and MCP server for Claude. Query, search, and write Salesforce data using your browser session cookie — no connected app or OAuth setup required.

## Requirements

- Python 3.10+
- A Salesforce org you can log into in a browser

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/<your-user>/sfdc-tools.git
cd sfdc-tools
```

Two files do the work, and they must stay in the same directory:

- `sfdc_export.py` — Salesforce client + CLI
- `sfdc_mcp_server.py` — MCP server for Claude

### 2. Create a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows (PowerShell)
venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 3. Get your Salesforce session cookie

1. Log into Salesforce in Chrome or Firefox.
2. Open DevTools (`F12`) → **Application** tab → **Cookies** → select your Salesforce domain.
3. Find the cookie named `sid` and copy its **Value**. There may be several cookies named `sid`; you may need to try more than one before you find the one that works.
4. Copy your instance URL from the address bar (e.g. `https://myorg.my.salesforce.com`).

### 4. Save your credentials

```bash
python sfdc_export.py --setup
```

This writes your session to `.sfdc_config.json` in the repo directory. That file is gitignored — do not commit it. See `.sfdc_config.example.json` for the shape.

Alternatively, set environment variables, which take precedence over the file:

```bash
# macOS / Linux
export SFDC_SESSION_ID=your_sid_value
export SFDC_INSTANCE_URL=https://yourorg.my.salesforce.com

# Windows (PowerShell)
$env:SFDC_SESSION_ID = "your_sid_value"
$env:SFDC_INSTANCE_URL = "https://yourorg.my.salesforce.com"
```

### 5. Test the connection

```bash
python sfdc_export.py --test
```

---

## MCP Server Setup (Claude Code)

Register the server with Claude Code, using the Python interpreter from your venv:

```bash
# macOS / Linux
claude mcp add sfdc "$PWD/venv/bin/python" "$PWD/sfdc_mcp_server.py"

# Windows (PowerShell)
claude mcp add sfdc "$PWD\venv\Scripts\python.exe" "$PWD\sfdc_mcp_server.py"
```

Or add it manually to your Claude Code MCP config (`.claude.json`), substituting absolute paths:

```json
{
  "mcpServers": {
    "sfdc": {
      "command": "/path/to/sfdc-tools/venv/bin/python",
      "args": ["/path/to/sfdc-tools/sfdc_mcp_server.py"]
    }
  }
}
```

To pass credentials directly instead of using the saved config file, add an `env` block:

```json
{
  "mcpServers": {
    "sfdc": {
      "command": "/path/to/sfdc-tools/venv/bin/python",
      "args": ["/path/to/sfdc-tools/sfdc_mcp_server.py"],
      "env": {
        "SFDC_SESSION_ID": "your_sid_value",
        "SFDC_INSTANCE_URL": "https://yourorg.my.salesforce.com"
      }
    }
  }
}
```

Verify the server starts correctly — it should hang, waiting for stdio input, which means it's working:

```bash
python sfdc_mcp_server.py
```

Press `Ctrl+C` to exit.

---

## MCP Tools Reference

Once connected, Claude has access to these tools:

### Read

| Tool | Description |
|---|---|
| `sfdc_test_connection` | Verify the session is valid |
| `sfdc_list_objects` | List all queryable SObjects; optional `search` filter |
| `sfdc_describe_object` | Show all fields on an object |
| `sfdc_query` | Run a SOQL query, return results inline as CSV or JSON |
| `sfdc_query_to_file` | Run a SOQL query, write full results to a file |
| `sfdc_search` | Run a SOSL full-text search across multiple objects |
| `sfdc_search_to_file` | Run a SOSL search, write results to a file |
| `sfdc_opportunity_history` | Show who changed which Opportunity fields and when |

### Write

| Tool | Description |
|---|---|
| `sfdc_create_record` | Create a new record on any SObject |
| `sfdc_update_record` | Update one or more fields on an existing record by Id |

---

## CLI Reference

`sfdc_export.py` can also be used directly from the terminal.

```bash
# Test connection
python sfdc_export.py --test

# List queryable objects
python sfdc_export.py --objects

# Show fields on an object
python sfdc_export.py --describe Opportunity

# Run a SOQL query (stdout, CSV)
python sfdc_export.py --query "SELECT Id, Name, StageName, Amount FROM Opportunity WHERE IsClosed = false"

# Run a SOQL query and save to file
python sfdc_export.py --query "SELECT Id, Name FROM Account" --output accounts.csv

# Run a SOQL query and export as JSON
python sfdc_export.py --query "SELECT Id, Name FROM Contact" --format json --output contacts.json

# Run a SOSL search
python sfdc_export.py --search "FIND {Jane Smith} IN NAME FIELDS RETURNING Contact(Id, Name, Email)"
```

---

## Session Expiry

Salesforce session cookies expire after your org's session timeout, typically 2–8 hours of inactivity. When you see a `401` or `INVALID_SESSION_ID` error, grab a fresh `sid` cookie from your browser and re-run:

```bash
python sfdc_export.py --setup
```

---

## Security Notes

- **`.sfdc_config.json` holds a live Salesforce session token.** It is gitignored; keep it that way. On POSIX systems it is written with `0600` permissions. On Windows, file mode bits are not enforced, so the file is only as protected as your user profile directory.
- A `sid` cookie carries your full user permissions. Anyone who obtains it can read and write everything you can, until the session expires. Treat it like a password, and never paste it into an issue, log, or commit.
- **Exported data is your org's data.** Query results routinely contain customer names, contact details, and commercial terms. `.gitignore` excludes `*.csv` and `*.json` by default for exactly this reason — review anything you add before committing.
- `sfdc_delete_record` is intentionally not exposed as an MCP tool. The underlying `SalesforceClient.delete_record` method still exists for CLI or library use.
- SOQL and SOSL strings are passed to Salesforce as given. Do not interpolate untrusted input into them.

---

## License

MIT — see [LICENSE](LICENSE).
