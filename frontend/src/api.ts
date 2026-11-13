/**
 * Shared chat API types, boundary parsers, and JSON request helpers.
 *
 * Every server payload is validated before React state, and every failure
 * collapses to a fixed `ChatApiError` message; response text never surfaces.
 */

export type Role = "user" | "assistant";

export type DisplayEntry = { role: Role; content: string };

/** Server-resolved access state for one thread; only `available` is readable. */
export type ThreadAccess = "available" | "quarantined" | "department_revoked";

export type DepartmentLabel = { id: string; code: string; name: string };

export type MembershipSummary = {
  id: string;
  department: DepartmentLabel;
  membership_role: "staff" | "lead";
  dataset_ids: string[];
};

export type CapabilityName =
  | "manage_users"
  | "manage_roles"
  | "manage_departments"
  | "manage_department_staff"
  | "manage_catalog"
  | "manage_grants"
  | "view_audit";

export type Capabilities = Record<CapabilityName, boolean>;

export type ThreadSummary = {
  id: string;
  title: string;
  revision: number;
  created_at: string;
  updated_at: string;
  access: ThreadAccess;
  department: DepartmentLabel | null;
  page_url: string;
  detail_url: string;
  messages_url: string;
};

export type ActivityKind = "model" | "wren_tool";
export type ActivityStatus = "running" | "complete" | "failed";

export type ActivityStep = {
  id: number;
  kind: ActivityKind;
  status: ActivityStatus;
  title?: string;
  duration_ms?: number;
};

export type TurnActivity = {
  version: number;
  turn_id: string;
  steps: ActivityStep[];
  truncated: boolean;
};

export type TurnResponse = {
  requested_model: string | null;
  resolved_model: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  output_tokens: number | null;
  output_limit_reached: boolean;
};

export type AttachmentRef = {
  id: string;
  name: string;
  media_type: string;
  size: number;
};

export type TurnDetail = {
  turn_id: string;
  turn_index: number;
  response: TurnResponse | null;
  activity: TurnActivity | null;
  attachments: AttachmentRef[];
  can_regenerate: boolean;
  can_delete: boolean;
  regenerate_url: string;
  delete_url: string;
};

export type ActivityUpdate = {
  turn_id: string;
  step: ActivityStep | null;
  truncated: boolean;
};

export type ThreadDetail = ThreadSummary & {
  display: DisplayEntry[];
  turns: TurnDetail[];
  conversation_error: string | null;
};

export type BootstrapUser = { username: string; is_staff: boolean };

/** Server-owned application budgets; the client duplicates no constants. */
export type ChatLimits = {
  max_files_per_thread: number;
  max_files_per_turn: number;
  max_file_bytes: number;
  max_text_bytes_per_file: number;
  max_text_bytes_per_thread: number;
  context_window: number;
  max_output_tokens: number;
};

export type BootstrapUrls = {
  threads: string;
  login: string;
  logout: string;
  management: string | null;
};

export type Bootstrap = {
  user: BootstrapUser;
  threads: ThreadSummary[];
  active_thread: ThreadDetail | null;
  limits: ChatLimits;
  urls: BootstrapUrls;
  capabilities: Capabilities;
  memberships: MembershipSummary[];
  analytics_enabled: boolean;
  analytics_department_ids: string[];
  show_reasoning_detail?: boolean;
};

export const MAX_MESSAGE_LENGTH = 2000;
export const INVALID_MESSAGE = "Enter a message between 1 and 2,000 characters.";
export const CHAT_SERVER_ERROR = "Could not reach the chat server. Try again.";
export const CHAT_PROTOCOL_ERROR =
  "The chat server sent an unexpected response. Refresh and try again.";
export const CHAT_AUTH_MESSAGE = "Sign in to access your chats.";
export const CHAT_BUSY_MESSAGE = "Another chat action is still running. Try again.";
export const CHAT_THREAD_MISSING =
  "This chat is no longer available. Choose another chat or start a new one.";
export const INVALID_ATTACHMENT =
  "One or more files could not be read. Use a supported, non-empty file.";
export const UNSUPPORTED_ATTACHMENT =
  "Supported files: TXT, MD, CSV, JSON, PDF, PNG, JPG, GIF, WEBP, DOCX, XLSX. " +
  "Save older Word or Excel files as DOCX or XLSX.";
export const ATTACHMENT_LIMIT =
  "Files exceed the configured attachment limits. Remove a selected file or " +
  "start a new chat.";

/**
 * The one place the browser knows the server's file allowlist. It drives the
 * picker's `accept` list and the optimistic label of a pending upload; the
 * server still decides what a file is.
 */
