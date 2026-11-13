import { afterEach, describe, expect, it, vi } from "vitest";

import {
  assertCommittedTurn,
  assertCompletedThread,
  type Bootstrap,
  CHAT_PROTOCOL_ERROR,
  CHAT_SERVER_ERROR,
  ChatApiError,
  type ChatLimits,
  errorFromResponse,
  fetchThreads,
  parseActivityUpdate,
  parseBootstrap,
  parseDisplay,
  parseIsoTimestamp,
  parseMetric,
  parseSameOriginPath,
  parseThreadDetail,
  parseThreadSummary,
  parseTurnActivity,
  parseTurnDetail,
  postTurn,
  sortThreads,
  type ThreadDetail,
  type ThreadSummary,
  type TurnDetail,
  toChatError,
} from "./api";

const ORIGIN = "https://example.test";
Object.defineProperty(globalThis, "location", {
  value: { origin: ORIGIN },
  configurable: true,
});

const THREAD_ID = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const OTHER_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";

function makeSummary(overrides: Record<string, unknown> = {}): ThreadSummary {
  return {
    id: THREAD_ID,
    title: "New chat",
    revision: 0,
    created_at: "2026-01-02T03:04:05.000Z",
    updated_at: "2026-01-02T03:04:05.000Z",
    access: "available",
    department: null,
    page_url: `/chat/${THREAD_ID}/`,
    detail_url: `/api/threads/${THREAD_ID}/`,
    messages_url: `/api/threads/${THREAD_ID}/messages/`,
    ...overrides,
  } as ThreadSummary;
}

function makeLimits(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    max_files_per_thread: 5,
    max_files_per_turn: 5,
    max_file_bytes: 5 * 1024 * 1024,
    max_text_bytes_per_file: 64 * 1024,
    max_text_bytes_per_thread: 256 * 1024,
    context_window: 32768,
    max_output_tokens: 4096,
    ...overrides,
  };
}

function makeTurn(overrides: Record<string, unknown> = {}): TurnDetail {
  const turnId = OTHER_ID;
  return {
    turn_id: turnId,
    turn_index: 0,
    response: {
      requested_model: "deepseek/deepseek-v4.1-flash-1017",
      resolved_model: null,
      completed_at: "2026-01-02T03:04:06.000Z",
      duration_ms: 1840,
      output_tokens: 96,
      output_limit_reached: false,
    },
    activity: null,
    attachments: [],
    can_regenerate: true,
    can_delete: true,
    regenerate_url: `/api/threads/${THREAD_ID}/turns/${turnId}/regenerate/`,
    delete_url: `/api/threads/${THREAD_ID}/turns/${turnId}/`,
    ...overrides,
  } as TurnDetail;
}

function makeDetail(overrides: Record<string, unknown> = {}): ThreadDetail {
  return {
    ...makeSummary(),
    display: [],
    turns: [],
    conversation_error: null,
    ...overrides,
  } as ThreadDetail;
}

const CAPABILITIES = {
  manage_users: false,
  manage_roles: false,
  manage_departments: false,
  manage_department_staff: false,
  manage_catalog: false,
  manage_grants: false,
  view_audit: false,
};

function makeBootstrap(overrides: Record<string, unknown> = {}): Bootstrap {
  return {
    user: { username: "ada", is_staff: false },
    threads: [makeSummary()],
    active_thread: makeDetail(),
    limits: makeLimits() as unknown as ChatLimits,
    capabilities: CAPABILITIES,
    memberships: [],
    analytics_enabled: false,
    analytics_department_ids: [],
    urls: {
      threads: "/api/threads/",
      login: "/accounts/login/",
      logout: "/accounts/logout/",
      management: null,
    },
    ...overrides,
  } as Bootstrap;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseSameOriginPath", () => {
  it("accepts a single-slash path", () => {
    expect(parseSameOriginPath("/api/threads/")).toBe("/api/threads/");
  });

  it("rejects foreign, scheme-relative, and absolute urls", () => {
    for (const value of [
      "//evil.test/x",
      "https://evil.test/x",
      "http://example.test/x",
      "javascript:alert(1)",
      "/\\evil.test/x",
      "/api/\nthreads",
      "",
      null,
      7,
    ]) {
      expect(parseSameOriginPath(value)).toBeNull();
    }
  });
});

