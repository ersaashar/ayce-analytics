// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { Bootstrap, ChatLimits, ThreadDetail } from "./api";
import {
  ATTACHMENT_ID,
  deferredStream,
  frame,
  installDomShims,
  jsonResponse,
  makeActivity,
  makeAttachment,
  makeDetail,
  makeLimits,
  makeSummary,
  makeTurn,
  OTHER_THREAD_ID,
  sseResponse,
  THREAD_ID,
  TURN_ID,
  TURN_ID_TWO,
} from "./test-support";
import { Workspace } from "./Workspace";

beforeAll(() => installDomShims());

afterEach(cleanup);

type Call = { url: string; method: string; body: unknown };

function bootstrapWith(
  detail: ThreadDetail | null,
  limits: ChatLimits = makeLimits(),
): Bootstrap {
  return {
    user: { username: "ada", is_staff: false },
    threads: [
      makeSummary({
        id: detail?.id ?? THREAD_ID,
        title: detail?.title ?? "New chat",
        revision: detail?.revision ?? 0,
      }),
      makeSummary({ id: OTHER_THREAD_ID, title: "Other chat", revision: 0 }),
    ],
    active_thread: detail,
    limits,
    capabilities: {
      manage_users: false,
      manage_roles: false,
      manage_departments: false,
      manage_department_staff: false,
      manage_catalog: false,
      manage_grants: false,
      view_audit: false,
    },
    memberships: [],
    analytics_enabled: false,
    analytics_department_ids: [],
    urls: {
      threads: "/api/threads/",
      login: "/accounts/login/",
      logout: "/accounts/logout/",
      management: null,
    },
  };
}

function installFetch(
  route: (call: Call) => Response | null | Promise<Response | null>,
) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const raw = init?.body;
    const call: Call = {
      url: String(input),
      method,
      body:
        typeof raw === "string"
          ? JSON.parse(raw)
          : raw instanceof FormData
            ? raw
            : undefined,
    };
    calls.push(call);
    const response = await route(call);
    if (response === null) throw new Error(`unrouted ${method} ${call.url}`);
    return response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

function composer(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: "Message" });
}

async function sendMessage(text: string): Promise<void> {
  const box = composer();
  await userEvent.click(box);
  await userEvent.type(box, text);
  await userEvent.keyboard("{Enter}");
}

function assistantNodes(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>(".message[data-role='assistant']"),
  );
}

function actionLabels(node: HTMLElement): string[] {
  return within(node)
    .queryAllByRole("button")
    .map((button) => button.textContent ?? "");
}

describe("sending a turn", () => {
  it("appends the committed turn and retires the older actions", async () => {
    const firstTurn = makeTurn({ turn_id: TURN_ID, turn_index: 0 });
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "first question" },
        { role: "assistant", content: "first answer" },
      ],
      turns: [firstTurn],
    });
    const secondTurn = makeTurn({ turn_id: TURN_ID_TWO, turn_index: 1 });

    installFetch((call) => {
      if (call.url === `/api/threads/${THREAD_ID}/messages/`) {
        return sseResponse([
          frame.activity(TURN_ID_TWO, { id: 0, kind: "model", status: "running" }),
          frame.delta("second "),
          frame.delta("answer"),
          frame.done(
            "second answer",
            makeSummary({ revision: 2, title: "New chat" }),
            secondTurn,
          ),
        ]);
      }
      return null;
    });

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await sendMessage("second question");

    await waitFor(() => expect(assistantNodes()).toHaveLength(2));
    await waitFor(() =>
      expect(assistantNodes()[1]?.textContent).toContain("second answer"),
    );

    expect(actionLabels(assistantNodes()[0] as HTMLElement)).toEqual(["Copy"]);
    expect(actionLabels(assistantNodes()[1] as HTMLElement)).toEqual([
      "Copy",
      "Regenerate",
      "Delete",
    ]);

    // The optimistic pair is gone and only the committed one remains.
    expect(document.querySelectorAll(".message[data-role='user']")).toHaveLength(2);
    expect(composer().value).toBe("");
  });

  it("shows factual activity while the answer streams", async () => {
    const active = makeDetail({ revision: 0 });
    const stream = deferredStream();
    installFetch((call) =>
      call.url === `/api/threads/${THREAD_ID}/messages/` ? stream.response : null,
    );

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await sendMessage("count the orders");

    stream.emit(
      frame.activity("2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f", {
        id: 0,
        kind: "model",
        status: "running",
      }),
    );
    stream.emit(frame.delta("Working on it"));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Thoughts" }).getAttribute("aria-expanded"),
      ).toBe("true"),
    );
    expect(document.body.textContent).toContain("Preparing response");
    // No completion actions while the turn is still open.
    expect(document.querySelector(".message-actions")).toBeNull();
  });
});

