import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ATTACHMENT_LIMIT,
  type AttachmentRef,
  assertCommittedTurn,
  assertCompletedThread,
  attachmentMediaType,
  type Bootstrap,
  CHAT_BUSY_MESSAGE,
  CHAT_SERVER_ERROR,
  type ChatLimits,
  createThread,
  deleteThread as deleteThreadRequest,
  deleteTurn as deleteTurnRequest,
  fetchThread,
  fetchThreads,
  INVALID_ATTACHMENT,
  INVALID_MESSAGE,
  MAX_MESSAGE_LENGTH,
  postTurn,
  readCsrfToken,
  regenerateTurn,
  renameThread as renameThreadRequest,
  type ThreadDetail,
  type ThreadSummary,
  type TurnDetail,
  toChatError,
  UNSUPPORTED_ATTACHMENT,
  upsertThread,
} from "./api";
import {
  Chat,
  type ChatMessage,
  type DraftAttachment,
  type RegenerationPreview,
} from "./Chat";
import { PanelLeftIcon, PlusIcon, Sidebar } from "./Sidebar";
import { type ActivityState, applyActivityUpdate, readChatResponse } from "./stream";

type Action =
  | "send"
  | "create"
  | "switch"
  | "rename"
  | "delete"
  | "regenerate"
  | "delete-turn"
  | null;

type PendingPair = {
  threadId: string;
  userId: string;
  assistantId: string;
  text: string;
  attachments: AttachmentRef[];
};

type Regeneration = RegenerationPreview & { turnId: string };

type LayoutMode = "mobile" | "tablet" | "desktop";

const NEW_THREAD_DRAFT = "new";

// Selected files only need list-unique ids in this page's session.
let draftSequence = 0;

function nextDraftId(): string {
  draftSequence += 1;
  return `draft-${draftSequence}`;
}

function readLayoutMode(): LayoutMode {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "desktop";
  }
  if (window.matchMedia("(max-width: 47.999rem)").matches) return "mobile";
  if (window.matchMedia("(max-width: 63.999rem)").matches) return "tablet";
  return "desktop";
}

function toMessages(detail: ThreadDetail | null): ChatMessage[] {
  if (detail === null) return [];
  const turns = new Map<number, TurnDetail>(
    detail.turns.map((turn) => [turn.turn_index, turn]),
  );
  return detail.display.map((entry, index) => {
    const turn = turns.get(Math.floor(index / 2)) ?? null;
    return {
      id: `${detail.id}:${index}`,
      role: entry.role,
      content: entry.content,
      turn: entry.role === "assistant" ? turn : null,
      attachments: entry.role === "user" ? (turn?.attachments ?? []) : [],
      pending: false,
    };
  });
}

/** Local pre-check of one picked batch against the server's advertised quotas. */
function selectionRejection(
  selected: readonly File[],
  current: readonly DraftAttachment[],
  storedCount: number,
  limits: ChatLimits,
): { code: string; message: string } | null {
  for (const file of selected) {
    if (attachmentMediaType(file.name) === null) {
      return { code: "unsupported_attachment", message: UNSUPPORTED_ATTACHMENT };
    }
    if (file.size <= 0) {
      return { code: "invalid_attachment", message: INVALID_ATTACHMENT };
    }
    if (file.size > limits.max_file_bytes) {
      return { code: "attachment_limit", message: ATTACHMENT_LIMIT };
    }
  }
  if (current.length + selected.length > limits.max_files_per_turn) {
    return { code: "attachment_limit", message: ATTACHMENT_LIMIT };
  }
  if (storedCount + current.length + selected.length > limits.max_files_per_thread) {
    return { code: "attachment_limit", message: ATTACHMENT_LIMIT };
  }
  return null;
}

/** A turn that is no longer the latest exchange is never actionable here. */
function withoutActions(turn: TurnDetail): TurnDetail {
  return { ...turn, can_regenerate: false, can_delete: false };
}

function historyTurns(turns: readonly TurnDetail[]): TurnDetail[] {
  return turns.map((turn) =>
    turn.can_regenerate || turn.can_delete ? withoutActions(turn) : turn,
  );
}

function assistantIdFor(detail: ThreadDetail, turnIndex: number): string {
  return `${detail.id}:${turnIndex * 2 + 1}`;
}

function isLatestTurn(detail: ThreadDetail, turn: TurnDetail): boolean {
  return turn.turn_index === detail.display.length / 2 - 1;
}

function draftKey(detail: ThreadDetail | null): string {
  return detail === null ? NEW_THREAD_DRAFT : detail.id;
}

const ANALYTICS_UNAVAILABLE_NOTE =
  "Analytics queries are temporarily unavailable. You can still chat and work within your department.";
const ANALYTICS_ENABLED_NOTE =
  "Synthetic Jaffle DEV analytics is enabled for this department.";

