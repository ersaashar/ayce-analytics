# Operations, Deployment & Security

This document covers system operation, account administration, database maintenance, reverse proxy configurations, and the security model in **AYCE Analytics V2**.

---

## 1. Security Architecture & Threat Model

AYCE Analytics V2 is designed for controlled enterprise deployment. It enforces zero-trust boundaries at every tier.

### 1.1. No Public Signups
The application deliberately avoids public self-registration. All user accounts must be provisioned by a system administrator using Django's management commands or the staff admin interface.

```bash
# Provision the first operator/superuser account
uv run --locked python manage.py createsuperuser
```

### 1.2. Role-Based Permissions & Staff Access
- Normal users can only access their own chats and account settings.
- Staff accounts (`is_staff=True`) can sign into `/admin/` to create, rename, reset, or deactivate accounts.
- Admin links in the user menu are only rendered when `user.is_staff and user.has_perm("auth.view_user")`.
- Deactivating an account blocks sign-in immediately while preserving the account's historical conversation data.

### 1.3. CSRF & Session Protections
- **Strict CSRF Verification**: Every non-idempotent HTTP request (`POST`, `PATCH`, `DELETE`) is guarded by Django's `CsrfViewMiddleware` with custom JSON failure mapping.
- **Session Cookie Flags**:
  - `SESSION_COOKIE_HTTPONLY = True`: Blocks access from JavaScript.
  - `SESSION_COOKIE_SAMESITE = "Lax"`: Protects against cross-site request forgery.
  - `SESSION_ENGINE = "django.contrib.sessions.backends.db"`: Sessions are stored in the database.
- **Cache-Control**: All private pages and JSON endpoints return `Cache-Control: no-store` to prevent caching by shared intermediate proxies or browser disk caches.

### 1.4. Multi-Modal Security & Prompt Injection Defense
- Document file names and contents are treated as untrusted data.
- System prompts instruct the LLM to treat document data and tool outputs as passive reference data rather than instructions.
- File extensions are validated against magic byte signatures (`_check_signature`).
- Word and Excel files are parsed through strict in-memory filters (`_inspect_ooxml`) with entity expansion blocked and macro files rejected.

---

## 2. Production Deployment Guide

### 2.1. Server Architecture
In production, run the ASGI application with `uvicorn` behind an SSL-terminating reverse proxy (such as Nginx, Traefik, or Caddy).

```text
Browser (HTTPS)
      │
      ▼
Nginx (SSL Termination, Static Cache, Proxy Buffering Off)
      │  http://127.0.0.1:8000
      ▼
Uvicorn ASGI Server (config.asgi:application)
      │
      ├─► SQLite Database (db.sqlite3)
      ├─► External LLM Gateway
      ├─► Independent Wren MCP (discovery always; native tools only under dev_jaffle)
```

### 2.2. Nginx Reverse Proxy Configuration
Crucial: Disable proxy buffering for SSE endpoints so token streams are delivered in real time:

```nginx
server {
    listen 443 ssl http2;
    server_name analytics.internal.example.com;

    ssl_certificate /etc/ssl/certs/analytics.crt;
    ssl_certificate_key /etc/ssl/private/analytics.key;

    # Static assets (pre-built)
    location /static/ {
        alias /path/to/ayce-analytics-v2/chat/static/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }

    # Main application & SSE Streaming
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Disable buffering for real-time SSE streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

---

## 3. Database Maintenance & Backups

The application stores data in SQLite (`db.sqlite3`).

### 3.1. Hot Backups with SQLite Online Backup API
Never copy `db.sqlite3` while the application is running, as writes may be in progress. Instead, perform an online backup:

```bash
# Safely snapshot the database using Python's sqlite3 backup API
python -c "import sqlite3; s=sqlite3.connect('db.sqlite3'); d=sqlite3.connect('db-backup-$(date +%Y%m%d%H%M%S).sqlite3'); s.backup(d)"
```

### 3.2. Data Migrations
Apply migrations using locked dependencies:
```bash
uv run --locked python manage.py migrate
```

Key historical migrations:
- `0001_initial`: Defines `ChatThread` and initial schema.
- `0002_remove_legacy_chat_session_keys`: Purges retired anonymous session payloads (`chat_history_json`, `chat_display`) from stored sessions.
- `0003_chatthread_turn_activity`: Adds `ChatTurn` checkpoints and activity timelines.
- `0004_chatattachment`: Adds `ChatAttachment` storage.
- `0005_alter_chatthread_options_chatthread_department_and_more`: Adds the thread department scope and updated thread options.
- `0006_legacy_classification`: Marks threads whose stored history cannot prove it was tool-free as locked.
- `0007_turn_dataset_provenance`: Adds `ChatTurnDataset` saved-query provenance.
- `datasets.0006_catalog_execution_metadata`: Retains the validated project and MDL snapshot as private catalog identity for admission and provenance. Not a production authorization contract.

---

## 4. Environment Variables Checklist

Ensure these variables are properly populated in `.env`:

```env
# Required Django Settings
DJANGO_SECRET_KEY=use-a-strong-random-secret-key-in-production
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=analytics.internal.example.com,127.0.0.1