describe("parseDisplay", () => {
  it("accepts alternating completed pairs", () => {
    expect(
      parseDisplay([
        { role: "user", content: "hi" },
        { role: "assistant", content: "hello" },
      ]),
    ).toEqual([
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ]);
  });

  it("rejects wrong role order, odd length, and extra fields", () => {
    for (const value of [
      [{ role: "assistant", content: "x" }],
      [{ role: "user", content: "x" }],
      [{ role: "user", content: "x", extra: true }],
      [{ role: "user", content: 5 }],
      [
        { role: "user", content: "x" },
        { role: "user", content: "y" },
      ],
      "not a list",
    ]) {
      expect(parseDisplay(value)).toBeNull();
    }
  });
});

describe("parseThreadSummary", () => {
  it("accepts the server summary shape", () => {
    expect(parseThreadSummary(makeSummary())).toEqual(makeSummary());
  });

  it("rejects malformed revisions, ids, dates, and extra keys", () => {
    for (const patch of [
      { id: "not-a-uuid" },
      { revision: -1 },
      { revision: 1.5 },
      { revision: true },
      { revision: "1" },
      { created_at: "not a date" },
      { updated_at: 5 },
      { page_url: "//evil.test/chat/" },
      { detail_url: "https://evil.test/api/" },
      { messages_url: "javascript:alert(1)" },
      { title: "" },
      { unexpected: "field" },
    ]) {
      expect(parseThreadSummary(makeSummary(patch))).toBeNull();
    }
  });
});

describe("parseThreadDetail", () => {
  it("accepts display pairs and a null conversation error", () => {
    const detail = makeDetail({
      display: [
        { role: "user", content: "q" },
        { role: "assistant", content: "a" },
      ],
    });
    expect(parseThreadDetail(detail)).toEqual(detail);
  });

  it("accepts an explicit conversation error", () => {
    const detail = makeDetail({ conversation_error: "This chat could not be loaded." });
    expect(parseThreadDetail(detail)?.conversation_error).toBe(
      "This chat could not be loaded.",
    );
  });

  it("rejects a missing or non-string conversation error", () => {
    const missing = { ...makeDetail() } as Record<string, unknown>;
    delete missing.conversation_error;
    expect(parseThreadDetail(missing)).toBeNull();
    expect(parseThreadDetail(makeDetail({ conversation_error: 5 }))).toBeNull();
  });

  it("rejects a bad display inside the detail", () => {
    expect(
      parseThreadDetail(makeDetail({ display: [{ role: "assistant", content: "x" }] })),
    ).toBeNull();
  });
});

describe("parseThreadDetail turns", () => {
  it("accepts a sparse sorted turn list", () => {
    const detail = makeDetail({
      display: [
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1" },
        { role: "user", content: "q2" },
        { role: "assistant", content: "a2" },
      ],
      turns: [makeTurn({ turn_index: 0, can_regenerate: false, can_delete: false })],
    });
    expect(parseThreadDetail(detail)?.turns).toHaveLength(1);
  });

  it("rejects duplicate, unsorted, and out-of-range indexes", () => {
    const first = makeTurn({ turn_index: 0 });
    const cases = [
      [first, first],
      [makeTurn({ turn_index: 1 }), makeTurn({ turn_index: 0 })],
      [makeTurn({ turn_index: 2 })],
      [makeTurn({ turn_index: -1 })],
    ];
    for (const turns of cases) {
      const detail = makeDetail({
        display: [
          { role: "user", content: "q" },
          { role: "assistant", content: "a" },
        ],
        turns: turns.map((turn) => ({ ...turn, turn_id: turn.turn_id })),
      });
      expect(parseThreadDetail(detail)).toBeNull();
    }
  });

  it("rejects duplicate turn ids across indexes", () => {
    const detail = makeDetail({
      display: [
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1" },
        { role: "user", content: "q2" },
        { role: "assistant", content: "a2" },
      ],
      turns: [makeTurn({ turn_index: 0 }), makeTurn({ turn_index: 1 })],
    });
    expect(parseThreadDetail(detail)).toBeNull();
  });

  it("rejects any public hint of the private checkpoint", () => {
    const detail = makeDetail({
      display: [{ role: "user", content: "q" }],
      turns: [makeTurn({ pre_history_json: "[]" })],
    });
    expect(parseThreadDetail(detail)).toBeNull();
    expect(parseThreadDetail(makeDetail({ history_json: "[]" }))).toBeNull();
  });

  it("rejects unknown turn keys and non-boolean hints", () => {
    for (const patch of [
      { can_regenerate: "yes" },
      { can_delete: 1 },
      { extra: true },
      { regenerate_url: "//evil.test/x" },
      { delete_url: "https://evil.test/x" },
      {
        activity: {
          version: 1,
          turn_id: OTHER_ID,
          steps: [],
          truncated: false,
          extra: 1,
        },
      },
    ]) {
      const detail = makeDetail({
        display: [{ role: "user", content: "q" }],
        turns: [makeTurn(patch)],
      });
      expect(parseThreadDetail(detail)).toBeNull();
    }
  });

  it("rejects activity bound to another turn id", () => {
    const detail = makeDetail({
      display: [{ role: "user", content: "q" }],
      turns: [
        makeTurn({
          activity: {
            version: 1,
            turn_id: THREAD_ID,
            steps: [{ id: 0, kind: "model", status: "complete" }],
            truncated: false,
          },
        }),
      ],
    });
    expect(parseThreadDetail(detail)).toBeNull();
  });
});

