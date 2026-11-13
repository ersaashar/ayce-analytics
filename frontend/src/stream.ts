/**
 * Incremental decoder for the chat server's SSE frames.
 *
 * POST plus a CSRF header rules out EventSource, so the fetch body is consumed
 * directly. Failures collapse to a fixed `ChatApiError` message.
 */

import {
  type ActivityStep,
  type ActivityUpdate,
  CHAT_PROTOCOL_ERROR,
  ChatApiError,
  errorFromResponse,
  hasExactKeys,
  isRecord,
  parseActivityUpdate,
  parseThreadSummary,
  parseTurnDetail,
  type ThreadSummary,
  type TurnDetail,
} from "./api";

export type ChatEvent =
  | { type: "delta"; text: string }
  | { type: "activity"; update: ActivityUpdate }
  | { type: "done"; reply: string; thread: ThreadSummary; turn: TurnDetail };

type Frame = { event: string; data: string };

/** Activity timeline accumulated for one in-flight turn. */
export type ActivityState = {
  turnId: string;
  steps: ActivityStep[];
  truncated: boolean;
};

function protocolError(): ChatApiError {
  return new ChatApiError("protocol_error", CHAT_PROTOCOL_ERROR);
}

function parseFrame(lines: readonly string[]): Frame | null {
  let event = "";
  const data: string[] = [];

  for (const line of lines) {
    if (line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }

  if (data.length === 0) return null;
  return { event: event || "message", data: data.join("\n") };
}

function toChatEvent(frame: Frame): ChatEvent {
  let payload: unknown;
  try {
    payload = JSON.parse(frame.data);
  } catch {
    throw protocolError();
  }

  if (frame.event === "delta") {
    if (!isRecord(payload) || !hasExactKeys(payload, ["text"])) throw protocolError();
    if (typeof payload.text !== "string") throw protocolError();
    return { type: "delta", text: payload.text };
  }

  if (frame.event === "activity") {
    const update = parseActivityUpdate(payload);
    if (update === null) throw protocolError();
    return { type: "activity", update };
  }

  if (frame.event === "done") {
    if (!isRecord(payload) || !hasExactKeys(payload, ["reply", "thread", "turn"])) {
      throw protocolError();
    }
    const { reply } = payload;
    if (typeof reply !== "string") throw protocolError();
    const thread = parseThreadSummary(payload.thread);
    const turn = parseTurnDetail(payload.turn);
    if (thread === null || turn === null) throw protocolError();
    return { type: "done", reply, thread, turn };
  }

  if (frame.event === "error") {
    if (!isRecord(payload) || !hasExactKeys(payload, ["error"])) throw protocolError();
    const envelope = payload.error;
    if (!isRecord(envelope)) throw protocolError();
    const { code, message } = envelope;
    if (typeof message !== "string" || message.length === 0) throw protocolError();
    if (code !== undefined && (typeof code !== "string" || code.length === 0)) {
      throw protocolError();
    }
    throw new ChatApiError(code ?? "provider_error", message);
  }

  throw protocolError();
}

/**
 * Merge one streamed activity change into the timeline being displayed.
 *
 * Indexes are positional and terminal states are final, so any regression is a
 * protocol failure rather than something to smooth over. An identical repeat is
 * ignored, which keeps a resumed stream idempotent.
 */
export function applyActivityUpdate(
  current: ActivityState | null,
  update: ActivityUpdate,
): ActivityState {
  if (current !== null && current.turnId !== update.turn_id) throw protocolError();

  const base: ActivityState = current ?? {
    turnId: update.turn_id,
    steps: [],
    truncated: false,
  };

  if (update.step === null) {
    if (base.truncated) return base;
    return { ...base, truncated: true };
  }

  const step = update.step;
  const existing = base.steps[step.id];

  if (existing === undefined) {
    if (step.id !== base.steps.length) throw protocolError();
    return {
      ...base,
      steps: [...base.steps, step],
      truncated: base.truncated || update.truncated,
    };
  }

  if (existing.kind !== step.kind) throw protocolError();
  if (existing.status !== "running") {
    if (existing.status === step.status) return base;
    throw protocolError();
  }
  if (step.status === "running") {
    if (update.truncated === base.truncated) return base;
    return { ...base, truncated: base.truncated || update.truncated };
  }

  const steps = [...base.steps];
  steps[step.id] = step;
  return { ...base, steps, truncated: base.truncated || update.truncated };
}

type Emitted = { event: ChatEvent; finished: boolean };

function emit(frame: Frame): Emitted {
  const event = toChatEvent(frame);
  return { event, finished: event.type === "done" };
}

/** Yield typed events from an SSE body, stopping after the terminal done. */
export async function* readChatStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let lines: string[] = [];
  let finished = false;

  const takeFrames = (chunk: string): Frame[] => {
    buffered += chunk;
    const found: Frame[] = [];
    let newline = buffered.indexOf("\n");
    while (newline !== -1) {
      let line = buffered.slice(0, newline);
      buffered = buffered.slice(newline + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (line === "") {
        const frame = parseFrame(lines);
        if (frame) found.push(frame);
        lines = [];
      } else {
        lines.push(line);
      }
      newline = buffered.indexOf("\n");
    }
    return found;
  };

  // A completed turn is terminal the moment its frame is decoded, so a consumer
  // that stops right after done must not cancel an already finished response.
  try {
    while (!finished) {
      const { value, done: ended } = await reader.read();
      if (ended) {
        const tail = decoder.decode();
        for (const frame of tail.length > 0 ? takeFrames(tail) : []) {
          const emitted = emit(frame);
          finished = emitted.finished;
          yield emitted.event;
          if (finished) break;
        }
        break;
      }

      for (const frame of takeFrames(decoder.decode(value, { stream: true }))) {
        const emitted = emit(frame);
        finished = emitted.finished;
        yield emitted.event;
        if (finished) break;
      }
    }
  } finally {
    if (!finished) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }

  if (!finished) throw protocolError();
}

/** Validate the response envelope before decoding any event frames. */
export async function* readChatResponse(response: Response): AsyncGenerator<ChatEvent> {
  if (!response.ok) throw await errorFromResponse(response);

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("text/event-stream") || !response.body) {
    throw protocolError();
  }

  yield* readChatStream(response.body);
}