# Network Bindings (scripts/serve.py)
DJANGO_HOST=127.0.0.1
DJANGO_PORT=8000

# LLM Gateway Configuration
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-proj-...
LLM_MODEL=gpt-4o

# Wren MCP endpoint. Blank disables discovery.
# With dev_jaffle it must equal AYCE_DEV_WREN_MCP_URL.
WREN_MCP_URL=http://127.0.0.1:18080/mcp

# Shipped default. Keep off outside synthetic DEV.
# dev_jaffle requires the gates below. scoped and any other value fail startup.
AYCE_ANALYTICS_MODE=off
AYCE_DEV_WREN_MCP_URL=
AYCE_DEV_WREN_PROJECT=
AYCE_DEV_JAFFLE_CONFIRMED=false

# Token Budgets
LLM_CONTEXT_WINDOW=131072
LLM_MAX_OUTPUT_TOKENS=8192

# Attachment Quotas
CHAT_MAX_FILES_PER_THREAD=5
CHAT_MAX_FILES_PER_TURN=5
CHAT_MAX_FILE_BYTES=5242880
CHAT_MAX_TEXT_BYTES_PER_FILE=65536
CHAT_MAX_TEXT_BYTES_PER_THREAD=262144

# Activity Detail Flag
CHAT_SHOW_REASONING_DETAIL=true
```

---

## Administration model and the dataset catalog

### Roles and scopes
Three fixed native groups are seeded by migration and can be repaired with:

```bash
uv run --locked python manage.py seed_ayce_roles --actor <native-superuser> --revision <current-revision>
```

- **AYCE Superadmin** — account, role, department, catalog and grant administration.
- **AYCE Lead** — manages Staff memberships and explicit dataset subsets of departments they actively lead.
- **AYCE Staff** — the base role every new account receives.

Membership in a group is never proof of department authority. Department scope
comes from an active `DepartmentMembership`; dataset access comes from an
explicit `MembershipDataset` pointing at that department's current grant.
Superadmins and native Django superusers get no implicit dataset access.

Effective application Superadmins are active native superusers or active members
of the fixed Superadmin group with `accounts.manage_users`. Only a native
superuser may grant or revoke the Superadmin group or change another Superadmin.
Every administration write is serialized by the `AuthorizationState` singleton,
which also carries the authorization epoch; the last active administrator can
never be deactivated.

### Dataset catalogue
`WREN_MCP_URL` identifies the independent Wren MCP endpoint used for metadata
discovery. Blank disables discovery. It is not query authorization by itself.
Each ASGI worker probes published models once at startup; a Superadmin can also
use **Refresh from Wren**. Discovery reads `wren://project` and argument-free
`get_mdl`, not warehouse rows. Those two calls stay internal. Newly discovered
models have no department grants.

