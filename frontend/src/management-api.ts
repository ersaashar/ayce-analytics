/**
 * Management contracts and operations. Responses are parsed strictly before
 * React state; transport reuses the chat client's fetch/CSRF/error plumbing.
 */

import {
  type Capabilities,
  type CapabilityName,
  ChatApiError,
  type DepartmentLabel,
  hasExactKeys,
  isRecord,
  parseDepartmentLabel,
  parseIsoTimestamp,
  parseRevision,
  parseSameOriginPath,
  parseUuid,
  parseUuidList,
  requestJson,
} from "./api";

export type ManagementSection =
  | "users"
  | "departments"
  | "datasets"
  | "roles"
  | "audit";

export type ManagementUrls = {
  me: string;
  self_departments: string;
  self_datasets: string;
  users: string;
  departments: string;
  roles: string;
  audit: string;
  datasets: string;
  chat: string;
};

export type ManagementBootstrap = {
  user: { id: number; username: string; first_name: string; last_name: string };
  capabilities: Capabilities;
  section: ManagementSection;
  revision: number;
  epoch: number;
  urls: ManagementUrls;
};

export type MembershipRole = "staff" | "lead";

export type MembershipChip = {
  department: DepartmentLabel;
  membership_role: MembershipRole;
  is_active: boolean;
};

export type MembershipDetail = MembershipChip & { dataset_ids: string[] };

export type ManagedUser = {
  id: number;
  username: string;
  first_name: string;
  last_name: string;
  email: string;
  is_active: boolean;
  roles: string[];
  memberships: MembershipChip[];
  memberships_detail?: MembershipDetail[];
};

export type Department = {
  id: string;
  code: string;
  name: string;
  description: string;
  is_active: boolean;
  unavailable_dataset_count: number;
  created_at: string;
  updated_at: string;
};

export type RosterMember = {
  user: { id: number; username: string; first_name: string; last_name: string };
  membership_role: MembershipRole;
  is_active: boolean;
  dataset_ids: string[];
};

export type DatasetAvailability =
  | "available"
  | "disabled"
  | "missing"
  | "needs_review"
  | "different_source";

export type DatasetRecord = {
  id: string;
  code: string;
  display_name: string;
  description: string;
  source_key: string;
  project_key: string;
  model_name: string;
  catalog: string;
  schema: string;
  physical_table: string;
  is_active: boolean;
  catalog_version: string;
  validation_status: "unverified" | "metadata_discovered";
  validated_at: string | null;
  department_ids: string[];
  availability: DatasetAvailability;
};

export type AuditRecord = {
  id: string;
  action: string;
  actor: string | null;
  target_type: string;
  target_id: string;
  allowed: boolean;
  reason_code: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  created_at: string;
};

export type RoleDefinition = {
  name: string;
  capabilities: CapabilityName[];
  uses_analytics: boolean;
};

export type Candidate = {
  id: number;
  username: string;
  first_name: string;
  last_name: string;
};

export type DepartmentDatasetRecord = {
  id: string;
  display_name: string;
  availability: DatasetAvailability;
};

export type CatalogStatus = {
  configured: boolean;
  last_attempt_at: string | null;
  last_success_at: string | null;
  error_code:
    | "connection_failed"
    | "invalid_metadata"
    | "unsupported_metadata"
    | "apply_conflict"
    | null;
};

export type CatalogRefreshResult = {
  created: number;
  updated: number;
  missing: number;
  review_required: number;
};

export type ListPage<T> = {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  revision: number;
  epoch: number;
};

export type EntityEnvelope<T> = { value: T; revision: number; epoch: number };

const ROLE_LABELS = ["AYCE Superadmin", "AYCE Lead", "AYCE Staff"] as const;
const CAPABILITY_KEYS: readonly CapabilityName[] = [
  "manage_users",
  "manage_roles",
  "manage_departments",
  "manage_department_staff",
  "manage_catalog",
  "manage_grants",
  "view_audit",
];
const MEMBERSHIP_ROLES: readonly MembershipRole[] = ["staff", "lead"];
const VALIDATION_STATUSES = ["unverified", "metadata_discovered"] as const;
const DATASET_AVAILABILITIES: readonly DatasetAvailability[] = [
  "available",
  "disabled",
  "missing",
  "needs_review",
  "different_source",
];
const SECTIONS: readonly ManagementSection[] = [
  "users",
  "departments",
  "datasets",
  "roles",
  "audit",
];

