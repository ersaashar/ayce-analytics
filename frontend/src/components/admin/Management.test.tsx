// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { Capabilities } from "../../api";
import type {
  ManagementBootstrap,
  ManagementSection,
  ManagementUrls,
} from "../../management-api";
import { installDomShims } from "../../test-support";
import { Management } from "./Management";

beforeAll(() => installDomShims());

afterEach(cleanup);

const URLS: ManagementUrls = {
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

const ALL_CAPABILITIES: Capabilities = {
  manage_users: true,
  manage_roles: true,
  manage_departments: true,
  manage_department_staff: true,
  manage_catalog: true,
  manage_grants: true,
  view_audit: true,
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
  validated_at: null,
  department_ids: [],
  availability: "available",
};

const DEPARTMENT_DATASET = {
  id: DATASET.id,
  display_name: "Orders",
  availability: "available",
};

const NEEDS_REVIEW_DATASET = {
  ...DEPARTMENT_DATASET,
  availability: "needs_review",
};

const MISSING_DATASET = {
  ...DEPARTMENT_DATASET,
  availability: "missing",
};

const USER = {
  id: 5,
  username: "ada",
  first_name: "Ada",
  last_name: "Lovelace",
  email: "",
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

const CATALOG_STATUS = {
  configured: true,
  last_attempt_at: "2026-01-02T03:04:05.000Z",
  last_success_at: "2026-01-02T03:04:05.000Z",
  error_code: null,
};

const CATALOG_REFRESH = {
  created: 1,
  updated: 0,
  missing: 0,
  review_required: 0,
};

function bootstrap(
  section: ManagementSection,
  capabilities: Capabilities = ALL_CAPABILITIES,
): ManagementBootstrap {
  return {
    user: { id: 1, username: "root", first_name: "", last_name: "" },
    capabilities,
    section,
    revision: 7,
    epoch: 3,
    urls: URLS,
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function page(item: unknown, revision = 7, epoch = 3) {
  return {
    items: [item],
    page: 1,
    page_size: 25,
    total: 1,
    revision,
    epoch,
  };
}

function emptyPage(revision = 7, epoch = 3) {
  return {
    items: [],
    page: 1,
    page_size: 25,
    total: 0,
    revision,
    epoch,
  };
}

type Call = { url: string; method: string; body: unknown };

function stubFetch(route: (call: Call) => Response) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = init?.body;
      const call: Call = {
        url: String(input),
        method: (init?.method ?? "GET").toUpperCase(),
        body: typeof raw === "string" ? JSON.parse(raw) : undefined,
      };
      calls.push(call);
      return route(call);
    }),
  );
  return calls;
}

describe("users administration", () => {
  it("lists accounts and start dialogs with Cancel focused", async () => {
    const calls = stubFetch((call) => {
      if (call.method === "GET") return json(200, page(USER));
      return json(201, { user: USER, revision: 8, epoch: 4 });
    });
    render(<Management bootstrap={bootstrap("users")} />);

    expect(await screen.findByText("ada")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "New account" }));

    const dialog = await screen.findByRole("dialog", { name: "New account" });
    await waitFor(() => {
      expect(document.activeElement?.textContent).toBe("Cancel");
    });

    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "Username" }),
      "grace",
    );
    await userEvent.type(
      within(dialog).getByLabelText("Initial password"),
      "manage-pass-7f3a",
    );
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Create account" }),
    );

    await waitFor(() => {
      const created = calls.find((call) => call.method === "POST");
      expect(created?.body).toMatchObject({ revision: 7, username: "grace" });
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("keeps a stale save visible instead of overwriting", async () => {
    stubFetch((call) => {
      if (call.method === "GET") return json(200, page(USER));
      return json(409, {
        error: {
          code: "administration_conflict",
          message: "These settings changed. Reload them before saving.",
        },
      });
    });
    render(<Management bootstrap={bootstrap("users")} />);
    expect(await screen.findByText("ada")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog", { name: "Edit ada" });
    const firstName = within(dialog).getByRole("textbox", { name: "First name" });
    await userEvent.clear(firstName);
    await userEvent.type(firstName, "Grace");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save account" }));

    expect(
      await screen.findByText("These settings changed. Reload them before saving."),
    ).toBeTruthy();
    expect((firstName as HTMLInputElement).value).toBe("Grace");
    expect(screen.getByRole("dialog", { name: "Edit ada" })).toBeTruthy();
  });

  it("clears rows when management capability is lost", async () => {
    stubFetch(() =>
      json(403, {
        error: {
          code: "permission_denied",
          message: "You do not have permission to perform this action.",
        },
      }),
    );
    render(<Management bootstrap={bootstrap("users")} />);
    expect(
      await screen.findByText(/Your account no longer has administration access/),
    ).toBeTruthy();
    expect(screen.queryByText("ada")).toBeNull();
    expect(screen.getByRole("link", { name: "Back to chat" })).toBeTruthy();
  });
});

describe("department creation", () => {
  it("submits a name without a code and retains it on conflict", async () => {
    const calls = stubFetch((call) =>
      call.method === "POST"
        ? json(409, {
            error: {
              code: "department_name_conflict",
              message: "A department with this name already exists.",
            },
          })
        : json(200, page(DEPARTMENT)),
    );
    render(<Management bootstrap={bootstrap("departments")} />);
    await screen.findByText("Finance");
    await userEvent.click(screen.getByRole("button", { name: "New department" }));
    const dialog = await screen.findByRole("dialog", { name: "New department" });
    expect(within(dialog).queryByLabelText("Code")).toBeNull();
    await waitFor(() => {
      expect(document.activeElement?.textContent).toBe("Cancel");
    });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Test");
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Create department" }),
    );
    expect((await within(dialog).findByRole("alert")).textContent).toContain(
      "A department with this name already exists.",
    );
    expect((within(dialog).getByLabelText("Name") as HTMLInputElement).value).toBe(
      "Test",
    );
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      revision: 7,
      name: "Test",
      description: "",
    });
  });
});