An outage retains the last successful catalog for the same endpoint, including
the private `DatasetCatalogState.execution_metadata_json` snapshot. Snapshot,
structure, and definition hashes identify that catalog. They are never exposed
in chat APIs or model context. They are not physical isolation, not a
credential boundary, and not a production allow-deny proof. A changed endpoint
selects a separate source. Missing or structurally changed models leave
existing grants visible but ineffective until the department assignment is
explicitly confirmed; confirmation clears member subsets, which must then be
reassigned. Dataset enablement is controlled separately by an administrator.

### Synthetic DEV analytics
Shipped default is `AYCE_ANALYTICS_MODE=off`. Blank means `off`. `off` ignores
the DEV variables and attaches no analytics tools. The only other accepted
value is `dev_jaffle`. `scoped` and every other value fail startup with the
setting name only.

`dev_jaffle` also requires all of the following, or startup fails naming the
settings and not the endpoint values:

- `DJANGO_DEBUG=true`
- `AYCE_DEV_JAFFLE_CONFIRMED=true`
- `AYCE_DEV_WREN_PROJECT=jaffle-wren`
- `AYCE_DEV_WREN_MCP_URL` exactly equal to `WREN_MCP_URL` after trim
- both URLs absolute HTTP(S), literal host `127.0.0.1` or `[::1]`, explicit
  port, no username, password, query, or fragment

The original URL string is kept. It is not rewritten into a canonical form.
Runtime policy calls recheck the same gates and return no scope when they fail.

Admission is department-specific and does not open Wren. It requires a
successful current catalog, the synthetic identity above, published models
exactly `customers` and `orders` with the known fixture physical tables, and
explicit eligibility for both dataset IDs in that department. A partial grant,
a disabled or unassigned model, a role without an explicit assignment, or
grants split across departments returns no scope. Superadmins and Leads follow
the same rule. There is no orders-only mode and no SQL-text exception.

When admission succeeds, chat may attach the reviewed native subset
(`list_models`, `describe_model`, `get_instructions`, `dry_plan`, `run_sql`)
through `chat/mcp.py`. The browser and the model never supply the endpoint.
A successful model-visible result records the whole admitted project. Commit
writes one private `ChatTurnDataset` row per captured dataset in the same
transaction as the turn. A mismatch rolls the turn back. It never downgrades
to empty provenance.

`AYCE_DEV_JAFFLE_CONFIRMED=true` is an operator attestation that the loopback
process is the unchanged synthetic Jaffle deployment. The loopback host, the
project name, and the catalog hashes do not prove physical isolation. Wren
source, profile, and deployment definitions stay unchanged. A Wren project
edit or restart requires draining runs, refreshing the catalog, and
re-admitting scope. That is an operator assumption, not a production boundary.

The workspace reports eligibility per department, not from a global flag.
`analytics_enabled` is true only when this principal has at least one eligible
department. Bootstrap `analytics_department_ids` lists those department UUIDs.
`/api/me/` keeps the boolean only. An eligible department shows `Synthetic
Jaffle DEV analytics is enabled for this department.` Every other department
context keeps `Analytics queries are temporarily unavailable. You can still
chat and work within your department.` General chat is never analytics-enabled.
Reload is how the UI learns a grant change. Every run still rechecks on the
server. A runtime Wren failure uses sanitized `mcp_error`, not that notice.

### Wren `run_sql` read-only enforcement

AYCE forwards a bounded statement. It does not parse, rewrite, or classify
SQL itself, so `chat/mcp.py` has no SQL engine and no AST-only isolation
claim. Read-only enforcement belongs to the independent, unchanged Wren
service. In the installed Wren used for the attested DEV service:

- `wren/policy.py::validate_read_only_ast` accepts only the roots `Select`,
  `SetOperation`, `Subquery`, and `Values`. It then walks the whole tree and
  rejects any DDL/DML marker node and the explicit `Alter`, `Analyze`,
  `Block`, `Command`, `Copy`, `Create`, `Delete`, `Drop`, `Grant`, `Insert`,
  `Into`, `Lock`, `Merge`, `Set`, `TruncateTable`, `Update`, and `Use` nodes,
  so a data-modifying CTE or a hidden second statement cannot ride a
  `Select` root. This statement check is always on; `strict_mode` governs
  which tables may be named, not what may be done to them.