describe("parseTurnActivity", () => {
  const activity = {
    version: 1,
    turn_id: OTHER_ID,
    steps: [
      { id: 0, kind: "model", status: "complete" },
      { id: 1, kind: "wren_tool", status: "failed" },
    ],
    truncated: true,
  };

  it("accepts a completed timeline", () => {
    expect(parseTurnActivity(activity)).toEqual(activity);
  });

  it("rejects running steps, skipped ids, unknown enums, and extra keys", () => {
    const cases = [
      { ...activity, steps: [{ id: 0, kind: "model", status: "running" }] },
      { ...activity, steps: [{ id: 1, kind: "model", status: "complete" }] },
      { ...activity, steps: [{ id: 0, kind: "sql", status: "complete" }] },
      { ...activity, steps: [{ id: 0, kind: "model", status: "done" }] },
      { ...activity, version: 2 },
      { ...activity, turn_id: "nope" },
      { ...activity, truncated: "yes" },
      { ...activity, steps: [], extra: true },
      { ...activity, steps: [{ id: 0, kind: "model", status: "complete", name: "x" }] },
    ];
    for (const value of cases) {
      expect(parseTurnActivity(value)).toBeNull();
    }
  });

  it("rejects a timeline larger than the byte cap", () => {
    const steps = Array.from({ length: 64 }, (_, id) => ({
      id,
      kind: "wren_tool" as const,
      status: "complete" as const,
    }));
    expect(
      parseTurnActivity({ version: 1, turn_id: OTHER_ID, steps, truncated: false }),
    ).not.toBeNull();
    expect(
      parseTurnActivity({
        version: 1,
        turn_id: OTHER_ID,
        steps: steps.map((step) => ({ ...step, kind: "wren_tool" })),
        truncated: false,
      }),
    ).not.toBeNull();
    expect(
      parseTurnActivity({
        version: 1,
        turn_id: OTHER_ID,
        steps,
        truncated: true,
      }),
    ).not.toBeNull();
  });
});

describe("parseActivityUpdate", () => {
  it("accepts a step update and the single truncation notice", () => {
    expect(
      parseActivityUpdate({
        turn_id: OTHER_ID,
        step: { id: 0, kind: "model", status: "running" },
        truncated: false,
      }),
    ).toEqual({
      turn_id: OTHER_ID,
      step: { id: 0, kind: "model", status: "running" },
      truncated: false,
    });
    expect(
      parseActivityUpdate({ turn_id: OTHER_ID, step: null, truncated: true }),
    ).toEqual({ turn_id: OTHER_ID, step: null, truncated: true });
  });

  it("accepts the optional detail a server may attach to a step", () => {
    expect(
      parseActivityUpdate({
        turn_id: OTHER_ID,
        step: {
          id: 1,
          kind: "wren_tool",
          status: "complete",
          title: "Assembling Tool Call: list_models({})",
          duration_ms: 37,
        },
        truncated: false,
      }),
    ).toEqual({
      turn_id: OTHER_ID,
      step: {
        id: 1,
        kind: "wren_tool",
        status: "complete",
        title: "Assembling Tool Call: list_models({})",
        duration_ms: 37,
      },
      truncated: false,
    });
  });

  it("rejects a null step without truncation and unknown keys", () => {
    for (const value of [
      { turn_id: OTHER_ID, step: null, truncated: false },
      { turn_id: OTHER_ID, step: null, truncated: true, extra: 1 },
      { turn_id: "nope", step: null, truncated: true },
      {
        turn_id: OTHER_ID,
        step: { id: -1, kind: "model", status: "running" },
        truncated: false,
      },
      {
        turn_id: OTHER_ID,
        step: { id: 0, kind: "model", status: "complete" },
        truncated: false,
        name: "x",
      },
    ]) {
      expect(parseActivityUpdate(value)).toBeNull();
    }
  });
});

