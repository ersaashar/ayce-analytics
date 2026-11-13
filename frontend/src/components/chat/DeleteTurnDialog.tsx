import { useEffect, useRef } from "react";

export type DeleteTurnDialogProps = {
  open: boolean;
  busy: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: () => void;
};

/**
 * Confirmation for removing one prompt and response pair.
 *
 * Escape and the backdrop cancel; focus returns to the trigger because the
 * dialog closes itself rather than unmounting mid-action.
 */
export function DeleteTurnDialog({
  open,
  busy,
  error,
  onCancel,
  onConfirm,
}: DeleteTurnDialogProps) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const cancel = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const element = dialog.current;
    if (element === null) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);

  useEffect(() => {
    if (open) cancel.current?.focus();
  }, [open]);

  return (
    <dialog
      ref={dialog}
      className="dialog"
      aria-labelledby="delete-turn-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
      onClick={(event) => {
        if (event.target === dialog.current && !busy) onCancel();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !busy) onCancel();
      }}
    >
      {open ? (
        <div className="dialog-form">
          <h2 className="dialog-title" id="delete-turn-title">
            Delete this exchange?
          </h2>
          <p className="dialog-text">
            The prompt and its response will both be removed.
          </p>
          {error !== "" ? (
            <p className="form-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className="dialog-actions">
            <button
              type="button"
              className="button danger"
              disabled={busy}
              onClick={onConfirm}
            >
              Delete exchange
            </button>
            <button
              ref={cancel}
              type="button"
              className="button secondary"
              disabled={busy}
              onClick={onCancel}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
    </dialog>
  );
}
