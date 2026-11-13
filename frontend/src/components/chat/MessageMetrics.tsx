import type { TurnResponse } from "../../api";

const SHORT_FORMAT = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const FULL_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "full",
  timeStyle: "short",
});

export type MessageMetricsProps = {
  response: TurnResponse | null;
};

/**
 * Provenance for one completed response: requested model alias and completion
 * time in the reader's timezone. Only server-stored facts appear; missing ones
 * are omitted rather than borrowed from the thread.
 */
export function MessageMetrics({ response }: MessageMetricsProps) {
  if (response === null) return null;

  const model = response.resolved_model ?? response.requested_model;

  const completedAt = response.completed_at;
  let time: Date | null = null;
  if (completedAt !== null) {
    const parsed = new Date(completedAt);
    if (Number.isFinite(parsed.getTime())) time = parsed;
  }

  if (model === null && time === null) return null;

  return (
    <div className="message-meta">
      <p className="message-metrics">
        {model !== null ? <span className="message-metric">{model}</span> : null}
        {time !== null ? (
          <span className="message-metric">
            <time dateTime={completedAt ?? undefined} title={FULL_FORMAT.format(time)}>
              {SHORT_FORMAT.format(time)}
            </time>
          </span>
        ) : null}
      </p>
    </div>
  );
}