// Null department is general chat and is never analytics-enabled.
function departmentAnalyticsNote(
  eligibleDepartmentIds: ReadonlySet<string>,
  departmentId: string | null,
): string | null {
  if (departmentId === null) return null;
  return eligibleDepartmentIds.has(departmentId)
    ? ANALYTICS_ENABLED_NOTE
    : ANALYTICS_UNAVAILABLE_NOTE;
}

export function Workspace({ bootstrap }: { bootstrap: Bootstrap }) {
  const { user, urls } = bootstrap;
  const limits = bootstrap.limits;
  // Active departments this account may start a department chat in. An empty
  // list is normal: general chat needs no membership at all.
  const departmentOptions = useMemo(
    () => bootstrap.memberships.map((membership) => membership.department),
    [bootstrap.memberships],
  );
  const analyticsDepartments = useMemo(
    () => new Set(bootstrap.analytics_department_ids),
    [bootstrap.analytics_department_ids],
  );
  const defaultContext =
    departmentOptions.length === 1 ? departmentOptions[0].id : null;
  const csrfToken = useMemo(() => readCsrfToken(), []);

  const [threads, setThreads] = useState<ThreadSummary[]>(bootstrap.threads);
  const [active, setActive] = useState<ThreadDetail | null>(bootstrap.active_thread);
  const [completed, setCompleted] = useState<ChatMessage[]>(() =>
    toMessages(bootstrap.active_thread),
  );
  const [drafts, setDrafts] = useState<Record<string, string>>(() => ({
    [draftKey(bootstrap.active_thread)]: "",
  }));
  // Selected files live beside the text drafts and never leave this page.
  const [draftAttachments, setDraftAttachments] = useState<
    Record<string, DraftAttachment[]>
  >(() => ({ [draftKey(bootstrap.active_thread)]: [] }));
  const [pending, setPending] = useState<PendingPair | null>(null);
  const [streamText, setStreamText] = useState("");
  const [streamActivity, setStreamActivity] = useState<ActivityState | null>(null);
  const [regeneration, setRegeneration] = useState<Regeneration | null>(null);
  const [action, setAction] = useState<Action>(null);
  const [error, setError] = useState(bootstrap.active_thread?.conversation_error ?? "");
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [epoch, setEpoch] = useState(0);
  const [focusRequest, setFocusRequest] = useState(0);
  const [expanded, setExpanded] = useState(() => readLayoutMode() !== "tablet");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [mode, setMode] = useState<LayoutMode>(readLayoutMode);
  const [newThreadContext, setNewThreadContext] = useState<string | null>(
    defaultContext,
  );
  const [chooserOpen, setChooserOpen] = useState(false);
  const [chooserChoice, setChooserChoice] = useState<string | null>(defaultContext);

  const lock = useRef(false);
  const abort = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const drawer = useRef<HTMLDialogElement | null>(null);
  const chooser = useRef<HTMLDialogElement | null>(null);
  const chooserCancel = useRef<HTMLButtonElement | null>(null);
  const expandToggle = useRef<HTMLButtonElement | null>(null);

  const activeKey = draftKey(active);
  const draft = drafts[activeKey] ?? "";
  const selectedAttachments = draftAttachments[activeKey] ?? [];
  const storedAttachmentCount = useMemo(
    () =>
      (active?.turns ?? []).reduce((total, turn) => total + turn.attachments.length, 0),
    [active],
  );
  const remainingThreadSlots = Math.max(
    0,
    limits.max_files_per_thread - storedAttachmentCount - selectedAttachments.length,
  );
  // A locked thread keeps its metadata but can never be read, renamed or run.
  const locked = active !== null && active.access !== "available";
  const blocked =
    action !== null || locked || (active?.conversation_error ?? null) !== null;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      abort.current?.abort();
    };
  }, []);

  useEffect(() => {
    const mobile = window.matchMedia("(max-width: 47.999rem)");
    const tablet = window.matchMedia("(max-width: 63.999rem)");
    const update = () =>
      setMode(mobile.matches ? "mobile" : tablet.matches ? "tablet" : "desktop");
    update();
    mobile.addEventListener("change", update);
    tablet.addEventListener("change", update);
    return () => {
      mobile.removeEventListener("change", update);
      tablet.removeEventListener("change", update);
    };
  }, []);

  useEffect(() => {
    setExpanded(mode !== "tablet");
    setDrawerOpen(false);
  }, [mode]);

  useEffect(() => {
    const element = chooser.current;
    if (element === null) return;
    if (chooserOpen && !element.open) {
      element.showModal();
      // Cancel starts focused; the primary action stays one Tab away.
      requestAnimationFrame(() => chooserCancel.current?.focus());
    }
    if (!chooserOpen && element.open) element.close();
  }, [chooserOpen]);

  useEffect(() => {
    const element = drawer.current;
    if (element === null) return;
    if (mode !== "mobile") {
      if (element.open) element.close();
      return;
    }
    if (drawerOpen && !element.open) element.showModal();
    if (!drawerOpen && element.open) element.close();
  }, [drawerOpen, mode]);

  const messages = useMemo<ChatMessage[]>(() => {
    if (pending === null) return completed;
    return [
      ...completed,
      {
        id: pending.userId,
        role: "user" as const,
        content: pending.text,
        turn: null,
        attachments: pending.attachments,
        pending: false,
      },
      {
        id: pending.assistantId,
        role: "assistant" as const,
        content: streamText,
        turn: null,
        attachments: [],
        pending: true,
      },
    ];
  }, [completed, pending, streamText]);

  const announce = useCallback((message: string) => {
    setStatus(message);
    setFocusRequest((value) => value + 1);
  }, []);

  const redirectToLogin = useCallback(() => {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(`${urls.login}?next=${encodeURIComponent(next)}`);
  }, [urls.login]);

  const clearPrivateState = useCallback(() => {
    setThreads([]);
    setActive(null);
    setCompleted([]);
    setPending(null);
    setStreamText("");
    setStreamActivity(null);
    setRegeneration(null);
    setDrafts({});
    // File references are dropped with the account view, never restored later.
    setDraftAttachments({});
    setError("");
    setErrorCode(null);
    setStatus("");
  }, []);

  const failureMessage = useCallback(
    (caught: unknown): string => {
      const failure = toChatError(caught);
      if (failure.code === "authentication_required") {
        clearPrivateState();
        redirectToLogin();
        return failure.message;
      }
      setError(failure.message);
      setErrorCode(failure.code);
      setStatus("");
      return failure.message;
    },
    [clearPrivateState, redirectToLogin],
  );

  const fail = useCallback(
    (caught: unknown, restoreKey: string, rawDraft?: string) => {
      const failure = toChatError(caught);
      if (failure.code === "aborted") return;
      if (failure.code === "authentication_required") {
        clearPrivateState();
        redirectToLogin();
        return;
      }
      if (rawDraft !== undefined) {
        setDrafts((previous) => ({ ...previous, [restoreKey]: rawDraft }));
      }
      setError(failure.message);
      setErrorCode(failure.code);
      setStatus("");
    },
    [clearPrivateState, redirectToLogin],
  );

  const startChat = useCallback(
    async (contextId: string | null) => {
      lock.current = true;
      setAction("create");
      setStatus("Creating chat…");
      setError("");
      setErrorCode(null);
      try {
        const created = await createThread(urls.threads, contextId);
        setThreads((previous) => upsertThread(previous, created));
        setActive(created);
        setCompleted([]);
        setPending(null);
        setStreamText("");
        setStreamActivity(null);
        setRegeneration(null);
        setNewThreadContext(defaultContext);
        setDrafts((previous) => ({ ...previous, [created.id]: "" }));
        setDraftAttachments((previous) => ({ ...previous, [created.id]: [] }));
        setError(created.conversation_error ?? "");
        setErrorCode(null);
        window.history.pushState({}, "", created.page_url);
        setEpoch((value) => value + 1);
        setDrawerOpen(false);
        if (mode === "tablet") setExpanded(false);
        announce("New chat ready.");
      } catch (caught) {
        fail(caught, activeKey);
      } finally {
        lock.current = false;
        if (mounted.current) {
          setAction(null);
          setFocusRequest((value) => value + 1);
        }
      }
    },
    [activeKey, announce, defaultContext, fail, mode, urls.threads],
  );

  // A locked or re-authorized thread drops its visible content and re-reads
  // the server's safe metadata instead of trusting the previous transcript.
  useEffect(() => {
    if (
      errorCode !== "history_quarantined" &&
      errorCode !== "department_access_revoked" &&
      errorCode !== "authorization_changed"
    ) {
      return;
    }
    setCompleted([]);
    setPending(null);
    setStreamText("");
    setStreamActivity(null);
    setRegeneration(null);
    const detailUrl = active?.detail_url;
    if (detailUrl === undefined) return;
    const controller = new AbortController();
    void fetchThread(detailUrl, controller.signal)
      .then((detail) => {
        if (!mounted.current) return;
        setActive(detail);
        setCompleted(toMessages(detail));
        setThreads((previous) => upsertThread(previous, detail));
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [active?.detail_url, errorCode]);

  const newChat = useCallback(async () => {
    if (lock.current) return;
    // With several departments the chooser decides the context explicitly.
    if (departmentOptions.length > 1) {
      setChooserChoice(newThreadContext);
      setChooserOpen(true);
      return;
    }
    await startChat(defaultContext);
  }, [defaultContext, departmentOptions.length, newThreadContext, startChat]);

  const selectThread = useCallback(
    async (thread: ThreadSummary) => {
      if (lock.current) return;
      if (active !== null && active.id === thread.id) {
        setDrawerOpen(false);
        if (mode === "tablet") setExpanded(false);
        return;
      }

      lock.current = true;
      const controller = new AbortController();
      abort.current = controller;
      setAction("switch");
      setStatus("Loading chat…");
      setError("");
      setErrorCode(null);

      try {
        const detail = await fetchThread(thread.detail_url, controller.signal);
        setActive(detail);
        setCompleted(toMessages(detail));
        setPending(null);
        setStreamText("");
        setStreamActivity(null);
        setRegeneration(null);
        setError(detail.conversation_error ?? "");
        setErrorCode(null);
        window.history.pushState({}, "", detail.page_url);
        setEpoch((value) => value + 1);
        setDrawerOpen(false);
        if (mode === "tablet") setExpanded(false);
        announce("Chat loaded.");
      } catch (caught) {
        fail(caught, activeKey);
      } finally {
        abort.current = null;
        lock.current = false;
        if (mounted.current) {
          setAction(null);
          setFocusRequest((value) => value + 1);
        }
      }
    },
    [active, activeKey, announce, fail, mode],
  );

  const reloadActive = useCallback(async () => {
    if (active === null || lock.current) return;

    lock.current = true;
    const controller = new AbortController();
    abort.current = controller;
    setAction("switch");
    setStatus("Reloading chat…");

    try {
      const detail = await fetchThread(active.detail_url, controller.signal);
      setActive(detail);
      setCompleted(toMessages(detail));
      setPending(null);
      setStreamText("");
      setStreamActivity(null);
      setRegeneration(null);
      setError(detail.conversation_error ?? "");
      setErrorCode(null);
      window.history.replaceState({}, "", detail.page_url);
      setEpoch((value) => value + 1);
      announce("Chat reloaded.");
    } catch (caught) {
      fail(caught, activeKey);
    } finally {
      abort.current = null;
      lock.current = false;
      if (mounted.current) {
        setAction(null);
        setFocusRequest((value) => value + 1);
      }
    }
  }, [active, activeKey, announce, fail]);

  const refreshChats = useCallback(async () => {
    if (lock.current) return;

    lock.current = true;
    const controller = new AbortController();
    abort.current = controller;
    setAction("switch");
    setStatus("Refreshing chats…");

    try {
      const next = await fetchThreads(urls.threads, controller.signal);
      setThreads(next);
      if (active !== null && !next.some((thread) => thread.id === active.id)) {
        setActive(null);
        setCompleted([]);
        setPending(null);
        setStreamText("");
        setStreamActivity(null);
        setRegeneration(null);
        window.history.replaceState({}, "", "/");
        setEpoch((value) => value + 1);
        setError("");
      } else {
        setError(active?.conversation_error ?? "");
      }
      setErrorCode(null);
      announce("Chats refreshed.");
    } catch (caught) {
      fail(caught, activeKey);
    } finally {
      abort.current = null;
      lock.current = false;
      if (mounted.current) {
        setAction(null);
        setFocusRequest((value) => value + 1);
      }
    }
  }, [active, activeKey, announce, fail, urls.threads]);

  const send = useCallback(
    async (rawDraft: string) => {
      if (lock.current) return;
      if (active?.conversation_error != null || locked) {
        setError(active?.conversation_error ?? CHAT_SERVER_ERROR);
        return;
      }

      if (Array.from(rawDraft).length > MAX_MESSAGE_LENGTH) {
        setError(INVALID_MESSAGE);
        setErrorCode(null);
        return;
      }
      const text = rawDraft.trim();
      if (text.length === 0) {
        setError(INVALID_MESSAGE);
        setErrorCode(null);
        return;
      }

      lock.current = true;
      const controller = new AbortController();
      abort.current = controller;
      setAction("send");
      setStatus("Sending…");
      setError("");
      setErrorCode(null);
      setRegeneration(null);

      // Snapshot the selection so a committed turn owns these exact files.
      const selectedFiles = selectedAttachments.map((entry) => entry.file);
      const optimisticAttachments: AttachmentRef[] = selectedFiles.map(
        (file, index) => ({
          id: `pending-${index}`,
          name: file.name,
          media_type: attachmentMediaType(file.name) ?? "application/octet-stream",
          size: file.size,
        }),
      );
      let restoreKey = activeKey;
      let snapshot: ThreadDetail;

      try {
        if (active === null) {
          const created = await createThread(urls.threads, newThreadContext);
          setThreads((previous) => upsertThread(previous, created));
          setActive(created);
          setCompleted([]);
          setDrafts((previous) => {
            const next = { ...previous };
            const home = next[NEW_THREAD_DRAFT] ?? "";
            delete next[NEW_THREAD_DRAFT];
            next[created.id] = home;
            return next;
          });
          setDraftAttachments((previous) => {
            const next = { ...previous };
            const home = next[NEW_THREAD_DRAFT] ?? [];
            delete next[NEW_THREAD_DRAFT];
            next[created.id] = home;
            return next;
          });
          window.history.pushState({}, "", created.page_url);
          setEpoch((value) => value + 1);
          snapshot = created;
          restoreKey = created.id;
        } else {
          snapshot = active;
        }

        const base = snapshot.display;
        const userId = `${snapshot.id}:${base.length}`;
        const assistantId = `${snapshot.id}:${base.length + 1}`;
        setPending({
          threadId: snapshot.id,
          userId,
          assistantId,
          text,
          attachments: optimisticAttachments,
        });
        setStreamText("");
        setStreamActivity(null);
        setDrafts((previous) => ({ ...previous, [snapshot.id]: "" }));
        setDraftAttachments((previous) => ({ ...previous, [snapshot.id]: [] }));

        const response = await postTurn(
          snapshot.messages_url,
          text,
          snapshot.revision,
          controller.signal,
          selectedFiles,
        );

        let activity: ActivityState | null = null;
        for await (const event of readChatResponse(response)) {
          if (event.type === "delta") {
            setStreamText((previous) => previous + event.text);
            continue;
          }

          if (event.type === "activity") {
            activity = applyActivityUpdate(activity, event.update);
            setStreamActivity(activity);
            continue;
          }

          assertCompletedThread(event.thread, snapshot.id, snapshot.revision);
          assertCommittedTurn(event.turn, {
            turnId: activity?.turnId ?? event.turn.turn_id,
            turnIndex: base.length / 2,
          });

          const display = [
            ...base,
            { role: "user" as const, content: text },
            { role: "assistant" as const, content: event.reply },
          ];
          const detail: ThreadDetail = {
            ...snapshot,
            ...event.thread,
            display,
            turns: [...historyTurns(snapshot.turns), event.turn],
            conversation_error: null,
          };
          setCompleted(toMessages(detail));
          setActive(detail);
          setPending(null);
          setStreamText("");
          setStreamActivity(null);
          setThreads((previous) => upsertThread(previous, event.thread));
          setDrafts((previous) => ({ ...previous, [snapshot.id]: "" }));
          setError("");
          setErrorCode(null);
          announce(
            event.turn.response?.output_limit_reached === true
              ? "Output limit reached; the answer may be incomplete."
              : "Reply ready.",
          );
        }
      } catch (caught) {
        setPending(null);
        setStreamText("");
        setStreamActivity(null);
        if (controller.signal.aborted) {
          if (rawDraft !== undefined) {
            setDrafts((previous) => ({ ...previous, [restoreKey]: rawDraft }));
          }
          setDraftAttachments((previous) => ({
            ...previous,
            [restoreKey]: selectedAttachments,
          }));
          return;
        }
        if (toChatError(caught).code !== "authentication_required") {
          setDraftAttachments((previous) => ({
            ...previous,
            [restoreKey]: selectedAttachments,
          }));
        }
        fail(caught, restoreKey, rawDraft);
        return;
      } finally {
        abort.current = null;
        lock.current = false;
        if (mounted.current) {
          setAction(null);
          setFocusRequest((value) => value + 1);
        }
      }
    },
    [
      active,
      activeKey,
      announce,
      fail,
      locked,
      newThreadContext,
      selectedAttachments,
      urls.threads,
    ],
  );

  const regenerate = useCallback(
    async (turnId: string): Promise<string | null> => {
      if (lock.current) return CHAT_BUSY_MESSAGE;
      const snapshot = active;
      if (snapshot === null) return CHAT_SERVER_ERROR;

      const turn = snapshot.turns.find((entry) => entry.turn_id === turnId);
      if (turn === undefined || !turn.can_regenerate || !isLatestTurn(snapshot, turn)) {
        return CHAT_BUSY_MESSAGE;
      }

      lock.current = true;
      const controller = new AbortController();
      abort.current = controller;
      setAction("regenerate");
      setStatus("Regenerating…");
      setError("");
      setErrorCode(null);
      setRegeneration({
        turnId,
        messageId: assistantIdFor(snapshot, turn.turn_index),
        text: "",
        activity: null,
      });

      try {
        const response = await regenerateTurn(
          turn.regenerate_url,
          snapshot.revision,
          controller.signal,
        );

        let activity: ActivityState | null = null;
        for await (const event of readChatResponse(response)) {
          if (event.type === "delta") {
            setRegeneration((previous) =>
              previous === null
                ? previous
                : { ...previous, text: previous.text + event.text },
            );
            continue;
          }

          if (event.type === "activity") {
            activity = applyActivityUpdate(activity, event.update);
            setRegeneration((previous) =>
              previous === null ? previous : { ...previous, activity },
            );
            continue;
          }

          assertCompletedThread(event.thread, snapshot.id, snapshot.revision);
          assertCommittedTurn(event.turn, {
            turnId,
            turnIndex: turn.turn_index,
          });

          const display = [
            ...snapshot.display.slice(0, -1),
            { role: "assistant" as const, content: event.reply },
          ];
          const detail: ThreadDetail = {
            ...snapshot,
            ...event.thread,
            display,
            turns: snapshot.turns.map((entry) =>
              entry.turn_id === turnId ? event.turn : withoutActions(entry),
            ),
            conversation_error: null,
          };
          setCompleted(toMessages(detail));
          setActive(detail);
          setThreads((previous) => upsertThread(previous, event.thread));
          setRegeneration(null);
          setError("");
          setErrorCode(null);
          announce("Reply replaced.");
        }
        return null;
      } catch (caught) {
        setRegeneration(null);
        if (controller.signal.aborted) return null;
        return failureMessage(caught);
      } finally {
        abort.current = null;
        lock.current = false;
        if (mounted.current) {
          setAction(null);
          setFocusRequest((value) => value + 1);
        }
      }
    },
    [active, announce, failureMessage],
  );

  const removeTurn = useCallback(
    async (turnId: string): Promise<string | null> => {
      if (lock.current) return CHAT_BUSY_MESSAGE;
      const snapshot = active;
      if (snapshot === null) return CHAT_SERVER_ERROR;

      const turn = snapshot.turns.find((entry) => entry.turn_id === turnId);
      if (turn === undefined || !turn.can_delete || !isLatestTurn(snapshot, turn)) {
        return CHAT_BUSY_MESSAGE;
      }

      lock.current = true;
      const controller = new AbortController();
      abort.current = controller;
      setAction("delete-turn");
      setStatus("Deleting exchange…");

      try {
        const detail = await deleteTurnRequest(
          turn.delete_url,
          snapshot.revision,
          controller.signal,
        );
        setCompleted(toMessages(detail));
        setActive(detail);
        setThreads((previous) => upsertThread(previous, detail));
        setPending(null);
        setStreamText("");
        setStreamActivity(null);
        setRegeneration(null);
        setError("");
        setErrorCode(null);
        announce("Exchange deleted.");
        return null;
      } catch (caught) {
        return failureMessage(caught);
      } finally {
        abort.current = null;
        lock.current = false;
        if (mounted.current) {
          setAction(null);
          setFocusRequest((value) => value + 1);
        }
      }
    },
    [active, announce, failureMessage],
  );

  const rename = useCallback(
    async (threadId: string, title: string): Promise<string | null> => {
      if (lock.current) return CHAT_BUSY_MESSAGE;
      const summary = threads.find((thread) => thread.id === threadId);
      if (summary === undefined) return CHAT_SERVER_ERROR;

      lock.current = true;
      setAction("rename");
      try {
        const updated = await renameThreadRequest(
          summary.detail_url,
          title,
          summary.revision,
        );
        setThreads((previous) => upsertThread(previous, updated));
        setActive((previous) =>
          previous !== null && previous.id === threadId
            ? { ...previous, ...updated }
            : previous,
        );
        announce("Chat renamed.");
        return null;
      } catch (caught) {
        return failureMessage(caught);
      } finally {
        lock.current = false;
        if (mounted.current) {
          setAction(null);
          setFocusRequest((value) => value + 1);
        }
      }
    },
    [announce, failureMessage, threads],
  );

  const remove = useCallback(
    async (threadId: string): Promise<string | null> => {
      if (lock.current) return CHAT_BUSY_MESSAGE;
      const summary = threads.find((thread) => thread.id === threadId);
      if (summary === undefined) return CHAT_SERVER_ERROR;

      lock.current = true;
      setAction("delete");
      try {
        await deleteThreadRequest(summary.detail_url, summary.revision);
        setThreads((previous) => previous.filter((thread) => thread.id !== threadId));
        setDrafts((previous) => {
          const next = { ...previous };
          delete next[threadId];
          return next;
        });
        setDraftAttachments((previous) => {
          const next = { ...previous };
          delete next[threadId];
          return next;
        });
        if (active !== null && active.id === threadId) {
          setActive(null);
          setCompleted([]);
          setPending(null);
          setStreamText("");
          setStreamActivity(null);
          setRegeneration(null);
          setError("");
          setErrorCode(null);
          window.history.pushState({}, "", "/");
          setEpoch((value) => value + 1);
        }
        announce("Chat deleted.");
        return null;
      } catch (caught) {
        return failureMessage(caught);
      } finally {
        lock.current = false;
        if (mounted.current) {
          setAction(null);
          setFocusRequest((value) => value + 1);
        }
      }
    },
    [active, announce, failureMessage, threads],
  );

  const handleDraftChange = useCallback(
    (value: string) => {
      setDrafts((previous) => ({ ...previous, [activeKey]: value }));
    },
    [activeKey],
  );

  const attachFiles = useCallback(
    (files: File[]) => {
      const selected = Array.from(files);
      if (selected.length === 0) return;
      const rejection = selectionRejection(
        selected,
        selectedAttachments,
        storedAttachmentCount,
        limits,
      );
      if (rejection !== null) {
        setError(rejection.message);
        setErrorCode(rejection.code);
        setStatus("");
        return;
      }
      setDraftAttachments((previous) => ({
        ...previous,
        [activeKey]: [
          ...(previous[activeKey] ?? []),
          ...selected.map((file) => ({ id: nextDraftId(), file })),
        ],
      }));
      setError("");
      setErrorCode(null);
      announce(
        selected.length === 1 ? "File selected." : `${selected.length} files selected.`,
      );
    },
    [activeKey, announce, limits, selectedAttachments, storedAttachmentCount],
  );

  const removeAttachment = useCallback(
    (id: string) => {
      setDraftAttachments((previous) => ({
        ...previous,
        [activeKey]: (previous[activeKey] ?? []).filter((entry) => entry.id !== id),
      }));
      announce("File removed.");
    },
    [activeKey, announce],
  );

  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  // A collapsed sidebar hides its own controls, so focus moves to the toggle.
  const collapseSidebar = useCallback(() => {
    setExpanded(false);
    requestAnimationFrame(() => expandToggle.current?.focus());
  }, []);

  const alert: ReactNode =
    error === "" ? null : (
      <div className="alert" role="alert">
        <p className="alert-text">{error}</p>
        <div className="alert-actions">
          {errorCode === "thread_conflict" ||
          errorCode === "thread_busy" ||
          errorCode === "turn_unavailable" ? (
            <button
              type="button"
              className="button secondary"
              disabled={action !== null}
              onClick={() => void reloadActive()}
            >
              Reload chat
            </button>
          ) : null}
          {errorCode === "thread_not_found" ? (
            <>
              <button
                type="button"
                className="button secondary"
                disabled={action !== null}
                onClick={() => void refreshChats()}
              >
                Refresh chats
              </button>
              <button
                type="button"
                className="button primary"
                disabled={action !== null}
                onClick={() => void newChat()}
              >
                New chat
              </button>
            </>
          ) : null}
        </div>
      </div>
    );

  const newThreadAnalyticsNote = departmentAnalyticsNote(
    analyticsDepartments,
    newThreadContext,
  );
  const activeAnalyticsNote = departmentAnalyticsNote(
    analyticsDepartments,
    active?.department?.id ?? null,
  );
  const chooserAnalyticsNote = departmentAnalyticsNote(
    analyticsDepartments,
    chooserChoice,
  );

  return (
    <div
      className="workspace"
      data-mode={mode}
      data-sidebar={expanded ? "expanded" : "collapsed"}
    >
      {mode !== "mobile" ? (
        <aside
          id="chat-sidebar"
          className="sidebar"
          data-collapsed={expanded ? "false" : "true"}
          aria-label="Chats"
          aria-hidden={!expanded}
        >
          <Sidebar
            threads={threads}
            activeId={active?.id ?? null}
            busy={action !== null}
            username={user.username}
            managementUrl={urls.management}
            logoutUrl={urls.logout}
            csrfToken={csrfToken}
            onNewChat={() => void newChat()}
            onSelect={(thread) => void selectThread(thread)}
            onRename={rename}
            onDelete={remove}
            onCollapse={collapseSidebar}
            onClose={null}
          />
        </aside>
      ) : null}

      {mode === "tablet" && expanded ? (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label="Close sidebar"
          onClick={collapseSidebar}
        />
      ) : null}

      <main className="main">
        <div className="surface">
          <header className="main-header">
            {mode === "mobile" ? (
              <button
                type="button"
                className="icon-button header-toggle"
                aria-haspopup="dialog"
                aria-expanded={drawerOpen}
                onClick={() => setDrawerOpen(true)}
                aria-label="Open sidebar"
                title="Open sidebar"
              >
                <PanelLeftIcon />
              </button>
            ) : expanded ? null : (
              <button
                ref={expandToggle}
                type="button"
                className="icon-button header-toggle"
                aria-controls="chat-sidebar"
                aria-expanded={expanded}
                onClick={() => setExpanded(true)}
                aria-label="Open sidebar"
                title="Open sidebar"
              >
                <PanelLeftIcon />
              </button>
            )}
            {!expanded || mode === "mobile" ? (
              <button
                type="button"
                className="icon-button header-new-chat"
                onClick={() => void newChat()}
                disabled={action !== null}
                aria-label="New chat"
                title="New chat"
              >
                <PlusIcon />
              </button>
            ) : null}
            <h1 className="main-title">
              {active === null ? "AYCE Analytics V2" : active.title}
            </h1>
          </header>

          {active === null ? (
            departmentOptions.length > 0 ? (
              <div className="context-bar">
                <label className="context-field" htmlFor="chat-context">
                  <span className="field-label">Chat context</span>
                  <select
                    id="chat-context"
                    value={newThreadContext ?? ""}
                    disabled={action !== null}
                    onChange={(event) =>
                      setNewThreadContext(
                        event.target.value === "" ? null : event.target.value,
                      )
                    }
                  >
                    <option value="">General chat</option>
                    {departmentOptions.map((department) => (
                      <option key={department.id} value={department.id}>
                        {department.name}
                      </option>
                    ))}
                  </select>
                </label>
                {newThreadAnalyticsNote !== null ? (
                  <p className="context-note">{newThreadAnalyticsNote}</p>
                ) : null}
              </div>
            ) : null
          ) : active.department !== null && !locked ? (
            <p className="context-label">
              Context: {active.department.name}
              {activeAnalyticsNote !== null ? (
                <span className="context-note">{activeAnalyticsNote}</span>
              ) : null}
            </p>
          ) : null}

          <Chat
            key={`${activeKey}:${epoch}`}
            messages={messages}
            running={action === "send" || action === "regenerate"}
            disabled={blocked}
            draft={draft}
            status={status}
            alert={alert}
            focusRequest={focusRequest}
            transientActivity={streamActivity}
            regenPreview={regeneration}
            attachments={selectedAttachments}
            limits={limits}
            remainingThreadSlots={remainingThreadSlots}
            onDraftChange={handleDraftChange}
            onSend={(value) => void send(value)}
            onStop={() => abort.current?.abort()}
            onAttach={attachFiles}
            onRemoveAttachment={removeAttachment}
            onRegenerate={(turnId) => void regenerate(turnId)}
            onDeleteTurn={removeTurn}
          />
        </div>
      </main>

      {mode === "mobile" ? (
        <dialog
          ref={drawer}
          className="drawer-dialog"
          aria-label="Chats"
          onCancel={(event) => {
            event.preventDefault();
            closeDrawer();
          }}
          onClose={closeDrawer}
          onClick={(event) => {
            if (event.target === drawer.current) closeDrawer();
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") closeDrawer();
          }}
        >
          <Sidebar
            threads={threads}
            activeId={active?.id ?? null}
            busy={action !== null}
            username={user.username}
            managementUrl={urls.management}
            logoutUrl={urls.logout}
            csrfToken={csrfToken}
            onNewChat={() => {
              closeDrawer();
              void newChat();
            }}
            onSelect={(thread) => {
              closeDrawer();
              void selectThread(thread);
            }}
            onRename={rename}
            onDelete={remove}
            onCollapse={null}
            onClose={closeDrawer}
          />
        </dialog>
      ) : null}

      {departmentOptions.length > 1 ? (
        <dialog
          ref={chooser}
          className="dialog"
          aria-labelledby="context-dialog-title"
          onCancel={(event) => {
            event.preventDefault();
            setChooserOpen(false);
          }}
          onClose={() => setChooserOpen(false)}
        >
          <form
            className="dialog-form"
            onSubmit={(event) => {
              event.preventDefault();
              const chosen = chooserChoice;
              setChooserOpen(false);
              void startChat(chosen);
            }}
          >
            <h2 className="dialog-title" id="context-dialog-title">
              Start a new chat
            </h2>
            <fieldset className="field">
              <legend className="field-label">Chat context</legend>
              <label className="check-field">
                <input
                  type="radio"
                  name="chat-context"
                  checked={chooserChoice === null}
                  disabled={action !== null}
                  onChange={() => setChooserChoice(null)}
                />
                <span>General chat</span>
              </label>
              {departmentOptions.map((department) => (
                <label className="check-field" key={department.id}>
                  <input
                    type="radio"
                    name="chat-context"
                    checked={chooserChoice === department.id}
                    disabled={action !== null}
                    onChange={() => setChooserChoice(department.id)}
                  />
                  <span>{department.name}</span>
                </label>
              ))}
            </fieldset>
            {chooserAnalyticsNote !== null ? (
              <p className="hint">{chooserAnalyticsNote}</p>
            ) : null}
            <div className="dialog-actions">
              <button
                type="submit"
                className="button primary"
                disabled={action !== null}
              >
                Start chat
              </button>
              <button
                ref={chooserCancel}
                type="button"
                className="button secondary"
                disabled={action !== null}
                onClick={() => setChooserOpen(false)}
              >
                Cancel
              </button>
            </div>
          </form>
        </dialog>
      ) : null}
    </div>
  );
}