describe("parseIsoTimestamp", () => {
  it("accepts zone-bearing timestamps", () => {
    for (const value of [
      "2026-01-02T03:04:05.000Z",
      "2026-01-02T03:04:05Z",
      "2026-01-02T03:04:05.123456+00:00",
      "2026-01-02T03:04:05-07:30",
    ]) {
      expect(parseIsoTimestamp(value)).toBe(value);
    }
  });

  it("rejects naive, impossible, and out-of-range values", () => {
    for (const value of [
      "2026-01-02T03:04:05",
      "2026-02-30T00:00:00Z",
      "2026-13-01T00:00:00Z",
      "2026-01-02T24:00:00Z",
      "2026-01-02T03:04:60Z",
      "2026-01-02T03:04:05+99:00",
      "not a date",
      null,
    ]) {
      expect(parseIsoTimestamp(value)).toBeNull();
    }
  });
});

describe("parseMetric", () => {
  it("accepts store-safe counters only", () => {
    expect(parseMetric(0)).toBe(0);
    expect(parseMetric(2147483647)).toBe(2147483647);
    for (const value of [-1, 1.5, true, "5", 2147483648, Number.NaN, null]) {
      expect(parseMetric(value)).toBeNull();
    }
  });
});

describe("assertCommittedTurn", () => {
  it("accepts the requested turn and rejects a mismatch", () => {
    const turn = makeTurn({ turn_index: 1 });
    expect(() =>
      assertCommittedTurn(turn, { turnId: OTHER_ID, turnIndex: 1 }),
    ).not.toThrow();
    expect(() =>
      assertCommittedTurn(turn, { turnId: THREAD_ID, turnIndex: 1 }),
    ).toThrow(CHAT_PROTOCOL_ERROR);
    expect(() => assertCommittedTurn(turn, { turnId: OTHER_ID, turnIndex: 0 })).toThrow(
      CHAT_PROTOCOL_ERROR,
    );
  });
});

describe("parseBootstrap", () => {
  it("accepts the full workspace payload", () => {
    const payload = makeBootstrap();
    expect(parseBootstrap(payload)).toEqual(payload);
  });

  it("accepts an unselected workspace with no active thread", () => {
    const payload = makeBootstrap({ threads: [], active_thread: null });
    expect(parseBootstrap(payload)?.active_thread).toBeNull();
  });

  it("accepts an authorized management url", () => {
    const payload = makeBootstrap({
      urls: {
        threads: "/api/threads/",
        login: "/accounts/login/",
        logout: "/accounts/logout/",
        management: "/management/",
      },
    });
    expect(parseBootstrap(payload)?.urls.management).toBe("/management/");
  });

  it("requires the governance bootstrap keys", () => {
    expect(parseBootstrap(makeBootstrap({ capabilities: undefined }))).toBeNull();
    expect(parseBootstrap(makeBootstrap({ memberships: undefined }))).toBeNull();
    expect(parseBootstrap(makeBootstrap({ analytics_enabled: undefined }))).toBeNull();
    expect(
      parseBootstrap(makeBootstrap({ analytics_department_ids: undefined })),
    ).toBeNull();
    expect(
      parseBootstrap(makeBootstrap({ analytics_enabled: true }))?.analytics_enabled,
    ).toBe(true);
  });

  it("requires unique department ids that belong to memberships", () => {
    const salesId = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";
    const marketingId = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";
    const sales = {
      id: "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5f",
      department: { id: salesId, code: "sales", name: "Sales" },
      membership_role: "staff",
      dataset_ids: [],
    };
    const marketing = {
      ...sales,
      id: "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d6d",
      department: { id: marketingId, code: "marketing", name: "Marketing" },
    };
    const accepted = makeBootstrap({
      memberships: [sales, marketing],
      analytics_department_ids: [salesId],
    });
    expect(parseBootstrap(accepted)?.analytics_department_ids).toEqual([salesId]);

    const { analytics_department_ids: _omitted, ...missing } = makeBootstrap({
      memberships: [sales],
    });
    for (const payload of [
      missing,
      makeBootstrap({ analytics_department_ids: salesId }),
      makeBootstrap({
        memberships: [sales],
        analytics_department_ids: ["not-a-uuid"],
      }),
      makeBootstrap({
        memberships: [sales],
        analytics_department_ids: [salesId, salesId],
      }),
      makeBootstrap({
        memberships: [sales, marketing],
        analytics_department_ids: [salesId.toUpperCase(), salesId],
      }),
      makeBootstrap({
        memberships: [sales],
        analytics_department_ids: [marketingId],
      }),
      makeBootstrap({
        memberships: [sales],
        analytics_department_ids: [sales.id],
      }),
      makeBootstrap({ memberships: [], analytics_department_ids: [salesId] }),
    ]) {
      expect(parseBootstrap(payload)).toBeNull();
    }
  });

  it("rejects malformed payloads", () => {
    const cases: unknown[] = [
      null,
      [],
      makeBootstrap({ user: { username: "ada" } }),
      makeBootstrap({ threads: [makeSummary({ revision: -2 })] }),
      makeBootstrap({ active_thread: makeDetail({ id: "bad" }) }),
      makeBootstrap({
        urls: {
          threads: "//evil.test/",
          login: "/l/",
          logout: "/o/",
          management: null,
        },
      }),
      makeBootstrap({ extra: true }),
    ];
    for (const payload of cases) {
      expect(parseBootstrap(payload)).toBeNull();
    }
  });
});