function protocolError(): ChatApiError {
  return new ChatApiError(
    "protocol_error",
    "The server sent an unexpected response. Refresh and try again.",
  );
}

function nonEmptyString(value: unknown, max = 4000): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  return value.length > max ? null : value;
}

function parseIdentifier(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) return null;
  return value;
}

function parseStateEnvelope(
  value: unknown,
  extraKeys: readonly string[],
): Record<string, unknown> | null {
  if (!isRecord(value) || !hasExactKeys(value, [...extraKeys, "revision", "epoch"])) {
    return null;
  }
  if (parseRevision(value.revision) === null) return null;
  if (parseRevision(value.epoch) === null) return null;
  return value;
}

function stateOf(value: Record<string, unknown>): { revision: number; epoch: number } {
  return { revision: value.revision as number, epoch: value.epoch as number };
}

function parseListPage<T>(
  value: unknown,
  parseItem: (entry: unknown) => T | null,
): ListPage<T> | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["items", "page", "page_size", "total", "revision", "epoch"])
  ) {
    return null;
  }
  const page = parseIdentifier(value.page);
  const pageSize = parseIdentifier(value.page_size);
  const total = parseRevision(value.total);
  const revision = parseRevision(value.revision);
  const epoch = parseRevision(value.epoch);
  if (page === null || pageSize === null || total === null) return null;
  if (revision === null || epoch === null) return null;
  if (pageSize > 100) return null;
  if (!Array.isArray(value.items)) return null;

  const items: T[] = [];
  for (const entry of value.items) {
    const item = parseItem(entry);
    if (item === null) return null;
    items.push(item);
  }
  if (items.length > pageSize) return null;
  return { items, page, page_size: pageSize, total, revision, epoch };
}

function parseCapabilities(value: unknown): Capabilities | null {
  if (!isRecord(value) || !hasExactKeys(value, CAPABILITY_KEYS)) return null;
  const capabilities = {} as Capabilities;
  for (const name of CAPABILITY_KEYS) {
    if (typeof value[name] !== "boolean") return null;
    capabilities[name] = value[name];
  }
  return capabilities;
}

function parseUrls(value: unknown): ManagementUrls | null {
  const keys = [
    "me",
    "self_departments",
    "self_datasets",
    "users",
    "departments",
    "roles",
    "audit",
    "datasets",
    "chat",
  ];
  if (!isRecord(value) || !hasExactKeys(value, keys)) return null;
  const urls = {} as ManagementUrls;
  for (const key of keys) {
    const path = parseSameOriginPath(value[key]);
    if (path === null) return null;
    urls[key as keyof ManagementUrls] = path;
  }
  return urls;
}

export function parseManagementBootstrap(value: unknown): ManagementBootstrap | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "user",
      "capabilities",
      "section",
      "revision",
      "epoch",
      "urls",
    ])
  ) {
    return null;
  }
  const user = value.user;
  if (
    !isRecord(user) ||
    !hasExactKeys(user, ["id", "username", "first_name", "last_name"])
  ) {
    return null;
  }
  const id = parseIdentifier(user.id);
  const username = nonEmptyString(user.username, 150);
  const capabilities = parseCapabilities(value.capabilities);
  const section = SECTIONS.find((entry) => entry === value.section);
  const revision = parseRevision(value.revision);
  const epoch = parseRevision(value.epoch);
  const urls = parseUrls(value.urls);
  if (
    id === null ||
    username === null ||
    capabilities === null ||
    section === undefined ||
    revision === null ||
    epoch === null ||
    urls === null ||
    typeof user.first_name !== "string" ||
    typeof user.last_name !== "string"
  ) {
    return null;
  }
  return {
    user: {
      id,
      username,
      first_name: user.first_name,
      last_name: user.last_name,
    },
    capabilities,
    section,
    revision,
    epoch,
    urls,
  };
}

function parseMembershipRole(value: unknown): MembershipRole | null {
  return MEMBERSHIP_ROLES.find((entry) => entry === value) ?? null;
}

