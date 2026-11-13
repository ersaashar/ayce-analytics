import {
  AssistantRuntimeProvider,
  MessagePrimitive,
  type ThreadMessageLike,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import {
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { AttachmentRef, ChatLimits, Role, TurnDetail } from "./api";
import { ATTACHMENT_ACCEPT } from "./api";
import { DeleteTurnDialog } from "./components/chat/DeleteTurnDialog";
import { MessageMetrics } from "./components/chat/MessageMetrics";
import { ThoughtsPanel } from "./components/chat/ThoughtsPanel";
import type { ActivityState } from "./stream";

export type ChatMessage = {
  id: string;
  role: Role;
  content: string;
  turn: TurnDetail | null;
  /** Labels only: uploaded bytes never re-enter the browser transcript. */
  attachments: AttachmentRef[];
  pending: boolean;
};

/** A selected file held in memory by the workspace, never persisted raw. */
export type DraftAttachment = { id: string; file: File };

/** A streamed replacement answer, shown beneath the response it replaces. */
export type RegenerationPreview = {
  messageId: string;
  text: string;
  activity: ActivityState | null;
};

export type ChatProps = {
  messages: ChatMessage[];
  running: boolean;
  disabled: boolean;
  draft: string;
  status: string;
  alert: ReactNode;
  focusRequest: number;
  transientActivity: ActivityState | null;
  regenPreview: RegenerationPreview | null;
  attachments: readonly DraftAttachment[];
  limits: ChatLimits;
  remainingThreadSlots: number;
  onDraftChange: (value: string) => void;
  onSend: (rawDraft: string) => void;
  onStop?: () => void;
  onAttach: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
  onRegenerate: (turnId: string) => void;
  onDeleteTurn: (turnId: string) => Promise<string | null>;
};

/** Compact byte label shared by selected and already-sent file chips. */
export function formatFileSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

// Kept in step with the shell's max textarea height; taller drafts scroll.
const TEXTAREA_MAX_HEIGHT = 160;

function ArrowUpIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M12 19V5" />
      <path d="M5 12l7-7 7 7" />
    </svg>
  );
}

function SquareIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="currentColor"
    >
      <rect width="18" height="18" x="3" y="3" rx="3" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

function ArrowDownIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="12" y1="5" x2="12" y2="19" />
      <polyline points="19 12 12 19 5 12" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="butt"
      strokeLinejoin="miter"
    >
      <rect x="3" y="7" width="14" height="14" />
      <path d="M7 7V3h14v14h-4" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="butt"
      strokeLinejoin="miter"
    >
      <path d="M20 8a8 8 0 0 0-13.66-2" />
      <path d="M4 16a8 8 0 0 0 13.66 2" />
      <path d="M20 3v5h-5z" fill="currentColor" stroke="none" />
      <path d="M4 21v-5h5z" fill="currentColor" stroke="none" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="butt"
      strokeLinejoin="miter"
    >
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M6 6l1 14h10l1-14" />
      <path d="M9 10v7m6-7v7" />
    </svg>
  );
}

export function getMessageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (
          part &&
          typeof part === "object" &&
          "text" in part &&
          typeof part.text === "string"
        ) {
          return part.text;
        }
        return "";
      })
      .join("");
  }
  return "";
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall back to execCommand below
  }
  try {
    if (typeof document === "undefined") return false;
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const success = document.execCommand("copy");
    document.body.removeChild(textarea);
    return success;
  } catch {
    return false;
  }
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleCopy = useCallback(async () => {
    const success = await copyToClipboard(text);
    if (success) {
      setCopied(true);
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
      timeoutRef.current = setTimeout(() => setCopied(false), 2000);
    }
  }, [text]);

  useEffect(() => {
    return () => {
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
    };
  }, []);

  return (
    <button
      type="button"
      className="action-button"
      onClick={() => void handleCopy()}
      aria-label={copied ? "Copied" : "Copy"}
      title={copied ? "Copied" : "Copy"}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
      <span className="sr-only">{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

export function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleCopy = useCallback(async () => {
    const success = await copyToClipboard(code);
    if (success) {
      setCopied(true);
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
      timeoutRef.current = setTimeout(() => setCopied(false), 2000);
    }
  }, [code]);

  useEffect(() => {
    return () => {
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
    };
  }, []);

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-language">{language || "code"}</span>
        <button
          type="button"
          className="code-copy-button"
          onClick={() => void handleCopy()}
          aria-label={copied ? "Copied code" : "Copy code"}
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
          <span>{copied ? "Copied" : "Copy code"}</span>
        </button>
      </div>
      <pre className="code-pre">
        <code>{code}</code>
      </pre>
    </div>
  );
}