describe("regenerating the latest answer", () => {
  it("keeps the old answer visible, then replaces it without duplicating the prompt", async () => {
    const turn = makeTurn({ turn_id: TURN_ID, turn_index: 0 });
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "original question" },
        { role: "assistant", content: "original answer" },
      ],
      turns: [turn],
    });
    const stream = deferredStream();
    const replacement = makeTurn({
      turn_id: TURN_ID,
      turn_index: 0,
      can_regenerate: true,
    });

    installFetch((call) => {
      if (call.url === turn.regenerate_url) return stream.response;
      return null;
    });

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await waitFor(() => expect(assistantNodes()).toHaveLength(1));
    await userEvent.click(
      within(assistantNodes()[0] as HTMLElement).getByRole("button", {
        name: "Regenerate",
      }),
    );

    stream.emit(frame.activity(TURN_ID, { id: 0, kind: "model", status: "running" }));
    stream.emit(frame.delta("replacement text"));

    await waitFor(() =>
      expect(document.querySelector(".regen-preview")?.textContent).toContain(
        "Regenerating response",
      ),
    );
    expect(assistantNodes()[0]?.textContent).toContain("original answer");
    expect(document.querySelectorAll(".message[data-role='user']")).toHaveLength(1);

    stream.emit(
      frame.done(
        "replacement answer",
        makeSummary({ revision: 2, title: "New chat" }),
        replacement,
      ),
    );
    stream.close();

    await waitFor(() =>
      expect(assistantNodes()[0]?.textContent).toContain("replacement answer"),
    );
    expect(document.querySelector(".regen-preview")).toBeNull();
    expect(assistantNodes()[0]?.textContent).not.toContain("original answer");
    expect(document.querySelectorAll(".message[data-role='user']")).toHaveLength(1);
    expect(composer().value).toBe("");
  });

  it("restores the original answer when stopped", async () => {
    const turn = makeTurn({ turn_id: TURN_ID, turn_index: 0 });
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "original question" },
        { role: "assistant", content: "original answer" },
      ],
      turns: [turn],
    });
    const stream = deferredStream();
    installFetch((call) => (call.url === turn.regenerate_url ? stream.response : null));

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await waitFor(() => expect(assistantNodes()).toHaveLength(1));
    await userEvent.click(
      within(assistantNodes()[0] as HTMLElement).getByRole("button", {
        name: "Regenerate",
      }),
    );
    stream.emit(frame.delta("half a replacement"));
    await waitFor(() =>
      expect(document.querySelector(".regen-preview")?.textContent).toContain(
        "half a replacement",
      ),
    );

    await userEvent.click(screen.getByRole("button", { name: "Stop generating" }));
    // An aborted request errors its body in a real browser; end it here.
    stream.close();

    await waitFor(() => expect(document.querySelector(".regen-preview")).toBeNull());
    expect(assistantNodes()[0]?.textContent).toContain("original answer");
    expect(document.querySelector(".alert")).toBeNull();
    expect(
      within(assistantNodes()[0] as HTMLElement).getByRole("button", {
        name: "Regenerate",
      }),
    ).toBeTruthy();
  });

  it("keeps a clear reload affordance when another tab moved the chat", async () => {
    const turn = makeTurn({ turn_id: TURN_ID, turn_index: 0 });
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "q" },
        { role: "assistant", content: "a" },
      ],
      turns: [turn],
    });
    installFetch((call) =>
      call.url === turn.regenerate_url
        ? jsonResponse(
            {
              error: {
                code: "thread_conflict",
                message:
                  "This chat changed in another tab. Reload it before trying again.",
              },
            },
            409,
          )
        : null,
    );

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await waitFor(() => expect(assistantNodes()).toHaveLength(1));
    await userEvent.click(
      within(assistantNodes()[0] as HTMLElement).getByRole("button", {
        name: "Regenerate",
      }),
    );

    await waitFor(() => expect(document.querySelector(".alert")).not.toBeNull());
    expect(screen.getByRole("button", { name: "Reload chat" })).toBeTruthy();
    expect(assistantNodes()[0]?.textContent).toContain("a");
  });
});

