// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { Chat, type ChatMessage } from "./Chat";
import {
  installDomShims,
  makeActivity,
  makeAttachment,
  makeLimits,
  makeTurn,
  TURN_ID,
  TURN_ID_TWO,
} from "./test-support";

beforeAll(() => installDomShims());

beforeEach(() => {
  // jsdom has no clipboard implementation.
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn(async () => undefined) },
  });
});

afterEach(cleanup);

function assistant(
  id: string,
  content: string,
  turn: ChatMessage["turn"] = null,
  pending = false,
): ChatMessage {
  return { id, role: "assistant", content, turn, attachments: [], pending };
}

function user(id: string, content: string): ChatMessage {
  return {
    id,
    role: "user",
    content,
    turn: null,
    attachments: [],
    pending: false,
  };
}

const baseProps = {
  messages: [] as ChatMessage[],
  running: false,
  disabled: false,
  draft: "",
  status: "",
  alert: null,
  focusRequest: 0,
  transientActivity: null,
  regenPreview: null,
  attachments: [],
  limits: makeLimits(),
  remainingThreadSlots: 5,
  onDraftChange: () => undefined,
  onSend: () => undefined,
  onAttach: () => undefined,
  onRemoveAttachment: () => undefined,
  onRegenerate: () => undefined,
  onDeleteTurn: async () => null,
};

function renderChat(overrides: Partial<Parameters<typeof Chat>[0]> = {}) {
  return render(<Chat {...baseProps} {...overrides} />);
}

function assistantMessage() {
  return document.querySelector(".message[data-role='assistant']") as HTMLElement;
}

describe("message actions", () => {
  it("offers Copy on a completed answer and nothing else for a legacy turn", () => {
    renderChat({ messages: [user("t:0", "q"), assistant("t:1", "old answer")] });

    const actions = within(assistantMessage()).getAllByRole("button");
    expect(actions.map((button) => button.textContent)).toEqual(["Copy"]);
  });

  it("offers Regenerate and Delete only for the latest actionable turn", async () => {
    const onRegenerate = vi.fn();
    renderChat({
      messages: [
        user("t:0", "first"),
        assistant(
          "t:1",
          "first answer",
          makeTurn({ turn_index: 0, can_regenerate: false, can_delete: false }),
        ),
        user("t:2", "second"),
        assistant(
          "t:3",
          "second answer",
          makeTurn({ turn_index: 1, can_regenerate: true, can_delete: true }),
        ),
      ],
      onRegenerate,
    });

    const messages = document.querySelectorAll(".message[data-role='assistant']");
    const older = within(messages[0] as HTMLElement).getAllByRole("button");
    expect(older.map((button) => button.textContent)).toEqual(["Copy"]);

    const latest = within(messages[1] as HTMLElement).getAllByRole("button");
    expect(latest.map((button) => button.textContent)).toEqual([
      "Copy",
      "Regenerate",
      "Delete",
    ]);

    await userEvent.click(
      within(messages[1] as HTMLElement).getByRole("button", { name: "Regenerate" }),
    );
    expect(onRegenerate).toHaveBeenCalledWith(TURN_ID);
  });

  it("shows no actions on a pending answer", () => {
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "streaming", null, true)],
      running: true,
    });
    expect(within(assistantMessage()).queryAllByRole("button")).toHaveLength(0);
  });

  it("disables response actions while busy", () => {
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "answer", makeTurn())],
      disabled: true,
    });
    for (const name of ["Regenerate", "Delete"]) {
      const button = within(assistantMessage()).getByRole("button", { name });
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
  });
});