export const ATTACHMENT_MEDIA_TYPES: Readonly<Record<string, string>> = {
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".csv": "text/csv",
  ".json": "application/json",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

export const ATTACHMENT_ACCEPT = Object.keys(ATTACHMENT_MEDIA_TYPES).join(",");

/** The media type this app accepts for a filename, or `null`. */
export function attachmentMediaType(name: string): string | null {
  const lowered = name.toLowerCase();
  const dot = lowered.lastIndexOf(".");
  if (dot <= 0) return null;
  return ATTACHMENT_MEDIA_TYPES[lowered.slice(dot)] ?? null;
}

export const MAX_ATTACHMENT_NAME = 255;

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Accept only a complete timestamp that carries its own zone designator.
const ISO_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/;

/** Matches `PositiveIntegerField` storage for the public metric counters. */
const MAX_METRIC = 2_147_483_647;
const MAX_ACTIVITY_STEPS = 64;
const MAX_ACTIVITY_BYTES = 65536;
const ACTIVITY_KINDS: readonly ActivityKind[] = ["model", "wren_tool"];
const ACTIVITY_STATUSES: readonly ActivityStatus[] = ["running", "complete", "failed"];
const REQUIRED_STEP_KEYS = ["id", "kind", "status"];
const ALLOWED_STEP_KEYS = new Set(["id", "kind", "status", "title", "duration_ms"]);

function hasControlCharacter(value: string): boolean {
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    if (code <= 0x1f || code === 0x7f) return true;
  }
  return false;
}

/** A public error carrying a stable machine code and safe display message. */
export class ChatApiError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "ChatApiError";
    this.code = code;
  }
}

function protocolError(): ChatApiError {
  return new ChatApiError("protocol_error", CHAT_PROTOCOL_ERROR);
}

/** The module's single object guard; every parser narrows through it. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Whether a record carries exactly the expected keys and nothing else. */
export function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  const expected = new Set(keys);
  if (Object.keys(value).length !== expected.size) return false;
  return keys.every((key) => key in value);
}

function currentOrigin(): string | null {
  // Node has no `location`; one named cast keeps the optional read honest.
  const scope = globalThis as { location?: { origin?: unknown } };
  const origin = scope.location?.origin;
  return typeof origin === "string" && origin !== "null" ? origin : null;
}

/** Accept only a single-slash path that resolves to the current origin. */
export function parseSameOriginPath(value: unknown): string | null {
  if (typeof value !== "string") return null;
  if (!value.startsWith("/") || value.startsWith("//")) return null;
  if (value.includes("\\") || hasControlCharacter(value)) return null;

  const origin = currentOrigin();
  if (origin === null) return null;

  let resolved: URL;
  try {
    resolved = new URL(value, origin);
  } catch {
    return null;
  }
  if (resolved.origin !== origin) return null;
  return value;
}

export function parseUuid(value: unknown): string | null {
  return typeof value === "string" && UUID_PATTERN.test(value) ? value : null;
}

export function parseRevision(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    return null;
  }
  return value;
}

function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

/**
 * Validate a timezone-bearing ISO timestamp, rejecting impossible dates.
 *
 * `Date.parse` alone accepts values like `2026-02-30`, so the calendar fields
 * are checked here before the value is trusted.
 */
export function parseIsoTimestamp(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = ISO_PATTERN.exec(value);
  if (match === null) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > daysInMonth(year, month)) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;

  const zone = match[7];
  if (zone !== "Z") {
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return null;
  }

  return Number.isFinite(Date.parse(value)) ? value : null;
}

/** Accept only a counter the server can store in a positive integer field. */
export function parseMetric(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) return null;
  if (value < 0 || value > MAX_METRIC) return null;
  return value;
}

export function parseDisplay(value: unknown): DisplayEntry[] | null {
  if (!Array.isArray(value) || value.length % 2 !== 0) return null;

  const display: DisplayEntry[] = [];
  for (const [index, entry] of value.entries()) {
    if (!isRecord(entry) || !hasExactKeys(entry, ["role", "content"])) return null;
    const expected: Role = index % 2 === 0 ? "user" : "assistant";
    if (entry.role !== expected || typeof entry.content !== "string") return null;
    display.push({ role: expected, content: entry.content });
  }
  return display;
}

