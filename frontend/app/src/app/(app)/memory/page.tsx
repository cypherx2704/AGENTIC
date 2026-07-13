'use client';

import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  EmptyState,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  Select,
  Switch,
  Table,
  Textarea,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import {
  createMemorySession,
  deleteMemory,
  gdprWipeMemories,
  getMemory,
  searchMemories,
  storeMemory,
  updateMemory,
} from '@/lib/services';
import type { MemoryRecord, MemorySession, MemoryVisibility } from '@/lib/types';
import { formatNumber, formatTime } from '@/lib/utils';

// ── shared helpers ────────────────────────────────────────────────────────────
type SearchBody = {
  query: string;
  top_k?: number;
  type?: string | null;
  tags?: string[] | null;
  include_shared?: boolean;
};

const SCOPE_OPTIONS: Array<{ value: MemoryVisibility; label: string }> = [
  { value: 'principal_only', label: 'Principal Only' },
  { value: 'tenant_shared', label: 'Tenant Shared' },
];

/** Comma-separated string → trimmed, de-blanked string[]. */
function parseTags(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Fixed-precision numeric display with an em-dash fallback for null/undefined. */
function num(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits);
}

function scopeLabel(scope: MemoryVisibility): string {
  return scope === 'tenant_shared' ? 'Tenant Shared' : 'Principal Only';
}

function ScopeBadge({ scope }: { scope: MemoryVisibility }) {
  return scope === 'tenant_shared' ? <Badge tone="info">Tenant Shared</Badge> : <Badge>Principal Only</Badge>;
}

function TagList({ tags }: { tags: string[] }) {
  if (!tags || tags.length === 0) return <span className="text-muted">—</span>;
  const shown = tags.slice(0, 3);
  return (
    <div className="flex flex-wrap gap-1">
      {shown.map((t) => (
        <Badge key={t}>{t}</Badge>
      ))}
      {tags.length > shown.length && <span className="text-xs text-muted">+{tags.length - shown.length}</span>}
    </div>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-muted">{label}</dt>
      <dd className="mt-0.5 break-words font-mono text-xs text-fg">{children}</dd>
    </div>
  );
}

