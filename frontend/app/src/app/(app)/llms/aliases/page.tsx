'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ErrorBanner,
  humanizeStatus,
  Input,
  Loading,
  Select,
  Switch,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import {
  createAlias,
  createLlmRule,
  deleteAlias,
  deleteLlmRule,
  listAliases,
  listLlmRules,
  updateAlias,
  type LlmAlias,
  type LlmRule,
} from '@/lib/services';

export default function LlmAliasesPage() {
  return (
    <Page>
      <PageHeader
        title="LLM Aliases & Rules"
        description="Define task-typed model aliases (one default per tenant) and the user-owned rules that allow/block models or exempt them from billing."
        actions={
          <Link href="/llms" className="text-[13px] font-medium text-brand hover:underline">
            ← LLM Connections
          </Link>
        }
      />
      <PageBody>
        <div className="flex flex-col gap-3">
          <AliasesCard />
          <RulesCard />
        </div>
      </PageBody>
    </Page>
  );
}

function AliasesCard() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listAliases({}, signal), []);
  const [form, setForm] = useState({ alias: '', model_id: '', provider: 'anthropic', task_type: '' });
  const [busy, setBusy] = useState(false);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await createAlias({
        alias: form.alias.trim(),
        model_id: form.model_id.trim(),
        provider: form.provider.trim(),
        task_type: form.task_type.trim() || undefined,
      });
      toast.success('Alias created.');
      setForm({ alias: '', model_id: '', provider: 'anthropic', task_type: '' });
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Create failed.');
    } finally {
      setBusy(false);
    }
  }

  async function setDefault(a: LlmAlias) {
    try {
      await updateAlias(a.alias, { is_default: true });
      toast.success(`'${a.alias}' is now the default.`);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Update failed.');
    }
  }

  async function remove(a: LlmAlias) {
    try {
      await deleteAlias(a.alias);
      toast.success('Alias deleted.');
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed.');
    }
  }

  const columns: Array<Column<LlmAlias>> = [
    {
      key: 'alias',
      header: 'Alias',
      render: (a) => (
        <span className="inline-flex items-center gap-2 font-medium text-fg">
          {a.alias} {a.is_default ? <Badge tone="info">Default</Badge> : null}
        </span>
      ),
    },
    { key: 'model', header: 'Model', render: (a) => <span className="font-mono text-xs">{a.model_id}</span> },
    { key: 'provider', header: 'Provider', render: (a) => <span className="capitalize">{a.provider}</span> },
    { key: 'task', header: 'Task Type', render: (a) => (a.task_type ? <Badge>{a.task_type}</Badge> : <span className="text-muted">—</span>) },
    {
      key: 'scope',
      header: 'Scope',
      render: (a) => <span className="text-xs text-muted">{a.tenant_id ? 'Tenant' : 'Platform'}</span>,
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (a) =>
        a.tenant_id ? (
          <div className="flex justify-end gap-2">
            {!a.is_default && (
              <Button size="sm" variant="secondary" onClick={() => setDefault(a)}>
                Make Default
              </Button>
            )}
            <Button size="sm" variant="danger" onClick={() => remove(a)}>
              Delete
            </Button>
          </div>
        ) : (
          <span className="text-xs text-muted">Read-Only</span>
        ),
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Model Aliases"
        description="The first alias created becomes the default; an alias's task_type guides the orchestrator's model choice per sub-agent task."
      />
      <CardBody className="flex flex-col gap-4">
        <form onSubmit={add} className="grid grid-cols-2 gap-3 sm:grid-cols-5 sm:items-end">
          <Input label="Alias" value={form.alias} onChange={(e) => setForm({ ...form, alias: e.target.value })} required />
          <Input label="Model ID" value={form.model_id} onChange={(e) => setForm({ ...form, model_id: e.target.value })} required />
          <Input label="Provider" value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })} required />
          <Input label="Task Type" value={form.task_type} onChange={(e) => setForm({ ...form, task_type: e.target.value })} placeholder="code-generation" />
          <Button type="submit" size="md" className="h-9" loading={busy} disabled={!form.alias.trim() || !form.model_id.trim()}>
            Add Alias
          </Button>
        </form>
        {error ? (
          <ErrorBanner error={error} title="Could not load aliases" />
        ) : loading ? (
          <Loading label="Loading aliases…" />
        ) : (
          <Table columns={columns} rows={data?.data ?? []} rowKey={(a) => a.id} empty="No aliases yet." />
        )}
      </CardBody>
    </Card>
  );
}

function RulesCard() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listLlmRules(signal), []);
  const [form, setForm] = useState({ provider: 'anthropic', model_id: '', rule_type: 'allow', billing_bypass: false, can_be_used_by_agents: true });
  const [busy, setBusy] = useState(false);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await createLlmRule({
        provider: form.provider.trim(),
        model_id: form.model_id.trim(),
        rule_type: form.rule_type,
        billing_bypass: form.billing_bypass,
        can_be_used_by_agents: form.can_be_used_by_agents,
      });
      toast.success('Rule saved.');
      setForm({ ...form, model_id: '' });
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Save failed.');
    } finally {
      setBusy(false);
    }
  }

  async function remove(r: LlmRule) {
    try {
      await deleteLlmRule(r.rule_id);
      toast.success('Rule deleted.');
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed.');
    }
  }

  const columns: Array<Column<LlmRule>> = [
    { key: 'model', header: 'Model', render: (r) => <span className="font-mono text-xs">{r.provider}/{r.model_id}</span> },
    { key: 'type', header: 'Rule', render: (r) => <Badge tone={r.rule_type === 'block' ? 'danger' : 'success'}>{humanizeStatus(r.rule_type)}</Badge> },
    { key: 'agents', header: 'Agents', render: (r) => (r.can_be_used_by_agents ? 'Allowed' : 'Blocked') },
    { key: 'billing', header: 'Billing', render: (r) => (r.billing_bypass ? <Badge tone="info">Exempt</Badge> : <span className="text-muted">Metered</span>) },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (r) => (
        <Button size="sm" variant="danger" onClick={() => remove(r)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <Card>
      <CardHeader
        title="User LLM Rules"
        description="The tenant-owned 'ultimate truth': block models, restrict agent use, or exempt user-added models from billing."
      />
      <CardBody className="flex flex-col gap-4">
        <form onSubmit={add} className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Input label="Provider" value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })} required />
            <Input label="Model ID" value={form.model_id} onChange={(e) => setForm({ ...form, model_id: e.target.value })} required />
            <Select label="Rule" value={form.rule_type} onChange={(e) => setForm({ ...form, rule_type: e.target.value })}>
              <option value="allow">Allow</option>
              <option value="block">Block</option>
            </Select>
          </div>
          <div className="flex flex-wrap items-center gap-4">
            <Switch
              checked={form.billing_bypass}
              onChange={(v) => setForm({ ...form, billing_bypass: v })}
              label="Billing Bypass"
            />
            <Switch
              checked={form.can_be_used_by_agents}
              onChange={(v) => setForm({ ...form, can_be_used_by_agents: v })}
              label="Agents Allowed"
            />
            <Button type="submit" size="md" className="ml-auto" loading={busy} disabled={!form.model_id.trim()}>
              Save Rule
            </Button>
          </div>
        </form>
        {error ? (
          <ErrorBanner error={error} title="Could not load rules" />
        ) : loading ? (
          <Loading label="Loading rules…" />
        ) : (
          <Table columns={columns} rows={data?.data ?? []} rowKey={(r) => r.rule_id} empty="No rules — all models allowed and metered." />
        )}
      </CardBody>
    </Card>
  );
}