export function parseActivityStep(
  value: unknown,
  expectedId: number,
): ActivityStep | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (!REQUIRED_STEP_KEYS.every((k) => k in value)) return null;
  if (!keys.every((k) => ALLOWED_STEP_KEYS.has(k))) return null;
  if (value.id !== expectedId) return null;
  const kind = ACTIVITY_KINDS.find((entry) => entry === value.kind);
  const status = ACTIVITY_STATUSES.find((entry) => entry === value.status);
  if (kind === undefined || status === undefined) return null;

  const step: ActivityStep = { id: expectedId, kind, status };
  if ("title" in value && typeof value.title === "string") {
    const cleaned = value.title.trim();
    if (cleaned.length > 0) {
      step.title = cleaned.slice(0, 500);
    }
  }
  if (
    "duration_ms" in value &&
    typeof value.duration_ms === "number" &&
    Number.isSafeInteger(value.duration_ms) &&
    value.duration_ms >= 0
  ) {
    step.duration_ms = value.duration_ms;
  }
  return step;
}

function activityEnvelopeBytes(activity: TurnActivity): number {
  const canonical = JSON.stringify({
    version: activity.version,
    turn_id: activity.turn_id,
    steps: activity.steps.map((step) => ({
      id: step.id,
      kind: step.kind,
      status: step.status,
      ...(step.title !== undefined ? { title: step.title } : {}),
      ...(step.duration_ms !== undefined ? { duration_ms: step.duration_ms } : {}),
    })),
    truncated: activity.truncated,
  });
  return new TextEncoder().encode(canonical).length;
}

/** Validate one completed activity timeline, or return `null`. */
export function parseTurnActivity(value: unknown): TurnActivity | null {
  if (!isRecord(value)) return null;
  if (!hasExactKeys(value, ["version", "turn_id", "steps", "truncated"])) {
    return null;
  }
  if (value.version !== 1) return null;

  const turnId = parseUuid(value.turn_id);
  if (turnId === null) return null;

  const rawSteps = value.steps;
  if (!Array.isArray(rawSteps) || rawSteps.length > MAX_ACTIVITY_STEPS) return null;

  const steps: ActivityStep[] = [];
  for (const [index, entry] of rawSteps.entries()) {
    const step = parseActivityStep(entry, index);
    if (step === null || step.status === "running") return null;
    steps.push(step);
  }

  if (typeof value.truncated !== "boolean") return null;

  const activity: TurnActivity = {
    version: 1,
    turn_id: turnId,
    steps,
    truncated: value.truncated,
  };
  if (activityEnvelopeBytes(activity) > MAX_ACTIVITY_BYTES) return null;
  return activity;
}

/** Validate one streamed activity change. */
export function parseActivityUpdate(value: unknown): ActivityUpdate | null {
  if (!isRecord(value)) return null;
  if (!hasExactKeys(value, ["turn_id", "step", "truncated"])) return null;

  const turnId = parseUuid(value.turn_id);
  if (turnId === null) return null;
  if (typeof value.truncated !== "boolean") return null;

  if (value.step === null) {
    if (!value.truncated) return null;
    return { turn_id: turnId, step: null, truncated: true };
  }

  const raw = value.step;
  if (!isRecord(raw)) return null;
  const stepId = parseRevision(raw.id);
  if (stepId === null) return null;
  // The streamed step uses the stored step's schema: server-side detail such
  // as a title or duration rides along whenever the server enables it.
  const step = parseActivityStep(raw, stepId);
  if (step === null) return null;
  return { turn_id: turnId, step, truncated: value.truncated };
}

/** `null` stays null, a valid value stays a string, anything else is rejected. */
function parseOptionalModel(raw: unknown): string | null | undefined {
  if (raw === null) return null;
  if (typeof raw !== "string" || raw.length === 0 || raw.length > 200) return undefined;
  if (hasControlCharacter(raw)) return undefined;
  return raw;
}

function parseOptionalTimestamp(raw: unknown): string | null | undefined {
  if (raw === null) return null;
  return parseIsoTimestamp(raw) ?? undefined;
}

function parseOptionalMetric(raw: unknown): number | null | undefined {
  if (raw === null) return null;
  return parseMetric(raw) ?? undefined;
}