describe("delete exchange dialog", () => {
  it("names both messages, then removes them on confirm", async () => {
    const onDeleteTurn = vi.fn(async () => null);
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "answer", makeTurn())],
      onDeleteTurn,
    });

    await userEvent.click(
      within(assistantMessage()).getByRole("button", { name: "Delete" }),
    );

    const dialog = screen.getByRole("dialog", { name: "Delete this exchange?" });
    expect(dialog.textContent).toContain(
      "The prompt and its response will both be removed.",
    );
    expect(document.activeElement).toBe(
      within(dialog).getByRole("button", { name: "Cancel" }),
    );

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete exchange" }),
    );
    await waitFor(() => expect(onDeleteTurn).toHaveBeenCalledWith(TURN_ID));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Delete this exchange?" }),
      ).toBeNull(),
    );
  });

  it("keeps the dialog and shows the server error on failure", async () => {
    const onDeleteTurn = vi.fn(async () => "This chat changed in another tab.");
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "answer", makeTurn())],
      onDeleteTurn,
    });

    await userEvent.click(
      within(assistantMessage()).getByRole("button", { name: "Delete" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Delete this exchange?" });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete exchange" }),
    );

    await waitFor(() =>
      expect(within(dialog).getByRole("alert").textContent).toBe(
        "This chat changed in another tab.",
      ),
    );
    expect(screen.getByRole("dialog", { name: "Delete this exchange?" })).toBeTruthy();
    expect(assistantMessage().textContent).toContain("answer");
  });

  it("cancels with Escape without deleting", async () => {
    const onDeleteTurn = vi.fn(async () => null);
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "answer", makeTurn())],
      onDeleteTurn,
    });
    await userEvent.click(
      within(assistantMessage()).getByRole("button", { name: "Delete" }),
    );
    await userEvent.keyboard("{Escape}");

    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Delete this exchange?" }),
      ).toBeNull(),
    );
    expect(onDeleteTurn).not.toHaveBeenCalled();
  });
});

describe("thoughts panel", () => {
  it("renders nothing without real steps", () => {
    renderChat({
      messages: [
        user("t:0", "q"),
        assistant("t:1", "answer", makeTurn({ activity: makeActivity([]) })),
      ],
    });
    expect(document.querySelector(".thoughts")).toBeNull();
  });

  it("expands once while running and collapses when complete", async () => {
    const { rerender } = renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "working", null, true)],
      running: true,
      transientActivity: {
        turnId: TURN_ID,
        steps: [{ id: 0, kind: "model", status: "running" }],
        truncated: false,
      },
    });

    const toggle = screen.getByRole("button", { name: "Thoughts" });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.body.textContent).toContain("Preparing response");

    rerender(
      <Chat
        {...baseProps}
        messages={[
          user("t:0", "q"),
          assistant(
            "t:1",
            "done",
            makeTurn({
              activity: makeActivity([{ id: 0, kind: "model", status: "complete" }]),
            }),
          ),
        ]}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Thoughts" }).getAttribute("aria-expanded"),
    ).toBe("false");
  });

  it("shows persisted steps collapsed and keeps a manual choice", async () => {
    renderChat({
      messages: [
        user("t:0", "q"),
        assistant(
          "t:1",
          "answer",
          makeTurn({
            activity: makeActivity([
              { id: 0, kind: "model", status: "complete" },
              { id: 1, kind: "wren_tool", status: "failed" },
            ]),
          }),
        ),
      ],
    });

    const toggle = screen.getByRole("button", { name: "Thoughts" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await userEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.body.textContent).toContain("Response prepared");
    expect(document.body.textContent).toContain("Analytics step failed");
    // Fixed labels only: no tool names, arguments, or results.
    expect(document.body.textContent).not.toContain("run_sql");
  });

  it("shows distinct thoughts panels for separate turns", async () => {
    renderChat({
      messages: [
        user("t:0", "first question"),
        assistant(
          "t:1",
          "first answer",
          makeTurn({
            activity: makeActivity([
              {
                id: 0,
                kind: "model",
                status: "complete",
                title: "First title",
                duration_ms: 1500,
              },
            ]),
          }),
        ),
        user("t:2", "second question"),
        assistant(
          "t:3",
          "second answer",
          makeTurn({
            turn_id: TURN_ID_TWO,
            activity: makeActivity([
              {
                id: 0,
                kind: "model",
                status: "complete",
                title: "Second title",
                duration_ms: 2000,
              },
            ]),
          }),
        ),
      ],
    });

    // First turn's thoughts panel
    const messages = document.querySelectorAll(".message[data-role='assistant']");
    const firstMsg = messages[0] as HTMLElement;
    const firstToggle = within(firstMsg).getByRole("button", { name: "Thoughts" });
    await userEvent.click(firstToggle);
    expect(firstToggle.getAttribute("aria-expanded")).toBe("true");
    expect(within(firstMsg).queryByText("First title")).not.toBeNull();
    expect(within(firstMsg).queryByText("Second title")).toBeNull();

    // Second turn's thoughts panel
    const secondMsg = messages[1] as HTMLElement;
    const secondToggle = within(secondMsg).getByRole("button", { name: "Thoughts" });
    await userEvent.click(secondToggle);
    expect(secondToggle.getAttribute("aria-expanded")).toBe("true");
    expect(within(secondMsg).queryByText("Second title")).not.toBeNull();
    expect(within(secondMsg).queryByText("First title")).toBeNull();

    // Rerender with a pending third turn and transient activity; historical panels unchanged
    renderChat({
      messages: [
        user("t:0", "first question"),
        assistant(
          "t:1",
          "first answer",
          makeTurn({
            activity: makeActivity([
              {
                id: 0,
                kind: "model",
                status: "complete",
                title: "First title",
                duration_ms: 1500,
              },
            ]),
          }),
        ),
        user("t:2", "second question"),
        assistant(
          "t:3",
          "second answer",
          makeTurn({
            turn_id: TURN_ID_TWO,
            activity: makeActivity([
              {
                id: 0,
                kind: "model",
                status: "complete",
                title: "Second title",
                duration_ms: 2000,
              },
            ]),
          }),
        ),
        user("t:4", "third question"),
        assistant("t:5", "third answer", null, true),
      ],
      running: true,
      transientActivity: {
        turnId: "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        steps: [{ id: 0, kind: "model", status: "running" }],
        truncated: false,
      },
    });

    // Re-query historical messages after rerender
    const rerenderedMessages = document.querySelectorAll(
      ".message[data-role='assistant']",
    );
    const rerenderedFirstMsg = rerenderedMessages[0] as HTMLElement;
    expect(within(rerenderedFirstMsg).queryByText("First title")).not.toBeNull();
    expect(within(rerenderedFirstMsg).queryByText("Second title")).toBeNull();

    const rerenderedSecondMsg = rerenderedMessages[1] as HTMLElement;
    expect(within(rerenderedSecondMsg).queryByText("Second title")).not.toBeNull();
    expect(within(rerenderedSecondMsg).queryByText("First title")).toBeNull();
  });

  it("latest turn with can_regenerate=false, can_delete=true offers Delete but not Regenerate", () => {
    renderChat({
      messages: [
        user("t:0", "first"),
        assistant(
          "t:1",
          "answer",
          makeTurn({ turn_index: 0, can_regenerate: false, can_delete: true }),
        ),
      ],
    });

    const actions = within(assistantMessage()).getAllByRole("button");
    const actionTexts = actions.map((button) => button.textContent);
    expect(actionTexts).toContain("Delete");
    expect(actionTexts).not.toContain("Regenerate");
  });

  it("reports truncation once with the fixed notice", () => {
    renderChat({
      messages: [
        user("t:0", "q"),
        assistant(
          "t:1",
          "answer",
          makeTurn({
            activity: makeActivity(
              [{ id: 0, kind: "wren_tool", status: "complete" }],
              true,
            ),
          }),
        ),
      ],
    });
    expect(document.body.textContent).toContain("Additional activity omitted");
  });
});

