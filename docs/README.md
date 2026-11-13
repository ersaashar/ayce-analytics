# AYCE Analytics V2 Documentation

Welcome to the comprehensive technical documentation for **AYCE Analytics V2**.

## Documentation Sitemap

1. **[System Architecture & Design Overview](architecture.md)**
   - High-level system topology and component interactions.
   - ASGI server lifecycle, single-origin static file serving.
   - End-to-end SSE turn execution sequence diagram.

2. **[PydanticAI Agent & Tool Integration](agent-and-tools.md)**
   - PydanticAI agent setup, `OpenAICompatibleProvider`, prompt engineering.
   - Default-off synthetic DEV Wren tools; production partial scopes stay disabled.
   - Activity collection, debouncing, and reasoning step disclosure (`ThoughtsPanel`).

3. **[Concurrency, Storage & Checkpoints](concurrency-and-storage.md)**
   - Database schema and ER diagram (`ChatThread`, `ChatTurn`, `ChatAttachment`).
   - Optimistic concurrency control via Compare-and-Swap (CAS) `revision`.
   - Run lease locking (`active_run_token`), heartbeats, and race prevention.
   - Checkpoint-based regeneration and non-destructive exchange deletion.

4. **[Attachments Pipeline & Token Budgeting](attachments-and-budgeting.md)**
   - Upload ingestion pipeline and magic byte signature verification.
   - Office documents (DOCX, XLSX) safe decompression, zip-bomb & macro guards.
   - Native multimodal PDF/image forwarding.
   - Dehydration and hydration lifecycle; conservative token budgeting hooks.

5. **[API Reference & SSE Wire Protocol](api-and-protocol.md)**
   - Complete HTTP endpoint catalog, request and response contracts.
   - Server-Sent Events (SSE) streaming protocol (`delta`, `activity`, `done`, `error`).
   - Comprehensive public error codes catalog and HTTP status mappings.

6. **[Frontend Architecture & Workspace Design](frontend-workspace.md)**
   - React 19, TypeScript, Vite 8, and Biome stack.
   - State management, ephemeral per-thread drafts, optimistic UI updates.
   - Responsive layout modes (Desktop, Tablet, Mobile Drawer).

7. **[Operations, Deployment & Security](operations-and-security.md)**
   - Threat model, zero-trust account provisioning, CSRF & session policies.
   - Production ASGI deployment behind Nginx (SSE buffering disabled).
   - Database backups with SQLite Online Backup API and migration guide.
   - Default `AYCE_ANALYTICS_MODE=off`. `dev_jaffle` is loopback synthetic DEV only. Catalog hashes are not isolation. Production partial-dataset execution stays disabled.