function parseTurnResponse(value: unknown): TurnResponse | null {
  if (!isRecord(value)) return null;
  if (
    !hasExactKeys(value, [
      "requested_model",
      "resolved_model",
      "completed_at",
      "duration_ms",
      "output_tokens",
      "output_limit_reached",
    ])
  ) {
    return null;
  }

  const requested = parseOptionalModel(value.requested_model);
  const resolved = parseOptionalModel(value.resolved_model);
  if (requested === undefined || resolved === undefined) return null;

  const completedAt = parseOptionalTimestamp(value.completed_at);
  if (completedAt === undefined) return null;

  const duration = parseOptionalMetric(value.duration_ms);
  const tokens = parseOptionalMetric(value.output_tokens);
  if (duration === undefined || tokens === undefined) return null;
  if (typeof value.output_limit_reached !== "boolean") return null;

  return {
    requested_model: requested,
    resolved_model: resolved,
    completed_at: completedAt,
    duration_ms: duration,
    output_tokens: tokens,
    output_limit_reached: value.output_limit_reached,
  };
}

/** Validate one stored attachment label; quotas are never re-applied here. */
export function parseAttachmentRef(value: unknown): AttachmentRef | null {
  if (!isRecord(value) || !hasExactKeys(value, ["id", "name", "media_type", "size"])) {
    return null;
  }
  const id = parseUuid(value.id);
  if (id === null) return null;

  const name = value.name;
  if (typeof name !== "string") return null;
  const characters = Array.from(name);
  if (characters.length === 0 || characters.length > MAX_ATTACHMENT_NAME) return null;
  if (name.trim().length === 0 || hasControlCharacter(name)) return null;

  const mediaType = value.media_type;
  if (typeof mediaType !== "string") return null;
  const allowed = Object.values(ATTACHMENT_MEDIA_TYPES);
  if (!allowed.includes(mediaType)) return null;

  const size = value.size;
  if (typeof size !== "number" || !Number.isSafeInteger(size)) return null;
  if (size <= 0 || size > MAX_METRIC) return null;

  return { id, name, media_type: mediaType, size };
}

function parseAttachments(value: unknown): AttachmentRef[] | null {
  if (!Array.isArray(value)) return null;
  const attachments: AttachmentRef[] = [];
  const seen = new Set<string>();
  for (const entry of value) {
    const attachment = parseAttachmentRef(entry);
    if (attachment === null) return null;
    if (seen.has(attachment.id)) return null;
    seen.add(attachment.id);
    attachments.push(attachment);
  }
  return attachments;
}

export function parseTurnDetail(value: unknown): TurnDetail | null {
  if (!isRecord(value)) return null;
  if (
    !hasExactKeys(value, [
      "turn_id",
      "turn_index",
      "response",
      "activity",
      "attachments",
      "can_regenerate",
      "can_delete",
      "regenerate_url",
      "delete_url",
    ])
  ) {
    return null;
  }

  const turnId = parseUuid(value.turn_id);
  const revision = parseRevision(value.turn_index);
  const regenerateUrl = parseSameOriginPath(value.regenerate_url);
  const deleteUrl = parseSameOriginPath(value.delete_url);
  if (turnId === null || revision === null) return null;
  if (regenerateUrl === null || deleteUrl === null) return null;
  if (typeof value.can_regenerate !== "boolean") return null;
  if (typeof value.can_delete !== "boolean") return null;

  const response = value.response === null ? null : parseTurnResponse(value.response);
  if (response === null && value.response !== null) return null;

  const activity = value.activity === null ? null : parseTurnActivity(value.activity);
  if (activity === null && value.activity !== null) return null;
  if (activity !== null && activity.turn_id !== turnId) return null;

  const attachments = parseAttachments(value.attachments);
  if (attachments === null) return null;

  return {
    turn_id: turnId,
    turn_index: revision,
    response,
    activity,
    attachments,
    can_regenerate: value.can_regenerate,
    can_delete: value.can_delete,
    regenerate_url: regenerateUrl,
    delete_url: deleteUrl,
  };
}

const SUMMARY_KEYS = [
  "id",
  "title",
  "revision",
  "created_at",
  "updated_at",
  "access",
  "department",
  "page_url",
  "detail_url",
  "messages_url",
] as const;

const THREAD_ACCESS: readonly ThreadAccess[] = [
  "available",
  "quarantined",
  "department_revoked",
];

/** Validate one department label, used by threads, memberships and grants. */
export function parseDepartmentLabel(value: unknown): DepartmentLabel | null {
  if (!isRecord(value) || !hasExactKeys(value, ["id", "code", "name"])) return null;
  const id = parseUuid(value.id);
  if (id === null) return null;
  if (typeof value.code !== "string" || value.code.length === 0) return null;
  if (typeof value.name !== "string" || value.name.length === 0) return null;
  if (value.code.length > 64 || value.name.length > 150) return null;
  return { id, code: value.code, name: value.name };
}

