import { describe, expect, it } from "vitest";

import {
  CHAT_PROTOCOL_ERROR,
  ChatApiError,
  type ThreadSummary,
  type TurnDetail,
} from "./api";
import {
  type ActivityState,
  applyActivityUpdate,
  type ChatEvent,
  readChatResponse,
  readChatStream,
} from "./stream";

const ORIGIN = "https://example.test";
Object.defineProperty(globalThis, "location", {
  value: { origin: ORIGIN },
  configurable: true,
});

const THREAD_ID = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const TURN_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";

function makeSummary(overrides: Partial<ThreadSummary> = {}): ThreadSummary {
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
  };
}

function streamOf(
  chunks: readonly (string | Uint8Array)[],
): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
      }
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<ChatEvent[]> {
  const events: ChatEvent[] = [];
  for await (const event of readChatStream(stream)) events.push(event);
  return events;
}

function makeTurn(overrides: Partial<TurnDetail> = {}): TurnDetail {
  return {
    turn_id: TURN_ID,
    turn_index: 0,
    response: {
      requested_model: "offline-model",
      resolved_model: null,
      completed_at: "2026-01-02T03:04:06.000Z",
      duration_ms: 1200,
      output_tokens: 42,
      output_limit_reached: false,
    },
    activity: null,
    attachments: [],
    can_regenerate: true,
    can_delete: true,
    regenerate_url: `/api/threads/${THREAD_ID}/turns/${TURN_ID}/regenerate/`,
    delete_url: `/api/threads/${THREAD_ID}/turns/${TURN_ID}/`,
    ...overrides,
  };
}

const deltaFrame = (text: string) =>
  `event: delta\ndata: ${JSON.stringify({ text })}\n\n`;

const activityFrame = (payload: unknown) =>
  `event: activity\ndata: ${JSON.stringify(payload)}\n\n`;

const doneFrame = (reply: string, thread: Partial<ThreadSummary> = {}) =>
  `event: done\ndata: ${JSON.stringify({
    reply,
    thread: makeSummary({ revision: 1, ...thread }),
    turn: makeTurn(),
  })}\n\n`;

const completed = (reply: string) => ({
  type: "done" as const,
  reply,
  thread: makeSummary({ revision: 1 }),
  turn: makeTurn(),
});