describe("deleting an exchange", () => {
  it("removes the prompt and response pair only after confirming", async () => {
    const turn = makeTurn({ turn_id: TURN_ID, turn_index: 0 });
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "remove me" },
        { role: "assistant", content: "and me" },
      ],
      turns: [turn],
    });
    const emptied = makeDetail({ revision: 2, display: [], turns: [] });
    const { calls } = installFetch((call) =>
      call.url === turn.delete_url ? jsonResponse({ thread: emptied }) : null,
    );

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await waitFor(() => expect(assistantNodes()).toHaveLength(1));
    await userEvent.click(
      within(assistantNodes()[0] as HTMLElement).getByRole("button", {
        name: "Delete",
      }),
    );

    const dialog = screen.getByRole("dialog", { name: "Delete this exchange?" });
    expect(dialog.textContent).toContain(
      "The prompt and its response will both be removed.",
    );
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete exchange" }),
    );

    await waitFor(() => expect(assistantNodes()).toHaveLength(0));
    expect(document.querySelectorAll(".message[data-role='user']")).toHaveLength(0);
    expect(calls.some((call) => call.method === "DELETE")).toBe(true);
    const deleteCall = calls.find((call) => call.method === "DELETE");
    expect(deleteCall?.body).toEqual({ revision: 1 });
  });

  it("keeps the conversation when the server refuses", async () => {
    const turn = makeTurn({ turn_id: TURN_ID, turn_index: 0 });
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "keep me" },
        { role: "assistant", content: "kept answer" },
      ],
      turns: [turn],
    });
    installFetch((call) =>
      call.url === turn.delete_url
        ? jsonResponse(
            {
              error: {
                code: "thread_conflict",
                message:
                  "This chat changed in another tab. Reload it before trying again.",
              },
            },
            409,
          )
        : null,
    );

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await waitFor(() => expect(assistantNodes()).toHaveLength(1));
    await userEvent.click(
      within(assistantNodes()[0] as HTMLElement).getByRole("button", {
        name: "Delete",
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "Delete this exchange?" });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete exchange" }),
    );

    await waitFor(() =>
      expect(within(dialog).getByRole("alert").textContent).toContain(
        "This chat changed in another tab.",
      ),
    );
    expect(assistantNodes()[0]?.textContent).toContain("kept answer");
    expect(document.querySelectorAll(".message[data-role='user']")).toHaveLength(1);
  });
});

describe("reloading a stored conversation", () => {
  it("restores persisted activity, metadata, and latest-only actions", async () => {
    const active = makeDetail({
      revision: 2,
      display: [
        { role: "user", content: "stored question" },
        { role: "assistant", content: "stored answer" },
      ],
      turns: [
        makeTurn({
          turn_id: TURN_ID,
          turn_index: 0,
          can_regenerate: true,
          can_delete: true,
          activity: makeActivity([
            { id: 0, kind: "model", status: "complete" },
            { id: 1, kind: "wren_tool", status: "complete" },
          ]),
        }),
      ],
    });
    installFetch(() => null);

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await waitFor(() => expect(assistantNodes()).toHaveLength(1));

    const toggle = screen.getByRole("button", { name: "Thoughts" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    await userEvent.click(toggle);
    expect(document.body.textContent).toContain("Response prepared");
    expect(document.body.textContent).toContain("Analytics step completed");

    expect(document.querySelector(".message-meta")?.textContent).toContain(
      "deepseek/deepseek-v4.1-flash-1017",
    );
    expect(
      within(assistantNodes()[0] as HTMLElement).getByRole("button", {
        name: "Delete",
      }),
    ).toBeTruthy();
  });

  it("keeps the draft when switching threads and back", async () => {
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "q" },
        { role: "assistant", content: "a" },
      ],
      turns: [],
    });
    const other = makeDetail({ id: OTHER_THREAD_ID, title: "Other chat", revision: 0 });
    installFetch((call) => {
      if (call.url === other.detail_url) return jsonResponse({ thread: other });
      if (call.url === active.detail_url) return jsonResponse({ thread: active });
      return null;
    });

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    await userEvent.click(composer());
    await userEvent.keyboard("half-written draft");

    await userEvent.click(screen.getByRole("link", { name: "Other chat" }));
    await waitFor(() => expect(composer().value).toBe(""));

    await userEvent.click(screen.getByRole("link", { name: "New chat" }));
    await waitFor(() => expect(composer().value).toBe("half-written draft"));
  });
});

