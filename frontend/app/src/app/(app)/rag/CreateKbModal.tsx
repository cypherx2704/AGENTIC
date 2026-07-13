'use client';

import { useState } from 'react';
import { Button, ErrorBanner, Input, Modal, Select, Switch } from '@/components/ui';
import { createKnowledgeBase } from '@/lib/services';
import type { KbDetail } from '@/lib/types';

type ChunkingStrategy = 'sentence' | 'fixed';

/** Parse a numeric field, treating blank as "unset" so the server applies its own default. */
function toNum(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : undefined;
}

/**
 * Create-a-KB modal. Owns its own form + submit; on success it hands the created KbDetail
 * back to the parent, which toasts and navigates to the new KB's detail page.
 */
export function CreateKbModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (kb: KbDetail) => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [strategy, setStrategy] = useState<ChunkingStrategy>('sentence');
  const [chunkSize, setChunkSize] = useState('512');
  const [chunkOverlap, setChunkOverlap] = useState('50');
  const [alias, setAlias] = useState('embed');
  const [isPrivate, setIsPrivate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const kb = await createKnowledgeBase({
        name: name.trim(),
        description: description.trim() || undefined,
        chunking_strategy: strategy,
        chunk_size: toNum(chunkSize),
        chunk_overlap: toNum(chunkOverlap),
        embedding_model_alias: alias.trim() || undefined,
        private: isPrivate,
      });
      onCreated(kb);
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
      title="New Knowledge Base"
      description="Group documents under one chunking and embedding configuration."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="create-kb-form" type="submit" loading={busy} disabled={!name.trim()}>
            Create Knowledge Base
          </Button>
        </>
      }
    >
      <form id="create-kb-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required />
        <Input
          label="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          hint="Optional — a short note on what this KB holds."
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Select
            label="Chunking Strategy"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value as ChunkingStrategy)}
          >
            <option value="sentence">Sentence</option>
            <option value="fixed">Fixed</option>
          </Select>
          <Input label="Embedding Model Alias" value={alias} onChange={(e) => setAlias(e.target.value)} />
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Input
            label="Chunk Size"
            type="number"
            min={1}
            value={chunkSize}
            onChange={(e) => setChunkSize(e.target.value)}
          />
          <Input
            label="Chunk Overlap"
            type="number"
            min={0}
            value={chunkOverlap}
            onChange={(e) => setChunkOverlap(e.target.value)}
          />
        </div>
        <Switch
          checked={isPrivate}
          onChange={setIsPrivate}
          label="Private"
          hint="Only you or principals with explicit ACL grants can access it."
        />
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}
