'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { Modal } from './Modal';
import { Button } from './Button';
import { Input } from './Input';

/**
 * A reusable confirmation dialog over Modal for destructive/irreversible actions
 * (revoke a key, delete a connection, deactivate an agent). It echoes what is about to
 * happen, holds a loading state on the confirm button, and locks itself shut while the
 * action is in flight so a misclick can't double-fire or dismiss a pending request.
 *
 * For high-blast-radius actions (GDPR wipe, delete a KB with documents) pass `confirmPhrase`
 * — the confirm button then stays disabled until the operator types that exact phrase.
 */
export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  children,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  confirmVariant = 'danger',
  loading = false,
  confirmPhrase,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: ReactNode;
  description?: ReactNode;
  /** Optional extra context shown in the body (e.g. the resource being affected). */
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  confirmVariant?: 'danger' | 'primary';
  loading?: boolean;
  /** When set, require the operator to type this exact string to enable the confirm button. */
  confirmPhrase?: string;
}) {
  const [typed, setTyped] = useState('');

  // Reset the typed guard whenever the dialog reopens so a stale value can't pre-arm it.
  useEffect(() => {
    if (open) setTyped('');
  }, [open]);

  const phraseOk = !confirmPhrase || typed.trim() === confirmPhrase;

  return (
    <Modal
      open={open}
      onClose={loading ? () => {} : onClose}
      closeOnBackdrop={!loading}
      title={title}
      description={description}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button variant={confirmVariant} onClick={onConfirm} loading={loading} disabled={!phraseOk}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children ?? null}
      {confirmPhrase ? (
        <div className="mt-3">
          <Input
            label={
              <>
                Type <span className="font-mono text-fg">{confirmPhrase}</span> to confirm
              </>
            }
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            disabled={loading}
          />
        </div>
      ) : null}
    </Modal>
  );
}
