// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ChatApiError } from "./api";
import {
  createManagedUser,
  fetchAudit,
  fetchCatalogStatus,
  fetchDatasets,
  fetchDepartmentDatasets,
  fetchDepartments,
  fetchManagedUsers,
  fetchMemberDatasets,
  parseManagementBootstrap,
  patchDataset,
  putDepartmentDatasets,
  putUserMemberships,
  refreshCatalog,
} from "./management-api";
import { installDomShims } from "./test-support";

beforeAll(() => installDomShims());

afterEach(() => {
  vi.unstubAllGlobals();
});

const URLS = {
  me: "/api/me/",
  self_departments: "/api/me/departments/",
  self_datasets: "/api/me/datasets/",
  users: "/api/admin/users/",
  departments: "/api/admin/departments/",
  roles: "/api/admin/roles/",
  audit: "/api/admin/audit/",
  datasets: "/api/admin/datasets/",
  chat: "/",
};

const BOOTSTRAP = {
  user: { id: 1, username: "root", first_name: "", last_name: "" },
  capabilities: {
    manage_users: true,
    manage_roles: true,
    manage_departments: true,
    manage_department_staff: true,
    manage_catalog: true,
    manage_grants: true,
    view_audit: true,
  },
  section: "users",
  revision: 7,
  epoch: 3,
  urls: URLS,
};

const DEPARTMENT = {
  id: "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  code: "finance",
  name: "Finance",
  description: "",
  is_active: true,
  unavailable_dataset_count: 0,
  created_at: "2026-01-02T03:04:05.000Z",
  updated_at: "2026-01-02T03:04:05.000Z",
};

const USER = {
  id: 5,
  username: "ada",
  first_name: "Ada",
  last_name: "Lovelace",
  email: "ada@example.test",
  is_active: true,
  roles: ["AYCE Staff"],
  memberships: [
    {
      department: { id: DEPARTMENT.id, code: "finance", name: "Finance" },
      membership_role: "staff",
      is_active: true,
    },
  ],
  memberships_detail: [
    {
      department: { id: DEPARTMENT.id, code: "finance", name: "Finance" },
      membership_role: "staff",
      is_active: true,
      dataset_ids: [],
    },
  ],
};

const DATASET = {
  id: "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
  code: "orders",
  display_name: "Orders",
  description: "",
  source_key: "wren-0123456789abcdef",
  project_key: "jaffle-wren",
  model_name: "orders",
  catalog: "jaffle_shop",
  schema: "main",
  physical_table: "orders",
  is_active: true,
  catalog_version: "a".repeat(64),
  validation_status: "metadata_discovered",
  validated_at: "2026-01-02T03:04:05.000Z",
  department_ids: [],
  availability: "available",
};

const DEPARTMENT_DATASET = {
  id: DATASET.id,
  display_name: "Orders",
  availability: "needs_review",
};
const CATALOG_STATUS = {
  configured: true,
  last_attempt_at: "2026-01-02T03:04:05.000Z",
  last_success_at: "2026-01-02T03:04:05.000Z",
  error_code: null,
};

const CATALOG_REFRESH = {
  created: 1,
  updated: 2,
  missing: 3,
  review_required: 4,
};

const AUDIT = {
  id: "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5e",
  action: "user.created",
  actor: "root",
  target_type: "user",
  target_id: "5",
  allowed: true,
  reason_code: "accepted",
  before: {},
  after: { username: "ada" },
  created_at: "2026-01-02T03:04:05.000Z",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function listPage(item: unknown) {
  return {
    items: [item],
    page: 1,
    page_size: 25,
    total: 1,
    revision: 7,
    epoch: 3,
  };
}

function stubFetch(route: (url: string, init?: RequestInit) => Response) {
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = init?.body;
      calls.push({
        url: String(input),
        method: (init?.method ?? "GET").toUpperCase(),
        body: typeof raw === "string" ? JSON.parse(raw) : undefined,
      });
      return route(String(input), init);
    }),
  );
  return calls;
}

describe("parseManagementBootstrap", () => {
  it("accepts the management payload", () => {
    expect(parseManagementBootstrap(BOOTSTRAP)).toEqual(BOOTSTRAP);
  });

  it("rejects missing keys, extra keys and foreign urls", () => {
    const cases: unknown[] = [
      null,
      { ...BOOTSTRAP, capabilities: undefined },
      { ...BOOTSTRAP, revision: -1 },
      { ...BOOTSTRAP, section: "secrets" },
      { ...BOOTSTRAP, extra: true },
      { ...BOOTSTRAP, urls: { ...URLS, users: "//evil.test/" } },
    ];
    for (const payload of cases) {
      expect(parseManagementBootstrap(payload)).toBeNull();
    }
  });
});