function parseMembershipChip(value: unknown): MembershipChip | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["department", "membership_role", "is_active"])
  ) {
    return null;
  }
  const department = parseDepartmentLabel(value.department);
  const role = parseMembershipRole(value.membership_role);
  if (department === null || role === null || typeof value.is_active !== "boolean") {
    return null;
  }
  return { department, membership_role: role, is_active: value.is_active };
}

function parseMembershipDetail(value: unknown): MembershipDetail | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["department", "membership_role", "is_active", "dataset_ids"])
  ) {
    return null;
  }
  const chip = parseMembershipChip({
    department: value.department,
    membership_role: value.membership_role,
    is_active: value.is_active,
  });
  const datasetIds = parseUuidList(value.dataset_ids);
  if (chip === null || datasetIds === null) return null;
  return { ...chip, dataset_ids: datasetIds };
}

function parseManagedUser(value: unknown): ManagedUser | null {
  if (!isRecord(value)) return null;
  const keys = [
    "id",
    "username",
    "first_name",
    "last_name",
    "email",
    "is_active",
    "roles",
    "memberships",
  ];
  const allowed = new Set([...keys, "memberships_detail"]);
  if (!keys.every((key) => key in value)) return null;
  if (!Object.keys(value).every((key) => allowed.has(key))) return null;

  const id = parseIdentifier(value.id);
  const username = nonEmptyString(value.username, 150);
  if (id === null || username === null) return null;
  if (typeof value.first_name !== "string" || typeof value.last_name !== "string") {
    return null;
  }
  if (typeof value.email !== "string" || typeof value.is_active !== "boolean") {
    return null;
  }
  if (!Array.isArray(value.roles)) return null;
  const roles: string[] = [];
  for (const role of value.roles) {
    const label = ROLE_LABELS.find((entry) => entry === role);
    if (label === undefined) return null;
    roles.push(label);
  }
  if (!Array.isArray(value.memberships)) return null;
  const memberships: MembershipChip[] = [];
  for (const entry of value.memberships) {
    const chip = parseMembershipChip(entry);
    if (chip === null) return null;
    memberships.push(chip);
  }
  let detail: MembershipDetail[] | undefined;
  if ("memberships_detail" in value) {
    if (!Array.isArray(value.memberships_detail)) return null;
    detail = [];
    for (const entry of value.memberships_detail) {
      const row = parseMembershipDetail(entry);
      if (row === null) return null;
      detail.push(row);
    }
  }
  return {
    id,
    username,
    first_name: value.first_name,
    last_name: value.last_name,
    email: value.email,
    is_active: value.is_active,
    roles,
    memberships,
    memberships_detail: detail,
  };
}

function parseDepartment(value: unknown): Department | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "id",
      "code",
      "name",
      "description",
      "is_active",
      "unavailable_dataset_count",
      "created_at",
      "updated_at",
    ])
  ) {
    return null;
  }
  const id = parseUuid(value.id);
  const code = nonEmptyString(value.code, 64);
  const name = nonEmptyString(value.name, 150);
  const unavailableDatasetCount = parseRevision(value.unavailable_dataset_count);
  const createdAt = parseIsoTimestamp(value.created_at);
  const updatedAt = parseIsoTimestamp(value.updated_at);
  if (
    id === null ||
    code === null ||
    name === null ||
    unavailableDatasetCount === null ||
    createdAt === null ||
    updatedAt === null ||
    typeof value.description !== "string" ||
    typeof value.is_active !== "boolean"
  ) {
    return null;
  }
  return {
    id,
    code,
    name,
    description: value.description,
    is_active: value.is_active,
    unavailable_dataset_count: unavailableDatasetCount,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

function parseRosterMember(value: unknown): RosterMember | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["user", "membership_role", "is_active", "dataset_ids"])
  ) {
    return null;
  }
  const user = value.user;
  if (
    !isRecord(user) ||
    !hasExactKeys(user, ["id", "username", "first_name", "last_name"])
  ) {
    return null;
  }
  const id = parseIdentifier(user.id);
  const username = nonEmptyString(user.username, 150);
  const role = parseMembershipRole(value.membership_role);
  const datasetIds = parseUuidList(value.dataset_ids);
  if (id === null || username === null || role === null || datasetIds === null) {
    return null;
  }
  if (typeof user.first_name !== "string" || typeof user.last_name !== "string") {
    return null;
  }
  if (typeof value.is_active !== "boolean") return null;
  return {
    user: {
      id,
      username,
      first_name: user.first_name,
      last_name: user.last_name,
    },
    membership_role: role,
    is_active: value.is_active,
    dataset_ids: datasetIds,
  };
}

