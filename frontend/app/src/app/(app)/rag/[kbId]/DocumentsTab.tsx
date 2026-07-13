'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  ErrorBanner,
  FileDropzone,
  Input,
  Loading,
  Modal,
  Select,
  StatusBadge,
  Table,
  Textarea,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import {
  deleteDocument,
  finalizeDocument,
  inlineIngest,
  listDocuments,
  requestUploadUrl,
} from '@/lib/services';
import type { RagDocument } from '@/lib/types';
import { formatNumber, formatTime } from '@/lib/utils';

const POLL_MS = 4000;
const MAX_INLINE_BYTES = 100 * 1024;

/** A document is still moving through the pipeline (drives the auto-poll). */
function isActive(d: RagDocument): boolean {
  return d.status === 'pending' || d.status === 'processing';
}

/** Marks a failure in the *direct* browser→object-store transfer (usually CORS in local dev). */
class DirectUploadError extends Error {
  constructor() {
    super('Direct upload to the object store was blocked.');
    this.name = 'DirectUploadError';
  }
}

/**
 * Presigned-upload lifecycle: request a grant, PUT/POST the bytes DIRECTLY to the object
 * store (never through the BFF), then finalize to enqueue ingestion. A direct-transfer
 * failure is surfaced as DirectUploadError so the caller can steer the user to Paste Text.
 */
async function uploadDirect(kbId: string, file: File): Promise<void> {
  const contentType = file.type || 'application/octet-stream';
  const grant = await requestUploadUrl(kbId, {
    filename: file.name,
    size_bytes: file.size,
    content_type: contentType,
  });

  try {
    const fields = grant.fields;
    if (fields && typeof fields === 'object' && Object.keys(fields).length > 0) {
      // Multipart POST form-upload: every presigned field first, the file LAST.
      const form = new FormData();
      for (const [k, v] of Object.entries(fields)) form.append(k, v);
      form.append('file', file);
      const res = await fetch(grant.upload_url, { method: 'POST', body: form });
      if (!res.ok) throw new DirectUploadError();
    } else {
      // Raw PUT with a matching Content-Type.
      const res = await fetch(grant.upload_url, {
        method: 'PUT',
        headers: { 'Content-Type': contentType },
        body: file,
      });
      if (!res.ok) throw new DirectUploadError();
    }
  } catch (err) {
    if (err instanceof DirectUploadError) throw err;
    throw new DirectUploadError();
  }

  await finalizeDocument(kbId, grant.doc_id, crypto.randomUUID());
}