function fileInput(): HTMLInputElement {
  const input = document.querySelector("input[type='file']");
  if (!(input instanceof HTMLInputElement)) throw new Error("missing file input");
  return input;
}

function pickFile(name = "notes.txt", size = 5): File {
  const file = new File(["x".repeat(size)], name, { type: "text/plain" });
  void userEvent.upload(fileInput(), file);
  return file;
}

describe("attachments in the workspace", () => {
  it("posts multipart and shows the authoritative metadata", async () => {
    const active = makeDetail({ revision: 1 });
    const committed = makeTurn({
      turn_id: TURN_ID,
      turn_index: 0,
      attachments: [makeAttachment({ id: ATTACHMENT_ID, name: "notes.txt", size: 5 })],
    });
    const { calls } = installFetch((call) =>
      call.url === `/api/threads/${THREAD_ID}/messages/`
        ? sseResponse([
            frame.done("grounded answer", makeSummary({ revision: 2 }), committed),
          ])
        : null,
    );

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    pickFile();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove notes.txt" })).toBeTruthy(),
    );

    await sendMessage("what is this?");
    await waitFor(() => expect(document.body.textContent).toContain("grounded answer"));

    const post = calls.find((call) => call.method === "POST");
    const body = post?.body;
    expect(body).toBeInstanceOf(FormData);
    const form = body as FormData;
    expect(form.get("message")).toBe("what is this?");
    expect(form.get("revision")).toBe("1");
    const files = form.getAll("files");
    expect(files).toHaveLength(1);
    expect((files[0] as File).name).toBe("notes.txt");

    const userNode = document.querySelector(".message[data-role='user']");
    expect(userNode?.textContent).toContain("notes.txt");
    expect(document.querySelector(".composer-file")).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove notes.txt" })).toBeNull();
  });

  it("restores selected files when the turn fails", async () => {
    const active = makeDetail({ revision: 1 });
    installFetch((call) =>
      call.url === `/api/threads/${THREAD_ID}/messages/`
        ? jsonResponse(
            {
              error: {
                code: "provider_error",
                message:
                  "The model provider could not complete your request. Try again.",
              },
            },
            502,
          )
        : null,
    );

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    pickFile();
    await sendMessage("hello");

    await waitFor(() => expect(document.querySelector(".alert")).not.toBeNull());
    expect(screen.getByRole("button", { name: "Remove notes.txt" })).toBeTruthy();
    expect(document.querySelector(".message[data-role='user']")).toBeNull();
  });

  it("restores selected files when the reply is stopped", async () => {
    const active = makeDetail({ revision: 1 });
    const stream = deferredStream();
    installFetch((call) =>
      call.url === `/api/threads/${THREAD_ID}/messages/` ? stream.response : null,
    );

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    pickFile();
    await sendMessage("hello");
    await userEvent.click(
      await screen.findByRole("button", { name: "Stop generating" }),
    );
    stream.close();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove notes.txt" })).toBeTruthy(),
    );
  });

  it("disables only attachment selection when the thread is full", async () => {
    const attachment = makeAttachment({ id: ATTACHMENT_ID, name: "kept.txt" });
    const active = makeDetail({
      revision: 1,
      display: [
        { role: "user", content: "q" },
        { role: "assistant", content: "a" },
      ],
      turns: [makeTurn({ turn_index: 0, attachments: [attachment] })],
    });
    const committed = makeTurn({ turn_id: TURN_ID_TWO, turn_index: 1 });
    installFetch((call) =>
      call.url === `/api/threads/${THREAD_ID}/messages/`
        ? sseResponse([
            frame.done("second answer", makeSummary({ revision: 2 }), committed),
          ])
        : null,
    );

    render(
      <Workspace
        bootstrap={bootstrapWith(
          active,
          makeLimits({ max_files_per_thread: 1, max_files_per_turn: 1 }),
        )}
      />,
    );
    await waitFor(() => expect(assistantNodes()).toHaveLength(1));

    const attach = screen.getByRole("button", { name: "Attach a file to this chat" });
    expect((attach as HTMLButtonElement).disabled).toBe(true);

    await sendMessage("still sends");
    await waitFor(() => expect(document.body.textContent).toContain("second answer"));
  });

  it("keeps each thread's selected files while switching", async () => {
    const active = makeDetail({ revision: 1 });
    const other = makeDetail({ id: OTHER_THREAD_ID, title: "Other chat", revision: 0 });
    installFetch((call) => {
      if (call.url === other.detail_url) return jsonResponse({ thread: other });
      if (call.url === active.detail_url) return jsonResponse({ thread: active });
      return null;
    });

    render(<Workspace bootstrap={bootstrapWith(active)} />);
    pickFile("kept-here.txt");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove kept-here.txt" })).toBeTruthy(),
    );

    await userEvent.click(screen.getByRole("link", { name: "Other chat" }));
    await waitFor(() => expect(document.body.textContent).toContain("Other chat"));
    expect(screen.queryByRole("button", { name: "Remove kept-here.txt" })).toBeNull();

    await userEvent.click(screen.getByRole("link", { name: "New chat" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove kept-here.txt" })).toBeTruthy(),
    );
  });
});

