# AGENTS.md

Agent-facing guide for this repository. `README.md` is for humans; deep domain notes live in `docs/` (`architecture.md`, `api-and-protocol.md`, `concurrency-and-storage.md`, `agent-and-tools.md`, `attachments-and-budgeting.md`, `frontend-workspace.md`, `operations-and-security.md`).

## Project overview

**AYCE Analytics V2** is a multi-user, self-hosted, authenticated chat workspace for data analytics: Django 5.2 (ASGI) + PydanticAI backend streaming SSE to a React 19 frontend. AYCE owns identity, grants, orchestration and history. An independent Wren service supplies catalog metadata, and native analytics tools only when `AYCE_ANALYTICS_MODE=dev_jaffle` passes its gates for the full synthetic Jaffle project. Shipped default is `off`: no analytics tools, ordinary chat and attached-file analysis stay enabled. Safe multi-modal attachment ingestion (DOCX, XLSX, PDF, images), CAS state sync, and non-destructive turn rollbacks are included.

## Setup commands

- Install Python deps (locked): `uv sync --locked`
- Install frontend deps: `npm ci`
- Apply migrations: `uv run --locked python manage.py migrate`
- All of the above: `just setup`

Requires `uv`, Node.js 20.19+/22.12+, and optionally `just`. Run Python through `uv run --locked` so the locked environment is used.

## Build and run

- Build frontend bundle into `chat/static/chat/dist/`: `npm run build` (or `just frontend-build`)
- Dev server: `just dev` (builds frontend, then runs `scripts/serve.py`)
- Dev server without autoreload: `just dev-noreload`
- Django system checks: `uv run --locked python manage.py check` (or `just check`)
- Frontend lint + typecheck: `npm run lint && npm run typecheck` (or `just frontend-check`)

## Testing instructions

- Backend suite (fully offline, no network): `uv run --locked python manage.py test tests --verbosity 2` (or `just test`)
- Frontend suite (Vitest, jsdom): `npm test` (or `just frontend-test`)
- Full CI locally: `just ci` — lint, format check, Django checks, frontend lint/typecheck/tests/build, backend tests
- One backend test class: `uv run --locked python manage.py test tests.test_chat_threads -v 2`
- Fix lint, type, and test failures before finishing. Add tests for changed behavior, especially concurrency, quota, tenant-scope, and error-mapping edges.

## Code style

- Python 3.12 target. `from __future__ import annotations` in every module. Strict type annotations (`typing`, `dataclasses`, `collections.abc`).
- Keep async and sync cleanly separated; call Django ORM from async code through `asgiref.sync.sync_to_async(..., thread_sensitive=True)`.
- Ruff lint: `uv run --locked ruff check .`; format: `uv run --locked ruff format --check .`; autofix: `just fmt`.
- TypeScript strict mode. Biome rules enforced (`biome.json`), no `any` — use strict unions or explicit interfaces.
- Preserve accessible DOM: `role`, `aria-label`, `aria-expanded`, keyboard shortcuts, visible focus indicators.
- Surgical, minimal edits. Keep existing comments and docstrings; preserve function signatures and error contracts unless explicitly directed. No speculative refactors.

## Architecture invariants

Every change must preserve these.

### Tenant isolation and ownership scoping
- Every chat thread is owned by an authenticated `auth.User`.
- Never query `ChatThread` without `owner_id=request.user.pk` (or `owner=user`).
- Non-existent and foreign thread IDs must return the identical `404 thread_not_found`. Never confirm that a foreign thread exists.

### CAS concurrency
- `ChatThread.revision` increments on every state modification (turn completion, rename, exchange deletion).
- Mutating endpoints (`PATCH /api/threads/<id>/`, `DELETE /api/threads/<id>/`, `POST .../messages/`, `POST .../regenerate/`, `DELETE .../turns/<id>/`) must validate the client revision.
- Mismatch ⇒ immediate `409 thread_conflict`.

### Run lease serialization (`active_run_token`)
- Every streaming run acquires `active_run_token` + `active_run_expires_at` (300 s TTL, renewed every 60 s). Busy thread ⇒ `409 thread_busy`.
- Release is token-scoped and idempotent. A successful commit clears the lease in the same conditional `ChatThread` update that writes history and revision. Every other exit clears it in the SSE generator's shielded `finally`: init failure, cancellation, tool/provider error, normal completion, client disconnect.
- When Django closes the `StreamingHttpResponse` before its iterator starts, the synchronous closer (`_LeaseClosingStream.close()`) performs the same token-scoped clear. Never clear or overwrite `active_run_token` by any other path.

