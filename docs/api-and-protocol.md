# API Reference & SSE Wire Protocol

This document provides a complete specification of all HTTP endpoints, JSON contracts, Server-Sent Events (SSE) streaming protocols, and the public error catalog in **AYCE Analytics V2**.

---

## 1. General API Conventions

- **Base URL**: `/`
- **Authentication**: All chat endpoints require an authenticated session cookie (`sessionid`).
- **CSRF Protection**: All mutating methods (`POST`, `PATCH`, `DELETE`) require Django's CSRF token passed via `X-CSRFToken` header.
- **Cache Policy**: All authenticated HTML and API responses include `Cache-Control: no-store`.
- **Content Types**:
  - Regular API: `application/json`
  - Multipart turns: `multipart/form-data`
  - Streaming turns: `text/event-stream; charset=utf-8`

---

## 2. HTTP Route Catalog

| Method | Path | Django Name | Description | Status Codes |
| --- | --- | --- | --- | --- |
| `GET` | `/` | `chat:index` | Render unselected new-chat workspace | `200`, `302` |
| `GET` | `/chat/<uuid:id>/` | `chat:thread_page` | Render workspace with selected thread | `200`, `404`, `302` |
| `GET` | `/api/threads/` | `chat:threads` | List all threads owned by authenticated user | `200`, `401` |
| `POST` | `/api/threads/` | `chat:threads` | Create a new empty thread | `201`, `400`, `401` |
| `GET` | `/api/threads/<id>/` | `chat:thread_detail` | Get thread details, display pairs, and turns | `200`, `401`, `404` |
| `PATCH` | `/api/threads/<id>/` | `chat:thread_detail` | Rename thread title (requires CAS revision) | `200`, `400`, `401`, `404`, `409` |
| `DELETE` | `/api/threads/<id>/` | `chat:thread_detail` | Delete thread (requires CAS revision) | `200`, `400`, `401`, `404`, `409` |
| `POST` | `/api/threads/<id>/messages/` | `chat:thread_messages` | Send message turn and stream response (SSE) | `200` (SSE), `400`, `401`, `403`, `404`, `409`, `413`, `502`, `503`, `504` |
| `POST` | `/api/threads/<id>/turns/<turn>/regenerate/` | `chat:thread_regenerate` | Regenerate latest turn response (SSE) | `200` (SSE), `400`, `401`, `403`, `404`, `409`, `502`, `503`, `504` |
| `DELETE` | `/api/threads/<id>/turns/<turn>/` | `chat:thread_turn` | Delete latest turn pair and rollback | `200`, `400`, `401`, `404`, `409` |

---

## 3. Request & Response Schemas

### 3.1. Thread Summary (List Entry)
```json
{
  "id": "e305e55e-0498-4447-b7d1-e5d762e847c2",
  "title": "Quarterly Sales Analysis",
  "revision": 3,
  "created_at": "2026-11-10T11:42:00Z",
  "updated_at": "2026-11-10T11:45:30Z",
  "page_url": "/chat/e305e55e-0498-4447-b7d1-e5d762e847c2/",
  "detail_url": "/api/threads/e305e55e-0498-4447-b7d1-e5d762e847c2/",
  "messages_url": "/api/threads/e305e55e-0498-4447-b7d1-e5d762e847c2/messages/"
}
```

### 3.2. Thread Detail
```json
{
  "id": "e305e55e-0498-4447-b7d1-e5d762e847c2",
  "title": "Quarterly Sales Analysis",
  "revision": 3,
  "created_at": "2026-11-10T11:42:00Z",
  "updated_at": "2026-11-10T11:45:30Z",
  "page_url": "/chat/e305e55e-0498-4447-b7d1-e5d762e847c2/",
  "detail_url": "/api/threads/e305e55e-0498-4447-b7d1-e5d762e847c2/",
  "messages_url": "/api/threads/e305e55e-0498-4447-b7d1-e5d762e847c2/messages/",
  "display": [
    {"role": "user", "content": "Show total revenue by region for Q3."},
    {"role": "assistant", "content": "Here is the revenue breakdown..."}
  ],
  "turns": [
    {
      "turn_id": "8f7d9210-234b-48ae-94a2-e64e10c7104b",
      "turn_index": 0,
      "response": {
        "requested_model": "gpt-4o",
        "resolved_model": null,
        "completed_at": "2026-11-10T11:45:30Z",
        "duration_ms": 3410,
        "output_tokens": 512,
        "output_limit_reached": false
      },
      "activity": {
        "version": 1,
        "turn_id": "8f7d9210-234b-48ae-94a2-e64e10c7104b",
        "steps": [
          {"id": 0, "kind": "model", "status": "complete", "title": "Analyzing query...", "duration_ms": 250},
          {"id": 1, "kind": "wren_tool", "status": "complete", "title": "Assembling Tool Call: analytics lookup (historical)", "duration_ms": 2100},
          {"id": 2, "kind": "model", "status": "complete", "title": "Response prepared", "duration_ms": 1060}
        ],
        "truncated": false
      },
      "attachments": [],
      "can_regenerate": true,
      "can_delete": true,
      "regenerate_url": "/api/threads/e305.../turns/8f7d.../regenerate/",
      "delete_url": "/api/threads/e305.../turns/8f7d.../"
    }
  ],
  "conversation_error": null
}
```