describe("readChatStream", () => {
  it("yields deltas in order followed by the terminal turn", async () => {
    const events = await collect(
      streamOf([deltaFrame("Hel"), deltaFrame("lo"), doneFrame("Hello")]),
    );

    expect(events).toEqual([
      { type: "delta", text: "Hel" },
      { type: "delta", text: "lo" },
      completed("Hello"),
    ]);
  });

  it("reconstructs text split inside UTF-8 sequences", async () => {
    const text = "héllo 😀 語";
    const bytes = new TextEncoder().encode(deltaFrame(text));
    // Split the 4-byte emoji twice and the 3-byte kanji once.
    const emoji = bytes.indexOf(0xf0) + 1;
    const kanji = bytes.indexOf(0xe8) + 2;
    const chunks = [
      bytes.slice(0, emoji),
      bytes.slice(emoji, kanji),
      bytes.slice(kanji),
    ];

    const events = await collect(streamOf([...chunks, doneFrame("ok")]));

    expect(events[0]).toEqual({ type: "delta", text });
  });

  it("handles CRLF frame boundaries split across chunks", async () => {
    const events = await collect(
      streamOf([
        'event: delta\r\ndata: {"text":"a"}\r',
        '\n\r\nevent: done\r\ndata: {"reply":"a","thread":',
        `${JSON.stringify(makeSummary({ revision: 1 }))},"turn":` +
          `${JSON.stringify(makeTurn())}}\r\n\r`,
        "\n",
      ]),
    );

    expect(events).toEqual([{ type: "delta", text: "a" }, completed("a")]);
  });

  it("parses several frames delivered in one chunk", async () => {
    const events = await collect(
      streamOf([`${deltaFrame("one")}${deltaFrame("two")}${doneFrame("onetwo")}`]),
    );

    expect(events.map((event) => event.type)).toEqual(["delta", "delta", "done"]);
  });

  it("ignores comment and empty frames", async () => {
    const events = await collect(
      streamOf([": connected\n\n\n\n", deltaFrame("x"), doneFrame("x")]),
    );

    expect(events).toEqual([{ type: "delta", text: "x" }, completed("x")]);
  });

  it("joins multiple data lines of one frame", async () => {
    const events = await collect(
      streamOf([
        'event: delta\ndata: {"text":\ndata: "joined"}\n\n',
        doneFrame("joined"),
      ]),
    );

    expect(events[0]).toEqual({ type: "delta", text: "joined" });
  });

  it("stops after the terminal frame without reading further", async () => {
    const events = await collect(streamOf([doneFrame("last"), deltaFrame("ignored")]));

    expect(events).toEqual([completed("last")]);
  });

  it("fails with a public protocol error on invalid JSON", async () => {
    const events: ChatEvent[] = [];
    await expect(async () => {
      for await (const event of readChatStream(
        streamOf(["event: delta\ndata: {oops\n\n"]),
      )) {
        events.push(event);
      }
    }).rejects.toThrow(CHAT_PROTOCOL_ERROR);
    expect(events).toEqual([]);
  });

  it("fails on a malformed delta shape", async () => {
    await expect(
      collect(streamOf(['event: delta\ndata: {"text":5}\n\n'])),
    ).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("fails on an unknown event name", async () => {
    await expect(
      collect(streamOf(['event: ping\ndata: {"text":"x"}\n\n'])),
    ).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("rejects a done frame without thread metadata", async () => {
    await expect(
      collect(streamOf(['event: done\ndata: {"reply":"x"}\n\n'])),
    ).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("rejects thread metadata with an unsafe page url", async () => {
    const payload = JSON.stringify({
      reply: "x",
      thread: makeSummary({ revision: 1, page_url: "//evil.test/chat/" }),
    });
    await expect(
      collect(streamOf([`event: done\ndata: ${payload}\n\n`])),
    ).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("surfaces a server error frame's code and public message", async () => {
    const failure = collect(
      streamOf([
        deltaFrame("partial"),
        'event: error\ndata: {"error":{"code":"provider_timeout","message":"The model provider timed out. Try again."}}\n\n',
      ]),
    );

    await expect(failure).rejects.toThrow("The model provider timed out. Try again.");
    await expect(failure).rejects.toMatchObject({ code: "provider_timeout" });
  });

  it("propagates a thread conflict code from an error frame", async () => {
    await expect(
      collect(
        streamOf([
          'event: error\ndata: {"error":{"code":"thread_conflict","message":"This chat changed in another tab. Reload it before trying again."}}\n\n',
        ]),
      ),
    ).rejects.toMatchObject({ code: "thread_conflict" });
  });

  it("fails with the fixed message when an error frame has no message", async () => {
    await expect(
      collect(streamOf(['event: error\ndata: {"error":{"code":"x"}}\n\n'])),
    ).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("does not cancel a body that already delivered done", async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(doneFrame("x")));
      },
      cancel() {
        cancelled = true;
      },
    });

    const events = await collect(stream);

    expect(events).toEqual([completed("x")]);
    expect(cancelled).toBe(false);
  });

  it("does not cancel when a consumer stops right after done", async () => {
    let cancelled = false;
    const encoder = new TextEncoder();
    let pulled = 0;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(doneFrame("x")));
      },
      pull(controller) {
        pulled += 1;
        controller.enqueue(encoder.encode(deltaFrame("late")));
      },
      cancel() {
        cancelled = true;
      },
    });

    for await (const event of readChatStream(stream)) {
      expect(event.type).toBe("done");
      break;
    }

    expect(cancelled).toBe(false);
    expect(pulled).toBeLessThanOrEqual(1);
  });

  it("cancels a body abandoned on a protocol error", async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("event: delta\ndata: {oops\n\n"));
      },
      cancel() {
        cancelled = true;
      },
    });

    await expect(collect(stream)).rejects.toThrow(CHAT_PROTOCOL_ERROR);
    expect(cancelled).toBe(true);
  });

  it("fails when the stream ends before done", async () => {
    const events: ChatEvent[] = [];
    await expect(async () => {
      for await (const event of readChatStream(streamOf([deltaFrame("half")]))) {
        events.push(event);
      }
    }).rejects.toThrow(CHAT_PROTOCOL_ERROR);
    expect(events).toEqual([{ type: "delta", text: "half" }]);
  });
});