- `wren/engine.py` plans the statement and then re-checks the planned SQL
  with `validate_planned_sql`, because planning inlines MDL view statements
  and model `ref_sql` bodies into what the connector receives. That second
  check fails open when the planned string cannot be re-parsed: a mutating
  node that parses is rejected, an unparseable planned statement is not.
- `strict_mode` is Wren operator configuration, not an AYCE setting. It is
  true in the DEV Wren configuration, where it enables the fail-closed table
  allow-list and blocks its named data-reader functions (`read_csv`,
  `dblink`, `st_read`, and peers) in every AST position. A configured
  `denied_functions` list adds function-level denials.
- `wren/connector/duckdb.py` attaches each discovered fixture `.duckdb` file
  as `ATTACH DATABASE '<file>' AS "<alias>" (READ_ONLY)`, so the connector's
  own connection cannot write those files.

Observed on 2026-11-12 against the installed service and a temporary copy of
the approved fixture: the count query returned 99; `DELETE`, `INSERT`,
`UPDATE`, `DROP`, `CREATE`, and `SELECT 1; DELETE ...` were rejected by the
read-only attach and left the copy byte-identical; `COPY ... TO <file>` did
execute at the DuckDB layer, so the read-only attach does not by itself stop
a filesystem write, and Wren's AST policy is the layer that rejects it
(`COPY` is in the forbidden node list, verified through
`validate_read_only_ast`). `read_csv(...)` parses as read-only and is refused
only by `strict_mode`.

These layers describe the installed service. The planned-SQL re-parse gap,
the operator-owned `strict_mode` value, and the shared DEV profile are why
AYCE does not claim physical, credential, or per-dataset isolation from
them. Production partial-dataset execution stays disabled.

### Fresh worktree database
Synthetic proof uses a new SQLite file created by `migrate` in the feature
worktree. Do not copy another checkout's database, users, sessions, grants, or
chat history. If `db.sqlite3` already exists and is not that fresh file, use
another worktree path instead of deleting it. Create `wren-dev-operator` with
`createsuperuser` and an environment-only password. Never print the password.
Later accounts, departments, catalog refresh, and grants go through
`accounts.services` and `datasets.services` with `actor_id` and the current
revision. Do not write permissions, memberships, or grants directly.

### Operator smoke proofs
`scripts/smoke_wren_chat.py` is operator-only. It never prints passwords,
cookies, endpoints, or raw transport errors. `--database` must resolve to this
worktree's `db.sqlite3` and to Django's configured database. Actions are
mutually exclusive:

```bash
uv run --locked python scripts/smoke_wren_chat.py --diagnose-only
uv run --locked python scripts/smoke_wren_chat.py --service-only --provision --database "$PWD/db.sqlite3"
uv run --locked python scripts/serve.py --noreload
uv run --locked python scripts/smoke_wren_chat.py --execute --provision --base-url http://127.0.0.1:8001 --database "$PWD/db.sqlite3" --session-file "$TEMP/ayce-wren-smoke-session.json"
```

- `--diagnose-only` (default) verifies live project identity and the reviewed
  tool inventory, runs `SELECT COUNT(*) AS total_orders FROM orders` with
  limit 1, and prints safe status. It rejects `--provision`, `--database`,
  `--base-url`, and `--session-file`.
- `--service-only` requires `--provision` and `--database`. It refreshes the
  catalog through the audited service, creates fresh principals, calls
  `stream_answer` with a trusted `AnalyticsRun`, prints the count-backed
  reply, and releases the lease. It does not commit that diagnostic turn.
  HTTP chat is not the proof at this action.
- `--execute` also requires `--base-url` on a loopback ASGI origin. It logs in
  as the full principal, commits a Sales thread, and checks canonical history
  for `run_sql` plus both provenance rows. The partial principal must finish
  ordinary chat with zero native tools and zero provenance. `--session-file`
  must sit outside the repository. The script also writes `<stem>-partial.json`
  and never prints the cookies.

`--provision` creates `wren-full-<suffix>` and `wren-partial-<suffix>`, plus
`Jaffle Sales <suffix>` and `Jaffle Marketing <suffix>`, through audited
services. The full principal receives both Sales models and only orders in
Marketing. The partial principal receives only orders in Sales. Passwords stay
in memory.