`can_regenerate` is false for any turn that still carries private dataset provenance while analytics is unavailable; ordinary provenance-free turns (including ones with attachments) keep `can_regenerate: true` and regenerate normally. `can_delete` is unaffected.

---

## 4. SSE Streaming Wire Protocol

Streaming endpoints (`/api/threads/<id>/messages/` and `/.../regenerate/`) use Server-Sent Events.

### 4.1. Response Headers
```http
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-store
X-Accel-Buffering: no
```

### 4.2. Stream Frames

1. **Connection Prelude**:
   Sent immediately to unblock client buffers:
   ```text
   : connected\n\n
   ```

2. **Delta Event (`event: delta`)**:
   Carries raw assistant output text chunk:
   ```text
   event: delta
   data: {"text":"Here is"}

   event: delta
   data: {"text":" the summary:"}
   ```

3. **Activity Event (`event: activity`)**:
   Carries atomic timeline step state updates:
   ```text
   event: activity
   data: {"turn_id":"8f7d9210...","step":{"id":1,"kind":"wren_tool","status":"running"},"truncated":false}
   ```

4. **Done Event (`event: done`)**:
   Emitted once after the turn and its new revision are durably committed to SQLite:
   ```text
   event: done
   data: {"reply":"Full answer text","thread":{...},"turn":{...}}
   ```

5. **Error Event (`event: error`)**:
   Emitted if generation fails after response headers have been sent:
   ```text
   event: error
   data: {"error":{"code":"mcp_error","message":"The analytics service could not complete this request. Try again."}}
   ```
   `mcp_error` (HTTP 502) is the sanitized Wren transport failure. `warehouse_unavailable` (HTTP 503) is returned when regeneration is denied for a turn that still carries dataset provenance. That refusal stays in force while provenance exists; it is not an "analytics is off" switch. Authorization failures keep their existing codes (`authentication_required`, `authorization_changed`, `department_access_revoked`, `history_quarantined`) and take precedence over backend errors.

---

## 5. Complete Public Error Catalog

All application errors return standard error envelopes:
```json
{
  "error": {
    "code": "thread_conflict",
    "message": "This chat changed in another tab. Reload it before trying again."
  }
}
```

| Error Code | HTTP Status | Public Message |
| --- | --- | --- |
| `invalid_message` | `400` | Enter a message between 1 and 2,000 characters. |
| `invalid_thread` | `400` | Enter a chat name between 1 and 80 characters and a valid revision. |
| `invalid_turn` | `400` | Enter a valid chat revision. |
| `invalid_attachment` | `400` | One or more files could not be read. Use a supported, non-empty file. |
| `unsupported_attachment` | `400` | Supported files: TXT, MD, CSV, JSON, PDF, PNG, JPG, GIF, WEBP, DOCX, XLSX. Save older Word or Excel files as DOCX or XLSX. |
| `context_limit` | `400` | This request exceeds the configured context budget. Shorten the message, use smaller files, or start a new chat. |
| `output_limit` | `400` | The model reached its output limit before producing answer text. Try a shorter request or raise LLM_MAX_OUTPUT_TOKENS. |
| `conversation_limit` | `400` | This conversation has reached its limit. Start a new chat. |
| `authentication_required` | `401` | Sign in to access your chats. |
| `csrf_failed` | `403` | Your page token is missing or expired. Refresh and try again. |
| `history_quarantined` | `403` | This chat is locked because its previous data access could not be verified. |
| `department_access_revoked` | `403` | You no longer have access to this chat's department. |
| `thread_not_found` | `404` | This chat is no longer available. Choose another chat or start a new one. |
| `turn_not_found` | `404` | This exchange is no longer available. Reload the chat. |
| `method_not_allowed` | `405` | This HTTP method is not allowed. |
| `thread_conflict` | `409` | This chat changed in another tab. Reload it before trying again. |
| `thread_busy` | `409` | This chat is already working on a reply. Wait for it to finish. |
| `turn_unavailable` | `409` | Only the latest completed exchange with a saved checkpoint can be changed. |
| `conversation_invalid` | `409` | This chat could not be loaded. Start a new chat or delete it. |
| `authorization_changed` | `409` | Access settings changed while this reply was running. Start the request again. |
| `attachment_limit` | `413` | Files exceed the configured attachment limits. Remove a selected file or start a new chat. |
| `provider_rate_limited` | `429` | The model provider is rate limited. Try again later. |
| `provider_error` | `502` | The model provider could not complete your request. Try again. |
| `mcp_error` | `502` | The analytics service could not complete this request. Try again. |
| `attachment_model_error` | `502` | The model provider could not process this chat's files. Check that the configured model supports PDF or image input and try again. |
| `provider_not_configured` | `503` | Configure your LLM settings in .env. |
| `warehouse_unavailable` | `503` | Analytics queries are temporarily unavailable. You can still chat and work within your department. |
| `provider_timeout` | `504` | The model provider timed out. Try again. |