function parseDataset(value: unknown): DatasetRecord | null {
  const keys = [
    "id",
    "code",
    "display_name",
    "description",
    "source_key",
    "project_key",
    "model_name",
    "catalog",
    "schema",
    "physical_table",
    "is_active",
    "catalog_version",
    "validation_status",
    "validated_at",
    "department_ids",
    "availability",
  ];
  if (!isRecord(value) || !hasExactKeys(value, keys)) return null;
  const id = parseUuid(value.id);
  const code = nonEmptyString(value.code, 64);
  const displayName = nonEmptyString(value.display_name, 150);
  const sourceKey = nonEmptyString(value.source_key, 64);
  const projectKey = nonEmptyString(value.project_key, 128);
  const modelName = nonEmptyString(value.model_name, 128);
  const availability =
    DATASET_AVAILABILITIES.find((entry) => entry === value.availability) ?? null;
  const status = VALIDATION_STATUSES.find((entry) => entry === value.validation_status);
  const departmentIds = parseUuidList(value.department_ids);
  const validatedAt =
    value.validated_at === null ? null : parseIsoTimestamp(value.validated_at);
  if (
    id === null ||
    code === null ||
    displayName === null ||
    sourceKey === null ||
    projectKey === null ||
    modelName === null ||
    availability === null ||
    status === undefined ||
    departmentIds === null ||
    (validatedAt === null && value.validated_at !== null) ||
    typeof value.description !== "string" ||
    typeof value.catalog !== "string" ||
    typeof value.schema !== "string" ||
    typeof value.physical_table !== "string" ||
    typeof value.catalog_version !== "string" ||
    typeof value.is_active !== "boolean"
  ) {
    return null;
  }
  return {
    id,
    code,
    display_name: displayName,
    description: value.description,
    source_key: sourceKey,
    project_key: projectKey,
    model_name: modelName,
    catalog: value.catalog,
    schema: value.schema,
    physical_table: value.physical_table,
    is_active: value.is_active,
    catalog_version: value.catalog_version,
    validation_status: status,
    validated_at: validatedAt,
    department_ids: departmentIds,
    availability,
  };
}

function parseDepartmentDataset(value: unknown): DepartmentDatasetRecord | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["id", "display_name", "availability"])
  ) {
    return null;
  }
  const id = parseUuid(value.id);
  const displayName = nonEmptyString(value.display_name, 150);
  const availability =
    DATASET_AVAILABILITIES.find((entry) => entry === value.availability) ?? null;
  if (id === null || displayName === null || availability === null) return null;
  return { id, display_name: displayName, availability };
}

function parseCatalogStatus(value: unknown): CatalogStatus | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "configured",
      "last_attempt_at",
      "last_success_at",
      "error_code",
    ])
  ) {
    return null;
  }
  const lastAttemptAt =
    value.last_attempt_at === null ? null : parseIsoTimestamp(value.last_attempt_at);
  const lastSuccessAt =
    value.last_success_at === null ? null : parseIsoTimestamp(value.last_success_at);
  const errorCodes = [
    "connection_failed",
    "invalid_metadata",
    "unsupported_metadata",
    "apply_conflict",
  ] as const;
  const errorCode =
    value.error_code === null
      ? null
      : errorCodes.find((entry) => entry === value.error_code);
  if (
    typeof value.configured !== "boolean" ||
    (lastAttemptAt === null && value.last_attempt_at !== null) ||
    (lastSuccessAt === null && value.last_success_at !== null) ||
    (errorCode === undefined && value.error_code !== null)
  ) {
    return null;
  }
  return {
    configured: value.configured,
    last_attempt_at: lastAttemptAt,
    last_success_at: lastSuccessAt,
    error_code: errorCode ?? null,
  };
}