export function parseThreadSummary(value: unknown): ThreadSummary | null {
  if (!isRecord(value) || !hasExactKeys(value, SUMMARY_KEYS)) return null;

  const id = parseUuid(value.id);
  const revision = parseRevision(value.revision);
  const createdAt = parseIsoTimestamp(value.created_at);
  const updatedAt = parseIsoTimestamp(value.updated_at);
  const pageUrl = parseSameOriginPath(value.page_url);
  const detailUrl = parseSameOriginPath(value.detail_url);
  const messagesUrl = parseSameOriginPath(value.messages_url);
  const access = THREAD_ACCESS.find((entry) => entry === value.access);
  if (access === undefined) return null;
  const department =
    value.department === null ? null : parseDepartmentLabel(value.department);
  if (department === null && value.department !== null) return null;
  if (
    id === null ||
    revision === null ||
    createdAt === null ||
    updatedAt === null ||
    pageUrl === null ||
    detailUrl === null ||
    messagesUrl === null ||
    typeof value.title !== "string" ||
    value.title.length === 0
  ) {
    return null;
  }

  return {
    id,
    title: value.title,
    revision,
    created_at: createdAt,
    updated_at: updatedAt,
    access,
    department,
    page_url: pageUrl,
    detail_url: detailUrl,
    messages_url: messagesUrl,
  };
}

/** Validate the sparse per-turn records that accompany a display transcript. */
export function parseTurns(value: unknown, pairCount: number): TurnDetail[] | null {
  if (!Array.isArray(value)) return null;

  const turns: TurnDetail[] = [];
  let previousIndex = -1;
  const seen = new Set<string>();
  for (const entry of value) {
    const turn = parseTurnDetail(entry);
    if (turn === null) return null;
    if (turn.turn_index >= pairCount) return null;
    if (turn.turn_index <= previousIndex) return null;
    if (seen.has(turn.turn_id)) return null;
    seen.add(turn.turn_id);
    previousIndex = turn.turn_index;
    turns.push(turn);
  }
  return turns;
}

export function parseThreadDetail(value: unknown): ThreadDetail | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [...SUMMARY_KEYS, "display", "turns", "conversation_error"])
  ) {
    return null;
  }

  const summary = parseThreadSummary(
    Object.fromEntries(SUMMARY_KEYS.map((key) => [key, value[key]])),
  );
  if (summary === null) return null;

  const display = parseDisplay(value.display);
  if (display === null) return null;

  const turns = parseTurns(value.turns, display.length / 2);
  if (turns === null) return null;

  const conversationError = value.conversation_error;
  if (conversationError !== null && typeof conversationError !== "string") {
    return null;
  }

  return {
    ...summary,
    display,
    turns,
    conversation_error: conversationError,
  };
}

function parseThreadList(value: unknown): ThreadSummary[] | null {
  if (!Array.isArray(value)) return null;
  const threads: ThreadSummary[] = [];
  for (const entry of value) {
    const summary = parseThreadSummary(entry);
    if (summary === null) return null;
    threads.push(summary);
  }
  return threads;
}

function parseBootstrapUser(value: unknown): BootstrapUser | null {
  if (!isRecord(value) || !hasExactKeys(value, ["username", "is_staff"])) {
    return null;
  }
  if (typeof value.username !== "string" || value.username.length === 0) {
    return null;
  }
  if (typeof value.is_staff !== "boolean") return null;
  return { username: value.username, is_staff: value.is_staff };
}

function parseBootstrapUrls(value: unknown): BootstrapUrls | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["threads", "login", "logout", "management"])
  ) {
    return null;
  }
  const threads = parseSameOriginPath(value.threads);
  const login = parseSameOriginPath(value.login);
  const logout = parseSameOriginPath(value.logout);
  const management =
    value.management === null ? null : parseSameOriginPath(value.management);
  if (threads === null || login === null || logout === null) return null;
  if (value.management !== null && management === null) return null;
  return { threads, login, logout, management };
}

const CAPABILITY_NAMES: readonly CapabilityName[] = [
  "manage_users",
  "manage_roles",
  "manage_departments",
  "manage_department_staff",
  "manage_catalog",
  "manage_grants",
  "view_audit",
];

function parseCapabilities(value: unknown): Capabilities | null {
  if (!isRecord(value) || !hasExactKeys(value, CAPABILITY_NAMES)) return null;
  const capabilities = {} as Capabilities;
  for (const name of CAPABILITY_NAMES) {
    const flag = value[name];
    if (typeof flag !== "boolean") return null;
    capabilities[name] = flag;
  }
  return capabilities;
}

