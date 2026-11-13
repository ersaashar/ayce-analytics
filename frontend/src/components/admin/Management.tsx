/**
 * Single administration surface for accounts, departments and datasets.
 *
 * Each save carries the server's current revision, so a stale write prompts a
 * reload instead of overwriting newer state.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type Capabilities,
  type CapabilityName,
  type ChatApiError,
  requestJson,
  toChatError,
} from "../../api";
import {
  type AuditRecord,
  type Candidate,
  type CatalogStatus,
  createManagedDepartment,
  createManagedUser,
  type DatasetAvailability,
  type DatasetRecord,
  type Department,
  type DepartmentDatasetRecord,
  fetchAudit,
  fetchCatalogStatus,
  fetchDatasets,
  fetchDepartment,
  fetchDepartmentDatasets,
  fetchDepartmentMembers,
  fetchDepartments,
  fetchManagedUsers,
  fetchMemberDatasets,
  fetchRoles,
  fetchUserMemberships,
  type ListPage,
  type ManagedUser,
  type ManagementBootstrap,
  type ManagementSection,
  type ManagementUrls,
  type MemberDatasetRecord,
  patchDataset,
  patchDepartment,
  patchManagedUser,
  putDepartmentDatasets,
  putDepartmentMembers,
  putMemberDatasets,
  putUserMemberships,
  type RoleDefinition,
  type RosterMember,
  refreshCatalog,
  resetManagedPassword,
  searchCandidates,
  setManagedRole,
} from "../../management-api";

type SectionProps = {
  urls: ManagementUrls;
  capabilities: Capabilities;
  onState: (revision: number, epoch: number) => void;
  onNotice: (message: string) => void;
  onFailure: (caught: unknown) => string;
};

type SectionState = { revision: number; epoch: number };

const SECTION_LABELS: Record<ManagementSection, string> = {
  users: "Users",
  departments: "Departments",
  datasets: "Datasets",
  roles: "Roles",
  audit: "Audit",
};

function visibleSections(capabilities: Capabilities): ManagementSection[] {
  const sections: ManagementSection[] = [];
  if (capabilities.manage_users) sections.push("users");
  if (capabilities.manage_department_staff || capabilities.manage_departments) {
    sections.push("departments");
  }
  if (capabilities.manage_catalog || capabilities.manage_department_staff) {
    sections.push("datasets");
  }
  if (capabilities.manage_roles) sections.push("roles");
  if (capabilities.view_audit) sections.push("audit");
  return sections;
}

const CAPABILITY_LABELS: Record<CapabilityName, string> = {
  manage_users: "Manage accounts",
  manage_roles: "Manage roles",
  manage_departments: "Manage departments",
  manage_department_staff: "Manage department staff",
  manage_catalog: "Manage catalog",
  manage_grants: "Manage grants",
  view_audit: "View audit log",
};

function describeFailure(caught: unknown): ChatApiError {
  const failure = toChatError(caught);
  return { code: failure.code, message: failure.message } as ChatApiError;
}

function isDenied(caught: unknown): boolean {
  const code = toChatError(caught).code;
  return code === "permission_denied" || code === "authentication_required";
}
function availabilityDescription(availability: DatasetAvailability): string {
  switch (availability) {
    case "available":
      return "Available";
    case "disabled":
      return "Disabled locally";
    case "missing":
      return "Missing from the current Wren catalog";
    case "needs_review":
      return "Needs assignment review";
    case "different_source":
      return "Belongs to a different Wren source";
  }
}

// One visible error surface at a time: a dialog reports its own failure, and
// the page-level alert is hidden while that dialog is open.
const DialogOpenContext = createContext<((open: boolean) => void) | null>(null);

function Dialog({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const element = useRef<HTMLDialogElement | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const setDialogOpen = useContext(DialogOpenContext);

  useEffect(() => {
    setDialogOpen?.(open);
  }, [open, setDialogOpen]);

  useEffect(() => {
    const node = element.current;
    if (node === null) return;
    if (open && !node.open) {
      trigger.current = document.activeElement as HTMLElement | null;
      node.showModal();
      // Cancel starts focused so Escape/backdrop and Cancel agree.
      requestAnimationFrame(() => {
        node.querySelector<HTMLElement>(".dialog-cancel")?.focus();
      });
    }
    if (!open && node.open) node.close();
  }, [open]);

  useEffect(() => {
    const node = element.current;
    return () => {
      setDialogOpen?.(false);
      if (node?.open === true) node.close();
    };
  }, [setDialogOpen]);

  return (
    <dialog
      ref={element}
      className="dialog"
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        onClose();
        requestAnimationFrame(() => {
          const source = trigger.current;
          if (source?.isConnected === true) source.focus();
        });
      }}
      onMouseDown={(event) => {
        if (event.target === element.current) onClose();
      }}
    >
      {open ? <div className="dialog-form">{children}</div> : null}
    </dialog>
  );
}

function Pager({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="management-pager">
      <button
        type="button"
        className="button secondary"
        disabled={page <= 1}
        onClick={() => onPage(page - 1)}
      >
        Previous
      </button>
      <span className="hint">
        Page {page} of {pages} · {total} total
      </span>
      <button
        type="button"
        className="button secondary"
        disabled={page >= pages}
        onClick={() => onPage(page + 1)}
      >
        Next
      </button>
    </div>
  );
}

function StateLine({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="management-state">
      <h3 className="management-subtitle">{title}</h3>
      {children}
    </div>
  );
}

type UserDialog =
  | { kind: "create" }
  | { kind: "edit"; user: ManagedUser }
  | { kind: "password"; user: ManagedUser }
  | { kind: "roles"; user: ManagedUser }
  | { kind: "memberships"; user: ManagedUser }
  | null;

function UsersSection({
  urls,
  capabilities,
  state,
  onState,
  onNotice,
  onFailure,
}: SectionProps & { state: SectionState }) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<ListPage<ManagedUser> | null>(null);
  const [loading, setLoading] = useState(false);
  const [dialog, setDialog] = useState<UserDialog>(null);

  const load = useCallback(
    async (signal: AbortSignal) => {
      setLoading(true);
      try {
        const pageResult = await fetchManagedUsers(
          urls,
          { q: query, page, page_size: 25 },
          signal,
        );
        setResult(pageResult);
        onState(pageResult.revision, pageResult.epoch);
      } catch (caught) {
        onFailure(caught);
      } finally {
        setLoading(false);
      }
    },
    [onFailure, onState, page, query, urls],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (!capabilities.manage_users) {
    return <p className="hint">You do not have account administration access.</p>;
  }

  const close = () => setDialog(null);

  return (
    <section aria-label="Users">
      <div className="management-toolbar">
        <label className="field management-search">
          <span className="field-label">Search accounts</span>
          <input
            type="search"
            value={query}
            placeholder="Username or name"
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
          />
        </label>
        <button
          type="button"
          className="button primary"
          onClick={() => setDialog({ kind: "create" })}
        >
          New account
        </button>
      </div>

      {loading ? <p className="hint">Loading accounts…</p> : null}
      {result === null ? (
        <p className="hint">No accounts loaded.</p>
      ) : result.items.length === 0 ? (
        <p className="hint">No accounts match this search.</p>
      ) : (
        <div className="management-table-wrap">
          <table className="management-table">
            <thead>
              <tr>
                <th scope="col">Account</th>
                <th scope="col">Status</th>
                <th scope="col">Roles</th>
                <th scope="col">Departments</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {result.items.map((user) => (
                <tr key={user.id}>
                  <td data-label="Account">
                    <span className="management-primary">{user.username}</span>
                    <span className="hint">
                      {[user.first_name, user.last_name].filter(Boolean).join(" ")}
                      {user.email ? ` · ${user.email}` : ""}
                    </span>
                  </td>
                  <td data-label="Status">
                    {user.is_active ? "Active" : "Deactivated"}
                  </td>
                  <td data-label="Roles">{user.roles.join(", ") || "—"}</td>
                  <td data-label="Departments">
                    {user.memberships.length === 0
                      ? "—"
                      : user.memberships
                          .map(
                            (membership) =>
                              `${membership.department.name} (${membership.membership_role})`,
                          )
                          .join(", ")}
                  </td>
                  <td data-label="Actions" className="management-actions">
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() => setDialog({ kind: "edit", user })}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() => setDialog({ kind: "roles", user })}
                    >
                      Role
                    </button>
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() => setDialog({ kind: "memberships", user })}
                    >
                      Departments
                    </button>
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() => setDialog({ kind: "password", user })}
                    >
                      Password
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result !== null ? (
        <Pager
          page={result.page}
          pageSize={result.page_size}
          total={result.total}
          onPage={setPage}
        />
      ) : null}

      {dialog?.kind === "create" ? (
        <UserCreateDialog
          urls={urls}
          revision={state.revision}
          onClose={close}
          onSaved={(revision, epoch) => {
            onState(revision, epoch);
            onNotice("Account created.");
            close();
            setResult(null);
            void load(new AbortController().signal);
          }}
          onFailure={onFailure}
        />
      ) : null}
      {dialog?.kind === "edit" ? (
        <UserEditDialog
          urls={urls}
          user={dialog.user}
          revision={state.revision}
          onClose={close}
          onSaved={(revision, epoch) => {
            onState(revision, epoch);
            onNotice("Account updated.");
            close();
            void load(new AbortController().signal);
          }}
          onFailure={onFailure}
        />
      ) : null}
      {dialog?.kind === "password" ? (
        <PasswordDialog
          urls={urls}
          user={dialog.user}
          revision={state.revision}
          onClose={close}
          onSaved={(revision, epoch) => {
            onState(revision, epoch);
            onNotice("Password changed. Existing sessions must sign in again.");
            close();
          }}
          onFailure={onFailure}
        />
      ) : null}
      {dialog?.kind === "roles" ? (
        <RoleDialog
          urls={urls}
          user={dialog.user}
          revision={state.revision}
          onClose={close}
          onSaved={(revision, epoch) => {
            onState(revision, epoch);
            onNotice("Role updated.");
            close();
            void load(new AbortController().signal);
          }}
          onFailure={onFailure}
        />
      ) : null}
      {dialog?.kind === "memberships" ? (
        <UserMembershipsDialog
          urls={urls}
          user={dialog.user}
          revision={state.revision}
          onClose={close}
          onSaved={(revision, epoch) => {
            onState(revision, epoch);
            onNotice("Department memberships updated.");
            close();
            void load(new AbortController().signal);
          }}
          onFailure={onFailure}
        />
      ) : null}
    </section>
  );
}

function UserCreateDialog({
  urls,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  revision: number;
  onClose: () => void;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [fields, setFields] = useState({
    username: "",
    first_name: "",
    last_name: "",
    email: "",
    password: "",
  });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <Dialog open title="New account" onClose={onClose}>
      <h2 className="dialog-title">New account</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (pending) return;
          setPending(true);
          setError("");
          void createManagedUser(urls, fields, revision)
            .then((saved) => onSaved(saved.revision, saved.epoch))
            .catch((caught) => setError(onFailure(caught)))
            .finally(() => setPending(false));
        }}
      >
        <label className="field">
          <span className="field-label">Username</span>
          <input
            type="text"
            required
            value={fields.username}
            onChange={(event) =>
              setFields((previous) => ({ ...previous, username: event.target.value }))
            }
          />
        </label>
        <label className="field">
          <span className="field-label">First name</span>
          <input
            type="text"
            value={fields.first_name}
            onChange={(event) =>
              setFields((previous) => ({
                ...previous,
                first_name: event.target.value,
              }))
            }
          />
        </label>
        <label className="field">
          <span className="field-label">Last name</span>
          <input
            type="text"
            value={fields.last_name}
            onChange={(event) =>
              setFields((previous) => ({ ...previous, last_name: event.target.value }))
            }
          />
        </label>
        <label className="field">
          <span className="field-label">Email</span>
          <input
            type="email"
            value={fields.email}
            onChange={(event) =>
              setFields((previous) => ({ ...previous, email: event.target.value }))
            }
          />
        </label>
        <label className="field">
          <span className="field-label">Initial password</span>
          <input
            type="password"
            required
            autoComplete="new-password"
            value={fields.password}
            onChange={(event) =>
              setFields((previous) => ({ ...previous, password: event.target.value }))
            }
          />
        </label>
        {error !== "" ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="button primary" disabled={pending}>
            Create account
          </button>
          <button
            type="button"
            className="button secondary dialog-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function UserEditDialog({
  urls,
  user,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  user: ManagedUser;
  revision: number;
  onClose: () => void;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [fields, setFields] = useState({
    first_name: user.first_name,
    last_name: user.last_name,
    email: user.email,
    is_active: user.is_active,
  });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <Dialog open title={`Edit ${user.username}`} onClose={onClose}>
      <h2 className="dialog-title">Edit {user.username}</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (pending) return;
          setPending(true);
          setError("");
          void patchManagedUser(urls, user.id, fields, revision)
            .then((saved) => onSaved(saved.revision, saved.epoch))
            .catch((caught) => setError(onFailure(caught)))
            .finally(() => setPending(false));
        }}
      >
        <label className="field">
          <span className="field-label">First name</span>
          <input
            type="text"
            value={fields.first_name}
            onChange={(event) =>
              setFields((previous) => ({
                ...previous,
                first_name: event.target.value,
              }))
            }
          />
        </label>
        <label className="field">
          <span className="field-label">Last name</span>
          <input
            type="text"
            value={fields.last_name}
            onChange={(event) =>
              setFields((previous) => ({ ...previous, last_name: event.target.value }))
            }
          />
        </label>
        <label className="field">
          <span className="field-label">Email</span>
          <input
            type="email"
            value={fields.email}
            onChange={(event) =>
              setFields((previous) => ({ ...previous, email: event.target.value }))
            }
          />
        </label>
        <label className="check-field">
          <input
            type="checkbox"
            checked={fields.is_active}
            onChange={(event) =>
              setFields((previous) => ({
                ...previous,
                is_active: event.target.checked,
              }))
            }
          />
          <span>Account is active</span>
        </label>
        {error !== "" ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="button primary" disabled={pending}>
            Save account
          </button>
          <button
            type="button"
            className="button secondary dialog-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function PasswordDialog({
  urls,
  user,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  user: ManagedUser;
  revision: number;
  onClose: () => void;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <Dialog open title={`Reset password for ${user.username}`} onClose={onClose}>
      <h2 className="dialog-title">Reset password</h2>
      <p className="dialog-text">
        Set a new password for {user.username}. Existing sessions for that account stop
        working.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (pending) return;
          if (newPassword !== confirm) {
            setError("The two new password fields did not match.");
            return;
          }
          setPending(true);
          setError("");
          void resetManagedPassword(
            urls,
            user.id,
            { current_password: currentPassword, new_password: newPassword },
            revision,
          )
            .then((saved) => onSaved(saved.revision, saved.epoch))
            .catch((caught) => setError(onFailure(caught)))
            .finally(() => {
              setCurrentPassword("");
              setNewPassword("");
              setConfirm("");
              setPending(false);
            });
        }}
      >
        <label className="field">
          <span className="field-label">Your current password</span>
          <input
            type="password"
            required
            autoComplete="current-password"
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">New password</span>
          <input
            type="password"
            required
            autoComplete="new-password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">Confirm new password</span>
          <input
            type="password"
            required
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
        </label>
        {error !== "" ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="button primary" disabled={pending}>
            Reset password
          </button>
          <button
            type="button"
            className="button secondary dialog-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function RoleDialog({
  urls,
  user,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  user: ManagedUser;
  revision: number;
  onClose: () => void;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [role, setRole] = useState<"staff" | "superadmin">(
    user.roles.includes("AYCE Superadmin") ? "superadmin" : "staff",
  );
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <Dialog open title={`Role for ${user.username}`} onClose={onClose}>
      <h2 className="dialog-title">Role for {user.username}</h2>
      <p className="dialog-text">
        A role alone grants no datasets. Department membership and its explicit dataset
        subset decide what data an account can see.
      </p>
      <fieldset className="field">
        <legend className="field-label">Base role</legend>
        <label className="check-field">
          <input
            type="radio"
            name="role"
            checked={role === "staff"}
            onChange={() => setRole("staff")}
          />
          <span>Staff</span>
        </label>
        <label className="check-field">
          <input
            type="radio"
            name="role"
            checked={role === "superadmin"}
            onChange={() => setRole("superadmin")}
          />
          <span>Superadmin</span>
        </label>
      </fieldset>
      {error !== "" ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="dialog-actions">
        <button
          type="button"
          className="button primary"
          disabled={pending}
          onClick={() => {
            if (pending) return;
            setPending(true);
            setError("");
            void setManagedRole(urls, user.id, role, revision)
              .then((saved) => onSaved(saved.revision, saved.epoch))
              .catch((caught) => setError(onFailure(caught)))
              .finally(() => setPending(false));
          }}
        >
          Save role
        </button>
        <button
          type="button"
          className="button secondary dialog-cancel"
          onClick={onClose}
        >
          Cancel
        </button>
      </div>
    </Dialog>
  );
}

type MembershipRow = {
  department: Department;
  role: "staff" | "lead";
  active: boolean;
  datasetIds: string[];
  granted: DepartmentDatasetRecord[] | null;
};

function UserMembershipsDialog({
  urls,
  user,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  user: ManagedUser;
  revision: number;
  onClose: () => void;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [rows, setRows] = useState<MembershipRow[] | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const [departments, detail] = await Promise.all([
          fetchDepartments(urls, { page: 1, page_size: 100 }, controller.signal),
          fetchUserMemberships(urls, user.id, controller.signal),
        ]);
        const stored = new Map(
          (detail.value.memberships_detail ?? []).map((row) => [
            row.department.id,
            row,
          ]),
        );
        setRows(
          departments.items.map((department) => {
            const membership = stored.get(department.id);
            return {
              department,
              role: membership?.membership_role ?? "staff",
              active: membership?.is_active ?? false,
              datasetIds: membership?.dataset_ids ?? [],
              granted: null,
            };
          }),
        );
      } catch (caught) {
        setError(onFailure(caught));
      }
    })();
    return () => controller.abort();
  }, [onFailure, urls, user.id]);

  async function loadGrants(departmentId: string) {
    try {
      const granted = await fetchDepartmentDatasets(urls, departmentId);
      setRows((previous) =>
        previous === null
          ? previous
          : previous.map((row) =>
              row.department.id === departmentId
                ? { ...row, granted: granted.datasets }
                : row,
            ),
      );
    } catch (caught) {
      setError(onFailure(caught));
    }
  }

  return (
    <Dialog open title={`Departments for ${user.username}`} onClose={onClose}>
      <h2 className="dialog-title">Departments for {user.username}</h2>
      <p className="dialog-text">
        Saving replaces this account's complete membership set. Every subset starts
        empty and never inherits the department's other datasets.
      </p>
      {rows === null ? (
        <p className="hint">Loading departments…</p>
      ) : (
        <div className="management-rows">
          {rows.map((row, index) => (
            <fieldset className="management-row" key={row.department.id}>
              <legend className="field-label">
                {row.department.name} ({row.department.code})
              </legend>
              <label className="check-field">
                <input
                  type="checkbox"
                  checked={row.active}
                  onChange={(event) =>
                    setRows((previous) =>
                      previous === null
                        ? previous
                        : previous.map((entry, position) =>
                            position === index
                              ? { ...entry, active: event.target.checked }
                              : entry,
                          ),
                    )
                  }
                />
                <span>Member</span>
              </label>
              <label className="field">
                <span className="field-label">Membership role</span>
                <select
                  value={row.role}
                  disabled={!row.active}
                  onChange={(event) =>
                    setRows((previous) =>
                      previous === null
                        ? previous
                        : previous.map((entry, position) =>
                            position === index
                              ? {
                                  ...entry,
                                  role: event.target.value as "staff" | "lead",
                                }
                              : entry,
                          ),
                    )
                  }
                >
                  <option value="staff">Staff</option>
                  <option value="lead">Lead</option>
                </select>
              </label>
              <button
                type="button"
                className="button secondary"
                onClick={() => void loadGrants(row.department.id)}
              >
                {row.granted === null ? "Load datasets" : "Reload datasets"}
              </button>
              {row.granted === null ? null : row.granted.length === 0 ? (
                <p className="hint">This department has no granted datasets.</p>
              ) : (
                <div className="management-checks">
                  {row.granted.map((dataset) => (
                    <label className="check-field" key={dataset.id}>
                      <input
                        type="checkbox"
                        checked={row.datasetIds.includes(dataset.id)}
                        disabled={
                          !row.active ||
                          (dataset.availability !== "available" &&
                            !row.datasetIds.includes(dataset.id))
                        }
                        onChange={(event) =>
                          setRows((previous) => {
                            if (previous === null) return previous;
                            return previous.map((entry, position) => {
                              if (position !== index) return entry;
                              const next = new Set(entry.datasetIds);
                              if (event.target.checked) next.add(dataset.id);
                              else next.delete(dataset.id);
                              return { ...entry, datasetIds: [...next] };
                            });
                          })
                        }
                      />
                      <span>
                        {dataset.display_name} (
                        {availabilityDescription(dataset.availability)})
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </fieldset>
          ))}
        </div>
      )}
      {error !== "" ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="dialog-actions">
        <button
          type="button"
          className="button primary"
          disabled={pending || rows === null}
          onClick={() => {
            if (pending || rows === null) return;
            setPending(true);
            setError("");
            void putUserMemberships(
              urls,
              user.id,
              rows.map((row) => ({
                department_id: row.department.id,
                membership_role: row.role,
                is_active: row.active,
                dataset_ids: row.datasetIds,
              })),
              revision,
            )
              .then((saved) => onSaved(saved.revision, saved.epoch))
              .catch((caught) => setError(onFailure(caught)))
              .finally(() => setPending(false));
          }}
        >
          Save memberships
        </button>
        <button
          type="button"
          className="button secondary dialog-cancel"
          onClick={onClose}
        >
          Cancel
        </button>
      </div>
    </Dialog>
  );
}

function DepartmentsSection({
  urls,
  capabilities,
  state,
  onState,
  onNotice,
  onFailure,
}: SectionProps & { state: SectionState }) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<ListPage<Department> | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const canCreate = capabilities.manage_departments && capabilities.manage_users;

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const pageResult = await fetchDepartments(
          urls,
          { q: query, page, page_size: 25 },
          signal,
        );
        setResult(pageResult);
        onState(pageResult.revision, pageResult.epoch);
      } catch (caught) {
        onFailure(caught);
      }
    },
    [onFailure, onState, page, query, urls],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return (
    <section aria-label="Departments">
      <div className="management-toolbar">
        <label className="field management-search">
          <span className="field-label">Search departments</span>
          <input
            type="search"
            value={query}
            placeholder="Code or name"
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
          />
        </label>
        {canCreate ? (
          <button
            type="button"
            className="button primary"
            onClick={() => setCreating(true)}
          >
            New department
          </button>
        ) : null}
      </div>
      {result === null ? (
        <p className="hint">No departments loaded.</p>
      ) : result.items.length === 0 ? (
        <p className="hint">
          {query === "" ? "No departments are visible to you." : "No matches."}
        </p>
      ) : (
        <ul className="management-cards">
          {result.items.map((department) => (
            <li key={department.id} className="management-card">
              <div>
                <span className="management-primary">{department.name}</span>
                <span className="hint">
                  {department.code} · {department.is_active ? "Active" : "Inactive"}
                </span>
                {department.unavailable_dataset_count > 0 ? (
                  <span className="hint">
                    {department.unavailable_dataset_count} unavailable dataset grant
                    {department.unavailable_dataset_count === 1 ? "" : "s"}
                  </span>
                ) : null}
              </div>
              <button
                type="button"
                className="button secondary"
                onClick={() => setSelected(department.id)}
              >
                Open
              </button>
            </li>
          ))}
        </ul>
      )}
      {result !== null ? (
        <Pager
          page={result.page}
          pageSize={result.page_size}
          total={result.total}
          onPage={setPage}
        />
      ) : null}

      {selected !== null ? (
        <DepartmentDetail
          urls={urls}
          capabilities={capabilities}
          departmentId={selected}
          onClose={() => setSelected(null)}
          onState={onState}
          onNotice={onNotice}
          onFailure={onFailure}
          onChanged={() => void load(new AbortController().signal)}
        />
      ) : null}
      {creating ? (
        <DepartmentCreateDialog
          urls={urls}
          revision={state.revision}
          onClose={() => setCreating(false)}
          onSaved={(revision, epoch) => {
            onState(revision, epoch);
            onNotice("Department created.");
            setCreating(false);
            void load(new AbortController().signal);
          }}
          onFailure={onFailure}
        />
      ) : null}
    </section>
  );
}

function DepartmentCreateDialog({
  urls,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  revision: number;
  onClose: () => void;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [fields, setFields] = useState({ name: "", description: "" });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <Dialog open title="New department" onClose={onClose}>
      <h2 className="dialog-title">New department</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (pending) return;
          setPending(true);
          setError("");
          void createManagedDepartment(urls, fields, revision)
            .then((saved) => onSaved(saved.revision, saved.epoch))
            .catch((caught) => setError(onFailure(caught)))
            .finally(() => setPending(false));
        }}
      >
        <label className="field">
          <span className="field-label">Name</span>
          <input
            type="text"
            required
            value={fields.name}
            onChange={(event) =>
              setFields((previous) => ({ ...previous, name: event.target.value }))
            }
          />
        </label>
        <label className="field">
          <span className="field-label">Description</span>
          <textarea
            value={fields.description}
            onChange={(event) =>
              setFields((previous) => ({
                ...previous,
                description: event.target.value,
              }))
            }
          />
        </label>
        {error !== "" ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="button primary" disabled={pending}>
            Create department
          </button>
          <button
            type="button"
            className="button secondary dialog-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function DepartmentDetail({
  urls,
  capabilities,
  departmentId,
  onClose,
  onState,
  onNotice,
  onFailure,
  onChanged,
}: {
  urls: ManagementUrls;
  capabilities: Capabilities;
  departmentId: string;
  onClose: () => void;
  onState: (revision: number, epoch: number) => void;
  onNotice: (message: string) => void;
  onFailure: (caught: unknown) => string;
  onChanged: () => void;
}) {
  const [department, setDepartment] = useState<Department | null>(null);
  const [roster, setRoster] = useState<RosterMember[] | null>(null);
  const [granted, setGranted] = useState<DepartmentDatasetRecord[] | null>(null);
  const [catalog, setCatalog] = useState<DatasetRecord[] | null>(null);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [savingGrants, setSavingGrants] = useState(false);
  const [candidateQuery, setCandidateQuery] = useState("");
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [subsetFor, setSubsetFor] = useState<RosterMember | null>(null);

  const globalScope = capabilities.manage_users;

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const [detail, members, grants] = await Promise.all([
          fetchDepartment(urls, departmentId, signal),
          fetchDepartmentMembers(urls, departmentId, signal),
          fetchDepartmentDatasets(urls, departmentId, signal),
        ]);
        setDepartment(detail.value);
        setRoster(members.members);
        setGranted(grants.datasets);
        setRevision(members.revision);
        onState(members.revision, members.epoch);
        if (capabilities.manage_catalog) {
          const all = await fetchDatasets(urls, { page: 1, page_size: 100 }, signal);
          setCatalog(all.items);
        }
      } catch (caught) {
        setError(onFailure(caught));
      }
    },
    [capabilities.manage_catalog, departmentId, onFailure, onState, urls],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function saveRoster(rows: RosterMember[]) {
    try {
      const saved = await putDepartmentMembers(
        urls,
        departmentId,
        rows.map((row) => ({
          user_id: row.user.id,
          membership_role: row.membership_role,
          is_active: row.is_active,
          dataset_ids: row.dataset_ids,
        })),
        revision,
      );
      setRoster(saved.members);
      setRevision(saved.revision);
      onState(saved.revision, saved.epoch);
      setNotice("Roster saved.");
      onNotice("Department roster saved.");
      onChanged();
    } catch (caught) {
      setError(onFailure(caught));
    }
  }

  async function saveGrants(datasetIds: string[], confirmedDatasetIds: string[] = []) {
    if (savingGrants) return;
    setSavingGrants(true);
    try {
      const saved = await putDepartmentDatasets(
        urls,
        departmentId,
        datasetIds,
        revision,
        confirmedDatasetIds,
      );
      setRevision(saved.revision);
      onState(saved.revision, saved.epoch);
      setNotice(
        confirmedDatasetIds.length > 0
          ? "Department grant restored. Reassign members whose subsets were cleared."
          : "Department datasets saved.",
      );
      onNotice(
        confirmedDatasetIds.length > 0
          ? "Department grant restored; member subsets must be reassigned."
          : "Department datasets saved.",
      );
      await load(new AbortController().signal);
      onChanged();
    } catch (caught) {
      setError(onFailure(caught));
    } finally {
      setSavingGrants(false);
    }
  }

  if (department === null || roster === null || granted === null) {
    return (
      <section className="management-detail" aria-label="Department detail">
        {error === "" ? (
          <p className="hint">Loading department…</p>
        ) : (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button type="button" className="button secondary" onClick={onClose}>
          Close
        </button>
      </section>
    );
  }

  const grantedIds = new Set(granted.map((dataset) => dataset.id));
  const grantedById = new Map(granted.map((dataset) => [dataset.id, dataset]));
  const catalogById = new Map((catalog ?? []).map((dataset) => [dataset.id, dataset]));
  const datasetRows: DepartmentDatasetRecord[] = [
    ...(catalog ?? []).map((dataset) => {
      const grant = grantedById.get(dataset.id);
      return {
        id: dataset.id,
        display_name:
          grant?.availability === "different_source"
            ? grant.display_name
            : dataset.display_name,
        availability: grant?.availability ?? dataset.availability,
      };
    }),
    ...granted.filter((dataset) => !catalogById.has(dataset.id)),
  ];
  const editableRows = globalScope
    ? roster
    : roster.filter((row) => row.membership_role === "staff");

  return (
    <section className="management-detail" aria-label="Department detail">
      <header className="management-detail-header">
        <div>
          <h2 className="management-subtitle">{department.name}</h2>
          <p className="hint">
            {department.code} · {department.is_active ? "Active" : "Inactive"}
          </p>
          {department.unavailable_dataset_count > 0 ? (
            <p className="form-error" role="status">
              {department.unavailable_dataset_count} dataset grant
              {department.unavailable_dataset_count === 1 ? " is" : "s are"}{" "}
              unavailable. Review the cause below; unavailable grants do not provide
              access.
            </p>
          ) : null}
        </div>
        <button type="button" className="button secondary" onClick={onClose}>
          Close
        </button>
      </header>

      {error !== "" ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      {notice !== "" ? <p className="hint">{notice}</p> : null}

      <StateLine title="Department details">
        {globalScope ? (
          <DepartmentEditForm
            urls={urls}
            department={department}
            revision={revision}
            onSaved={(savedRevision, epoch) => {
              setRevision(savedRevision);
              onState(savedRevision, epoch);
              setNotice("Department updated.");
              void load(new AbortController().signal);
            }}
            onFailure={onFailure}
          />
        ) : (
          <p className="hint">{department.description || "No description."}</p>
        )}
      </StateLine>

      <StateLine title="Department datasets">
        {globalScope ? (
          <div className="management-checks">
            {catalog === null && datasetRows.length === 0 ? (
              <p className="hint">Loading available Wren models…</p>
            ) : datasetRows.length === 0 ? (
              <p className="hint">
                No models are available or assigned in this department.
              </p>
            ) : (
              datasetRows.map((dataset) => {
                const grant = grantedById.get(dataset.id);
                const isGranted = grant !== undefined;
                return (
                  <div className="management-row" key={dataset.id}>
                    <label className="check-field">
                      <input
                        type="checkbox"
                        checked={isGranted}
                        disabled={
                          savingGrants ||
                          (!isGranted && dataset.availability !== "available")
                        }
                        onChange={(event) => {
                          const next = new Set(grantedIds);
                          if (event.target.checked) next.add(dataset.id);
                          else next.delete(dataset.id);
                          void saveGrants([...next]);
                        }}
                      />
                      <span>
                        {dataset.display_name} (
                        {availabilityDescription(dataset.availability)})
                      </span>
                    </label>
                    {grant?.availability === "needs_review" ? (
                      <button
                        type="button"
                        className="button secondary"
                        disabled={savingGrants}
                        onClick={() => {
                          if (
                            window.confirm(
                              "Updating this assignment restores the department grant and clears member subset assignments for this dataset. Members must be assigned again explicitly. Continue?",
                            )
                          ) {
                            void saveGrants([...grantedIds], [dataset.id]);
                          }
                        }}
                      >
                        Update assignment
                      </button>
                    ) : null}
                  </div>
                );
              })
            )}
          </div>
        ) : granted.length === 0 ? (
          <p className="hint">No datasets are granted to this department.</p>
        ) : (
          <ul className="management-list">
            {granted.map((dataset) => (
              <li key={dataset.id}>
                {dataset.display_name} ({availabilityDescription(dataset.availability)})
              </li>
            ))}
          </ul>
        )}
      </StateLine>

      <StateLine title="Members">
        {editableRows.length === 0 ? (
          <p className="hint">No members in this scope yet.</p>
        ) : (
          <div className="management-table-wrap">
            <table className="management-table">
              <thead>
                <tr>
                  <th scope="col">Member</th>
                  <th scope="col">Role</th>
                  <th scope="col">Active</th>
                  <th scope="col">Datasets</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {editableRows.map((row) => (
                  <tr key={row.user.id}>
                    <td data-label="Member">{row.user.username}</td>
                    <td data-label="Role">
                      {row.membership_role === "lead" ? "Lead" : "Staff"}
                    </td>
                    <td data-label="Active">
                      <input
                        type="checkbox"
                        aria-label={`Active membership for ${row.user.username}`}
                        checked={row.is_active}
                        onChange={(event) => {
                          const next = editableRows.map((entry) =>
                            entry.user.id === row.user.id
                              ? { ...entry, is_active: event.target.checked }
                              : entry,
                          );
                          void saveRoster(next);
                        }}
                      />
                    </td>
                    <td data-label="Datasets">
                      {row.dataset_ids.length === 0
                        ? "None"
                        : `${row.dataset_ids.length} assigned`}
                    </td>
                    <td data-label="Actions" className="management-actions">
                      <button
                        type="button"
                        className="button secondary"
                        onClick={() => setSubsetFor(row)}
                      >
                        Edit subsets
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="management-toolbar">
          <label className="field management-search">
            <span className="field-label">Find a person to add</span>
            <input
              type="search"
              value={candidateQuery}
              placeholder="At least 2 characters"
              onChange={(event) => setCandidateQuery(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="button secondary"
            disabled={candidateQuery.trim().length < 2}
            onClick={() => {
              void searchCandidates(urls, departmentId, {
                q: candidateQuery.trim(),
                page: 1,
                page_size: 10,
              })
                .then((pageResult) => setCandidates(pageResult.items))
                .catch((caught) => setError(onFailure(caught)));
            }}
          >
            Search
          </button>
        </div>
        {candidates === null ? null : candidates.length === 0 ? (
          <p className="hint">No matching people.</p>
        ) : (
          <ul className="management-list">
            {candidates.map((candidate) => (
              <li key={candidate.id} className="management-row">
                <span>{candidate.username}</span>
                <button
                  type="button"
                  className="button secondary"
                  disabled={roster.some(
                    (row) => row.user.id === candidate.id && row.is_active,
                  )}
                  onClick={() => {
                    const next: RosterMember[] = [
                      ...editableRows,
                      {
                        user: {
                          id: candidate.id,
                          username: candidate.username,
                          first_name: candidate.first_name,
                          last_name: candidate.last_name,
                        },
                        membership_role: "staff",
                        is_active: true,
                        dataset_ids: [],
                      },
                    ];
                    setCandidates(null);
                    void saveRoster(next);
                  }}
                >
                  Add as Staff
                </button>
              </li>
            ))}
          </ul>
        )}
        {globalScope ? (
          <p className="hint">
            Lead rows are managed by a native superuser. Save changes one row at a time;
            every save submits the current roster.
          </p>
        ) : (
          <p className="hint">
            Staff selections are limited to datasets already granted to this department.
          </p>
        )}
      </StateLine>

      {subsetFor !== null ? (
        <SubsetDialog
          urls={urls}
          departmentId={departmentId}
          member={subsetFor}
          granted={granted}
          revision={revision}
          onClose={() => setSubsetFor(null)}
          onSaved={(saved) => {
            setRevision(saved.revision);
            onState(saved.revision, saved.epoch);
            setNotice("Member datasets saved.");
            setSubsetFor(null);
            void load(new AbortController().signal);
            onChanged();
          }}
          onFailure={onFailure}
        />
      ) : null}
    </section>
  );
}

function DepartmentEditForm({
  urls,
  department,
  revision,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  department: Department;
  revision: number;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [fields, setFields] = useState({
    name: department.name,
    description: department.description,
    is_active: department.is_active,
  });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (pending) return;
        setPending(true);
        setError("");
        void patchDepartment(urls, department.id, fields, revision)
          .then((saved) => onSaved(saved.revision, saved.epoch))
          .catch((caught) => setError(onFailure(caught)))
          .finally(() => setPending(false));
      }}
    >
      <label className="field">
        <span className="field-label">Name</span>
        <input
          type="text"
          value={fields.name}
          onChange={(event) =>
            setFields((previous) => ({ ...previous, name: event.target.value }))
          }
        />
      </label>
      <label className="field">
        <span className="field-label">Description</span>
        <textarea
          value={fields.description}
          onChange={(event) =>
            setFields((previous) => ({
              ...previous,
              description: event.target.value,
            }))
          }
        />
      </label>
      <label className="check-field">
        <input
          type="checkbox"
          checked={fields.is_active}
          onChange={(event) =>
            setFields((previous) => ({
              ...previous,
              is_active: event.target.checked,
            }))
          }
        />
        <span>Department is active</span>
      </label>
      {error !== "" ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      <button type="submit" className="button primary" disabled={pending}>
        Save department
      </button>
    </form>
  );
}

function SubsetDialog({
  urls,
  departmentId,
  member,
  granted,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  departmentId: string;
  member: RosterMember;
  granted: DepartmentDatasetRecord[];
  revision: number;
  onClose: () => void;
  onSaved: (saved: {
    value: MemberDatasetRecord;
    revision: number;
    epoch: number;
  }) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [selected, setSelected] = useState<string[]>(member.dataset_ids);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <Dialog open title={`Datasets for ${member.user.username}`} onClose={onClose}>
      <h2 className="dialog-title">Datasets for {member.user.username}</h2>
      <p className="dialog-text">
        The subset is explicit and can stay empty. Existing unavailable selections stay
        visible until removed; you cannot add them again unless their department grant
        becomes available.
      </p>
      {granted.length === 0 ? (
        <p className="hint">Grant datasets to the department first.</p>
      ) : (
        <div className="management-checks">
          {granted.map((dataset) => (
            <label className="check-field" key={dataset.id}>
              <input
                type="checkbox"
                checked={selected.includes(dataset.id)}
                disabled={
                  dataset.availability !== "available" && !selected.includes(dataset.id)
                }
                onChange={(event) => {
                  const next = new Set(selected);
                  if (event.target.checked) next.add(dataset.id);
                  else next.delete(dataset.id);
                  setSelected([...next]);
                }}
              />
              <span>
                {dataset.display_name} ({availabilityDescription(dataset.availability)})
              </span>
            </label>
          ))}
        </div>
      )}
      {error !== "" ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="dialog-actions">
        <button
          type="button"
          className="button primary"
          disabled={pending}
          onClick={() => {
            if (pending) return;
            setPending(true);
            setError("");
            void fetchMemberDatasets(urls, departmentId, member.user.id)
              .then(() =>
                putMemberDatasets(
                  urls,
                  departmentId,
                  member.user.id,
                  selected,
                  revision,
                ),
              )
              .then(onSaved)
              .catch((caught) => setError(onFailure(caught)))
              .finally(() => setPending(false));
          }}
        >
          Save subsets
        </button>
        <button
          type="button"
          className="button secondary dialog-cancel"
          onClick={onClose}
        >
          Cancel
        </button>
      </div>
    </Dialog>
  );
}

function DatasetsSection({
  urls,
  capabilities,
  state,
  onState,
  onNotice,
  onFailure,
}: SectionProps & { state: SectionState }) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [departmentId, setDepartmentId] = useState("");
  const [departments, setDepartments] = useState<Department[]>([]);
  const [result, setResult] = useState<ListPage<DatasetRecord> | null>(null);
  const [editing, setEditing] = useState<DatasetRecord | null>(null);
  const [catalogStatus, setCatalogStatus] = useState<CatalogStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusError, setStatusError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  const catalogAdmin = capabilities.manage_catalog && capabilities.manage_users;

  useEffect(() => {
    if (catalogAdmin) return;
    const controller = new AbortController();
    void fetchDepartments(urls, { page: 1, page_size: 100 }, controller.signal)
      .then((pageResult) => {
        setDepartments(pageResult.items);
        setDepartmentId((previous) =>
          previous === "" && pageResult.items.length > 0
            ? pageResult.items[0].id
            : previous,
        );
      })
      .catch((caught) => {
        if (!controller.signal.aborted) onFailure(caught);
      });
    return () => controller.abort();
  }, [catalogAdmin, onFailure, urls]);

  const load = useCallback(
    async (signal: AbortSignal) => {
      if (!catalogAdmin && departmentId === "") return;
      try {
        const pageResult = await fetchDatasets(
          urls,
          {
            q: query,
            page,
            page_size: 25,
            ...(catalogAdmin ? {} : { department_id: departmentId }),
          },
          signal,
        );
        setResult(pageResult);
        onState(pageResult.revision, pageResult.epoch);
      } catch (caught) {
        if (!signal.aborted) onFailure(caught);
      }
    },
    [catalogAdmin, departmentId, onFailure, onState, page, query, urls],
  );

  const loadStatus = useCallback(
    async (signal?: AbortSignal) => {
      if (!catalogAdmin) return;
      setStatusLoading(true);
      try {
        const status = await fetchCatalogStatus(urls, signal);
        setCatalogStatus(status.value);
        setStatusError("");
        onState(status.revision, status.epoch);
      } catch (caught) {
        if (!signal?.aborted) setStatusError(onFailure(caught));
      } finally {
        if (!signal?.aborted) setStatusLoading(false);
      }
    },
    [catalogAdmin, onFailure, onState, urls],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    void loadStatus(controller.signal);
    return () => controller.abort();
  }, [loadStatus]);

  async function refresh() {
    if (!catalogAdmin || !catalogStatus?.configured || refreshing) return;
    setRefreshing(true);
    setRefreshError("");
    try {
      const saved = await refreshCatalog(urls, state.revision);
      onState(saved.revision, saved.epoch);
      onNotice(
        `Wren catalog refreshed: ${saved.value.created} created, ${saved.value.updated} updated, ${saved.value.missing} newly missing, ${saved.value.review_required} grants needing review.`,
      );
      await Promise.all([load(new AbortController().signal), loadStatus()]);
    } catch (caught) {
      setRefreshError(toChatError(caught).message);
      await loadStatus();
    } finally {
      setRefreshing(false);
    }
  }

  let emptyCatalogMessage: string | null = null;
  if (catalogAdmin && catalogStatus?.configured) {
    if (catalogStatus.error_code !== null && catalogStatus.last_success_at === null) {
      emptyCatalogMessage = "No successful Wren catalog snapshot is available yet.";
    } else if (catalogStatus.last_success_at !== null) {
      emptyCatalogMessage = "No models are published in this Wren environment.";
    } else {
      emptyCatalogMessage =
        "No successful Wren discovery yet. Refresh to load published models.";
    }
  }

  return (
    <section aria-label="Datasets">
      <div className="management-toolbar">
        <label className="field management-search">
          <span className="field-label">Search datasets</span>
          <input
            type="search"
            value={query}
            placeholder="Name, code or model"
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
          />
        </label>
        {catalogAdmin ? (
          <button
            type="button"
            className="button primary"
            disabled={refreshing || statusLoading || !catalogStatus?.configured}
            onClick={() => void refresh()}
          >
            {refreshing ? "Refreshing datasets…" : "Refresh from Wren"}
          </button>
        ) : (
          <label className="field">
            <span className="field-label">Department</span>
            <select
              value={departmentId}
              disabled={departments.length === 0}
              onChange={(event) => {
                setDepartmentId(event.target.value);
                setPage(1);
              }}
            >
              {departments.length === 0 ? (
                <option value="">No departments available</option>
              ) : (
                departments.map((department) => (
                  <option key={department.id} value={department.id}>
                    {department.name}
                  </option>
                ))
              )}
            </select>
          </label>
        )}
      </div>

      {catalogAdmin ? (
        <div className="management-state" aria-live="polite">
          {catalogStatus === null ? (
            <p className={statusError === "" ? "hint" : "form-error"} role="status">
              {statusLoading
                ? "Loading Wren catalog status…"
                : statusError || "Catalog status is unavailable."}
            </p>
          ) : !catalogStatus.configured ? (
            <p className="hint">Wren MCP is not configured.</p>
          ) : (
            <>
              {catalogStatus.last_success_at !== null ? (
                <p className="hint">
                  Last successful refresh:{" "}
                  {new Date(catalogStatus.last_success_at).toLocaleString()}
                </p>
              ) : (
                <p className="hint">
                  No successful Wren discovery yet. Refresh to load published models.
                </p>
              )}
              {catalogStatus.error_code !== null ? (
                <>
                  <p className="form-error" role="alert">
                    Wren dataset discovery is unavailable. Try refreshing again.
                  </p>
                  {catalogStatus.last_success_at !== null ? (
                    <p className="hint">
                      Showing the last successful catalog snapshot.
                    </p>
                  ) : null}
                </>
              ) : null}
              {refreshError !== "" ? (
                <p className="form-error" role="alert">
                  {refreshError}
                </p>
              ) : null}
              {refreshing ? <p className="hint">Refreshing datasets…</p> : null}
            </>
          )}
        </div>
      ) : null}

      {result === null ? (
        <p className="hint">Loading datasets…</p>
      ) : result.items.length === 0 ? (
        query !== "" ? (
          <p className="hint">No datasets match this search.</p>
        ) : catalogAdmin ? (
          emptyCatalogMessage === null ? null : (
            <p className="hint">{emptyCatalogMessage}</p>
          )
        ) : (
          <p className="hint">No datasets are granted to this department.</p>
        )
      ) : (
        <div className="management-table-wrap">
          <table className="management-table">
            <thead>
              <tr>
                <th scope="col">Dataset</th>
                <th scope="col">Wren project and location</th>
                <th scope="col">Availability</th>
                <th scope="col">Local access</th>
                {catalogAdmin ? <th scope="col">Actions</th> : null}
              </tr>
            </thead>
            <tbody>
              {result.items.map((dataset) => {
                const namespace = [dataset.catalog, dataset.schema]
                  .filter((part) => part !== "")
                  .join(".");
                return (
                  <tr key={dataset.id}>
                    <td data-label="Dataset">
                      <span className="management-primary">{dataset.display_name}</span>
                      <span className="hint">{dataset.code}</span>
                      <span className="hint">Model: {dataset.model_name}</span>
                    </td>
                    <td data-label="Wren project and location">
                      <span>{dataset.project_key}</span>
                      <span className="hint">
                        {namespace || "Semantic model"}
                        {dataset.physical_table !== ""
                          ? ` · ${dataset.physical_table}`
                          : ""}
                      </span>
                    </td>
                    <td data-label="Availability">
                      {availabilityDescription(dataset.availability)}
                    </td>
                    <td data-label="Local access">
                      {dataset.is_active ? "Enabled" : "Disabled"} ·{" "}
                      {dataset.validation_status === "metadata_discovered"
                        ? "Metadata discovered"
                        : "Unverified"}
                    </td>
                    {catalogAdmin ? (
                      <td data-label="Actions">
                        <button
                          type="button"
                          className="button secondary"
                          onClick={() => setEditing(dataset)}
                        >
                          Edit
                        </button>
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {result !== null ? (
        <Pager
          page={result.page}
          pageSize={result.page_size}
          total={result.total}
          onPage={setPage}
        />
      ) : null}
      {editing !== null ? (
        <DatasetEditDialog
          urls={urls}
          dataset={editing}
          revision={state.revision}
          onClose={() => setEditing(null)}
          onSaved={(revision, epoch) => {
            onState(revision, epoch);
            onNotice("Dataset setting updated.");
            setEditing(null);
            void load(new AbortController().signal);
          }}
          onFailure={onFailure}
        />
      ) : null}
    </section>
  );
}

function DatasetEditDialog({
  urls,
  dataset,
  revision,
  onClose,
  onSaved,
  onFailure,
}: {
  urls: ManagementUrls;
  dataset: DatasetRecord;
  revision: number;
  onClose: () => void;
  onSaved: (revision: number, epoch: number) => void;
  onFailure: (caught: unknown) => string;
}) {
  const [isActive, setIsActive] = useState(dataset.is_active);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  return (
    <Dialog open title={`Edit ${dataset.display_name}`} onClose={onClose}>
      <h2 className="dialog-title">Edit {dataset.display_name}</h2>
      <p className="dialog-text">
        Wren controls this model metadata. Only its local enabled state can be changed
        here.
      </p>
      <p className="hint">Wren project: {dataset.project_key}</p>
      <p className="hint">Model: {dataset.model_name}</p>
      <p className="hint">
        Location:{" "}
        {[dataset.catalog, dataset.schema, dataset.physical_table]
          .filter((part) => part !== "")
          .join(".") || "Semantic model"}
      </p>
      {dataset.description !== "" ? (
        <p className="dialog-text">{dataset.description}</p>
      ) : null}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (pending) return;
          setPending(true);
          setError("");
          void patchDataset(urls, dataset.id, { is_active: isActive }, revision)
            .then((saved) => onSaved(saved.revision, saved.epoch))
            .catch((caught) => setError(onFailure(caught)))
            .finally(() => setPending(false));
        }}
      >
        <label className="check-field">
          <input
            type="checkbox"
            checked={isActive}
            onChange={(event) => setIsActive(event.target.checked)}
          />
          <span>Dataset is enabled for assignments</span>
        </label>
        {error !== "" ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="button primary" disabled={pending}>
            Save dataset
          </button>
          <button
            type="button"
            className="button secondary dialog-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function RolesSection({
  urls,
  capabilities,
  onFailure,
}: SectionProps & { state: SectionState }) {
  const [roles, setRoles] = useState<RoleDefinition[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetchRoles(urls, controller.signal)
      .then((result) => setRoles(result.roles))
      .catch((caught) => onFailure(caught));
    return () => controller.abort();
  }, [onFailure, urls]);

  if (!capabilities.manage_roles) {
    return <p className="hint">You do not have role administration access.</p>;
  }

  return (
    <section aria-label="Roles">
      <p className="hint">
        These three native groups are fixed definitions. A role alone grants no
        datasets; department membership and its explicit subset decide access.
      </p>
      {roles === null ? (
        <p className="hint">Loading roles…</p>
      ) : (
        <div className="management-table-wrap">
          <table className="management-table">
            <thead>
              <tr>
                <th scope="col">Role</th>
                <th scope="col">Capabilities</th>
                <th scope="col">Analytics chat</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => (
                <tr key={role.name}>
                  <td data-label="Role">{role.name}</td>
                  <td data-label="Capabilities">
                    {role.capabilities.length === 0
                      ? "None"
                      : role.capabilities
                          .map((capability) => CAPABILITY_LABELS[capability])
                          .join(", ")}
                  </td>
                  <td data-label="Analytics chat">
                    {role.uses_analytics ? "Allowed" : "Not allowed"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function AuditSection({
  urls,
  capabilities,
  onFailure,
  state,
}: SectionProps & { state: SectionState }) {
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<ListPage<AuditRecord> | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetchAudit(urls, { page, page_size: 25 }, controller.signal)
      .then(setResult)
      .catch((caught) => onFailure(caught));
    return () => controller.abort();
  }, [onFailure, page, urls]);

  if (!capabilities.view_audit) {
    return <p className="hint">You do not have audit access.</p>;
  }

  return (
    <section aria-label="Audit log">
      <p className="hint">Current administration revision {state.revision}.</p>
      {result === null ? (
        <p className="hint">Loading audit records…</p>
      ) : result.items.length === 0 ? (
        <p className="hint">No audit records yet.</p>
      ) : (
        <div className="management-table-wrap">
          <table className="management-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Action</th>
                <th scope="col">Actor</th>
                <th scope="col">Target</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {result.items.map((record) => (
                <tr key={record.id}>
                  <td data-label="When">{record.created_at}</td>
                  <td data-label="Action">{record.action}</td>
                  <td data-label="Actor">{record.actor ?? "system"}</td>
                  <td data-label="Target">
                    {record.target_type}:{record.target_id}
                  </td>
                  <td data-label="Result">
                    {record.allowed ? "Allowed" : "Denied"} · {record.reason_code}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {result !== null ? (
        <Pager
          page={result.page}
          pageSize={result.page_size}
          total={result.total}
          onPage={setPage}
        />
      ) : null}
    </section>
  );
}

export function Management({ bootstrap }: { bootstrap: ManagementBootstrap }) {
  const { urls, capabilities, section } = bootstrap;
  const [state, setState] = useState<SectionState>({
    revision: bootstrap.revision,
    epoch: bootstrap.epoch,
  });
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);

  const onFailure = useCallback((caught: unknown) => {
    const failure = describeFailure(caught);
    if (isDenied(caught)) {
      setDenied(true);
      setError("Your account no longer has administration access.");
      return failure.message;
    }
    setError(failure.message);
    return failure.message;
  }, []);

  const onState = useCallback((revision: number, epoch: number) => {
    setState({ revision, epoch });
  }, []);

  const onNotice = useCallback((message: string) => {
    setError("");
    setNotice(message);
  }, []);

  // Re-read the signed-in principal whenever the management page regains focus.
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "hidden") return;
      void requestJson(urls.me, "GET")
        .then((payload) => {
          if (
            typeof payload === "object" &&
            payload !== null &&
            "capabilities" in payload
          ) {
            setDenied(false);
          }
        })
        .catch((caught) => {
          if (isDenied(caught)) setDenied(true);
        });
    };
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [urls.me]);

  if (denied) {
    return (
      <div className="management-shell">
        <main className="management-body">
          <p className="alert" role="alert">
            Your account no longer has administration access. Sign in again or return to
            chat.
          </p>
          <p>
            <a className="button secondary" href={urls.chat}>
              Back to chat
            </a>
          </p>
        </main>
      </div>
    );
  }

  return (
    <DialogOpenContext.Provider value={setDialogOpen}>
      <div className="management-shell" data-section={section}>
        <header className="management-header">
          <div>
            <p className="sidebar-brand">AYCE Analytics V2</p>
            <h1 className="management-title">Administration</h1>
          </div>
          <div className="management-identity">
            <span className="hint">{bootstrap.user.username}</span>
            <a className="button secondary" href={urls.chat}>
              Back to chat
            </a>
          </div>
        </header>

        <nav className="management-nav" aria-label="Management sections">
          {visibleSections(capabilities).map((entry) => (
            <a
              key={entry}
              className="management-nav-link"
              href={`?section=${entry}`}
              aria-current={entry === section ? "page" : undefined}
            >
              {SECTION_LABELS[entry]}
            </a>
          ))}
        </nav>

        <p className="sr-only" role="status">
          {notice}
        </p>
        {error !== "" && !dialogOpen ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}

        <main className="management-body">
          {section === "users" ? (
            <UsersSection
              urls={urls}
              capabilities={capabilities}
              state={state}
              onState={onState}
              onNotice={onNotice}
              onFailure={onFailure}
            />
          ) : null}
          {section === "departments" ? (
            <DepartmentsSection
              urls={urls}
              capabilities={capabilities}
              state={state}
              onState={onState}
              onNotice={onNotice}
              onFailure={onFailure}
            />
          ) : null}
          {section === "datasets" ? (
            <DatasetsSection
              urls={urls}
              capabilities={capabilities}
              state={state}
              onState={onState}
              onNotice={onNotice}
              onFailure={onFailure}
            />
          ) : null}
          {section === "roles" ? (
            <RolesSection
              urls={urls}
              capabilities={capabilities}
              state={state}
              onState={onState}
              onNotice={onNotice}
              onFailure={onFailure}
            />
          ) : null}
          {section === "audit" ? (
            <AuditSection
              urls={urls}
              capabilities={capabilities}
              state={state}
              onState={onState}
              onNotice={onNotice}
              onFailure={onFailure}
            />
          ) : null}
        </main>
      </div>
    </DialogOpenContext.Provider>
  );
}