function parseCatalogRefreshResult(value: unknown): CatalogRefreshResult | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["created", "updated", "missing", "review_required"])
  ) {
    return null;
  }
  const created = parseRevision(value.created);
  const updated = parseRevision(value.updated);
  const missing = parseRevision(value.missing);
  const reviewRequired = parseRevision(value.review_required);
  if (
    created === null ||
    updated === null ||
    missing === null ||
    reviewRequired === null
  ) {
    return null;
  }
  return {
    created,
    updated,
    missing,
    review_required: reviewRequired,
  };
}

function parseAudit(value: unknown): AuditRecord | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "id",
      "action",
      "actor",
      "target_type",
      "target_id",
      "allowed",
      "reason_code",
      "before",
      "after",
      "created_at",
    ])
  ) {
    return null;
  }
  const id = parseUuid(value.id);
  const createdAt = parseIsoTimestamp(value.created_at);
  const action = nonEmptyString(value.action, 64);
  const targetType = nonEmptyString(value.target_type, 32);
  const reason = nonEmptyString(value.reason_code, 64);
  if (
    id === null ||
    createdAt === null ||
    action === null ||
    targetType === null ||
    reason === null ||
    (typeof value.actor !== "string" && value.actor !== null) ||
    typeof value.allowed !== "boolean" ||
    typeof value.target_id !== "string" ||
    !isRecord(value.before) ||
    !isRecord(value.after)
  ) {
    return null;
  }
  return {
    id,
    action,
    actor: value.actor,
    target_type: targetType,
    target_id: value.target_id,
    allowed: value.allowed,
    reason_code: reason,
    before: value.before,
    after: value.after,
    created_at: createdAt,
  };
}

function parseRoleDefinition(value: unknown): RoleDefinition | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["name", "capabilities", "uses_analytics"])
  ) {
    return null;
  }
  const name = ROLE_LABELS.find((entry) => entry === value.name);
  if (name === undefined || typeof value.uses_analytics !== "boolean") return null;
  if (!Array.isArray(value.capabilities)) return null;
  const capabilities: CapabilityName[] = [];
  for (const entry of value.capabilities) {
    const capability = CAPABILITY_KEYS.find((name) => name === entry);
    if (capability === undefined) return null;
    capabilities.push(capability);
  }
  return { name, capabilities, uses_analytics: value.uses_analytics };
}

function parseCandidate(value: unknown): Candidate | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["id", "username", "first_name", "last_name"])
  ) {
    return null;
  }
  const id = parseIdentifier(value.id);
  const username = nonEmptyString(value.username, 150);
  if (id === null || username === null) return null;
  if (typeof value.first_name !== "string" || typeof value.last_name !== "string") {
    return null;
  }
  return {
    id,
    username,
    first_name: value.first_name,
    last_name: value.last_name,
  };
}

function childUrl(base: string, id: string | number, suffix = ""): string {
  return `${base}${encodeURIComponent(String(id))}/${suffix}`;
}

type ListQuery = {
  q?: string;
  page?: number;
  page_size?: number;
  department_id?: string;
};

function queryString(query: ListQuery): string {
  const params = new URLSearchParams();
  if (query.q !== undefined && query.q.length > 0) params.set("q", query.q);
  if (query.page !== undefined) params.set("page", String(query.page));
  if (query.page_size !== undefined) params.set("page_size", String(query.page_size));
  if (query.department_id !== undefined) {
    params.set("department_id", query.department_id);
  }
  const encoded = params.toString();
  return encoded.length > 0 ? `?${encoded}` : "";
}

function entityOf<T>(
  payload: unknown,
  key: string,
  parse: (value: unknown) => T | null,
): EntityEnvelope<T> {
  const record = parseStateEnvelope(payload, [key]);
  if (record === null) throw protocolError();
  const entity = parse(record[key]);
  if (entity === null) throw protocolError();
  return { value: entity, ...stateOf(record) };
}

function pageOf<T>(payload: unknown, parse: (value: unknown) => T | null): ListPage<T> {
  const page = parseListPage(payload, parse);
  if (page === null) throw protocolError();
  return page;
}

export async function fetchManagedUsers(
  urls: ManagementUrls,
  query: ListQuery,
  signal?: AbortSignal,
): Promise<ListPage<ManagedUser>> {
  const payload = await requestJson(
    `${urls.users}${queryString(query)}`,
    "GET",
    undefined,
    signal,
  );
  return pageOf(payload, parseManagedUser);
}