export function parseMembershipSummary(value: unknown): MembershipSummary | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["id", "department", "membership_role", "dataset_ids"])
  ) {
    return null;
  }
  const id = parseUuid(value.id);
  const department = parseDepartmentLabel(value.department);
  if (id === null || department === null) return null;
  if (value.membership_role !== "staff" && value.membership_role !== "lead") {
    return null;
  }
  const datasetIds = parseUuidList(value.dataset_ids);
  if (datasetIds === null) return null;
  return {
    id,
    department,
    membership_role: value.membership_role,
    dataset_ids: datasetIds,
  };
}

export function parseUuidList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const entry of value) {
    const id = parseUuid(entry);
    if (id === null || seen.has(id)) return null;
    seen.add(id);
    ids.push(id);
  }
  return ids;
}

function parseMembershipList(value: unknown): MembershipSummary[] | null {
  if (!Array.isArray(value)) return null;
  const memberships: MembershipSummary[] = [];
  for (const entry of value) {
    const membership = parseMembershipSummary(entry);
    if (membership === null) return null;
    memberships.push(membership);
  }
  return memberships;
}

function parseLimitValue(raw: unknown): number | null {
  if (typeof raw !== "number" || !Number.isSafeInteger(raw)) return null;
  if (raw <= 0 || raw > MAX_METRIC) return null;
  return raw;
}

/** Validate the server-owned budgets, including their cross-field rules. */
export function parseChatLimits(value: unknown): ChatLimits | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "max_files_per_thread",
      "max_files_per_turn",
      "max_file_bytes",
      "max_text_bytes_per_file",
      "max_text_bytes_per_thread",
      "context_window",
      "max_output_tokens",
    ])
  ) {
    return null;
  }

  const maxFilesPerThread = parseLimitValue(value.max_files_per_thread);
  const maxFilesPerTurn = parseLimitValue(value.max_files_per_turn);
  const maxFileBytes = parseLimitValue(value.max_file_bytes);
  const maxTextPerFile = parseLimitValue(value.max_text_bytes_per_file);
  const maxTextPerThread = parseLimitValue(value.max_text_bytes_per_thread);
  const contextWindow = parseLimitValue(value.context_window);
  const maxOutputTokens = parseLimitValue(value.max_output_tokens);
  if (
    maxFilesPerThread === null ||
    maxFilesPerTurn === null ||
    maxFileBytes === null ||
    maxTextPerFile === null ||
    maxTextPerThread === null ||
    contextWindow === null ||
    maxOutputTokens === null
  ) {
    return null;
  }
  if (maxFilesPerTurn > maxFilesPerThread) return null;
  if (maxTextPerFile > maxTextPerThread) return null;
  if (maxOutputTokens >= contextWindow) return null;

  return {
    max_files_per_thread: maxFilesPerThread,
    max_files_per_turn: maxFilesPerTurn,
    max_file_bytes: maxFileBytes,
    max_text_bytes_per_file: maxTextPerFile,
    max_text_bytes_per_thread: maxTextPerThread,
    context_window: contextWindow,
    max_output_tokens: maxOutputTokens,
  };
}

const BOOTSTRAP_REQUIRED_KEYS = [
  "user",
  "threads",
  "active_thread",
  "limits",
  "urls",
  "capabilities",
  "memberships",
  "analytics_enabled",
  "analytics_department_ids",
];
const BOOTSTRAP_ALLOWED_KEYS = new Set([
  ...BOOTSTRAP_REQUIRED_KEYS,
  "show_reasoning_detail",
]);

export function parseBootstrap(value: unknown): Bootstrap | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (!BOOTSTRAP_REQUIRED_KEYS.every((k) => k in value)) return null;
  if (!keys.every((k) => BOOTSTRAP_ALLOWED_KEYS.has(k))) return null;

  const user = parseBootstrapUser(value.user);
  const threads = parseThreadList(value.threads);
  const limits = parseChatLimits(value.limits);
  const urls = parseBootstrapUrls(value.urls);
  const capabilities = parseCapabilities(value.capabilities);
  const memberships = parseMembershipList(value.memberships);
  if (typeof value.analytics_enabled !== "boolean") return null;
  if (
    user === null ||
    threads === null ||
    limits === null ||
    urls === null ||
    capabilities === null ||
    memberships === null
  ) {
    return null;
  }
  const analyticsDepartmentIds = parseUuidList(value.analytics_department_ids);
  if (analyticsDepartmentIds === null) return null;
  const departmentIds = new Set(
    memberships.map((membership) => membership.department.id),
  );
  if (analyticsDepartmentIds.some((id) => !departmentIds.has(id))) return null;

  let active: ThreadDetail | null = null;
  if (value.active_thread !== null) {
    active = parseThreadDetail(value.active_thread);
    if (active === null) return null;
  }

  const showReasoningDetail =
    typeof value.show_reasoning_detail === "boolean"
      ? value.show_reasoning_detail
      : undefined;

  return {
    user,
    threads,
    active_thread: active,
    limits,
    urls,
    capabilities,
    memberships,
    analytics_enabled: value.analytics_enabled,
    analytics_department_ids: analyticsDepartmentIds,
    ...(showReasoningDetail !== undefined
      ? { show_reasoning_detail: showReasoningDetail }
      : {}),
  };
}

