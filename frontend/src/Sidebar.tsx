import {
  type FormEvent,
  type MouseEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { ThreadSummary } from "./api";

export type SidebarProps = {
  threads: ThreadSummary[];
  activeId: string | null;
  busy: boolean;
  username: string;
  managementUrl: string | null;
  logoutUrl: string;
  csrfToken: string;
  onNewChat: () => void;
  onSelect: (thread: ThreadSummary) => void;
  onRename: (threadId: string, title: string) => Promise<string | null>;
  onDelete: (threadId: string) => Promise<string | null>;
  onCollapse: (() => void) | null;
  onClose: (() => void) | null;
};

type DialogMode = "edit" | "delete";

const MAX_TITLE_LENGTH = 80;

type IconProps = { children: ReactNode };

function Icon({ children }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

export function PlusIcon() {
  return (
    <Icon>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </Icon>
  );
}

export function PanelLeftIcon() {
  return (
    <Icon>
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <path d="M9 3v18" />
    </Icon>
  );
}

export function SearchIcon() {
  return (
    <Icon>
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.3-4.3" />
    </Icon>
  );
}

export function CloseIcon() {
  return (
    <Icon>
      <path d="M6 6l12 12" />
      <path d="M18 6L6 18" />
    </Icon>
  );
}

export function MoreIcon() {
  return (
    <Icon>
      <path d="M5 12h.01" strokeWidth="3" />
      <path d="M12 12h.01" strokeWidth="3" />
      <path d="M19 12h.01" strokeWidth="3" />
    </Icon>
  );
}

export function Sidebar({
  threads,
  activeId,
  busy,
  username,
  managementUrl,
  logoutUrl,
  csrfToken,
  onNewChat,
  onSelect,
  onRename,
  onDelete,
  onCollapse,
  onClose,
}: SidebarProps) {
  const [search, setSearch] = useState("");
  const [isSearching, setIsSearching] = useState(false);
  const [target, setTarget] = useState<ThreadSummary | null>(null);
  const [mode, setMode] = useState<DialogMode>("edit");
  const [titleValue, setTitleValue] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [pending, setPending] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  const dialog = useRef<HTMLDialogElement | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const newChat = useRef<HTMLButtonElement | null>(null);
  const searchInput = useRef<HTMLInputElement | null>(null);
  const footerRef = useRef<HTMLDivElement | null>(null);
  const profileTriggerRef = useRef<HTMLButtonElement | null>(null);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (needle.length === 0) return threads;
    return threads.filter((thread) => thread.title.toLowerCase().includes(needle));
  }, [search, threads]);

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (target !== null && !element.open) element.showModal();
    if (target === null && element.open) element.close();
  }, [target]);

  // Handle outside clicks and Escape key to close the profile menu
  useEffect(() => {
    if (!menuOpen) return;

    function handleOutsideClick(event: globalThis.MouseEvent) {
      if (footerRef.current && !footerRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMenuOpen(false);
        profileTriggerRef.current?.focus();
      }
    }

    document.addEventListener("mousedown", handleOutsideClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  function restoreFocus() {
    const source = trigger.current;
    requestAnimationFrame(() => {
      const next = source?.isConnected === true ? source : newChat.current;
      next?.focus();
    });
  }

  function openDialog(thread: ThreadSummary, next: DialogMode) {
    trigger.current = document.activeElement as HTMLElement | null;
    setTarget(thread);
    setMode(next);
    setTitleValue(thread.title);
    setDialogError("");
  }

  function closeDialog() {
    setTarget(null);
    setDialogError("");
    setPending(false);
    restoreFocus();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (target === null || pending || busy) return;

    if (mode === "delete") {
      setPending(true);
      const failure = await onDelete(target.id);
      setPending(false);
      if (failure === null) closeDialog();
      else setDialogError(failure);
      return;
    }

    if (Array.from(titleValue).length > MAX_TITLE_LENGTH) {
      setDialogError("Chat names are at most 80 characters.");
      return;
    }
    if (titleValue.trim().length === 0) {
      setDialogError("Enter a chat name.");
      return;
    }

    setPending(true);
    const failure = await onRename(target.id, titleValue);
    setPending(false);
    if (failure === null) closeDialog();
    else setDialogError(failure);
  }

  function followThread(event: MouseEvent<HTMLAnchorElement>, thread: ThreadSummary) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (event.button !== 0) return;
    event.preventDefault();
    onSelect(thread);
  }

  function startSearch() {
    setIsSearching(true);
    requestAnimationFrame(() => {
      searchInput.current?.focus();
    });
  }

  function stopSearch() {
    setSearch("");
    setIsSearching(false);
  }

  const locked = busy || pending;

  return (
    <div className="sidebar-inner">
      <div className="sidebar-header">
        <p className="sidebar-brand">AYCE Analytics V2</p>
        <div className="sidebar-header-actions">
          {onCollapse !== null ? (
            <button
              type="button"
              className="icon-button"
              onClick={onCollapse}
              aria-label="Collapse sidebar"
              title="Collapse sidebar"
            >
              <PanelLeftIcon />
            </button>
          ) : null}
          {onClose !== null ? (
            <button
              type="button"
              className="icon-button"
              onClick={onClose}
              aria-label="Close sidebar"
              title="Close sidebar"
            >
              <CloseIcon />
            </button>
          ) : null}
        </div>
      </div>

      <div className="sidebar-actions-nav">
        <button
          type="button"
          className="sidebar-new-chat-btn"
          ref={newChat}
          onClick={onNewChat}
          disabled={locked}
        >
          <PlusIcon />
          <span>New chat</span>
        </button>

        {isSearching || search.length > 0 ? (
          <div className="sidebar-search-box active">
            <span className="sidebar-search-icon" aria-hidden="true">
              <SearchIcon />
            </span>
            <input
              ref={searchInput}
              id="chat-search"
              type="search"
              value={search}
              placeholder="Search chats..."
              autoComplete="off"
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  stopSearch();
                }
              }}
            />
            <button
              type="button"
              className="sidebar-search-clear"
              aria-label="Clear search"
              onClick={stopSearch}
            >
              <CloseIcon />
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="sidebar-nav-item"
            onClick={startSearch}
            disabled={locked}
          >
            <SearchIcon />
            <span>Search chats</span>
          </button>
        )}
      </div>

      <div className="sidebar-threads-container">
        <h2 className="sidebar-heading" id="recents-heading">
          Recents
        </h2>

        {threads.length === 0 ? (
          <p className="sidebar-empty">
            No chats yet. Start a new chat and it will appear here.
          </p>
        ) : visible.length === 0 ? (
          <p className="sidebar-empty">No matching chats</p>
        ) : (
          <ul className="sidebar-list" aria-labelledby="recents-heading">
            {visible.map((thread) => (
              <li
                className="thread-row"
                key={thread.id}
                data-active={thread.id === activeId}
              >
                <a
                  className="thread-link"
                  href={thread.page_url}
                  aria-current={thread.id === activeId ? "page" : undefined}
                  onClick={(event) => followThread(event, thread)}
                >
                  <span className="thread-title">{thread.title}</span>
                </a>
                <button
                  type="button"
                  className="thread-action"
                  aria-label={`Chat actions: ${thread.title}`}
                  title={`Chat actions: ${thread.title}`}
                  disabled={locked}
                  onClick={() =>
                    openDialog(
                      thread,
                      thread.access === "available" ? "edit" : "delete",
                    )
                  }
                >
                  <MoreIcon />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="sidebar-footer" ref={footerRef}>
        {menuOpen ? (
          <div className="profile-menu" role="menu">
            <div className="profile-menu-header">
              <span className="profile-menu-name">{username}</span>
            </div>
            {managementUrl !== null ? (
              <a className="profile-menu-item" href={managementUrl} role="menuitem">
                Administration
              </a>
            ) : null}
            <div className="profile-menu-divider" />
            <form method="post" action={logoutUrl} className="profile-menu-form">
              <input type="hidden" name="csrfmiddlewaretoken" value={csrfToken} />
              <button
                type="submit"
                className="profile-menu-item profile-menu-item-danger"
                role="menuitem"
                disabled={locked}
              >
                Sign out
              </button>
            </form>
          </div>
        ) : null}

        <button
          type="button"
          ref={profileTriggerRef}
          className="sidebar-profile-trigger"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((prev) => !prev)}
          disabled={locked}
        >
          <span className="sidebar-avatar" aria-hidden="true">
            {username.charAt(0).toUpperCase() || "U"}
          </span>
          <span className="sidebar-user" title={username}>
            {username}
          </span>
          <span className="sidebar-profile-dots" aria-hidden="true">
            <MoreIcon />
          </span>
        </button>
      </div>

      <dialog
        ref={dialog}
        className="dialog"
        aria-labelledby="chat-dialog-title"
        onCancel={(event) => {
          event.preventDefault();
          closeDialog();
        }}
        onClose={() => {
          setTarget(null);
          restoreFocus();
        }}
      >
        {target !== null ? (
          <form className="dialog-form" onSubmit={(event) => void submit(event)}>
            <h2 className="dialog-title" id="chat-dialog-title">
              {mode === "delete" ? "Delete chat" : "Chat options"}
            </h2>

            {mode === "delete" ? (
              <p className="dialog-text">
                Delete “{target.title}”? Its messages are removed permanently.
              </p>
            ) : (
              <label className="field">
                <span className="field-label">Chat name</span>
                <input
                  type="text"
                  value={titleValue}
                  autoFocus
                  disabled={locked}
                  onChange={(event) => setTitleValue(event.target.value)}
                />
              </label>
            )}

            {dialogError !== "" ? (
              <p className="form-error" role="alert">
                {dialogError}
              </p>
            ) : null}

            <div className="dialog-actions">
              {mode === "delete" || target.access === "available" ? (
                <button
                  type="submit"
                  className={mode === "delete" ? "button danger" : "button primary"}
                  disabled={locked}
                >
                  {mode === "delete" ? "Delete chat" : "Save name"}
                </button>
              ) : null}
              <button
                type="button"
                className="button secondary"
                disabled={locked}
                onClick={closeDialog}
              >
                Cancel
              </button>
              {mode === "edit" && target.access === "available" ? (
                <button
                  type="button"
                  className="button danger"
                  disabled={locked}
                  onClick={() => {
                    setMode("delete");
                    setDialogError("");
                  }}
                >
                  Delete chat
                </button>
              ) : null}
            </div>
          </form>
        ) : null}
      </dialog>
    </div>
  );
}