describe("response metrics", () => {
  it("shows the model and completion time", () => {
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "answer", makeTurn())],
    });
    const meta = document.querySelector(".message-meta");
    expect(meta?.textContent).toContain("deepseek/deepseek-v4.1-flash-1017");
    expect(meta?.textContent).not.toContain("Requested");
    const time = document.querySelector(".message-meta time");
    expect(time?.getAttribute("datetime")).toBe("2026-01-02T03:04:06.000Z");
    expect(time?.textContent).not.toBe("");
  });

  it("prefers a resolved model when the server provides one", () => {
    renderChat({
      messages: [
        user("t:0", "q"),
        assistant(
          "t:1",
          "answer",
          makeTurn({
            response: {
              requested_model: "alias",
              resolved_model: "vendor/actual",
              completed_at: "2026-01-02T03:04:06.000Z",
              duration_ms: 10,
              output_tokens: 5,
              output_limit_reached: false,
            },
          }),
        ),
      ],
    });
    const meta = document.querySelector(".message-meta");
    expect(meta?.textContent).toContain("vendor/actual");
    expect(meta?.textContent).not.toContain("alias");
  });

  it("omits metrics for legacy turns and corrupted metadata", () => {
    renderChat({
      messages: [
        user("t:0", "q"),
        assistant("t:1", "legacy"),
        assistant(
          "t:2",
          "answer",
          makeTurn({
            response: {
              requested_model: null,
              resolved_model: null,
              completed_at: null,
              duration_ms: null,
              output_tokens: null,
              output_limit_reached: false,
            },
          }),
        ),
      ],
    });
    expect(document.querySelector(".message-meta")).toBeNull();
  });
});