export async function createManagedUser(
  urls: ManagementUrls,
  input: {
    username: string;
    first_name: string;
    last_name: string;
    email: string;
    password: string;
  },
  revision: number,
): Promise<EntityEnvelope<ManagedUser>> {
  const payload = await requestJson(urls.users, "POST", { revision, ...input });
  return entityOf(payload, "user", parseManagedUser);
}

export async function fetchManagedUser(
  urls: ManagementUrls,
  userId: number,
  signal?: AbortSignal,
): Promise<EntityEnvelope<ManagedUser>> {
  const payload = await requestJson(
    childUrl(urls.users, userId),
    "GET",
    undefined,
    signal,
  );
  return entityOf(payload, "user", parseManagedUser);
}

export async function patchManagedUser(
  urls: ManagementUrls,
  userId: number,
  changes: {
    first_name?: string;
    last_name?: string;
    email?: string;
    is_active?: boolean;
  },
  revision: number,
): Promise<EntityEnvelope<ManagedUser>> {
  const payload = await requestJson(childUrl(urls.users, userId), "PATCH", {
    revision,
    ...changes,
  });
  return entityOf(payload, "user", parseManagedUser);
}

export async function resetManagedPassword(
  urls: ManagementUrls,
  userId: number,
  body: { current_password: string; new_password: string },
  revision: number,
): Promise<EntityEnvelope<ManagedUser>> {
  const payload = await requestJson(
    childUrl(urls.users, userId, "reset-password/"),
    "POST",
    { revision, ...body },
  );
  return entityOf(payload, "user", parseManagedUser);
}

export async function setManagedRole(
  urls: ManagementUrls,
  userId: number,
  role: "staff" | "superadmin",
  revision: number,
): Promise<EntityEnvelope<ManagedUser>> {
  const payload = await requestJson(childUrl(urls.users, userId, "roles/"), "PUT", {
    revision,
    role,
  });
  return entityOf(payload, "user", parseManagedUser);
}

export async function fetchUserMemberships(
  urls: ManagementUrls,
  userId: number,
  signal?: AbortSignal,
): Promise<EntityEnvelope<ManagedUser>> {
  const payload = await requestJson(
    childUrl(urls.users, userId, "departments/"),
    "GET",
    undefined,
    signal,
  );
  return entityOf(payload, "user", parseManagedUser);
}

export type MembershipSubmission = {
  department_id: string;
  membership_role: MembershipRole;
  is_active: boolean;
  dataset_ids: string[];
};

export async function putUserMemberships(
  urls: ManagementUrls,
  userId: number,
  memberships: MembershipSubmission[],
  revision: number,
): Promise<EntityEnvelope<ManagedUser>> {
  const payload = await requestJson(
    childUrl(urls.users, userId, "departments/"),
    "PUT",
    { revision, memberships },
  );
  return entityOf(payload, "user", parseManagedUser);
}

export async function fetchDepartments(
  urls: ManagementUrls,
  query: ListQuery,
  signal?: AbortSignal,
): Promise<ListPage<Department>> {
  const payload = await requestJson(
    `${urls.departments}${queryString(query)}`,
    "GET",
    undefined,
    signal,
  );
  return pageOf(payload, parseDepartment);
}

export async function createManagedDepartment(
  urls: ManagementUrls,
  input: { name: string; description: string },
  revision: number,
): Promise<EntityEnvelope<Department>> {
  const payload = await requestJson(urls.departments, "POST", { revision, ...input });
  return entityOf(payload, "department", parseDepartment);
}

export async function fetchDepartment(
  urls: ManagementUrls,
  departmentId: string,
  signal?: AbortSignal,
): Promise<EntityEnvelope<Department>> {
  const payload = await requestJson(
    childUrl(urls.departments, departmentId),
    "GET",
    undefined,
    signal,
  );
  return entityOf(payload, "department", parseDepartment);
}

export async function patchDepartment(
  urls: ManagementUrls,
  departmentId: string,
  changes: { name?: string; description?: string; is_active?: boolean },
  revision: number,
): Promise<EntityEnvelope<Department>> {
  const payload = await requestJson(childUrl(urls.departments, departmentId), "PATCH", {
    revision,
    ...changes,
  });
  return entityOf(payload, "department", parseDepartment);
}