### Production boundary
Production partial-dataset execution remains disabled. Do not add a `scoped`
mode, shared-endpoint partial-grant SQL, a registry service, a Governed MCP
Gateway, a SQL engine, or an AST-only isolation claim. A later production
implementation must pair operator-approved semantic and knowledge scopes with
restricted read-only database credentials, verify physical and semantic
allow-deny cases and protected networking, and obtain independent review.
Absence of that boundary does not block synthetic full-project DEV testing.
Keep `AYCE_ANALYTICS_MODE=off` for any deployment that is not the attested
loopback Jaffle service. `DJANGO_DEBUG=false` also refuses `dev_jaffle`.

- **Per-round authorization**: every model round re-checks the owner, thread,
  department scope and the authorization epoch before the model request. After
  a tool use, the captured scope is rechecked before protected text and before
  commit. A general chat ignores unrelated department policy changes.
- **Provenance locking**: a used synthetic turn, and any older saved query
  result, carries private per-turn dataset provenance (`ChatTurnDataset`).
  Thread reads, runs and regenerations require that provenance to still match
  a currently eligible assignment with unchanged definition and structure
  hashes; otherwise the thread locks as `quarantined`, its title is redacted,
  and no saved rows reach the browser or the model. Those hashes quarantine
  changed catalog identity. They do not isolate the warehouse. Authorization
  failures (`authentication_required`, `authorization_changed`,
  `department_access_revoked`, `history_quarantined`) take precedence over
  backend errors. A Wren transport failure is `502 mcp_error`.
- **Regeneration limit**: a turn that still carries provenance cannot be
  regenerated. A direct attempt returns `503 warehouse_unavailable` and mutates
  nothing. Ordinary provenance-free turns regenerate normally and receive no
  native tools.

### Run lease lifecycle and SSE cleanup

A streaming send run acquires `active_run_token` with a 300-second TTL and
renews it from a background watcher. Release is token-scoped: the clear
compares the stored token, so a duplicate or late release never clears a
newer run's lease. Every exit is covered:

- A successful commit clears the lease in the same conditional `ChatThread`
  update that writes history, display, title and revision. The revision CAS
  and the lease clear cannot diverge.
- The SSE iterator's shielded `finally` closes and drains the provider pump
  and the renewer, then clears the lease. That path covers normal completion,
  an initialization failure before the first model round, cancellation, a
  provider or tool error, and a client disconnect that closes the iterator.
- `_LeaseClosingStream.close()` is the response's synchronous closer. Django
  can close a `StreamingHttpResponse` before its async iterator has started,
  where the generator `finally` never runs. The closer clears the lease in
  place when no event loop is running, or schedules the same shielded
  token-scoped release on the live loop. The wrapper marks itself closed, so
  a repeat close never schedules a second release.

`done` is emitted only after that commit already cleared the lease, so no
post-commit `require_run_current` follows. A revocation after a successful
commit locks history and withholds the `done` frame instead of deleting the
committed turn.

### Legacy history classification
Migration `chat.0006_legacy_classification` sets `quarantined=True` for threads
whose stored history or activity cannot prove it was tool-free. It never
rewrites conversation bytes, checkpoints or attachments, never invents turns,
and never unlocks anything automatically. Locked threads remain listed with a
redacted title, cannot be read/renamed/run, and can still be deleted by their
owner with the current revision.

Saved query results use dynamic provenance instead: `chat.0007_turn_dataset_provenance`
records the datasets each turn exposed, and a thread locks the same way if that
provenance no longer matches a current eligible assignment with unchanged
catalog hashes. Regranting unchanged, still-eligible data restores dynamic
access; the legacy flag is never cleared.

### Django Admin
`/admin/` remains a native staff surface, but the stock User/Group write paths
are replaced by service-backed views: every save runs through the same audited
services, groups/permissions and grants are read-only, password changes require
the actor's own password, and users/departments/datasets/audit rows cannot be
deleted. The management page at `/management/` is the delegated surface.
