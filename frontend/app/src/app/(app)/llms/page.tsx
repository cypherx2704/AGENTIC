'use client';

import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  ErrorBanner,
  Input,
  Loading,
  Select,
  StatusBadge,
  useToast,
} from '@/components/ui';
import {
  createLlmConnection,
  deleteLlmConnection,
  listLlmConnections,
  type LlmConnection,
} from '@/lib/services';

// Provider presets — pre-fill the base URL + wire protocol. "Custom" lets you type any
// OpenAI-compatible endpoint. Adding a new provider is just a connection here — never code.
const PRESETS: Record<string, { label: string; base_url: string; kind: string }> = {
  openrouter: { label: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1', kind: 'openai_compatible' },
  openai: { label: 'OpenAI', base_url: 'https://api.openai.com/v1', kind: 'openai' },
  anthropic: { label: 'Anthropic (Claude)', base_url: '', kind: 'anthropic' },
  together: { label: 'Together AI', base_url: 'https://api.together.xyz/v1', kind: 'openai_compatible' },
  groq: { label: 'Groq', base_url: 'https://api.groq.com/openai/v1', kind: 'openai_compatible' },
  custom: { label: 'Custom / self-hosted (OpenAI-compatible)', base_url: '', kind: 'openai_compatible' },
};

export default function LlmConnectionsPage() {
  const toast = useToast();
  const [items, setItems] = useState<LlmConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const [provider, setProvider] = useState('openrouter');
  const [baseUrl, setBaseUrl] = useState(PRESETS.openrouter.base_url);
  const [kind, setKind] = useState(PRESETS.openrouter.kind);
  const [secret, setSecret] = useState('');
  const [label, setLabel] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<LlmConnection | null>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await listLlmConnections(signal);
      setItems(res.data ?? res.keys ?? []);
      setError(null);
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  function onProviderChange(value: string) {
    setProvider(value);
    const preset = PRESETS[value];
    if (preset) {
      setBaseUrl(preset.base_url);
      setKind(preset.kind);
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const providerName = provider === 'custom' ? (label.trim() || 'custom') : provider;
    if (!secret.trim()) {
      toast.error('An API key is required');
      return;
    }
    setSubmitting(true);
    try {
      await createLlmConnection({
        provider: providerName,
        secret: secret.trim(),
        base_url: baseUrl.trim() || null,
        kind: kind || null,
        label: label.trim() || null,
      });
      toast.success(`Connected ${providerName}`);
      setSecret('');
      setLabel('');
      await load();
    } catch (err) {
      toast.error((err as Error).message || 'Could not add the connection');
    } finally {
      setSubmitting(false);
    }
  }

  async function onDelete() {
    if (!confirmDelete) return;
    setDeleting(true);
    try {
      await deleteLlmConnection(confirmDelete.key_id);
      toast.success('Connection removed');
      setConfirmDelete(null);
      await load();
    } catch (err) {
      toast.error((err as Error).message || 'Delete failed');
    } finally {
      setDeleting(false);
    }
  }

  return (
    <Page>
      <PageHeader
        title="LLM Connections"
        description="Connect your own AI provider keys (OpenRouter, OpenAI, Claude, self-hosted, …). Keys are encrypted and stored against your tenant — never in env. Chat, RAG and Memory all use them; there is no platform fallback."
      />

      <PageBody fill>
        <Card className="mb-3 shrink-0">
          <CardHeader title="Add a Connection" description="A new provider is just a connection here — never code." />
          <CardBody>
            <form onSubmit={onSubmit} className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Select label="Provider" value={provider} onChange={(e) => onProviderChange(e.target.value)}>
                {Object.entries(PRESETS).map(([key, v]) => (
                  <option key={key} value={key}>
                    {v.label}
                  </option>
                ))}
              </Select>
              <Select label="Wire Protocol" value={kind} onChange={(e) => setKind(e.target.value)} hint="openai_compatible covers OpenRouter / OpenAI / self-hosted.">
                <option value="openai_compatible">openai_compatible</option>
                <option value="openai">openai</option>
                <option value="anthropic">anthropic</option>
              </Select>
              <Input
                label="Base URL"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://openrouter.ai/api/v1"
                hint="Required for OpenRouter / self-hosted; leave blank for native OpenAI/Anthropic."
              />
              <Input label="Label (Optional)" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="My OpenRouter key" />
              <Input
                className="sm:col-span-2"
                label="API Key"
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="sk-or-v1-…  (stored encrypted; shown only once)"
                autoComplete="off"
              />
              <div className="sm:col-span-2">
                <Button type="submit" size="md" loading={submitting} disabled={submitting}>
                  Add Connection
                </Button>
              </div>
            </form>
          </CardBody>
        </Card>

        <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <CardHeader title="Connected Providers" />
          <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
            {error ? (
              <div className="p-4">
                <ErrorBanner error={error} title="Could not load connections" />
              </div>
            ) : loading ? (
              <div className="p-4">
                <Loading label="Loading connections…" />
              </div>
            ) : items.length === 0 ? (
              <div className="p-4">
                <p className="text-sm text-muted">No connections yet — add one above to start using your own provider keys.</p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="text-left text-xs text-muted">
                      <th className="border-b border-border px-3 py-2.5 font-medium">Provider</th>
                      <th className="border-b border-border px-3 py-2.5 font-medium">Label</th>
                      <th className="border-b border-border px-3 py-2.5 font-medium">Base URL</th>
                      <th className="border-b border-border px-3 py-2.5 font-medium">Kind</th>
                      <th className="border-b border-border px-3 py-2.5 font-medium">Status</th>
                      <th className="border-b border-border px-3 py-2.5" />
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((c) => (
                      <tr key={c.key_id} className="border-b border-border transition-colors last:border-0 hover:bg-surface-2">
                        <td className="px-3 py-2.5 capitalize text-fg">{c.provider}</td>
                        <td className="px-3 py-2.5 text-fg">{c.label || '—'}</td>
                        <td className="px-3 py-2.5 font-mono text-xs text-muted">{c.base_url || '—'}</td>
                        <td className="px-3 py-2.5 font-mono text-xs text-muted">{c.kind || '—'}</td>
                        <td className="px-3 py-2.5">{c.status ? <StatusBadge status={c.status} /> : <Badge>Unknown</Badge>}</td>
                        <td className="px-3 py-2.5 text-right">
                          <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(c)}>
                            Remove
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardBody>
        </Card>
      </PageBody>

      <ConfirmDialog
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        onConfirm={onDelete}
        title="Remove this connection?"
        description="This cannot be undone."
        confirmLabel="Remove Connection"
        loading={deleting}
      >
        {confirmDelete && (
          <p className="text-sm text-muted">
            The <span className="font-medium capitalize text-fg">{confirmDelete.provider}</span>
            {confirmDelete.label ? ` (${confirmDelete.label})` : ''} connection and its stored key
            will be deleted. Chat, RAG and Memory calls relying on it will fail until you add a
            replacement — there is no platform fallback.
          </p>
        )}
      </ConfirmDialog>
    </Page>
  );
}
