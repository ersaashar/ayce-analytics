import { useCallback, useEffect, useId, useRef, useState } from "react";

import type { ActivityStatus, ActivityStep } from "../../api";

/** Both the streamed and the persisted timeline satisfy this view. */
export type ActivityView = {
  steps: ActivityStep[];
  truncated: boolean;
};

const LABELS: Record<ActivityStep["kind"], Record<ActivityStatus, string>> = {
  model: {
    running: "Preparing response",
    complete: "Response prepared",
    failed: "Response preparation failed",
  },
  wren_tool: {
    running: "Using analytics tools",
    complete: "Analytics step completed",
    failed: "Analytics step failed",
  },
};

function BrainIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="15"
      height="15"
      viewBox="0 0 256 256"
      fill="currentColor"
    >
      <path d="M248,124a56.11,56.11,0,0,0-32-50.61V72a48,48,0,0,0-88-26.49A48,48,0,0,0,40,72v1.39a56,56,0,0,0,0,101.2V176a48,48,0,0,0,88,26.49A48,48,0,0,0,216,176v-1.41A56.09,56.09,0,0,0,248,124ZM88,208a32,32,0,0,1-31.81-28.56A55.87,55.87,0,0,0,64,180h8a8,8,0,0,0,0-16H64A40,40,0,0,1,50.67,86.27,8,8,0,0,0,56,78.73V72a32,32,0,0,1,64,0v68.26A47.8,47.8,0,0,0,88,128a8,8,0,0,0,0,16,32,32,0,0,1,0,64Zm104-44h-8a8,8,0,0,0,0,16h8a55.87,55.87,0,0,0,7.81-.56A32,32,0,1,1,168,144a8,8,0,0,0,0-16,47.8,47.8,0,0,0-32,12.26V72a32,32,0,0,1,64,0v6.73a8,8,0,0,0,5.33,7.54A40,40,0,0,1,192,164Zm16-52a8,8,0,0,1-8,8h-4a36,36,0,0,1-36-36V80a8,8,0,0,1,16,0v4a20,20,0,0,0,20,20h4A8,8,0,0,1,208,112ZM60,120H56a8,8,0,0,1,0-16h4A20,20,0,0,0,80,84V80a8,8,0,0,1,16,0v4A36,36,0,0,1,60,120Z" />
    </svg>
  );
}

function CaretRightIcon() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="12"
      height="12"
      viewBox="0 0 256 256"
      fill="currentColor"
    >
      <path d="M181.66,133.66l-80,80a8,8,0,0,1-11.32-11.32L164.69,128,90.34,53.66a8,8,0,0,1,11.32-11.32l80,80A8,8,0,0,1,181.66,133.66Z" />
    </svg>
  );
}

function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}

export type ThoughtsPanelProps = {
  activity: ActivityView | null;
  running: boolean;
  durationMs?: number | null;
};

/**
 * Factual disclosure of a turn's activity steps and durations, or fallback
 * status labels when none were recorded.
 */
export function ThoughtsPanel({ activity, running, durationMs }: ThoughtsPanelProps) {
  const [open, setOpen] = useState(false);
  const [manual, setManual] = useState<"open" | "closed" | null>(null);
  const wasRunning = useRef(running);
  const bodyId = useId();

  useEffect(() => {
    if (running && !wasRunning.current) {
      setManual(null);
      setOpen(false);
    }
    wasRunning.current = running;
  }, [running]);

  useEffect(() => {
    if (manual !== null) return;
    if (running && (activity?.steps.length ?? 0) > 0) setOpen(true);
    if (!running) setOpen(false);
  }, [activity?.steps.length, manual, running]);

  const toggle = useCallback(() => {
    setManual(open ? "closed" : "open");
    setOpen(!open);
  }, [open]);

  const steps = activity?.steps ?? [];
  if (steps.length === 0) return null;

  const headerLabel =
    durationMs != null && durationMs > 0
      ? `Thought for ${formatDuration(durationMs)}`
      : running
        ? "Thoughts..."
        : "Thoughts";

  return (
    <div className="thoughts">
      <button
        type="button"
        className="thoughts-toggle"
        aria-label="Thoughts"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={toggle}
      >
        <span className="thoughts-toggle-icon" aria-hidden="true">
          <BrainIcon />
        </span>
        <span className="thoughts-label" aria-hidden="true">
          {headerLabel}
        </span>
        <span className="thoughts-chevron" aria-hidden="true">
          <CaretRightIcon />
        </span>
      </button>
      <div id={bodyId} className="thoughts-body" hidden={!open}>
        <ul className="thoughts-list">
          {steps.map((step) => {
            const isModel = step.kind === "model";
            const title = step.title ?? LABELS[step.kind][step.status];
            return (
              <li className="thoughts-item" key={step.id} data-status={step.status}>
                <span className="thoughts-rail" aria-hidden="true" />
                <span className="thoughts-marker" aria-hidden="true">
                  {isModel ? <BrainIcon /> : <span className="thoughts-dot" />}
                </span>
                <div className="thoughts-step-content">
                  <div className="thoughts-title">{title}</div>
                  {step.duration_ms != null && (
                    <div className="thoughts-duration">
                      {formatDuration(step.duration_ms)}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
        {activity?.truncated ? (
          <p className="thoughts-truncated">Additional activity omitted</p>
        ) : null}
      </div>
    </div>
  );
}