export function DocumentsTab({ kbId }: { kbId: string }) {
  const toast = useToast();
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const reload = useCallback(() => setReloadTick((t) => t + 1), []);

  const [pasteOpen, setPasteOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<RagDocument | null>(null);
  const [deleting, setDeleting] = useState(false);

  const anyActive = documents.some(isActive);

  // Load + self-scheduling poll: after each fetch, re-arm a 4s timer only while a document
  // is still pending/processing. Aborts + clears the timer on unmount or reload.
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function tick(showSpinner: boolean) {
      if (showSpinner) setLoading(true);
      try {
        const resp = await listDocuments(kbId, { limit: 100 }, controller.signal);
        if (!active) return;
        const docs = resp.documents ?? [];
        setDocuments(docs);
        setError(null);
        if (docs.some(isActive)) {
          timer = setTimeout(() => void tick(false), POLL_MS);
        }
      } catch (err) {
        if (active && !(err instanceof DOMException && err.name === 'AbortError')) setError(err);
      } finally {
        if (active && showSpinner) setLoading(false);
      }
    }

    void tick(true);
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [kbId, reloadTick]);

  async function onDelete(doc: RagDocument) {
    setDeleting(true);
    try {
      await deleteDocument(kbId, doc.doc_id);
      toast.success('Document deleted.');
      setConfirmDelete(null);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed.');
    } finally {
      setDeleting(false);
    }
  }

  const columns: Array<Column<RagDocument>> = [
    { key: 'name', header: 'Name', render: (d) => <span className="font-medium text-fg">{d.name}</span> },
    { key: 'source', header: 'Source Type', render: (d) => <span className="text-xs text-muted">{d.source_type}</span> },
    { key: 'status', header: 'Status', render: (d) => <StatusBadge status={d.status} /> },
    {
      key: 'attempts',
      header: 'Attempts',
      className: 'text-right',
      render: (d) => <span className="font-mono text-xs tabular-nums">{formatNumber(d.attempts)}</span>,
    },
    {
      key: 'error',
      header: 'Error',
      render: (d) =>
        d.error_msg ? (
          <span className="block max-w-[220px] truncate text-xs text-muted" title={d.error_msg}>
            {d.error_msg}
          </span>
        ) : (
          <span className="text-muted">—</span>
        ),
    },
    {
      key: 'created',
      header: 'Created',
      render: (d) => <span className="text-xs text-muted">{formatTime(d.created_at)}</span>,
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (d) => (
        <Button variant="danger" size="sm" onClick={() => setConfirmDelete(d)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Documents"
        description={
          anyActive
            ? 'Ingestion in progress — auto-refreshing.'
            : 'Ingest documents by pasting text or uploading a file.'
        }
        actions={
          <>
            <Button variant="secondary" size="md" onClick={() => setUploadOpen(true)}>
              Upload File
            </Button>
            <Button size="md" onClick={() => setPasteOpen(true)}>
              Paste Text
            </Button>
          </>
        }
      />
      <CardBody className="px-0 py-0">
        {error ? (
          <div className="p-4">
            <ErrorBanner error={error} title="Could not load documents" />
          </div>
        ) : loading && documents.length === 0 ? (
          <Loading label="Loading documents…" />
        ) : (
          <Table
            columns={columns}
            rows={documents}
            rowKey={(d) => d.doc_id}
            empty="No documents yet. Paste text or upload a file to ingest."
          />
        )}
      </CardBody>

      <PasteTextModal
        kbId={kbId}
        open={pasteOpen}
        onClose={() => setPasteOpen(false)}
        onIngested={() => {
          setPasteOpen(false);
          reload();
        }}
      />
      <UploadFileModal
        kbId={kbId}
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => {
          setUploadOpen(false);
          reload();
        }}
      />

      <ConfirmDialog
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => confirmDelete && onDelete(confirmDelete)}
        title="Delete This Document?"
        description="This removes the document and its chunks from the knowledge base."
        confirmLabel="Delete Document"
        loading={deleting}
      >
        {confirmDelete && (
          <p className="text-sm text-muted">
            <span className="font-medium text-fg">{confirmDelete.name}</span> will be removed. This cannot be undone.
          </p>
        )}
      </ConfirmDialog>
    </Card>
  );
}

/** Inline ingest — the always-available path (no object store needed). */
function PasteTextModal({
  kbId,
  open,
  onClose,
  onIngested,
}: {
  kbId: string;
  open: boolean;
  onClose: () => void;
  onIngested: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [sourceType, setSourceType] = useState<'markdown' | 'text'>('markdown');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const bytes = new TextEncoder().encode(content).length;
  const tooBig = bytes > MAX_INLINE_BYTES;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (tooBig) return;
    setBusy(true);
    setError(null);
    try {
      await inlineIngest(kbId, { name: name.trim(), content, source_type: sourceType });
      toast.success('Document queued for ingestion.');
      setName('');
      setContent('');
      onIngested();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Paste Text"
      description="Ingest markdown or plain text directly. This path always works, even without object storage."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            form="paste-text-form"
            type="submit"
            loading={busy}
            disabled={!name.trim() || !content.trim() || tooBig}
          >
            Ingest
          </Button>
        </>
      }
    >
      <form id="paste-text-form" onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_180px]">
          <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required />
          <Select
            label="Source Type"
            value={sourceType}
            onChange={(e) => setSourceType(e.target.value as 'markdown' | 'text')}
          >
            <option value="markdown">Markdown</option>
            <option value="text">Text</option>
          </Select>
        </div>
        <Textarea
          label="Content"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={10}
          required
          hint={`${(bytes / 1024).toFixed(1)} KiB / 100 KiB`}
          error={tooBig ? `Content is ${(bytes / 1024).toFixed(1)} KiB — the inline limit is 100 KiB.` : undefined}
        />
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

/** File upload via the presigned lifecycle, with a graceful fallback when CORS blocks it. */
function UploadFileModal({
  kbId,
  open,
  onClose,
  onUploaded,
}: {
  kbId: string;
  open: boolean;
  onClose: () => void;
  onUploaded: () => void;
}) {
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function reset() {
    setFile(null);
    setBusy(false);
    setBlocked(false);
    setError(null);
  }

  function close() {
    if (busy) return;
    reset();
    onClose();
  }

  async function submit() {
    if (!file) return;
    setBusy(true);
    setBlocked(false);
    setError(null);
    try {
      await uploadDirect(kbId, file);
      toast.success('File uploaded — queued for ingestion.');
      reset();
      onUploaded();
    } catch (err) {
      // A blocked direct transfer is expected in local dev (object-store CORS); steer to
      // Paste Text. A presign/finalize failure is a real API error worth showing verbatim.
      if (err instanceof DirectUploadError) setBlocked(true);
      else setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Upload File"
      description="Request a presigned URL, upload the bytes directly to the object store, then finalize."
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!file}>
            Upload
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FileDropzone
          onFile={(f) => {
            setFile(f);
            setBlocked(false);
            setError(null);
          }}
          selected={file}
          accept=".pdf,.md,.markdown,.txt,.text,.html,.htm"
          disabled={busy}
          hint="Drag a file here, or click to browse. Markdown, text, HTML, and PDF work best."
        />
        {blocked ? (
          <Callout tone="warning" title="Direct Upload Blocked">
            Direct upload was blocked (object-store CORS). Use Paste Text to ingest inline, which always works.
          </Callout>
        ) : null}
        {error ? <ErrorBanner error={error} title="Upload could not be completed" /> : null}
      </div>
    </Modal>
  );
}