---

## Management, self-service and governance endpoints

Every management route lives below `/api/`, so `csrf_failure` keeps returning the
JSON envelope. Reads include the current `revision` and `epoch`; every mutation
body carries an integer `revision`, and a stale value returns
`409 administration_conflict`.

`GET /api/me/` and the workspace bootstrap both include `analytics_enabled`.
That boolean is this principal's current eligibility, not a global flag.
Workspace bootstrap also requires `analytics_department_ids`: the eligible
department UUIDs, each of which must be one of the parsed memberships.
`/api/me/` does not add that list.

| Method | Path | Authority |
| --- | --- | --- |
| `GET` | `/api/me/` | Authenticated |
| `GET` | `/api/me/departments/` | Authenticated |
| `GET` | `/api/me/datasets/` | Authenticated (optional `department_id`) |
| `GET/POST` | `/api/admin/users/` | Superadmin |
| `GET/PATCH` | `/api/admin/users/<id>/` | Superadmin |
| `POST` | `/api/admin/users/<id>/reset-password/` | Superadmin; actor password required |
| `GET/PUT` | `/api/admin/users/<id>/roles/` | Superadmin; Superadmin changes need a native superuser |
| `GET/PUT` | `/api/admin/users/<id>/departments/` | Superadmin; complete membership replacement |
| `GET` | `/api/admin/roles/` | Superadmin |
| `GET/POST` | `/api/admin/departments/` | Superadmin; a Lead only lists led departments |
| `GET/PATCH` | `/api/admin/departments/<uuid:id>/` | Superadmin (PATCH); Superadmin or scoped Lead (GET) |
| `GET/PUT` | `/api/admin/departments/<id>/members/` | Superadmin, or a Lead for Staff rows only |
| `GET` | `/api/admin/departments/<id>/member-candidates/` | Superadmin or scoped Lead; `q` needs 2+ characters |
| `GET/PUT` | `/api/admin/departments/<id>/datasets/` | Superadmin (PUT); Superadmin or scoped Lead (GET) |
| `GET/PUT` | `/api/admin/departments/<id>/members/<user_id>/datasets/` | Superadmin, or a Lead for Staff memberships |
| `GET` | `/api/admin/datasets/` | Superadmin; a Lead reads granted metadata with `department_id` |
| `GET/PATCH` | `/api/admin/datasets/<uuid:id>/` | Superadmin; PATCH changes only `is_active` |
| `GET` | `/api/admin/datasets/catalog-status/` | Superadmin with catalog capability |
| `POST` | `/api/admin/datasets/refresh/` | Superadmin with catalog capability; body contains `revision` |
| `GET` | `/api/admin/audit/` | Superadmin with `accounts.view_audit` |

List endpoints accept only `q` (≤ 200 characters), `page` and `page_size`
(default 25, maximum 100) plus their documented extras. Unknown query keys,
booleans and malformed values return `400 invalid_management_request`.
Foreign and missing scoped ids return the same `404 resource_not_found`.

Create a department with `{revision, name, description}`. The server generates
its stable code and rejects normalized-name duplicates with
`department_name_conflict`.

Department grant replacement accepts `{revision, dataset_ids}` and optional
`confirmed_dataset_ids`. Confirming an available grant that needs review restores
the department grant and clears its member assignments; administrators must
explicitly assign those members again. Grant reads retain redacted references to
foreign-source datasets and report their availability.

The management page itself is `GET /management/?section=users|departments|datasets|roles|audit`. Unknown sections are 404, unauthorized sections 403, and
anonymous requests redirect to the login page.

### Governance error codes added in this release

| Code | HTTP | Meaning |
| --- | --- | --- |
| `permission_denied` | 403 | Native capability or object scope refused |
| `resource_not_found` | 404 | Missing or out-of-scope target (indistinguishable) |
| `invalid_management_request` | 400 | Whitelist, type or relationship validation failed |
| `administration_conflict` | 409 | Administration revision (or SQLite write contention) is stale |
| `last_admin_protected` | 409 | The action would leave no active administrator |
| `reauthentication_failed` | 403 | The actor's own password did not verify |
| `invalid_password` | 400 | The new password fails the configured validators |
| `catalog_unavailable` | 503 | Wren metadata discovery is unavailable |
| `department_name_conflict` | 409 | A department with the normalized name already exists |
| `history_quarantined` | 403 | The thread's legacy provenance cannot be verified |
| `department_access_revoked` | 403 | The thread's department is no longer accessible |
| `authorization_changed` | 409 | Access settings changed while a reply was running |

Thread summaries now carry `access` (`available`, `quarantined`,
`department_revoked`) and `department` (`{id, code, name}` or `null`). Locked
summaries redact the title to `Restricted chat`; locked details return empty
`display`/`turns` and a fixed `conversation_error`, while whole-thread deletion
still works with the current revision.