### Checkpoint-safe turn rollbacks
- Each completed turn stores the exact pre-turn canonical history in `ChatTurn.pre_history_json`.
- `commit_regeneration` and `delete_latest_turn` rewind from that checkpoint; no heuristic history slicing.
- `pre_history_json` is private server-side metadata and must never appear in public API responses.

### Attachment dehydration
- File contents live in `ChatAttachment` rows.
- `ChatThread.history_json` may contain only prompt text plus an attachment metadata marker with UUIDs (`ayce_attachment_ids`).
- Never serialize raw binary or extracted file bytes into `history_json`.

### Governance and administration boundaries
- Administration mutations go through `accounts.services` / `datasets.services` with keyword-only `actor_id` and `revision`. Never write roles, memberships, grants, or audit rows directly from views, admin forms, or scripts.
- The `AuthorizationState` singleton is the write lock and authorization epoch. Keep `select_for_update` out of SQLite paths.
- Dataset eligibility needs all of: active user, `chat.use_analytics`, active membership in an active department, explicit assignment, and a current-source, available, enabled dataset whose grant needs no review. Never infer inheritance for Superadmins or Leads.
- Chat runs re-check owner/department scope and the epoch before every model round. A locked (`quarantined` or department-revoked) thread never reaches a model or the browser.
- The browser and the model never supply an analytics context, Wren URL, or toolset. `AYCE_ANALYTICS_MODE=off` attaches no analytics tools. `dev_jaffle` attaches one reviewed native toolset only after Django admits the full synthetic project for that department; a partial grant attaches nothing.
- `get_mdl` and `wren://project` stay internal to metadata verification. Startup and authorized catalog refresh may read the configured endpoint's `wren://project` resource and argument-free `get_mdl` metadata.
- A turn carrying dataset provenance cannot be regenerated; regeneration never receives native tools.
- Catalog hashes identify the admitted snapshot; they are not physical isolation. `run_sql` read-only enforcement is the unchanged Wren service's: always-on statement allow-list, planned-SQL re-check that fails open on re-parse errors, `strict_mode` table and data-reader gates, DuckDB `ATTACH ... (READ_ONLY)`. AYCE ships no SQL parser and claims no production isolation beyond those layers. Production partial-dataset execution stays disabled. There is no Governed MCP Gateway and no production partial-scope executor.

### Public error sanitization
- Never leak raw exceptions, tracebacks, database errors, SQL, connection details, or Wren MCP details to clients.
- Translate errors at the boundary into an established `chat.views.ERRORS` code with its HTTP status and friendly public message.

## Directory layout

```text
accounts/    Roles, departments, admin audit (authorization.py, services.py, models.py)
datasets/    Wren catalog discovery and explicit grants (discovery.py, services.py)
chat/        agent, service (SSE streaming), threads (CAS/leases/checkpoints),
             attachments, context_budget, mcp + mcp_policy, views, models
config/      Django settings, ASGI entry, lifespan catalog probe
frontend/    React 19 app (src/api.ts, stream.ts, Chat.tsx, Sidebar.tsx, Workspace.tsx)
scripts/     django_check.py, serve.py, smoke_wren_chat.py
tests/       Offline Django suite; support.py holds FunctionModel agent mocks
docs/        Domain documentation
justfile     Task recipes; pyproject.toml (uv + Ruff); package.json (Biome/Vite/Vitest)
```

## Database migrations

- Never manually edit existing migration files.
- After changing `models.py`: `uv run --locked python manage.py makemigrations`
- Non-obvious migrations need an explanatory comment and Django best-practice ordering.

## Commit and PR instructions

- Conventional commits, matching existing history: `fix(analytics): ...`, `test(chat): ...`, `docs(analytics): ...`.
- Before committing or opening a PR: `just ci` must pass.
- Keep the diff scoped; describe behavioral change, invariants touched, and verification run.

## Security

- Do not log or return secrets, tokens, connection strings, or raw provider payloads.
- Untrusted input: uploaded files, prompt text, model output, catalog metadata. Validate at boundaries; keep OOXML/archive guards and attachment size/type limits in place.
- Analytics scope is server-derived only. Never trust browser- or model-supplied scope, URLs, toolsets, or SQL.