describe("regeneration preview", () => {
  it("labels the replacement and keeps the original answer visible", () => {
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "original answer", makeTurn())],
      running: true,
      regenPreview: {
        messageId: "t:1",
        text: "replacement in progress",
        activity: {
          turnId: TURN_ID,
          steps: [{ id: 0, kind: "model", status: "running" }],
          truncated: false,
        },
      },
    });

    expect(assistantMessage().textContent).toContain("original answer");
    const preview = document.querySelector(".regen-preview");
    expect(preview?.textContent).toContain("Regenerating response");
    expect(preview?.textContent).toContain("replacement in progress");
    // A preview is not a completed response, so it carries no actions.
    const previewButtons = preview?.querySelectorAll(".message-actions button");
    expect(previewButtons?.length ?? 0).toBe(0);
  });
});

describe("composer", () => {
  it("sends on Enter and keeps Shift+Enter for a newline", async () => {
    const onSend = vi.fn();
    const onDraftChange = vi.fn();
    const { rerender } = renderChat({
      messages: [],
      draft: "",
      onSend,
      onDraftChange,
    });

    const box = screen.getByRole("textbox", { name: "Message" });
    await userEvent.click(box);
    await userEvent.keyboard("hello");
    expect(onDraftChange).toHaveBeenCalled();

    rerender(<Chat {...baseProps} messages={[]} draft="hello" onSend={onSend} />);
    await userEvent.keyboard("{Shift>}{Enter}{/Shift}");
    expect(onSend).not.toHaveBeenCalled();

    await userEvent.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("shows Stop while running and aborts instead of sending", async () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "streaming", null, true)],
      running: true,
      draft: "ignored",
      onSend,
      onStop,
    });

    const stop = screen.getByRole("button", { name: "Stop generating" });
    await userEvent.click(stop);
    expect(onStop).toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not send while an IME composition is active", async () => {
    const onSend = vi.fn();
    renderChat({ messages: [], draft: "こんにちは", onSend });

    const box = screen.getByRole("textbox", { name: "Message" });
    box.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    await userEvent.click(box);
    await userEvent.keyboard("{Enter}");
    expect(onSend).not.toHaveBeenCalled();

    box.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
    await userEvent.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith("こんにちは");
  });
});

function fileInput(): HTMLInputElement {
  const input = document.querySelector("input[type='file']");
  if (!(input instanceof HTMLInputElement)) throw new Error("missing file input");
  return input;
}

function draftAttachment(name: string, size = 5, id = "draft-1") {
  return { id, file: new File(["x".repeat(size)], name, { type: "text/plain" }) };
}

describe("attachments", () => {
  it("passes picked files to the workspace and removes by filename", async () => {
    const onAttach = vi.fn();
    const onRemoveAttachment = vi.fn();
    const attachment = draftAttachment("alpha.txt");
    renderChat({
      messages: [],
      attachments: [attachment],
      remainingThreadSlots: 4,
      onAttach,
      onRemoveAttachment,
    });

    expect(screen.getByText("alpha.txt")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Remove alpha.txt" }));
    expect(onRemoveAttachment).toHaveBeenCalledWith("draft-1");

    await userEvent.upload(fileInput(), attachment.file);
    expect(onAttach).toHaveBeenCalledTimes(1);
    expect(onAttach.mock.calls[0]?.[0]).toEqual([attachment.file]);
    expect(fileInput().value).toBe("");
  });

  it("disables only attachment selection when a quota is exhausted", async () => {
    const onSend = vi.fn();
    renderChat({ messages: [], draft: "still sends", remainingThreadSlots: 0, onSend });

    const attach = screen.getByRole("button", { name: "Attach a file to this chat" });
    expect((attach as HTMLButtonElement).disabled).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSend).toHaveBeenCalledWith("still sends");
  });

  it("disables attach and remove while a reply is running", () => {
    renderChat({
      messages: [user("t:0", "q"), assistant("t:1", "streaming", null, true)],
      attachments: [draftAttachment("busy.txt")],
      running: true,
    });

    expect(
      (
        screen.getByRole("button", {
          name: "Attach a file to this chat",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Remove busy.txt" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("shows sent file labels and the output cap warning", () => {
    const turn = makeTurn();
    renderChat({
      messages: [
        {
          id: "t:0",
          role: "user",
          content: "question",
          turn: null,
          attachments: [makeAttachment({ name: "quarterly-report.docx", size: 2048 })],
          pending: false,
        },
        assistant(
          "t:1",
          "partial answer",
          makeTurn({
            response: turn.response && {
              ...turn.response,
              output_limit_reached: true,
            },
          }),
        ),
      ],
    });

    expect(screen.getByText("quarterly-report.docx")).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
    expect(
      screen.getByText("Output limit reached; the answer may be incomplete."),
    ).toBeTruthy();
  });
});