// ── New Memory modal ──────────────────────────────────────────────────────────
function NewMemoryModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (record: MemoryRecord) => void;
}) {
  const [content, setContent] = useState('');
  const [type, setType] = useState('note');
  const [scope, setScope] = useState<MemoryVisibility>('principal_only');
  const [tags, setTags] = useState('');
  const [ttl, setTtl] = useState('');
  const [importance, setImportance] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Reset to defaults each time the modal reopens so a stale draft can't leak in.
  useEffect(() => {
    if (open) {
      setContent('');
      setType('note');
      setScope('principal_only');
      setTags('');
      setTtl('');
      setImportance('');
      setSessionId('');
      setError(null);
    }
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!content.trim()) return;
    setBusy(true);
    setError(null);
    try {
      // A fresh Idempotency-Key per submission makes a double-click safe (never double-inserts).
      const record = await storeMemory(
        {
          content,
          type: type.trim() || undefined,
          scope,
          tags: parseTags(tags),
          ttl_seconds: ttl.trim() ? Number(ttl) : null,
          importance: importance.trim() ? Number(importance) : null,
          session_id: sessionId.trim() || null,
        },
        crypto.randomUUID(),
      );
      onCreated(record);
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
      size="lg"
      title="New Memory"
      description="Store a memory for the current principal. A retried submit is idempotent — it won't double-insert."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="new-memory-form" type="submit" loading={busy} disabled={!content.trim()}>
            Store Memory
          </Button>
        </>
      }
    >
      <form id="new-memory-form" onSubmit={submit} className="flex flex-col gap-4">
        <Textarea
          label="Content"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="What should the agent remember?"
          required
        />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Input label="Type" value={type} onChange={(e) => setType(e.target.value)} placeholder="note" />
          <Select label="Scope" value={scope} onChange={(e) => setScope(e.target.value as MemoryVisibility)}>
            {SCOPE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
        </div>
        <Input label="Tags" value={tags} onChange={(e) => setTags(e.target.value)} hint="Comma-separated." />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Input
            label="TTL Seconds"
            type="number"
            min={1}
            value={ttl}
            onChange={(e) => setTtl(e.target.value)}
            placeholder="optional"
          />
          <Input
            label="Importance"
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={importance}
            onChange={(e) => setImportance(e.target.value)}
            placeholder="0–1"
          />
          <Input
            label="Session ID"
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            placeholder="optional"
          />
        </div>
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

// ── View Memory modal ─────────────────────────────────────────────────────────
function ViewMemoryModal({
  record,
  onClose,
  onEdit,
}: {
  record: MemoryRecord | null;
  onClose: () => void;
  onEdit: (record: MemoryRecord) => void;
}) {
  const [full, setFull] = useState<MemoryRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Fetch the authoritative record on open; the search row is shown immediately meanwhile.
  useEffect(() => {
    if (!record) {
      setFull(null);
      setError(null);
      return;
    }
    let active = true;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getMemory(record.id, controller.signal)
      .then((r) => {
        if (active) setFull(r);
      })
      .catch((err) => {
        if (active && !(err instanceof DOMException && err.name === 'AbortError')) setError(err);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [record]);

  const m = full ?? record;

  return (
    <Modal
      open={record !== null}
      onClose={onClose}
      size="lg"
      title="Memory Detail"
      description={m ? <CopyButton value={m.id} label="Copy Memory ID" /> : undefined}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
          {m ? <Button onClick={() => onEdit(m)}>Edit</Button> : null}
        </>
      }
    >
      {!m ? (
        <Loading label="Loading memory…" />
      ) : (
        <div className="flex flex-col gap-4">
          {error ? <ErrorBanner error={error} title="Could not refresh this memory" /> : null}

          <div className="flex flex-wrap items-center gap-2">
            <ScopeBadge scope={m.scope} />
            <Badge>{m.type || 'untyped'}</Badge>
            {record?.similarity != null ? <Badge tone="info">Sim {record.similarity.toFixed(2)}</Badge> : null}
            {m.deduped ? <Badge tone="warning">Deduped</Badge> : null}
          </div>

          <div>
            <p className="mb-1 text-xs font-medium text-muted">Content</p>
            <div className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-3 text-sm text-fg">
              {m.content}
            </div>
          </div>

          <div>
            <p className="mb-1 text-xs font-medium text-muted">Tags</p>
            <TagList tags={m.tags} />
          </div>

          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Detail label="Score">{num(m.score)}</Detail>
            <Detail label="Importance">{num(m.importance_score)}</Detail>
            <Detail label="Similarity">{num(record?.similarity)}</Detail>
            <Detail label="Composite">{num(record?.composite_score)}</Detail>
            <Detail label="Access Count">{formatNumber(m.access_count ?? null)}</Detail>
            <Detail label="Scope">{scopeLabel(m.scope)}</Detail>
            <Detail label="Principal">
              <span className="inline-flex items-center gap-1.5">
                {m.principal_type}
                <CopyButton value={m.principal_id} label="Copy Principal ID" />
              </span>
            </Detail>
            <Detail label="Session">{m.session_id ?? '—'}</Detail>
            <Detail label="Created">{formatTime(m.created_at)}</Detail>
            <Detail label="Last Accessed">{formatTime(m.last_accessed_at)}</Detail>
            <Detail label="Expires">{formatTime(m.expires_at)}</Detail>
            {loading ? <Detail label="Status">refreshing…</Detail> : null}
          </dl>

          <div>
            <p className="mb-1 text-xs font-medium text-muted">Metadata</p>
            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface-2 p-3 font-mono text-xs text-fg">
              {JSON.stringify(m.metadata ?? {}, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </Modal>
  );
}

// ── Edit Memory modal ─────────────────────────────────────────────────────────
function EditMemoryModal({
  record,
  onClose,
  onSaved,
}: {
  record: MemoryRecord | null;
  onClose: () => void;
  onSaved: (record: MemoryRecord) => void;
}) {
  const [content, setContent] = useState('');
  const [scope, setScope] = useState<MemoryVisibility>('principal_only');
  const [tags, setTags] = useState('');
  const [ttl, setTtl] = useState('');
  const [metadata, setMetadata] = useState('{}');
  const [metaError, setMetaError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Hydrate the form from the record whenever a new memory is opened for editing.
  useEffect(() => {
    if (record) {
      setContent(record.content);
      setScope(record.scope);
      setTags(record.tags.join(', '));
      setTtl('');
      setMetadata(JSON.stringify(record.metadata ?? {}, null, 2));
      setMetaError(null);
      setError(null);
    }
  }, [record]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!record) return;

    // Parse + validate the metadata JSON before we touch the network.
    let parsedMeta: Record<string, unknown> | undefined;
    if (metadata.trim()) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(metadata);
      } catch {
        setMetaError('Metadata is not valid JSON.');
        return;
      }
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setMetaError('Metadata must be a JSON object.');
        return;
      }
      parsedMeta = parsed as Record<string, unknown>;
    }
    setMetaError(null);
    setBusy(true);
    setError(null);
    try {
      const updated = await updateMemory(record.id, {
        content,
        scope,
        tags: parseTags(tags),
        ttl_seconds: ttl.trim() ? Number(ttl) : undefined,
        metadata: parsedMeta,
      });
      onSaved(updated);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={record !== null}
      onClose={onClose}
      size="lg"
      title="Edit Memory"
      description={record ? <CopyButton value={record.id} label="Copy Memory ID" /> : undefined}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="edit-memory-form" type="submit" loading={busy} disabled={!content.trim()}>
            Save Changes
          </Button>
        </>
      }
    >
      <form id="edit-memory-form" onSubmit={submit} className="flex flex-col gap-4">
        <Textarea label="Content" value={content} onChange={(e) => setContent(e.target.value)} required />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Select label="Scope" value={scope} onChange={(e) => setScope(e.target.value as MemoryVisibility)}>
            {SCOPE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
          <Input
            label="TTL Seconds"
            type="number"
            min={1}
            value={ttl}
            onChange={(e) => setTtl(e.target.value)}
            hint="Leave blank to keep the current expiry."
          />
        </div>
        <Input label="Tags" value={tags} onChange={(e) => setTags(e.target.value)} hint="Comma-separated." />
        <Textarea
          label="Metadata (JSON)"
          value={metadata}
          onChange={(e) => setMetadata(e.target.value)}
          error={metaError ?? undefined}
          hint="A JSON object. Leave blank to keep the current metadata."
          rows={8}
          spellCheck={false}
        />
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

// ── Create Session modal ──────────────────────────────────────────────────────
function CreateSessionModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (session: MemorySession) => void;
}) {
  const [sessionId, setSessionId] = useState('');
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (open) {
      setSessionId('');
      setTitle('');
      setError(null);
    }
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!sessionId.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const session = await createMemorySession({
        session_id: sessionId.trim(),
        title: title.trim() || null,
      });
      onCreated(session);
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
      title="Create Session"
      description="Group related memories under a shared session id."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="create-session-form" type="submit" loading={busy} disabled={!sessionId.trim()}>
            Create Session
          </Button>
        </>
      }
    >
      <form id="create-session-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Session ID" value={sessionId} onChange={(e) => setSessionId(e.target.value)} required />
        <Input label="Title (Optional)" value={title} onChange={(e) => setTitle(e.target.value)} />
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

// ── Sessions card (compact) ───────────────────────────────────────────────────
function SessionsCard() {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [last, setLast] = useState<MemorySession | null>(null);

  return (
    <Card>
      <CardHeader
        title="Sessions"
        actions={
          <Button size="md" variant="secondary" onClick={() => setOpen(true)}>
            Create Session
          </Button>
        }
      />
      <CardBody>
        {last ? (
          <p className="text-sm text-muted">
            Created session <span className="font-mono text-fg">{last.session_id}</span>
            {last.title ? ` · ${last.title}` : ''}.
          </p>
        ) : (
          <p className="text-sm text-muted">
            Register a session id to group related memories, then reference it from the Session ID field when storing a
            memory.
          </p>
        )}
      </CardBody>
      <CreateSessionModal
        open={open}
        onClose={() => setOpen(false)}
        onCreated={(session) => {
          setOpen(false);
          setLast(session);
          toast.success('Session created.');
        }}
      />
    </Card>
  );
}

// ── GDPR danger zone ──────────────────────────────────────────────────────────
function GdprCard() {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [principalType, setPrincipalType] = useState('');
  const [principalId, setPrincipalId] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setPrincipalType('');
      setPrincipalId('');
      setReason('');
    }
  }, [open]);

  async function onWipe() {
    setBusy(true);
    try {
      const result = await gdprWipeMemories({
        principal_type: principalType.trim() || null,
        principal_id: principalId.trim() || null,
        reason: reason.trim() || null,
      });
      toast.success(`Wiped ${result.deleted_count} ${result.deleted_count === 1 ? 'memory' : 'memories'}.`);
      setOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Wipe failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader title="Danger Zone" />
      <CardBody className="flex flex-col gap-3">
        <Callout tone="danger" title="Right To Erasure">
          Permanently delete every memory for a principal. This cannot be undone. Defaults to your own principal; admins may
          target another principal in this tenant by id.
        </Callout>
        <div>
          <Button variant="danger" onClick={() => setOpen(true)}>
            Wipe Memories
          </Button>
        </div>
      </CardBody>

      <ConfirmDialog
        open={open}
        onClose={() => setOpen(false)}
        onConfirm={onWipe}
        title="Wipe Memories?"
        description="This permanently erases stored memories for the target principal."
        confirmLabel="Wipe Memories"
        confirmPhrase="WIPE"
        loading={busy}
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted">Leave the principal fields blank to wipe your own memories.</p>
          <Input
            label="Principal Type (Optional)"
            placeholder="e.g. user"
            value={principalType}
            onChange={(e) => setPrincipalType(e.target.value)}
            disabled={busy}
          />
          <Input
            label="Principal ID (Optional)"
            value={principalId}
            onChange={(e) => setPrincipalId(e.target.value)}
            disabled={busy}
          />
          <Input label="Reason (Optional)" value={reason} onChange={(e) => setReason(e.target.value)} disabled={busy} />
        </div>
      </ConfirmDialog>
    </Card>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function MemoryPage() {
  const toast = useToast();

  // search form
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState('10');
  const [typeFilter, setTypeFilter] = useState('');
  const [tagsFilter, setTagsFilter] = useState('');
  const [includeShared, setIncludeShared] = useState(true);

  // search execution (user-triggered → local state, no useAsync)
  const [results, setResults] = useState<MemoryRecord[] | null>(null);
  const [count, setCount] = useState(0);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<unknown>(null);
  const lastBody = useRef<SearchBody | null>(null);

  // modals / row actions
  const [createOpen, setCreateOpen] = useState(false);
  const [viewing, setViewing] = useState<MemoryRecord | null>(null);
  const [editing, setEditing] = useState<MemoryRecord | null>(null);
  const [deleting, setDeleting] = useState<MemoryRecord | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  async function runSearch(body: SearchBody) {
    setSearching(true);
    setSearchError(null);
    try {
      const resp = await searchMemories(body);
      setResults(resp.results);
      setCount(resp.count);
      lastBody.current = body;
    } catch (err) {
      setSearchError(err);
    } finally {
      setSearching(false);
    }
  }

  function onSearchSubmit(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    const parsedTags = parseTags(tagsFilter);
    runSearch({
      query: q,
      top_k: topK.trim() ? Number(topK) : undefined,
      type: typeFilter.trim() || null,
      tags: parsedTags.length ? parsedTags : null,
      include_shared: includeShared,
    });
  }

  function reRunSearch() {
    if (lastBody.current) runSearch(lastBody.current);
  }

  async function onDelete(record: MemoryRecord) {
    setDeleteBusy(true);
    try {
      await deleteMemory(record.id);
      toast.success('Memory deleted.');
      setResults((prev) => (prev ? prev.filter((r) => r.id !== record.id) : prev));
      setCount((c) => Math.max(0, c - 1));
      setDeleting(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed.');
    } finally {
      setDeleteBusy(false);
    }
  }

  const columns: Array<Column<MemoryRecord>> = [
    {
      key: 'content',
      header: 'Content',
      render: (m) => <span className="block max-w-[340px] truncate text-fg">{m.content}</span>,
    },
    { key: 'type', header: 'Type', render: (m) => <span className="text-sm text-muted">{m.type || '—'}</span> },
    { key: 'scope', header: 'Scope', render: (m) => <ScopeBadge scope={m.scope} /> },
    { key: 'tags', header: 'Tags', render: (m) => <TagList tags={m.tags} /> },
    {
      key: 'similarity',
      header: 'Similarity',
      className: 'text-right',
      render: (m) =>
        m.similarity != null ? (
          <Badge tone="info">Sim {m.similarity.toFixed(2)}</Badge>
        ) : (
          <span className="font-mono text-xs text-muted">—</span>
        ),
    },
    {
      key: 'created',
      header: 'Created',
      className: 'text-right',
      render: (m) => <span className="whitespace-nowrap text-xs text-muted">{formatTime(m.created_at)}</span>,
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (m) => (
        <div className="flex justify-end gap-1.5">
          <Button size="sm" variant="ghost" onClick={() => setViewing(m)}>
            View
          </Button>
          <Button size="sm" variant="secondary" onClick={() => setEditing(m)}>
            Edit
          </Button>
          <Button size="sm" variant="danger" onClick={() => setDeleting(m)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <Page>
      <PageHeader title="Memory" description="Search, curate, and govern your agents' long-term memory." />

      <PageBody>
      <div className="flex flex-col gap-3">
        <Card>
          <CardHeader title="Search Memory" description="Search is the browse surface — there is no full list of memories." />
          <CardBody>
            <form onSubmit={onSearchSubmit} className="flex flex-col gap-3">
              <Input
                label="Query"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search across stored memories…"
                required
              />
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Input label="Top-K" type="number" min={1} value={topK} onChange={(e) => setTopK(e.target.value)} />
                <Input
                  label="Type"
                  value={typeFilter}
                  onChange={(e) => setTypeFilter(e.target.value)}
                  placeholder="filter by type"
                />
                <Input
                  label="Tags"
                  value={tagsFilter}
                  onChange={(e) => setTagsFilter(e.target.value)}
                  hint="Comma-separated."
                />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Button type="submit" loading={searching} disabled={!query.trim()}>
                  Search
                </Button>
                <Button type="button" variant="secondary" onClick={() => setCreateOpen(true)}>
                  New Memory
                </Button>
                <div className="ml-auto">
                  <Switch label="Include Shared" checked={includeShared} onChange={setIncludeShared} />
                </div>
              </div>
            </form>
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Results"
            description={results !== null ? `${count} ${count === 1 ? 'match' : 'matches'} for your query.` : undefined}
          />
          <CardBody className="px-0 py-0">
            {searchError ? (
              <div className="p-4">
                <ErrorBanner error={searchError} title="Search failed" />
              </div>
            ) : searching ? (
              <Loading label="Searching memories…" />
            ) : results === null ? (
              <div className="p-4">
                <EmptyState
                  title="Search Your Memories"
                  description="Enter a query above and run a search. There is no full list — search is the only way to browse your agents' long-term memory."
                  action={
                    <Button variant="secondary" onClick={() => setCreateOpen(true)}>
                      New Memory
                    </Button>
                  }
                />
              </div>
            ) : (
              <Table columns={columns} rows={results} rowKey={(m) => m.id} empty="No memories matched your query." />
            )}
          </CardBody>
        </Card>

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <SessionsCard />
          <GdprCard />
        </div>
      </div>

      <NewMemoryModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(record) => {
          setCreateOpen(false);
          if (record.deduped) toast.info('Matched an existing memory.');
          else toast.success('Memory stored.');
          reRunSearch();
        }}
      />

      <ViewMemoryModal
        record={viewing}
        onClose={() => setViewing(null)}
        onEdit={(record) => {
          setViewing(null);
          setEditing(record);
        }}
      />

      <EditMemoryModal
        record={editing}
        onClose={() => setEditing(null)}
        onSaved={(updated) => {
          setEditing(null);
          toast.success('Memory updated.');
          // Keep the row's search-only similarity while refreshing the persisted fields.
          setResults((prev) =>
            prev ? prev.map((r) => (r.id === updated.id ? { ...r, ...updated, similarity: r.similarity } : r)) : prev,
          );
        }}
      />

      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        onConfirm={() => deleting && onDelete(deleting)}
        title="Delete This Memory?"
        description="This permanently removes the memory and cannot be undone."
        confirmLabel="Delete Memory"
        loading={deleteBusy}
      >
        {deleting ? (
          <div className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface-2 p-2 font-mono text-xs text-fg">
            {deleting.content}
          </div>
        ) : null}
      </ConfirmDialog>
      </PageBody>
    </Page>
  );
}