describe("management operations", () => {
  it("queries and parses a user page", async () => {
    const calls = stubFetch(() => json(200, listPage(USER)));
    const page = await fetchManagedUsers(URLS, { q: "ada", page: 2, page_size: 10 });
    expect(page.items[0]?.username).toBe("ada");
    expect(page.revision).toBe(7);
    expect(calls[0]?.url).toBe("/api/admin/users/?q=ada&page=2&page_size=10");
  });

  it("rejects a malformed user row", async () => {
    stubFetch(() => json(200, listPage({ ...USER, roles: ["Unknown Role"] })));
    await expect(fetchManagedUsers(URLS, {})).rejects.toBeInstanceOf(ChatApiError);
  });

  it("creates a user with the acting revision", async () => {
    const calls = stubFetch(() => json(201, { user: USER, revision: 8, epoch: 4 }));
    const saved = await createManagedUser(
      URLS,
      {
        username: "ada",
        first_name: "Ada",
        last_name: "Lovelace",
        email: "ada@example.test",
        password: "manage-pass-7f3a",
      },
      7,
    );
    expect(saved.revision).toBe(8);
    expect(calls[0]?.method).toBe("POST");
    expect(calls[0]?.body).toMatchObject({ revision: 7, username: "ada" });
  });

  it("replaces memberships with the complete array", async () => {
    const calls = stubFetch(() => json(200, { user: USER, revision: 8, epoch: 4 }));
    await putUserMemberships(
      URLS,
      5,
      [
        {
          department_id: DEPARTMENT.id,
          membership_role: "staff",
          is_active: true,
          dataset_ids: [],
        },
      ],
      7,
    );
    expect(calls[0]?.url).toBe("/api/admin/users/5/departments/");
    expect(calls[0]?.body).toMatchObject({
      revision: 7,
      memberships: [
        {
          department_id: DEPARTMENT.id,
          membership_role: "staff",
          is_active: true,
          dataset_ids: [],
        },
      ],
    });
  });

  it("loads status and refreshes from the configured Wren catalog", async () => {
    const calls = stubFetch((url) =>
      url.endsWith("catalog-status/")
        ? json(200, { result: CATALOG_STATUS, revision: 7, epoch: 3 })
        : json(200, { result: CATALOG_REFRESH, revision: 8, epoch: 4 }),
    );
    const status = await fetchCatalogStatus(URLS);
    expect(status.value).toEqual(CATALOG_STATUS);
    expect(calls[0]?.url).toBe("/api/admin/datasets/catalog-status/");
    expect(calls[0]?.method).toBe("GET");

    const refreshed = await refreshCatalog(URLS, 7);
    expect(refreshed.value).toEqual(CATALOG_REFRESH);
    expect(calls[1]?.url).toBe("/api/admin/datasets/refresh/");
    expect(calls[1]?.body).toEqual({ revision: 7 });
  });

  it("parses minimal department grants and submits explicit confirmations", async () => {
    const calls = stubFetch((_url, init) => {
      if ((init?.method ?? "GET") === "PUT") {
        return json(200, {
          result: { department: DEPARTMENT, dataset_ids: [DATASET.id] },
          revision: 8,
          epoch: 4,
        });
      }
      return json(200, {
        department: DEPARTMENT,
        datasets: [DEPARTMENT_DATASET],
        revision: 7,
        epoch: 3,
      });
    });
    const grants = await fetchDepartmentDatasets(URLS, DEPARTMENT.id);
    expect(grants.datasets).toEqual([DEPARTMENT_DATASET]);
    await putDepartmentDatasets(URLS, DEPARTMENT.id, [DATASET.id], 7, [DATASET.id]);
    expect(calls[1]?.body).toEqual({
      revision: 7,
      dataset_ids: [DATASET.id],
      confirmed_dataset_ids: [DATASET.id],
    });
  });

  it("rejects malformed status and expanded department grant records", async () => {
    stubFetch(() =>
      json(200, {
        result: { ...CATALOG_STATUS, unexpected: true },
        revision: 7,
        epoch: 3,
      }),
    );
    await expect(fetchCatalogStatus(URLS)).rejects.toBeInstanceOf(ChatApiError);

    stubFetch(() =>
      json(200, {
        department: DEPARTMENT,
        datasets: [{ ...DEPARTMENT_DATASET, source_key: "foreign" }],
        revision: 7,
        epoch: 3,
      }),
    );
    await expect(fetchDepartmentDatasets(URLS, DEPARTMENT.id)).rejects.toBeInstanceOf(
      ChatApiError,
    );
  });

  it("preserves permission_denied instead of inventing csrf_failed", async () => {
    stubFetch(() =>
      json(403, {
        error: {
          code: "permission_denied",
          message: "You do not have permission to perform this action.",
        },
      }),
    );
    await expect(fetchDepartments(URLS, {})).rejects.toMatchObject({
      code: "permission_denied",
    });
  });

  it("parses datasets, memberships and audit records", async () => {
    stubFetch((url) => {
      if (url.startsWith("/api/admin/datasets/")) {
        return json(200, listPage(DATASET));
      }
      if (url.includes("/members/")) {
        return json(200, {
          membership: {
            id: "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5e",
            department: { id: DEPARTMENT.id, code: "finance", name: "Finance" },
            user: { id: 5, username: "ada", first_name: "", last_name: "" },
            membership_role: "staff",
            dataset_ids: [],
          },
          revision: 7,
          epoch: 3,
        });
      }
      return json(200, listPage(AUDIT));
    });
    expect((await fetchDatasets(URLS, {})).items[0]?.code).toBe("orders");
    expect(
      (await fetchMemberDatasets(URLS, DEPARTMENT.id, 5)).value.dataset_ids,
    ).toEqual([]);
    expect((await fetchAudit(URLS, {})).items[0]?.action).toBe("user.created");
  });

  it("patches only the local enabled state", async () => {
    const calls = stubFetch(() =>
      json(200, { dataset: DATASET, revision: 8, epoch: 4 }),
    );
    await patchDataset(URLS, DATASET.id, { is_active: false }, 7);
    expect(calls[0]?.body).toEqual({ revision: 7, is_active: false });
  });
});