describe("Wren catalog", () => {
  it("shows published Wren models and refreshes with the current revision", async () => {
    let revision = 7;
    let refreshed = false;
    const calls = stubFetch((call) => {
      if (call.url.endsWith("/catalog-status/")) {
        return json(200, {
          result: CATALOG_STATUS,
          revision,
          epoch: refreshed ? 4 : 3,
        });
      }
      if (call.url.endsWith("/refresh/")) {
        refreshed = true;
        revision = 8;
        return json(200, {
          result: CATALOG_REFRESH,
          revision,
          epoch: 4,
        });
      }
      if (call.url.startsWith("/api/admin/datasets/")) {
        return json(200, page(DATASET, revision, refreshed ? 4 : 3));
      }
      return json(404, {});
    });
    render(<Management bootstrap={bootstrap("datasets")} />);

    expect(await screen.findByText("Orders")).toBeTruthy();
    expect(screen.getByText("jaffle-wren")).toBeTruthy();
    expect(screen.getByText("jaffle_shop.main · orders")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "New dataset" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Import catalog" })).toBeNull();

    await userEvent.click(
      await screen.findByRole("button", { name: "Refresh from Wren" }),
    );
    await waitFor(() => {
      const refreshCall = calls.find((call) => call.url.endsWith("/refresh/"));
      expect(refreshCall?.body).toEqual({ revision: 7 });
    });
    expect(calls.some((call) => call.url.endsWith("/catalog-status/"))).toBe(true);
    expect(calls.some((call) => call.url.startsWith("/api/admin/datasets/"))).toBe(
      true,
    );
  });

  it("keeps the last successful rows visible during a discovery outage", async () => {
    const outageStatus = {
      ...CATALOG_STATUS,
      error_code: "connection_failed",
    };
    stubFetch((call) =>
      call.url.endsWith("/catalog-status/")
        ? json(200, { result: outageStatus, revision: 7, epoch: 3 })
        : json(200, page(DATASET)),
    );
    render(<Management bootstrap={bootstrap("datasets")} />);

    expect(await screen.findByText("Orders")).toBeTruthy();
    expect(
      screen.getByText("Wren dataset discovery is unavailable. Try refreshing again."),
    ).toBeTruthy();
    expect(
      screen.getByText("Showing the last successful catalog snapshot."),
    ).toBeTruthy();
  });

  it("distinguishes an unconfigured MCP from a successful empty catalog", async () => {
    stubFetch((call) =>
      call.url.endsWith("/catalog-status/")
        ? json(200, {
            result: {
              configured: false,
              last_attempt_at: null,
              last_success_at: null,
              error_code: null,
            },
            revision: 7,
            epoch: 3,
          })
        : json(200, emptyPage()),
    );
    render(<Management bootstrap={bootstrap("datasets")} />);

    expect(await screen.findByText("Wren MCP is not configured.")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Refresh from Wren" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      screen.queryByText("No models are published in this Wren environment."),
    ).toBeNull();
  });

  it("reports a successful empty Wren publication", async () => {
    stubFetch((call) =>
      call.url.endsWith("/catalog-status/")
        ? json(200, { result: CATALOG_STATUS, revision: 7, epoch: 3 })
        : json(200, emptyPage()),
    );
    render(<Management bootstrap={bootstrap("datasets")} />);
    expect(
      await screen.findByText("No models are published in this Wren environment."),
    ).toBeTruthy();
  });
});

describe("membership subsets", () => {
  it("starts empty and never inherits a department's datasets", async () => {
    const calls = stubFetch((call) => {
      if (call.url.startsWith("/api/admin/departments/?")) {
        return json(200, page(DEPARTMENT));
      }
      if (call.url.endsWith("/departments/")) {
        return json(200, { user: USER, revision: 7, epoch: 3 });
      }
      if (call.url.endsWith("/datasets/") && call.method === "GET") {
        return json(200, {
          department: DEPARTMENT,
          datasets: [DEPARTMENT_DATASET],
          revision: 7,
          epoch: 3,
        });
      }
      if (call.method === "PUT") {
        return json(200, { user: USER, revision: 8, epoch: 4 });
      }
      return json(200, page(USER));
    });
    render(<Management bootstrap={bootstrap("users")} />);
    expect(await screen.findByText("ada")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Departments" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Departments for ada",
    });
    expect(await within(dialog).findByText("Finance (finance)")).toBeTruthy();
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Load datasets" }),
    );

    const checkbox = await within(dialog).findByRole("checkbox", {
      name: /Orders/,
    });
    expect((checkbox as HTMLInputElement).checked).toBe(false);
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Save memberships" }),
    );

    await waitFor(() => {
      const saved = calls.find((call) => call.method === "PUT");
      expect(saved?.body).toMatchObject({
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
  });
  it("confirms restoring a reviewed grant and sends its explicit ID", async () => {
    let updated = false;
    const currentDepartment = () => ({
      ...DEPARTMENT,
      unavailable_dataset_count: updated ? 0 : 1,
    });
    const calls = stubFetch((call) => {
      if (call.url.startsWith("/api/admin/departments/?")) {
        return json(200, page(currentDepartment()));
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/`) {
        return json(200, {
          department: currentDepartment(),
          revision: updated ? 8 : 7,
          epoch: updated ? 4 : 3,
        });
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/members/`) {
        return json(200, {
          department: currentDepartment(),
          members: [],
          revision: updated ? 8 : 7,
          epoch: updated ? 4 : 3,
        });
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/datasets/`) {
        if (call.method === "PUT") {
          updated = true;
          return json(200, {
            result: {
              department: currentDepartment(),
              dataset_ids: [DATASET.id],
            },
            revision: 8,
            epoch: 4,
          });
        }
        return json(200, {
          department: currentDepartment(),
          datasets: [updated ? DEPARTMENT_DATASET : NEEDS_REVIEW_DATASET],
          revision: updated ? 8 : 7,
          epoch: updated ? 4 : 3,
        });
      }
      if (call.url.startsWith("/api/admin/datasets/")) {
        return json(200, page(DATASET));
      }
      return json(404, {});
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Management bootstrap={bootstrap("departments")} />);
    await screen.findByText("Finance");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));

    expect(await screen.findByText(/1 dataset grant is unavailable/)).toBeTruthy();
    expect(await screen.findByText(/Needs assignment review/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Update assignment" }));

    await waitFor(() => {
      const saved = calls.find(
        (call) =>
          call.method === "PUT" &&
          call.url === `/api/admin/departments/${DEPARTMENT.id}/datasets/`,
      );
      expect(saved?.body).toEqual({
        revision: 7,
        dataset_ids: [DATASET.id],
        confirmed_dataset_ids: [DATASET.id],
      });
    });
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("clears member subset assignments"),
    );
    confirm.mockRestore();
  });

  it("keeps a missing grant visible and removable", async () => {
    const calls = stubFetch((call) => {
      if (call.url.startsWith("/api/admin/departments/?")) {
        return json(200, page({ ...DEPARTMENT, unavailable_dataset_count: 1 }));
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/`) {
        return json(200, {
          department: { ...DEPARTMENT, unavailable_dataset_count: 1 },
          revision: 7,
          epoch: 3,
        });
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/members/`) {
        return json(200, {
          department: { ...DEPARTMENT, unavailable_dataset_count: 1 },
          members: [],
          revision: 7,
          epoch: 3,
        });
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/datasets/`) {
        if (call.method === "PUT") {
          return json(200, {
            result: {
              department: { ...DEPARTMENT, unavailable_dataset_count: 0 },
              dataset_ids: [],
            },
            revision: 8,
            epoch: 4,
          });
        }
        return json(200, {
          department: { ...DEPARTMENT, unavailable_dataset_count: 1 },
          datasets: [MISSING_DATASET],
          revision: 7,
          epoch: 3,
        });
      }
      if (call.url.startsWith("/api/admin/datasets/")) {
        return json(200, page(DATASET));
      }
      return json(404, {});
    });
    render(<Management bootstrap={bootstrap("departments")} />);
    await screen.findByText("Finance");
    await userEvent.click(screen.getByRole("button", { name: "Open" }) as HTMLElement);

    expect(
      await screen.findByText(/Missing from the current Wren catalog/),
    ).toBeTruthy();
    const checkbox = (await screen.findByRole("checkbox", {
      name: /Missing from the current Wren catalog/,
    })) as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    await userEvent.click(checkbox);

    await waitFor(() => {
      const saved = calls.find(
        (call) =>
          call.method === "PUT" &&
          call.url === `/api/admin/departments/${DEPARTMENT.id}/datasets/`,
      );
      expect(saved?.body).toEqual({
        revision: 7,
        dataset_ids: [],
        confirmed_dataset_ids: [],
      });
    });
  });

  it("keeps a selected different-source member subset as a redacted warning", async () => {
    const memberRef = {
      id: 5,
      username: "ada",
      first_name: "Ada",
      last_name: "Lovelace",
    };
    const membership = {
      id: "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5e",
      department: {
        id: DEPARTMENT.id,
        code: DEPARTMENT.code,
        name: DEPARTMENT.name,
      },
      user: memberRef,
      membership_role: "staff",
      dataset_ids: [DATASET.id],
    };
    const calls = stubFetch((call) => {
      if (call.url.startsWith("/api/admin/departments/?")) {
        return json(200, page({ ...DEPARTMENT, unavailable_dataset_count: 1 }));
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/`) {
        return json(200, {
          department: { ...DEPARTMENT, unavailable_dataset_count: 1 },
          revision: 7,
          epoch: 3,
        });
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/members/`) {
        return json(200, {
          department: { ...DEPARTMENT, unavailable_dataset_count: 1 },
          members: [
            {
              user: memberRef,
              membership_role: "staff",
              is_active: true,
              dataset_ids: [DATASET.id],
            },
          ],
          revision: 7,
          epoch: 3,
        });
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/datasets/`) {
        return json(200, {
          department: { ...DEPARTMENT, unavailable_dataset_count: 1 },
          datasets: [
            {
              ...DEPARTMENT_DATASET,
              display_name: "Unavailable dataset",
              availability: "different_source",
            },
          ],
          revision: 7,
          epoch: 3,
        });
      }
      if (call.url === `/api/admin/departments/${DEPARTMENT.id}/members/5/datasets/`) {
        return json(200, {
          membership,
          revision: 7,
          epoch: 3,
        });
      }
      if (call.url.startsWith("/api/admin/datasets/")) {
        return json(200, emptyPage());
      }
      if (call.method === "PUT") {
        return json(200, {
          membership,
          revision: 8,
          epoch: 4,
        });
      }
      return json(404, {});
    });
    render(<Management bootstrap={bootstrap("departments")} />);
    await screen.findByText("Finance");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit subsets" }));

    const retained = await within(
      await screen.findByRole("dialog", { name: "Datasets for ada" }),
    ).findByRole("checkbox", {
      name: /Unavailable dataset/,
    });
    expect((retained as HTMLInputElement).checked).toBe(true);
    expect((retained as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByText(/Orders/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save subsets" }));

    await waitFor(() => {
      const saved = calls.find(
        (call) =>
          call.method === "PUT" &&
          call.url === `/api/admin/departments/${DEPARTMENT.id}/members/5/datasets/`,
      );
      expect(saved?.body).toEqual({
        revision: 7,
        dataset_ids: [DATASET.id],
      });
    });
  });
});

describe("navigation scope", () => {
  it("shows a Lead only the sections they may use", async () => {
    stubFetch(() => json(200, page(DEPARTMENT)));
    const leadCapabilities: Capabilities = {
      ...ALL_CAPABILITIES,
      manage_users: false,
      manage_roles: false,
      manage_catalog: false,
      manage_grants: false,
      view_audit: false,
    };
    render(<Management bootstrap={bootstrap("departments", leadCapabilities)} />);
    const nav = screen.getByRole("navigation", { name: "Management sections" });
    expect(within(nav).getByRole("link", { name: "Departments" })).toBeTruthy();
    expect(within(nav).queryByRole("link", { name: "Users" })).toBeNull();
    expect(within(nav).queryByRole("link", { name: "Roles" })).toBeNull();
    expect(within(nav).queryByRole("link", { name: "Audit" })).toBeNull();
    expect(within(nav).getByRole("link", { name: "Datasets" })).toBeTruthy();
  });
});