export type MarkdownBlock =
  | { id: string; type: "text"; content: string }
  | { id: string; type: "code"; language: string; content: string };

export function parseMarkdownBlocks(text: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const codeBlockRegex = /```([^\r\n]*)\r?\n([\s\S]*?)(?:```|$)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null = codeBlockRegex.exec(text);

  while (match !== null) {
    if (match.index > lastIndex) {
      const textContent = text.slice(lastIndex, match.index);
      if (textContent.length > 0) {
        blocks.push({
          id: `text-${lastIndex}`,
          type: "text",
          content: textContent,
        });
      }
    }
    blocks.push({
      id: `code-${match.index}`,
      type: "code",
      language: match[1]?.trim() || "text",
      content: match[2] ?? "",
    });
    lastIndex = match.index + match[0].length;
    match = codeBlockRegex.exec(text);
  }

  if (lastIndex < text.length) {
    const textContent = text.slice(lastIndex);
    if (textContent.length > 0) {
      blocks.push({
        id: `text-${lastIndex}`,
        type: "text",
        content: textContent,
      });
    }
  }

  return blocks.length > 0 ? blocks : [{ id: "text-0", type: "text", content: text }];
}

export function MarkdownRenderer({ content }: { content: string }) {
  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ href, children, node: _node, ...props }) {
            const isExternal =
              typeof href === "string" &&
              (href.startsWith("http://") || href.startsWith("https://"));
            return (
              <a
                href={href}
                target={isExternal ? "_blank" : undefined}
                rel={isExternal ? "noopener noreferrer" : undefined}
                {...props}
              >
                {children}
              </a>
            );
          },
          // Model-supplied remote images are never fetched; the alt text stays
          // readable so the answer keeps its meaning.
          img({ alt }) {
            return <span className="image-alt">{alt || "Image"}</span>;
          },
          table({ children, node: _node, ...props }) {
            return (
              // A labelled section is an implicit region, and the focus stop is
              // what lets a keyboard user scroll wide data (WCAG 2.1.1).
              // biome-ignore lint/a11y/noNoninteractiveTabindex: scrollable region needs keyboard focus
              <section className="table-scroll" aria-label="Table" tabIndex={0}>
                <table {...props}>{children}</table>
              </section>
            );
          },
          pre({ children }) {
            return <>{children}</>;
          },
          code({ className, children, node: _node, ...props }) {
            const match = /language-(\w+)/.exec(className || "");
            const isBlock = Boolean(match) || String(children).includes("\n");
            if (isBlock) {
              const language = match ? match[1] : "";
              const codeString = String(children).replace(/\n$/, "");
              return <CodeBlock code={codeString} language={language} />;
            }
            return (
              <code className="inline-code" {...props}>
                {children}
              </code>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

type ComposerFormProps = {
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  draft: string;
  disabled: boolean;
  running: boolean;
  status: string;
  attachments: readonly DraftAttachment[];
  limits: ChatLimits;
  remainingThreadSlots: number;
  onDraftChange: (value: string) => void;
  onSend: (value: string) => void;
  onStop?: () => void;
  onAttach: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
};

function ComposerForm({
  inputRef,
  draft,
  disabled,
  running,
  status,
  attachments,
  limits,
  remainingThreadSlots,
  onDraftChange,
  onSend,
  onStop,
  onAttach,
  onRemoveAttachment,
}: ComposerFormProps) {
  const composing = useRef(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

  // Both quotas disable only attachment selection; text still sends.
  const attachmentsLocked = disabled || running;
  const turnSlots = Math.max(0, limits.max_files_per_turn - attachments.length);
  const attachDisabled =
    attachmentsLocked || turnSlots === 0 || remainingThreadSlots === 0;

  function submit(event?: FormEvent<HTMLFormElement>) {
    if (event !== undefined) event.preventDefault();
    if (running) {
      onStop?.();
      return;
    }
    if (disabled || composing.current) return;
    const trimmed = draft.trim();
    if (trimmed.length === 0) return;
    onSend(trimmed);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      if (composing.current) return;
      event.preventDefault();
      submit();
    }
  }

  function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files === null ? [] : Array.from(event.target.files);
    // Reset so removing a file lets the same file be picked again.
    event.target.value = "";
    if (selected.length > 0) onAttach(selected);
  }

  const canSubmit = running || (!disabled && draft.trim().length > 0);

  return (
    <div className="composer-area">
      <form
        className="composer"
        onSubmit={(event) => submit(event)}
        data-running={running}
      >
        {attachments.length > 0 ? (
          <ul className="composer-file-list" aria-label="Selected files">
            {attachments.map((attachment) => (
              <li key={attachment.id} className="composer-file">
                <span className="composer-file-name" title={attachment.file.name}>
                  {attachment.file.name}
                </span>
                <span className="composer-file-size">
                  {formatFileSize(attachment.file.size)}
                </span>
                <button
                  type="button"
                  className="composer-file-remove"
                  aria-label={`Remove ${attachment.file.name}`}
                  title={`Remove ${attachment.file.name}`}
                  disabled={attachmentsLocked}
                  onClick={() => onRemoveAttachment(attachment.id)}
                >
                  <CloseIcon />
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        <div className="composer-input-row">
          <label className="sr-only" htmlFor="message">
            Message
          </label>
          <textarea
            ref={inputRef}
            id="message"
            name="message"
            rows={1}
            value={draft}
            disabled={disabled && !running}
            placeholder="Ask anything..."
            aria-describedby="composer-desc"
            aria-label="Message"
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
          />
        </div>

        <div className="composer-bottom-row">
          <div className="composer-actions">
            <input
              ref={fileInput}
              className="sr-only"
              type="file"
              multiple
              accept={ATTACHMENT_ACCEPT}
              disabled={attachDisabled}
              tabIndex={-1}
              aria-hidden="true"
              onChange={handleFiles}
            />
            <div className="composer-attach-wrapper">
              <button
                type="button"
                className="composer-attach"
                disabled={attachDisabled}
                aria-label="Attach a file to this chat"
                onClick={() => fileInput.current?.click()}
              >
                <PlusIcon />
                {attachments.length > 0 ? (
                  <span className="composer-attach-badge" aria-hidden="true">
                    {attachments.length}
                  </span>
                ) : null}
              </button>
              {!attachDisabled ? (
                <span className="composer-attach-tooltip" role="tooltip">
                  Attach a file to this chat
                </span>
              ) : null}
            </div>
          </div>
          <div className="composer-actions">
            <button
              type="submit"
              className={`composer-send ${running ? "stop" : ""}`}
              disabled={!canSubmit}
              aria-label={running ? "Stop generating" : "Send message"}
              title={running ? "Stop generating" : "Send message"}
            >
              {running ? <SquareIcon /> : <ArrowUpIcon />}
            </button>
          </div>
        </div>
      </form>

      <p className="sr-only" id="composer-desc">
        Enter sends. Shift+Enter adds a line break.
      </p>
      {status !== "" ? (
        <p className="sr-only" role="status" aria-live="polite">
          {status}
        </p>
      ) : null}
      <p className="composer-disclaimer">
        AYCE Analytics can make mistakes. Verify important info.
      </p>
    </div>
  );
}

export function Chat({
  messages,
  running,
  disabled,
  draft,
  status,
  alert,
  focusRequest,
  transientActivity,
  regenPreview,
  attachments,
  limits,
  remainingThreadSlots,
  onDraftChange,
  onSend,
  onStop,
  onAttach,
  onRemoveAttachment,
  onRegenerate,
  onDeleteTurn,
}: ChatProps) {
  const input = useRef<HTMLTextAreaElement | null>(null);
  const nearBottom = useRef(true);
  const [viewportElement, setViewportElement] = useState<HTMLDivElement | null>(null);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [confirmTurn, setConfirmTurn] = useState<string | null>(null);
  const [dialogError, setDialogError] = useState("");
  const [dialogPending, setDialogPending] = useState(false);
  const deleteTrigger = useRef<HTMLElement | null>(null);
  const hasMessages = messages.length > 0;
  const busy = disabled || running;

  // assistant-ui owns message identity, so runtime messages are joined to the
  // workspace records by id instead of assuming metadata survives conversion.
  const byId = useMemo(() => {
    const map = new Map<string, ChatMessage>();
    for (const message of messages) map.set(message.id, message);
    return map;
  }, [messages]);

  const closeDelete = useCallback(() => {
    setConfirmTurn(null);
    setDialogError("");
    const trigger = deleteTrigger.current;
    requestAnimationFrame(() => {
      if (trigger?.isConnected === true) trigger.focus();
      else input.current?.focus();
    });
  }, []);

  const openDelete = useCallback((turnId: string, trigger: HTMLElement) => {
    deleteTrigger.current = trigger;
    setDialogError("");
    setConfirmTurn(turnId);
  }, []);

  const confirmDelete = useCallback(async () => {
    if (confirmTurn === null || dialogPending) return;
    setDialogPending(true);
    const failure = await onDeleteTurn(confirmTurn);
    setDialogPending(false);
    if (failure === null) {
      setConfirmTurn(null);
      setDialogError("");
      requestAnimationFrame(() => input.current?.focus());
      return;
    }
    setDialogError(failure);
  }, [confirmTurn, dialogPending, onDeleteTurn]);

  useEffect(() => {
    if (focusRequest > 0 && !disabled) input.current?.focus();
  }, [focusRequest, disabled]);

  useLayoutEffect(() => {
    const element = input.current;
    if (element === null || element.value !== draft) return;
    element.style.height = "auto";
    const measured = Math.min(element.scrollHeight, TEXTAREA_MAX_HEIGHT);
    element.style.height = `${Math.max(measured, 24)}px`;
  }, [draft]);

  const scrollToLatest = useCallback(() => {
    if (viewportElement === null) return;
    viewportElement.scrollTop = viewportElement.scrollHeight;
    nearBottom.current = true;
    setShowScrollButton(false);
  }, [viewportElement]);

  useEffect(() => {
    if (viewportElement === null) return;

    const measure = () => {
      const distance =
        viewportElement.scrollHeight -
        viewportElement.scrollTop -
        viewportElement.clientHeight;
      const atBottom = distance <= 16;
      if (atBottom === nearBottom.current) return;
      nearBottom.current = atBottom;
      setShowScrollButton(!atBottom);
    };

    measure();
    viewportElement.addEventListener("scroll", measure, { passive: true });
    return () => viewportElement.removeEventListener("scroll", measure);
  }, [viewportElement]);

  useEffect(() => {
    if (viewportElement === null) return;

    const observer = new MutationObserver(() => {
      if (!nearBottom.current) return;
      viewportElement.scrollTop = viewportElement.scrollHeight;
    });
    observer.observe(viewportElement, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    return () => observer.disconnect();
  }, [viewportElement]);

  const convertMessage = useCallback(
    (message: ChatMessage): ThreadMessageLike => ({
      id: message.id,
      role: message.role,
      content: message.content,
      status: message.pending
        ? { type: "running" }
        : message.role === "assistant"
          ? { type: "complete", reason: "stop" }
          : undefined,
    }),
    [],
  );

  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages,
    convertMessage,
    isRunning: running,
    onNew: async (message) => {
      const text = message.content
        .map((part) => (part.type === "text" ? part.text : ""))
        .join("");
      onSend(text);
    },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root
        className="thread"
        data-empty={hasMessages ? "false" : "true"}
      >
        {hasMessages ? (
          <>
            <ThreadPrimitive.Viewport
              ref={setViewportElement}
              className="thread-viewport"
              aria-label="Conversation"
              autoScroll={false}
              scrollToBottomOnRunStart={false}
            >
              <div className="messages-list">
                <ThreadPrimitive.Messages>
                  {({ message }) => {
                    const id = String(message.id);
                    const record = byId.get(id);
                    const text = getMessageText(message.content);
                    const pending = record?.pending === true;
                    const turn = record?.turn ?? null;
                    const isStreaming = pending && running;
                    const activity = isStreaming
                      ? transientActivity
                      : (turn?.activity ?? null);
                    return (
                      <MessagePrimitive.Root
                        className="message"
                        data-role={message.role}
                        data-message-id={id}
                      >
                        <p className="sr-only">
                          {message.role === "user" ? "You" : "Assistant"}
                        </p>
                        {message.role === "assistant" ? (
                          <ThoughtsPanel
                            activity={activity}
                            running={isStreaming}
                            durationMs={turn?.response?.duration_ms}
                          />
                        ) : null}
                        <div className="content">
                          {message.role === "assistant" ? (
                            <MarkdownRenderer content={text} />
                          ) : (
                            <>
                              <p className="text">{text}</p>
                              {record !== undefined && record.attachments.length > 0 ? (
                                <ul
                                  className="message-files"
                                  aria-label="Attached files"
                                >
                                  {record.attachments.map((attachment) => (
                                    <li key={attachment.id} className="message-file">
                                      <span
                                        className="message-file-name"
                                        title={attachment.name}
                                      >
                                        {attachment.name}
                                      </span>
                                      <span className="message-file-size">
                                        {formatFileSize(attachment.size)}
                                      </span>
                                    </li>
                                  ))}
                                </ul>
                              ) : null}
                            </>
                          )}
                        </div>
                        {message.role === "assistant" ? (
                          <>
                            {turn?.response?.output_limit_reached === true ? (
                              <p className="turn-warning">
                                Output limit reached; the answer may be incomplete.
                              </p>
                            ) : null}
                            {pending ? null : (
                              <div className="message-actions-row">
                                <div className="message-actions">
                                  <CopyButton text={text} />
                                  {turn?.can_regenerate === true ? (
                                    <button
                                      type="button"
                                      className="action-button"
                                      disabled={busy}
                                      aria-label="Regenerate"
                                      title="Regenerate"
                                      onClick={() => onRegenerate(turn.turn_id)}
                                    >
                                      <RefreshIcon />
                                      <span className="sr-only">Regenerate</span>
                                    </button>
                                  ) : null}
                                  {turn?.can_delete === true ? (
                                    <button
                                      type="button"
                                      className="action-button danger"
                                      disabled={busy}
                                      aria-label="Delete"
                                      title="Delete"
                                      onClick={(event) =>
                                        openDelete(turn.turn_id, event.currentTarget)
                                      }
                                    >
                                      <TrashIcon />
                                      <span className="sr-only">Delete</span>
                                    </button>
                                  ) : null}
                                </div>
                                <MessageMetrics response={turn?.response ?? null} />
                              </div>
                            )}
                            {regenPreview !== null && regenPreview.messageId === id ? (
                              <div className="regen-preview">
                                <p className="regen-preview-label">
                                  Regenerating response
                                </p>
                                <ThoughtsPanel
                                  activity={regenPreview.activity}
                                  running={true}
                                />
                                {regenPreview.text.length > 0 ? (
                                  <MarkdownRenderer content={regenPreview.text} />
                                ) : null}
                              </div>
                            ) : null}
                          </>
                        ) : null}
                      </MessagePrimitive.Root>
                    );
                  }}
                </ThreadPrimitive.Messages>
              </div>
            </ThreadPrimitive.Viewport>

            <div className="thread-footer">
              {showScrollButton ? (
                <button
                  type="button"
                  className="scroll-latest"
                  aria-label="Scroll to latest message"
                  title="Scroll to latest message"
                  onClick={scrollToLatest}
                >
                  <ArrowDownIcon />
                </button>
              ) : null}
              {alert}
              <ComposerForm
                inputRef={input}
                draft={draft}
                disabled={disabled}
                running={running}
                status={status}
                attachments={attachments}
                limits={limits}
                remainingThreadSlots={remainingThreadSlots}
                onDraftChange={onDraftChange}
                onSend={onSend}
                onStop={onStop}
                onAttach={onAttach}
                onRemoveAttachment={onRemoveAttachment}
              />
            </div>
            <DeleteTurnDialog
              open={confirmTurn !== null}
              busy={dialogPending}
              error={dialogError}
              onCancel={closeDelete}
              onConfirm={() => void confirmDelete()}
            />
          </>
        ) : (
          <div className="thread-empty-container">
            <div className="thread-empty">
              <h2 className="empty-heading">Where should we begin?</h2>
              {alert}
              <ComposerForm
                inputRef={input}
                draft={draft}
                disabled={disabled}
                running={running}
                status={status}
                attachments={attachments}
                limits={limits}
                remainingThreadSlots={remainingThreadSlots}
                onDraftChange={onDraftChange}
                onSend={onSend}
                onStop={onStop}
                onAttach={onAttach}
                onRemoveAttachment={onRemoveAttachment}
              />
            </div>
          </div>
        )}
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
