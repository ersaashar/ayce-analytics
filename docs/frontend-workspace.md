# Frontend Architecture & Workspace Design

This document details the frontend implementation, React component tree, client-side streaming protocols, and UI design patterns in **AYCE Analytics V2**.

---

## 1. Technology Stack

- **Framework**: [React 19](https://react.dev/) + [React DOM 19](https://react.dev/)
- **Build Tool**: [Vite 8](https://vitejs.dev/) with `@vitejs/plugin-react`
- **Language**: [TypeScript 5.9](https://www.typescriptlang.org/) in strict mode (`tsc --noEmit`)
- **Linter & Formatter**: [Biome 2.6.6](https://biomejs.dev/)
- **UI & Assistant Primitives**: `@assistant-ui/react` (0.15.33)
- **Typography**: [Plus Jakarta Sans](https://github.com/tokotype/PlusJakartaSans) (`@fontsource/plus-jakarta-sans`)
- **Markdown Rendering**: `react-markdown` + `remark-gfm`
- **Testing**: [Vitest 5.1](https://vitest.dev/) with `@testing-library/react` and `jsdom`

---

## 2. Component Hierarchy

The frontend architecture is located in `frontend/src/`:

```text
frontend/src/
├── main.tsx                         # Client entry point; mounts Workspace into #root
├── Workspace.tsx                    # Root orchestrator: state, layout, keyboard & actions
├── Chat.tsx                         # Main conversation view, messages list, & input composer
├── Sidebar.tsx                      # Thread navigation list, new chat, rename/delete & user menu
├── api.ts                           # Typed fetch clients, CSRF management & CAS helpers
├── stream.ts                        # SSE parser, async event reader & activity reducer
├── test-support.tsx                 # Shared test mocks & synthetic factories
├── style.css                        # Complete light-mode design system & typography tokens
└── components/
    └── chat/
        ├── ThoughtsPanel.tsx        # Collapsible execution activity & reasoning accordion
        ├── MessageMetrics.tsx       # Provenance tags: model name & formatted completion time
        └── DeleteTurnDialog.tsx     # Accessible confirmation dialog for exchange deletion
```

---

## 3. State Management & Lifecycle

The frontend state is managed locally via React hooks without external store libraries (Redux or Zustand), avoiding unnecessary bundle weight and state desynchronization.

### 3.1. Thread List & Active Thread
- `threads`: Array of `ThreadSummary` items, newest first.
- `active`: Current `ThreadDetail` with full display pairs and turn metadata.
- `completed`: Array of completed `ChatMessage` objects derived from `active.display` and `active.turns`.

### 3.2. Ephemeral In-Memory Drafts
- `drafts: Record<string, string>` maps `threadId` (or `"new"`) to the user's uncommitted message text.
- `draftAttachments: Record<string, DraftAttachment[]>` maps `threadId` to pending selected `File` handles.
- Drafts survive tab switching in the browser; navigating between chats preserves text and selected files without saving drafts to local storage.

### 3.3. Optimistic Turn Flow
When the user clicks Send:
1. Optimistic messages (`pending`) are added to the messages list:
   - User message with optimistic file badges.
   - Assistant placeholder message with an empty delta string.
2. The composer input and selected files are immediately cleared from the draft state.
3. If sending fails or is cancelled, the original draft and file selections are automatically restored.

---

## 4. SSE Stream Reader (`stream.ts`)

The SSE parser uses an async generator to parse `text/event-stream` chunks without buffering entire responses:

```typescript
export async function* readChatResponse(
  response: Response
): AsyncGenerator<StreamEvent> {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  // Buffer and split on double newlines (\n\n)
  ...
}
```

### Event Handling:
- `delta`: Concatenates incremental text tokens into the in-flight assistant message.
- `activity`: Fed into `applyActivityUpdate(previous, update)` to update the live step timeline.
- `done`: Emits final committed thread summary and turn metadata; merges the committed state into `active` and replaces the optimistic turn.
- `error`: Triggers error banner and surfaces the server's public error message.

---

## 5. Key UI Components

### 5.1. ThoughtsPanel (`components/chat/ThoughtsPanel.tsx`)
- Displays during generation and persists with the completed turn.
- Shows total elapsed thinking time (`Thought for 2.4s`).
- Expands to reveal ordered execution steps with markers:
  - Brain icon for model reasoning.
  - Dot marker for historical data tools.
  - Step title, tool call arguments summary, status, and duration.
  - If activity was truncated by bounds, displays: *"Additional activity omitted"*.

### 5.2. MessageMetrics (`components/chat/MessageMetrics.tsx`)
- Appears quietly at the foot of each assistant reply.
- Displays the requested model alias (e.g. `gpt-4o`) and formatted completion timestamp in the reader's local timezone.

### 5.3. DeleteTurnDialog (`components/chat/DeleteTurnDialog.tsx`)
- Accessible modal dialog (`HTMLDialogElement`).
- Traps focus and confirms irreversible removal of the latest prompt/reply exchange.

---

## 6. Responsive Layout Modes

The workspace adapts dynamically using `window.matchMedia`:
1. **Desktop** ($\ge 64\text{rem}$): Full two-column view with collapsible sidebar.
2. **Tablet** ($48\text{rem} - 63.999\text{rem}$): Collapsed icon sidebar with slide-out overlay.
3. **Mobile** ($< 48\text{rem}$): Hidden sidebar accessible via hamburger toggle and modal drawer.

---

## Management surface, chat context and locked threads

`frontend/src/main.tsx` dispatches on the page root: `#chat-root` mounts the
existing `Workspace`, `#management-root` mounts `components/admin/Management.tsx`.
The management page is one HTML shell with a fixed section whitelist and
ordinary `?section=` navigation; `management-api.ts` owns its strictly parsed
contracts and reuses the chat client's `requestJson`, CSRF and error plumbing.

Chat bootstrap adds `capabilities`, `memberships`, `analytics_enabled`, and
`analytics_department_ids`, and replaces the old `urls.admin` with
`urls.management` (profile menu → Administration). `analytics_enabled` is true
only when this principal currently has at least one department that admits the
full synthetic scope. It is not a global flag, and no role enables tools by
itself. `analytics_department_ids` lists those department UUIDs. `/api/me/`
keeps the boolean and does not add the list. New threads choose a context near
the composer: `General chat` plus each active department, preselected when
there is exactly one. Multi-department New chat opens a small chooser (Cancel
focused, Escape/backdrop supported) and both creation paths submit the chosen
`department_id`. General chat is never analytics-enabled. An eligible
department shows `Synthetic Jaffle DEV analytics is enabled for this
department.` Every other department context keeps `Analytics queries are
temporarily unavailable. You can still chat and work within your department.`
That notice is eligibility, not a claim that Wren is healthy. A reload is how
the UI learns a grant change. The server rechecks every run. Ordinary chat and
attached-file analysis stay enabled.

Locked threads render only the server's safe projection: redacted title, empty
transcript, fixed explanation, no composer/regenerate/rename, whole-thread
delete still available. Authorization errors clear visible content and re-read
safe metadata instead of trusting cached state. Management dialogs use native
`<dialog>`, keep Cancel focused, restore focus on close, and show one error
surface at a time; losing management capability clears fetched rows and shows a
denied state.
