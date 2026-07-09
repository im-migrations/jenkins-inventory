# Azure DevOps Jenkinsfile Inventory

Scan one or more Azure DevOps organizations and produce a CSV inventory of the
repositories that contain a `Jenkinsfile` in the **root of their default
branch**.

## How it works

The tool walks the Azure DevOps hierarchy (**Organization → Project →
Repository**) and checks each repository for a filename matching a pattern
(default: exactly `Jenkinsfile`). It supports two scan modes:

| Mode | Flag | Description | Requirements |
|------|------|-------------|--------------|
| REST | *(default)* | Lists each repo's root items via the Git REST API. Always works. | PAT with **Code (Read)** |
| Fast | `--fast` | Uses the Code Search API — one request per org. Auto-falls back to REST when unavailable. | Code Search Marketplace extension installed on the org |

No repositories are cloned; everything is done over the REST APIs.

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Authentication

Create a [Personal Access Token](https://learn.microsoft.com/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate)
with the **Code (Read)** scope.

Set environment variables (see `.env.example`):

- `AZDO_PAT` — global token used for any org without a specific entry.
- `AZDO_ORGS` — comma-separated org names (optional; can also pass `--org`).
- `AZDO_TOKENS` — optional per-org tokens, either `org=pat,org2=pat2` or JSON
  `{"org":"pat"}`. Orgs not listed fall back to `AZDO_PAT`.

The easiest option is to copy `.env.example` to `.env` and fill in the values —
the script loads `.env` automatically at startup (existing environment
variables take precedence):

```bash
cp .env.example .env
# then edit .env
```

Alternatively, export the variables directly:

```bash
# Windows (PowerShell)
$env:AZDO_PAT="xxxxxxxx"
$env:AZDO_ORGS="contoso,fabrikam"

# macOS/Linux
export AZDO_PAT="xxxxxxxx"
export AZDO_ORGS="contoso,fabrikam"
```

## Usage

```bash
# Scan orgs from AZDO_ORGS, write jenkins_inventory.csv
python jenkins_inventory.py

# Specify orgs explicitly (repeatable)
python jenkins_inventory.py --org contoso --org fabrikam

# Only record repos that actually have a match
python jenkins_inventory.py --matches-only

# Match filename variants (e.g. Jenkinsfile, Jenkinsfile.ci)
python jenkins_inventory.py --pattern "Jenkinsfile*"

# Use Code Search (fast) mode with automatic fallback to REST
python jenkins_inventory.py --fast

# Custom output path and an org list from a file
python jenkins_inventory.py --orgs-file orgs.txt -o out.csv

# Load a specific env file
python jenkins_inventory.py --env-file prod.env
```

## Output

CSV with these columns:

| Column | Description |
|--------|-------------|
| `organization` | Azure DevOps organization |
| `project` | Project name |
| `repository` | Repository name |
| `repo_id` | Repository GUID |
| `default_branch` | Default branch (REST mode) |
| `matched_file` | The filename that matched the pattern |
| `has_jenkinsfile` | `True`/`False` |
| `web_url` | Repository URL (REST mode) |
| `mode` | `rest` or `fast` |
| `checked_at` | UTC timestamp |

## Notes

- **Fast mode caveats:** Code Search must be installed on the org and caps at
  1000 results per query; if either condition is not met, the tool logs a
  message and falls back to REST for that org. Fast-mode rows do not include
  `default_branch` or `web_url` (the REST scan is the source of truth for
  those).
- **Scope:** Root of the default branch only. Use `--pattern` for filename
  variants.
- Matching is **case-insensitive**.
