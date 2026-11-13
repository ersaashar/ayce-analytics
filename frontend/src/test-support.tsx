/**
 * jsdom shims plus a stubbed `fetch`; component code runs unmodified.
 */

import type {
  ActivityStep,
  AttachmentRef,
  ChatLimits,
  ThreadDetail,
  ThreadSummary,
  TurnDetail,
} from "./api";

export const THREAD_ID = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
export const OTHER_THREAD_ID = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";
export const TURN_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";
export const TURN_ID_TWO = "5d4c3b2a-1f0e-4d3c-8b7a-6f5e4d3c2b1a";
export const ATTACHMENT_ID = "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5e";
export const ATTACHMENT_ID_TWO = "7f6e5d4c-3b2a-4f0e-9d8c-7b6a5f4e3d2c";

const ORIGIN = "https://example.test";

/** Install the browser APIs jsdom leaves out, once per test file. */
export function installDomShims(): void {
  Object.defineProperty(globalThis, "location", {
    value: { origin: ORIGIN, pathname: "/", search: "", assign: () => undefined },
    configurable: true,
    writable: true,
  });

  if (typeof window.matchMedia !== "function") {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }),
    });
  }

  if (typeof globalThis.ResizeObserver !== "function") {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    Object.defineProperty(globalThis, "ResizeObserver", {
      configurable: true,
      writable: true,
      value: ResizeObserverStub,
    });
  }

  if (typeof HTMLDialogElement.prototype.showModal !== "function") {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      writable: true,
      value(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      },
    });
  }
  if (typeof HTMLDialogElement.prototype.close !== "function") {
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      writable: true,
      value(this: HTMLDialogElement) {
        this.removeAttribute("open");
        this.dispatchEvent(new Event("close"));
      },
    });
  }

  if (typeof window.scrollTo !== "function") {
    Object.defineProperty(window, "scrollTo", {
      configurable: true,
      writable: true,
      value: () => undefined,
    });
  }
}

export function makeSummary(overrides: Partial<ThreadSummary> = {}): ThreadSummary {
  const id = overrides.id ?? THREAD_ID;
  return {
    id,
    title: "New chat",
    revision: 1,
    created_at: "2026-01-02T03:04:05.000Z",
    updated_at: "2026-01-02T03:04:05.000Z",
    access: "available",
    department: null,
    page_url: `/chat/${id}/`,
    detail_url: `/api/threads/${id}/`,
    messages_url: `/api/threads/${id}/messages/`,
    ...overrides,
  };
}

export function makeLimits(overrides: Partial<ChatLimits> = {}): ChatLimits {
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

export function makeAttachment(overrides: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: ATTACHMENT_ID,
    name: "notes.txt",
    media_type: "text/plain",
    size: 2048,
    ...overrides,
  };
}

export function makeTurn(overrides: Partial<TurnDetail> = {}): TurnDetail {
  const turnId = overrides.turn_id ?? TURN_ID;
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
  };
}

export function makeActivity(
  steps: ActivityStep[],
  truncated = false,
  turnId = TURN_ID,
) {
  return { version: 1, turn_id: turnId, steps, truncated };
}

export function makeDetail(overrides: Partial<ThreadDetail> = {}): ThreadDetail {
  const id = overrides.id ?? THREAD_ID;
  return {
    ...makeSummary({ id }),
    display: [],
    turns: [],
    conversation_error: null,
    ...overrides,
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function sseResponse(chunks: readonly string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

export type StreamHandle = {
  response: Response;
  emit: (chunk: string) => void;
  close: () => void;
};

/** A streaming body the test drives frame by frame. */
export function deferredStream(): StreamHandle {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(next) {
        controller = next;
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
  return {
    response,
    emit(chunk: string) {
      controller?.enqueue(encoder.encode(chunk));
    },
    close() {
      controller?.close();
    },
  };
}

export const frame = {
  delta: (text: string) => `event: delta\ndata: ${JSON.stringify({ text })}\n\n`,
  activity: (turnId: string, step: ActivityStep | null, truncated = false) =>
    `event: activity\ndata: ${JSON.stringify({ turn_id: turnId, step, truncated })}\n\n`,
  done: (reply: string, thread: ThreadSummary, turn: TurnDetail) =>
    `event: done\ndata: ${JSON.stringify({ reply, thread, turn })}\n\n`,
  error: (code: string, message: string) =>
    `event: error\ndata: ${JSON.stringify({ error: { code, message } })}\n\n`,
};
