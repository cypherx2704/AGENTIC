'use client';

import type { ReactNode } from 'react';
import { Modal } from './Modal';
import { Button } from './Button';

/**
 * A reusable confirmation dialog over Modal for destructive/irreversible actions
 * (revoke a key, delete a connection, deactivate an agent). It echoes what is about to
 * happen, holds a loading state on the confirm button, and locks itself shut while the
 * action is in flight so a misclick can't double-fire or dismiss a pending request.
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
}) {
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
          <Button variant={confirmVariant} onClick={onConfirm} loading={loading}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children ?? null}
    </Modal>
  );
}