describe("activity frames", () => {
  const runningStep = {
    turn_id: TURN_ID,
    step: { id: 0, kind: "model", status: "running" },
    truncated: false,
  };

  it("decodes an activity frame between deltas", async () => {
    const events = await collect(
      streamOf([activityFrame(runningStep), deltaFrame("hi"), doneFrame("hi")]),
    );

    expect(events[0]).toEqual({ type: "activity", update: runningStep });
    expect(events[1]).toEqual({ type: "delta", text: "hi" });
    expect(events[2].type).toBe("done");
  });

  it("decodes the step detail the server sends when detail is enabled", async () => {
    const started = {
      turn_id: TURN_ID,
      step: {
        id: 0,
        kind: "model" as const,
        status: "running" as const,
        title: "Thinking...",
      },
      truncated: false,
    };
    const finished = {
      turn_id: TURN_ID,
      step: {
        id: 0,
        kind: "model" as const,
        status: "complete" as const,
        title: "Analyzing data...",
        duration_ms: 12,
      },
      truncated: false,
    };
    const events = await collect(
      streamOf([
        activityFrame(started),
        activityFrame(finished),
        deltaFrame("hi"),
        doneFrame("hi"),
      ]),
    );

    expect(events[0]).toEqual({ type: "activity", update: started });
    expect(events[1]).toEqual({ type: "activity", update: finished });

    const state = applyActivityUpdate(applyActivityUpdate(null, started), finished);
    expect(state.steps[0]).toEqual(finished.step);
  });

  it("rejects a malformed activity payload", async () => {
    for (const payload of [
      { ...runningStep, step: null, truncated: false },
      { ...runningStep, extra: 1 },
      { ...runningStep, step: { id: 0, kind: "model", status: "done" } },
      {
        ...runningStep,
        step: { id: 0, kind: "model", status: "running", tool_name: "run_sql" },
      },
      { turn_id: "nope", step: null, truncated: true },
    ]) {
      await expect(
        collect(streamOf([activityFrame(payload), doneFrame("x")])),
      ).rejects.toThrow(CHAT_PROTOCOL_ERROR);
    }
  });

  it("rejects a done frame without the committed turn", async () => {
    const payload = JSON.stringify({
      reply: "x",
      thread: makeSummary({ revision: 1 }),
    });
    await expect(
      collect(streamOf([`event: done\ndata: ${payload}\n\n`])),
    ).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("rejects unknown root keys on any frame", async () => {
    const delta = 'event: delta\ndata: {"text":"x","extra":1}\n\n';
    const done = `event: done\ndata: ${JSON.stringify({
      reply: "x",
      thread: makeSummary({ revision: 1 }),
      turn: makeTurn(),
      extra: 1,
    })}\n\n`;
    for (const body of [delta, done]) {
      await expect(collect(streamOf([body]))).rejects.toThrow(CHAT_PROTOCOL_ERROR);
    }
  });
});

describe("applyActivityUpdate", () => {
  const step = (id: number, status: "running" | "complete" | "failed") => ({
    turn_id: TURN_ID,
    step: { id, kind: "wren_tool" as const, status },
    truncated: false,
  });

  it("binds the turn id, appends steps, and finishes them", () => {
    let state: ActivityState | null = null;
    state = applyActivityUpdate(state, step(0, "running"));
    expect(state).toEqual({
      turnId: TURN_ID,
      steps: [{ id: 0, kind: "wren_tool", status: "running" }],
      truncated: false,
    });

    state = applyActivityUpdate(state, step(0, "complete"));
    expect(state.steps[0]?.status).toBe("complete");

    state = applyActivityUpdate(state, step(1, "failed"));
    expect(state.steps.map((entry) => entry.status)).toEqual(["complete", "failed"]);
  });

  it("ignores an identical repeat and a duplicate truncation notice", () => {
    const first = applyActivityUpdate(null, step(0, "running"));
    expect(applyActivityUpdate(first, step(0, "running"))).toEqual(first);

    const notice = { turn_id: TURN_ID, step: null, truncated: true };
    const truncated = applyActivityUpdate(first, notice);
    expect(truncated.truncated).toBe(true);
    expect(applyActivityUpdate(truncated, notice)).toEqual(truncated);
  });

  it("keeps truncation sticky across later steps", () => {
    const started = applyActivityUpdate(null, step(0, "running"));
    const truncated = applyActivityUpdate(started, {
      turn_id: TURN_ID,
      step: null,
      truncated: true,
    });
    const finished = applyActivityUpdate(truncated, {
      turn_id: TURN_ID,
      step: { id: 0, kind: "wren_tool", status: "complete" },
      truncated: true,
    });
    expect(finished.truncated).toBe(true);
    expect(finished.steps[0]?.status).toBe("complete");
  });

  it("rejects a terminal regression, a changed kind, and a skipped index", () => {
    const done = applyActivityUpdate(null, step(0, "complete"));
    expect(() => applyActivityUpdate(done, step(0, "failed"))).toThrow(
      CHAT_PROTOCOL_ERROR,
    );
    expect(() =>
      applyActivityUpdate(done, {
        turn_id: TURN_ID,
        step: { id: 0, kind: "model", status: "complete" },
        truncated: false,
      }),
    ).toThrow(CHAT_PROTOCOL_ERROR);
    expect(() => applyActivityUpdate(done, step(2, "running"))).toThrow(
      CHAT_PROTOCOL_ERROR,
    );
  });

  it("rejects an update for another turn", () => {
    const first = applyActivityUpdate(null, step(0, "running"));
    expect(() =>
      applyActivityUpdate(first, {
        turn_id: "2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f",
        step: { id: 1, kind: "model", status: "running" },
        truncated: false,
      }),
    ).toThrow(CHAT_PROTOCOL_ERROR);
  });
});

describe("readChatResponse", () => {
  it("rejects a non-SSE response with the fixed protocol message", async () => {
    const response = new Response("<html>nope</html>", {
      status: 200,
      headers: { "content-type": "text/html" },
    });

    await expect(async () => {
      for await (const _event of readChatResponse(response)) {
        // no events expected
      }
    }).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("surfaces a JSON error envelope's code and public message", async () => {
    const response = new Response(
      JSON.stringify({
        error: {
          code: "conversation_limit",
          message: "This conversation has reached its limit. Start a new chat.",
        },
      }),
      { status: 400, headers: { "content-type": "application/json" } },
    );

    const failure = readChatResponse(response);
    await expect(async () => {
      for await (const _event of failure) {
        // no events expected
      }
    }).rejects.toThrow("This conversation has reached its limit. Start a new chat.");
  });

  it("never shows an HTML error body", async () => {
    const response = new Response("<html>Sign in</html>", {
      status: 401,
      headers: { "content-type": "text/html" },
    });

    try {
      for await (const _event of readChatResponse(response)) {
        // no events expected
      }
      expect.unreachable();
    } catch (caught) {
      expect(caught).toBeInstanceOf(ChatApiError);
      expect(caught).toMatchObject({ code: "authentication_required" });
      expect((caught as Error).message).not.toContain("html");
    }
  });

  it("fails when a JSON error envelope is unreadable", async () => {
    const response = new Response("not json", {
      status: 502,
      headers: { "content-type": "application/json" },
    });

    await expect(async () => {
      for await (const _event of readChatResponse(response)) {
        // no events expected
      }
    }).rejects.toThrow(CHAT_PROTOCOL_ERROR);
  });

  it("decodes SSE responses", async () => {
    const response = new Response(`${deltaFrame("hi")}${doneFrame("hi")}`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });

    const events: ChatEvent[] = [];
    for await (const event of readChatResponse(response)) events.push(event);
    expect(events).toEqual([{ type: "delta", text: "hi" }, completed("hi")]);
  });
});