/** Reject a completed turn whose thread metadata contradicts the request. */
export function assertCompletedThread(
  summary: ThreadSummary,
  expectedId: string,
  expectedRevision: number,
): void {
  if (summary.id !== expectedId || summary.revision !== expectedRevision + 1) {
    throw protocolError();
  }
}

/**
 * Reject a committed turn that does not match the exchange that was requested.
 *
 * Send appends at the next index; regeneration keeps the existing turn id.
 */
export function assertCommittedTurn(
  turn: TurnDetail,
  expected: { turnId: string; turnIndex: number },
): void {
  if (turn.turn_id !== expected.turnId || turn.turn_index !== expected.turnIndex) {
    throw protocolError();
  }
}

/** Order metadata newest-update-first with a deterministic id tie-break. */
export function sortThreads(threads: readonly ThreadSummary[]): ThreadSummary[] {
  return [...threads].sort((left, right) => {
    const byTime = Date.parse(right.updated_at) - Date.parse(left.updated_at);
    if (byTime !== 0) return byTime;
    if (left.id === right.id) return 0;
    return left.id < right.id ? 1 : -1;
  });
}

export function upsertThread(
  threads: readonly ThreadSummary[],
  next: ThreadSummary,
): ThreadSummary[] {
  const replaced = threads.filter((thread) => thread.id !== next.id);
  return sortThreads([...replaced, next]);
}

export function readCsrfToken(): string {
  const input = document.querySelector<HTMLInputElement>(
    'input[name="csrfmiddlewaretoken"]',
  );
  return input?.value ?? "";
}

function parseErrorEnvelope(value: unknown): { code: string; message: string } | null {
  if (!isRecord(value) || !isRecord(value.error)) return null;
  const { code, message } = value.error;
  if (typeof code !== "string" || code.length === 0) return null;
  if (typeof message !== "string" || message.length === 0) return null;
  return { code, message };
}

const STATUS_FALLBACKS: Record<number, { code: string; message: string }> = {
  401: { code: "authentication_required", message: CHAT_AUTH_MESSAGE },
  403: {
    code: "csrf_failed",
    message: "Your page token is missing or expired. Refresh and try again.",
  },
  404: { code: "thread_not_found", message: CHAT_THREAD_MISSING },
  409: {
    code: "thread_conflict",
    message: "This chat changed in another tab. Reload it before trying again.",
  },
};

/** Convert a non-2xx response into a public error without reading HTML text. */
export async function errorFromResponse(response: Response): Promise<ChatApiError> {
  const contentType = (response.headers.get("content-type") ?? "").toLowerCase();
  if (contentType.includes("application/json")) {
    try {
      const payload: unknown = await response.json();
      const envelope = parseErrorEnvelope(payload);
      if (envelope !== null) {
        return new ChatApiError(envelope.code, envelope.message);
      }
    } catch {
      // Fall through to the status-based public message.
    }
  }

  const fallback = STATUS_FALLBACKS[response.status];
  if (fallback !== undefined) {
    return new ChatApiError(fallback.code, fallback.message);
  }
  return protocolError();
}

/** Normalize anything thrown during a chat action into public text and code. */
export function toChatError(caught: unknown): { code: string; message: string } {
  if (caught instanceof ChatApiError) {
    return { code: caught.code, message: caught.message };
  }
  return { code: "unknown_error", message: CHAT_SERVER_ERROR };
}

export type JsonMethod = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

function isFormData(body: unknown): body is FormData {
  return typeof FormData !== "undefined" && body instanceof FormData;
}

function mutationHeaders(body: unknown): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  // Let fetch write the multipart boundary itself.
  if (body !== undefined && !isFormData(body)) {
    headers["Content-Type"] = "application/json";
  }
  const token = readCsrfToken();
  if (token.length > 0) headers["X-CSRFToken"] = token;
  return headers;
}