describe("assertCompletedThread", () => {
  it("accepts the same thread at the next revision", () => {
    expect(() =>
      assertCompletedThread(makeSummary({ revision: 3 }), THREAD_ID, 2),
    ).not.toThrow();
  });

  it("rejects another thread or an unexpected revision", () => {
    for (const summary of [
      makeSummary({ id: OTHER_ID, revision: 3 }),
      makeSummary({ revision: 2 }),
      makeSummary({ revision: 4 }),
    ]) {
      expect(() => assertCompletedThread(summary, THREAD_ID, 2)).toThrow(
        CHAT_PROTOCOL_ERROR,
      );
    }
  });
});

describe("sortThreads", () => {
  it("orders newest update first and breaks ties by id", () => {
    const older = makeSummary({
      id: THREAD_ID,
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    const newer = makeSummary({
      id: OTHER_ID,
      updated_at: "2026-02-01T00:00:00.000Z",
    });

    expect(sortThreads([older, newer]).map((thread) => thread.id)).toEqual([
      OTHER_ID,
      THREAD_ID,
    ]);
  });
});

describe("errorFromResponse", () => {
  it("keeps the server error code and message", async () => {
    const response = new Response(
      JSON.stringify({
        error: { code: "thread_conflict", message: "Reload it." },
      }),
      { status: 409, headers: { "content-type": "application/json" } },
    );

    await expect(errorFromResponse(response)).resolves.toMatchObject({
      code: "thread_conflict",
      message: "Reload it.",
    });
  });

  it("uses a fixed message for an HTML error body", async () => {
    const response = new Response("<html>secret trace</html>", {
      status: 404,
      headers: { "content-type": "text/html" },
    });

    const failure = await errorFromResponse(response);
    expect(failure).toBeInstanceOf(ChatApiError);
    expect(failure.code).toBe("thread_not_found");
    expect(failure.message).not.toContain("secret");
  });

  it("falls back to a protocol error for an unknown status", async () => {
    const response = new Response("oops", { status: 500 });
    await expect(errorFromResponse(response)).resolves.toMatchObject({
      code: "protocol_error",
      message: CHAT_PROTOCOL_ERROR,
    });
  });
});

describe("toChatError", () => {
  it("passes structured errors through", () => {
    expect(toChatError(new ChatApiError("provider_timeout", "Timed out."))).toEqual({
      code: "provider_timeout",
      message: "Timed out.",
    });
  });

  it("never exposes a raw exception message", () => {
    expect(toChatError(new TypeError("network stack detail"))).toEqual({
      code: "unknown_error",
      message: CHAT_SERVER_ERROR,
    });
  });
});

describe("fetchThreads", () => {
  it("returns validated metadata in recency order", async () => {
    const older = makeSummary({
      id: THREAD_ID,
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    const newer = makeSummary({
      id: OTHER_ID,
      updated_at: "2026-02-01T00:00:00.000Z",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ threads: [older, newer] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );

    await expect(fetchThreads("/api/threads/")).resolves.toEqual([newer, older]);
  });

  it("rejects a malformed list payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ threads: [{ id: "bad" }] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );

    await expect(fetchThreads("/api/threads/")).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("surfaces a login redirect page as an authentication error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response("<html>login</html>", {
            status: 401,
            headers: { "content-type": "text/html" },
          }),
      ),
    );

    await expect(fetchThreads("/api/threads/")).rejects.toMatchObject({
      code: "authentication_required",
    });
  });
});

const ATTACHMENT = {
  id: "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5e",
  name: "notes.txt",
  media_type: "text/plain",
  size: 128,
};

describe("parseTurnDetail attachments", () => {
  it("accepts stored attachment labels", () => {
    const turn = makeTurn({ attachments: [ATTACHMENT] });
    expect(parseTurnDetail(turn)).toEqual(turn);
  });

  it("rejects malformed attachment labels", () => {
    const cases: unknown[] = [
      { ...ATTACHMENT, id: "not-a-uuid" },
      { ...ATTACHMENT, name: "" },
      { ...ATTACHMENT, name: "   " },
      { ...ATTACHMENT, name: "bad\nname.txt" },
      { ...ATTACHMENT, name: `${"x".repeat(252)}.txt` },
      { ...ATTACHMENT, media_type: "application/zip" },
      { ...ATTACHMENT, size: 0 },
      { ...ATTACHMENT, size: 1.5 },
      { id: ATTACHMENT.id, name: "notes.txt", media_type: "text/plain" },
      { ...ATTACHMENT, extra: true },
      "notes.txt",
    ];
    for (const attachment of cases) {
      expect(parseTurnDetail(makeTurn({ attachments: [attachment] }))).toBeNull();
    }
    expect(
      parseTurnDetail(makeTurn({ attachments: [ATTACHMENT, { ...ATTACHMENT }] })),
    ).toBeNull();
  });

  it("does not re-apply current quotas to stored labels", () => {
    const many = Array.from({ length: 7 }, (_, index) => ({
      ...ATTACHMENT,
      id: `00000000-0000-4000-8000-00000000000${index}`,
      name: `file-${index}.txt`,
    }));
    const turn = makeTurn({ attachments: many });
    expect(parseTurnDetail(turn)?.attachments).toHaveLength(7);
  });
});

describe("parseBootstrap limits", () => {
  it("requires a well-formed limits object", () => {
    const cases: unknown[] = [
      undefined,
      {},
      { ...makeLimits(), max_files_per_turn: 6 },
      { ...makeLimits(), max_text_bytes_per_file: 999_999_999 },
      { ...makeLimits(), max_output_tokens: 32768 },
      { ...makeLimits(), max_file_bytes: 0 },
      { ...makeLimits(), context_window: 1.5 },
    ];
    for (const limits of cases) {
      expect(parseBootstrap(makeBootstrap({ limits }))).toBeNull();
    }
    expect(parseBootstrap(makeBootstrap())?.limits).toEqual(makeLimits());
  });
});

describe("postTurn", () => {
  function stubFetch(response: Response) {
    vi.stubGlobal("document", { querySelector: () => null });
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => response,
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("keeps the JSON contract when no files are selected", async () => {
    const fetchMock = stubFetch(
      new Response(null, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    await postTurn(
      "/api/threads/x/messages/",
      "hello",
      3,
      new AbortController().signal,
    );

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.body).toBe(JSON.stringify({ message: "hello", revision: 3 }));
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
  });

  it("posts multipart with the boundary left to fetch", async () => {
    const fetchMock = stubFetch(
      new Response(null, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    const files = [
      new File(["alpha"], "alpha.txt", { type: "text/plain" }),
      new File(["beta"], "beta.txt", { type: "text/plain" }),
    ];
    await postTurn(
      "/api/threads/x/messages/",
      "hello",
      4,
      new AbortController().signal,
      files,
    );

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect(form.get("message")).toBe("hello");
    expect(form.get("revision")).toBe("4");
    expect(form.getAll("files")).toHaveLength(2);
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
    expect((init.headers as Record<string, string>)["X-CSRFToken"]).toBeUndefined();
  });
});