describe("locked threads", () => {
  it("redacts content and keeps whole-thread deletion", async () => {
    const locked = makeDetail({
      title: "Restricted chat",
      revision: 2,
      access: "quarantined",
      department: null,
      display: [],
      turns: [],
      conversation_error:
        "This chat is locked because its previous data access could not be verified.",
    });
    const summary = makeSummary({
      title: "Restricted chat",
      revision: 2,
      access: "quarantined",
    });
    const { calls } = installFetch((call) => {
      if (call.method === "DELETE") return jsonResponse({ ok: true });
      return null;
    });
    render(<Workspace bootstrap={{ ...bootstrapWith(locked), threads: [summary] }} />);

    expect(screen.getByRole("heading", { name: "Restricted chat" })).toBeTruthy();
    expect(document.body.textContent).toContain(
      "previous data access could not be verified",
    );
    expect((composer() as HTMLTextAreaElement).disabled).toBe(true);

    await userEvent.click(
      screen.getByRole("button", { name: "Chat actions: Restricted chat" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Delete chat" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete chat" }));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "DELETE")).toBe(true),
    );
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
  });
});

describe("new chat context", () => {
  const FINANCE_ID = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
  const SALES_ID = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";
  const MARKETING_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";
  const ENABLED = "Synthetic Jaffle DEV analytics is enabled for this department.";
  const UNAVAILABLE =
    "Analytics queries are temporarily unavailable. You can still chat and work within your department.";

  function membership(id: string, name: string) {
    return {
      id: `0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d${id.slice(-2)}`,
      department: { id, code: name.toLowerCase(), name },
      membership_role: "staff" as const,
      dataset_ids: [],
    };
  }

  it("asks multi-department users to choose a context", async () => {
    const created = makeDetail({
      id: OTHER_THREAD_ID,
      title: "New chat",
      revision: 0,
    });
    const { calls } = installFetch((call) => {
      if (call.url === "/api/threads/" && call.method === "POST") {
        return jsonResponse({ thread: created }, 201);
      }
      return null;
    });
    render(
      <Workspace
        bootstrap={{
          ...bootstrapWith(null),
          memberships: [
            membership(FINANCE_ID, "Finance"),
            membership(SALES_ID, "Sales"),
          ],
        }}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    const dialog = await screen.findByRole("dialog", { name: "Start a new chat" });
    await waitFor(() => {
      expect(document.activeElement?.textContent).toBe("Cancel");
    });
    await userEvent.click(within(dialog).getByRole("radio", { name: "Sales" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Start chat" }));

    await waitFor(() => {
      const created = calls.find((call) => call.method === "POST");
      expect(created?.body).toEqual({ department_id: SALES_ID });
    });
  });

  it("preselects the only department and says analytics is unavailable", async () => {
    installFetch(() => null);
    render(
      <Workspace
        bootstrap={{
          ...bootstrapWith(null),
          memberships: [membership(FINANCE_ID, "Finance")],
        }}
      />,
    );

    const selector = screen.getByLabelText("Chat context") as HTMLSelectElement;
    expect(selector.value).toBe(FINANCE_ID);
    expect(document.body.textContent).toContain(
      "Analytics queries are temporarily unavailable. You can still chat and work within your department.",
    );
    expect(screen.queryByRole("button", { name: "Start chat" })).toBeNull();
  });

  it("shows DEV analytics only for listed departments", async () => {
    installFetch(() => null);
    const memberships = [
      membership(SALES_ID, "Sales"),
      membership(MARKETING_ID, "Marketing"),
    ];
    const { unmount } = render(
      <Workspace
        bootstrap={{
          ...bootstrapWith(null),
          analytics_enabled: false,
          analytics_department_ids: [SALES_ID],
          memberships,
        }}
      />,
    );

    expect(document.body.textContent).not.toContain(ENABLED);
    expect(document.body.textContent).not.toContain(UNAVAILABLE);
    await userEvent.selectOptions(screen.getByLabelText("Chat context"), SALES_ID);
    expect(document.body.textContent).toContain(ENABLED);
    expect(document.body.textContent).not.toContain(UNAVAILABLE);
    await userEvent.selectOptions(screen.getByLabelText("Chat context"), MARKETING_ID);
    expect(document.body.textContent).toContain(UNAVAILABLE);
    expect(document.body.textContent).not.toContain(ENABLED);
    await userEvent.selectOptions(screen.getByLabelText("Chat context"), "");
    expect(document.body.textContent).not.toContain(ENABLED);
    expect(document.body.textContent).not.toContain(UNAVAILABLE);

    await userEvent.selectOptions(screen.getByLabelText("Chat context"), SALES_ID);
    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    const dialog = await screen.findByRole("dialog", { name: "Start a new chat" });
    expect(within(dialog).getByText(ENABLED)).toBeTruthy();
    await userEvent.click(within(dialog).getByRole("radio", { name: "Marketing" }));
    expect(within(dialog).getByText(UNAVAILABLE)).toBeTruthy();
    expect(within(dialog).queryByText(ENABLED)).toBeNull();
    await userEvent.click(within(dialog).getByRole("radio", { name: "General chat" }));
    expect(within(dialog).queryByText(ENABLED)).toBeNull();
    expect(within(dialog).queryByText(UNAVAILABLE)).toBeNull();
    unmount();

    const { unmount: unmountSales } = render(
      <Workspace
        bootstrap={{
          ...bootstrapWith(
            makeDetail({ department: membership(SALES_ID, "Sales").department }),
          ),
          analytics_enabled: false,
          analytics_department_ids: [SALES_ID],
          memberships,
        }}
      />,
    );
    expect(screen.getByText(/Context: Sales/)).toBeTruthy();
    expect(document.body.textContent).toContain(ENABLED);
    expect(document.body.textContent).not.toContain(UNAVAILABLE);
    unmountSales();

    render(
      <Workspace
        bootstrap={{
          ...bootstrapWith(
            makeDetail({
              department: membership(MARKETING_ID, "Marketing").department,
            }),
          ),
          analytics_enabled: true,
          analytics_department_ids: [SALES_ID],
          memberships,
        }}
      />,
    );
    expect(screen.getByText(/Context: Marketing/)).toBeTruthy();
    expect(document.body.textContent).toContain(UNAVAILABLE);
    expect(document.body.textContent).not.toContain(ENABLED);
  });

  it("never shows DEV analytics for a no-grant account", () => {
    installFetch(() => null);
    render(
      <Workspace
        bootstrap={{
          ...bootstrapWith(null),
          analytics_enabled: true,
        }}
      />,
    );
    expect(screen.queryByLabelText("Chat context")).toBeNull();
    expect(document.body.textContent).not.toContain(ENABLED);
  });
});