export async function fetchDepartmentMembers(
  urls: ManagementUrls,
  departmentId: string,
  signal?: AbortSignal,
): Promise<{
  department: Department;
  members: RosterMember[];
  revision: number;
  epoch: number;
}> {
  const payload = await requestJson(
    childUrl(urls.departments, departmentId, "members/"),
    "GET",
    undefined,
    signal,
  );
  return fetchRosterPayload(payload);
}

export type RosterSubmission = {
  user_id: number;
  membership_role: MembershipRole;
  is_active: boolean;
  dataset_ids: string[];
};

export async function putDepartmentMembers(
  urls: ManagementUrls,
  departmentId: string,
  members: RosterSubmission[],
  revision: number,
): Promise<{
  department: Department;
  members: RosterMember[];
  revision: number;
  epoch: number;
}> {
  const payload = await requestJson(
    childUrl(urls.departments, departmentId, "members/"),
    "PUT",
    { revision, members },
  );
  return fetchRosterPayload(payload);
}

function fetchRosterPayload(payload: unknown): {
  department: Department;
  members: RosterMember[];
  revision: number;
  epoch: number;
} {
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["department", "members", "revision", "epoch"])
  ) {
    throw protocolError();
  }
  const department = parseDepartment(payload.department);
  if (department === null || !Array.isArray(payload.members)) throw protocolError();
  const members = payload.members.map((entry) => {
    const member = parseRosterMember(entry);
    if (member === null) throw protocolError();
    return member;
  });
  return {
    department,
    members,
    revision: payload.revision as number,
    epoch: payload.epoch as number,
  };
}

export async function searchCandidates(
  urls: ManagementUrls,
  departmentId: string,
  query: ListQuery,
  signal?: AbortSignal,
): Promise<ListPage<Candidate>> {
  const payload = await requestJson(
    `${childUrl(urls.departments, departmentId, "member-candidates/")}${queryString(query)}`,
    "GET",
    undefined,
    signal,
  );
  return pageOf(payload, parseCandidate);
}

export async function fetchDepartmentDatasets(
  urls: ManagementUrls,
  departmentId: string,
  signal?: AbortSignal,
): Promise<{
  department: Department;
  datasets: DepartmentDatasetRecord[];
  revision: number;
  epoch: number;
}> {
  const payload = await requestJson(
    childUrl(urls.departments, departmentId, "datasets/"),
    "GET",
    undefined,
    signal,
  );
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["department", "datasets", "revision", "epoch"])
  ) {
    throw protocolError();
  }
  const department = parseDepartment(payload.department);
  if (department === null || !Array.isArray(payload.datasets)) throw protocolError();
  const datasets = payload.datasets.map((entry) => {
    const dataset = parseDepartmentDataset(entry);
    if (dataset === null) throw protocolError();
    return dataset;
  });
  return {
    department,
    datasets,
    revision: payload.revision as number,
    epoch: payload.epoch as number,
  };
}

export async function putDepartmentDatasets(
  urls: ManagementUrls,
  departmentId: string,
  datasetIds: string[],
  revision: number,
  confirmedDatasetIds: string[] = [],
): Promise<EntityEnvelope<{ department: Department; dataset_ids: string[] }>> {
  const payload = await requestJson(
    childUrl(urls.departments, departmentId, "datasets/"),
    "PUT",
    {
      revision,
      dataset_ids: datasetIds,
      confirmed_dataset_ids: confirmedDatasetIds,
    },
  );
  return entityOf(
    payload,
    "result",
    (value): { department: Department; dataset_ids: string[] } | null => {
      if (!isRecord(value) || !hasExactKeys(value, ["department", "dataset_ids"])) {
        return null;
      }
      const department = parseDepartment(value.department);
      const ids = parseUuidList(value.dataset_ids);
      if (department === null || ids === null) return null;
      return { department, dataset_ids: ids };
    },
  );
}

export type MemberDatasetRecord = {
  id: string;
  department: DepartmentLabel;
  user: { id: number; username: string; first_name: string; last_name: string };
  membership_role: MembershipRole;
  dataset_ids: string[];
};