/** Send one JSON request and return the decoded body or a public error. */
export async function requestJson(
  path: string,
  method: JsonMethod,
  body?: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let encoded: string | undefined;
  if (body !== undefined) {
    encoded = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  if (method !== "GET") {
    const token = readCsrfToken();
    if (token.length > 0) headers["X-CSRFToken"] = token;
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: encoded,
      credentials: "same-origin",
      signal,
    });
  } catch {
    if (signal?.aborted) throw new ChatApiError("aborted", CHAT_SERVER_ERROR);
    throw new ChatApiError("network_error", CHAT_SERVER_ERROR);
  }

  if (!response.ok) throw await errorFromResponse(response);

  const contentType = (response.headers.get("content-type") ?? "").toLowerCase();
  if (!contentType.includes("application/json")) throw protocolError();

  try {
    return (await response.json()) as unknown;
  } catch {
    throw protocolError();
  }
}

function parseEnvelope(value: unknown, key: string): unknown {
  if (!isRecord(value) || !hasExactKeys(value, [key])) return null;
  return value[key];
}

export async function fetchThreads(
  threadsUrl: string,
  signal?: AbortSignal,
): Promise<ThreadSummary[]> {
  const payload = await requestJson(threadsUrl, "GET", undefined, signal);
  const threads = parseThreadList(parseEnvelope(payload, "threads"));
  if (threads === null) throw protocolError();
  return sortThreads(threads);
}

export async function fetchThread(
  detailUrl: string,
  signal?: AbortSignal,
): Promise<ThreadDetail> {
  const payload = await requestJson(detailUrl, "GET", undefined, signal);
  const thread = parseThreadDetail(parseEnvelope(payload, "thread"));
  if (thread === null) throw protocolError();
  return thread;
}

export async function createThread(
  threadsUrl: string,
  departmentId: string | null = null,
): Promise<ThreadDetail> {
  const body = departmentId === null ? {} : { department_id: departmentId };
  const payload = await requestJson(threadsUrl, "POST", body);
  const thread = parseThreadDetail(parseEnvelope(payload, "thread"));
  if (thread === null) throw protocolError();
  return thread;
}

export async function renameThread(
  detailUrl: string,
  title: string,
  revision: number,
): Promise<ThreadSummary> {
  const payload = await requestJson(detailUrl, "PATCH", { title, revision });
  const thread = parseThreadSummary(parseEnvelope(payload, "thread"));
  if (thread === null) throw protocolError();
  return thread;
}

export async function deleteThread(detailUrl: string, revision: number): Promise<void> {
  const payload = await requestJson(detailUrl, "DELETE", { revision });
  const ok = isRecord(payload) && hasExactKeys(payload, ["ok"]) && payload.ok === true;
  if (!ok) throw protocolError();
}

/** POST a JSON or form body and return the unread SSE response body. */
async function postStream(
  path: string,
  body: FormData | unknown,
  signal: AbortSignal,
): Promise<Response> {
  const form = isFormData(body) ? body : null;
  const headers = mutationHeaders(body);
  headers.Accept = "text/event-stream";
  if (form === null) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers,
      body: form ?? JSON.stringify(body),
      credentials: "same-origin",
      signal,
    });
  } catch {
    if (signal.aborted) throw new ChatApiError("aborted", CHAT_SERVER_ERROR);
    throw new ChatApiError("network_error", CHAT_SERVER_ERROR);
  }
  return response;
}

export async function postTurn(
  messagesUrl: string,
  message: string,
  revision: number,
  signal: AbortSignal,
  files: readonly File[] = [],
): Promise<Response> {
  if (files.length === 0) {
    return await postStream(messagesUrl, { message, revision }, signal);
  }
  const body = new FormData();
  body.set("message", message);
  body.set("revision", String(revision));
  for (const file of files) body.append("files", file, file.name);
  return await postStream(messagesUrl, body, signal);
}

/** Request a replacement answer for the latest exchange, unread. */
export async function regenerateTurn(
  regenerateUrl: string,
  revision: number,
  signal: AbortSignal,
): Promise<Response> {
  return await postStream(regenerateUrl, { revision }, signal);
}

/** Remove the latest exchange and return the authoritative thread detail. */
export async function deleteTurn(
  deleteUrl: string,
  revision: number,
  signal: AbortSignal,
): Promise<ThreadDetail> {
  const payload = await requestJson(deleteUrl, "DELETE", { revision }, signal);
  const thread = parseThreadDetail(parseEnvelope(payload, "thread"));
  if (thread === null) throw protocolError();
  return thread;
}