function parseMemberDatasetRecord(value: unknown): MemberDatasetRecord | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["id", "department", "user", "membership_role", "dataset_ids"])
  ) {
    return null;
  }
  const id = parseUuid(value.id);
  const department = parseDepartmentLabel(value.department);
  const role = parseMembershipRole(value.membership_role);
  const ids = parseUuidList(value.dataset_ids);
  const user = value.user;
  if (id === null || department === null || role === null || ids === null) return null;
  if (
    !isRecord(user) ||
    !hasExactKeys(user, ["id", "username", "first_name", "last_name"])
  ) {
    return null;
  }
  const userId = parseIdentifier(user.id);
  const username = nonEmptyString(user.username, 150);
  if (userId === null || username === null) return null;
  if (typeof user.first_name !== "string" || typeof user.last_name !== "string") {
    return null;
  }
  return {
    id,
    department,
    user: {
      id: userId,
      username,
      first_name: user.first_name,
      last_name: user.last_name,
    },
    membership_role: role,
    dataset_ids: ids,
  };
}

export async function fetchMemberDatasets(
  urls: ManagementUrls,
  departmentId: string,
  userId: number,
  signal?: AbortSignal,
): Promise<EntityEnvelope<MemberDatasetRecord>> {
  const payload = await requestJson(
    childUrl(urls.departments, departmentId, `members/${userId}/datasets/`),
    "GET",
    undefined,
    signal,
  );
  return entityOf(payload, "membership", parseMemberDatasetRecord);
}

export async function putMemberDatasets(
  urls: ManagementUrls,
  departmentId: string,
  userId: number,
  datasetIds: string[],
  revision: number,
): Promise<EntityEnvelope<MemberDatasetRecord>> {
  const payload = await requestJson(
    childUrl(urls.departments, departmentId, `members/${userId}/datasets/`),
    "PUT",
    { revision, dataset_ids: datasetIds },
  );
  return entityOf(payload, "membership", parseMemberDatasetRecord);
}

export async function fetchRoles(
  urls: ManagementUrls,
  signal?: AbortSignal,
): Promise<{ roles: RoleDefinition[]; revision: number; epoch: number }> {
  const payload = await requestJson(urls.roles, "GET", undefined, signal);
  if (!isRecord(payload) || !hasExactKeys(payload, ["roles", "revision", "epoch"])) {
    throw protocolError();
  }
  if (!Array.isArray(payload.roles)) throw protocolError();
  const roles = payload.roles.map((entry) => {
    const role = parseRoleDefinition(entry);
    if (role === null) throw protocolError();
    return role;
  });
  return {
    roles,
    revision: payload.revision as number,
    epoch: payload.epoch as number,
  };
}

export async function fetchDatasets(
  urls: ManagementUrls,
  query: ListQuery,
  signal?: AbortSignal,
): Promise<ListPage<DatasetRecord>> {
  const payload = await requestJson(
    `${urls.datasets}${queryString(query)}`,
    "GET",
    undefined,
    signal,
  );
  return pageOf(payload, parseDataset);
}

export async function patchDataset(
  urls: ManagementUrls,
  datasetId: string,
  changes: { is_active: boolean },
  revision: number,
): Promise<EntityEnvelope<DatasetRecord>> {
  const payload = await requestJson(childUrl(urls.datasets, datasetId), "PATCH", {
    revision,
    ...changes,
  });
  return entityOf(payload, "dataset", parseDataset);
}

export async function fetchCatalogStatus(
  urls: ManagementUrls,
  signal?: AbortSignal,
): Promise<EntityEnvelope<CatalogStatus>> {
  const payload = await requestJson(
    `${urls.datasets}catalog-status/`,
    "GET",
    undefined,
    signal,
  );
  return entityOf(payload, "result", parseCatalogStatus);
}

export async function refreshCatalog(
  urls: ManagementUrls,
  revision: number,
  signal?: AbortSignal,
): Promise<EntityEnvelope<CatalogRefreshResult>> {
  const payload = await requestJson(
    `${urls.datasets}refresh/`,
    "POST",
    { revision },
    signal,
  );
  return entityOf(payload, "result", parseCatalogRefreshResult);
}

export async function fetchAudit(
  urls: ManagementUrls,
  query: ListQuery,
  signal?: AbortSignal,
): Promise<ListPage<AuditRecord>> {
  const payload = await requestJson(
    `${urls.audit}${queryString(query)}`,
    "GET",
    undefined,
    signal,
  );
  return pageOf(payload, parseAudit);
}
