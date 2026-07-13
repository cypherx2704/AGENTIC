'use client';

import { useState } from 'react';
import { Button, ErrorBanner, Input, Modal, Select, Switch, useToast } from '@/components/ui';
import { createPolicy, editPolicy } from '@/lib/services';
import type { Policy, PolicyRule } from '@/lib/types';
import { ACTION_OVERRIDES, RULE_CATALOG } from './rules-catalog';

interface EditableRule {
  rule_id: string;
  enabled: boolean;
  action_override: string;
}

function titleCase(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function buildInitialRules(policy: Policy | null): EditableRule[] {
  const byId = new Map<string, PolicyRule>();
  for (const r of policy?.rules ?? []) byId.set(r.rule_id, r);
  // Union the catalog with any rules already on the policy (incl. custom ones).
  const ids = new Set<string>(RULE_CATALOG.map((r) => r.rule_id));
  for (const r of policy?.rules ?? []) ids.add(r.rule_id);
  return Array.from(ids).map((rule_id) => {
    const existing = byId.get(rule_id);
    return {
      rule_id,
      enabled: existing ? existing.enabled : false,
      action_override: existing?.action_override ?? '',
    };
  });
}

/** Create or edit a guardrail policy (WP07 CRUD). */
export function PolicyEditor({
  open,
  policy,
  onClose,
  onSaved,
}: {
  open: boolean;
  policy: Policy | null; // null => create
  onClose: () => void;
  onSaved: (p: Policy) => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(policy?.name ?? '');
  const [streamMode, setStreamMode] = useState(policy?.stream_mode ?? 'buffer');
  const [failMode, setFailMode] = useState(policy?.fail_mode_override ?? '');
  const [rules, setRules] = useState<EditableRule[]>(() => buildInitialRules(policy));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const isEdit = policy !== null;
  const enabledCount = rules.filter((r) => r.enabled).length;

  function setRule(idx: number, patch: Partial<EditableRule>) {
    setRules((rs) => rs.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const payload = {
      name: name.trim(),
      stream_mode: streamMode,
      fail_mode_override: failMode || null,
      rules: rules
        .filter((r) => r.enabled)
        .map((r) => ({
          rule_id: r.rule_id,
          enabled: true,
          action_override: r.action_override || null,
        })),
    };
    try {
      const saved = isEdit ? await editPolicy(policy!.policy_id, payload) : await createPolicy(payload);
      toast.success(isEdit ? 'Policy updated.' : 'Policy created.');
      onSaved(saved);
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
      title={isEdit ? 'Edit Policy' : 'New Policy'}
      description="Toggle the rules and choose an optional per-rule action override."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="policy-form" type="submit" loading={busy} disabled={!name.trim()}>
            {isEdit ? 'Save Changes' : 'Create Policy'}
          </Button>
        </>
      }
    >
      <form id="policy-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Policy Name" value={name} onChange={(e) => setName(e.target.value)} required autoFocus />
        <div className="grid grid-cols-2 gap-3">
          <Select label="Stream Mode" value={streamMode} onChange={(e) => setStreamMode(e.target.value)}>
            <option value="buffer">Buffer</option>
            <option value="passthrough">Passthrough</option>
          </Select>
          <Select label="Fail Mode Override" value={failMode} onChange={(e) => setFailMode(e.target.value)}>
            <option value="">Policy Default</option>
            <option value="closed">Closed</option>
            <option value="open">Open</option>
          </Select>
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <p className="text-sm font-medium text-fg">Rules</p>
            <span className="text-xs text-muted">{enabledCount} enabled</span>
          </div>
          <div className="max-h-72 overflow-y-auto rounded-md border border-border">
            {rules.map((r, i) => {
              const meta = RULE_CATALOG.find((c) => c.rule_id === r.rule_id);
              return (
                <div key={r.rule_id} className="flex items-center gap-3 border-b border-border px-3 py-2.5 last:border-0">
                  <Switch
                    checked={r.enabled}
                    onChange={(v) => setRule(i, { enabled: v })}
                    ariaLabel={`Enable ${r.rule_id}`}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-fg">{meta?.label ?? r.rule_id}</p>
                    <p className="truncate font-mono text-xs text-muted">
                      {r.rule_id}
                      {meta && <span className="ml-2 uppercase">{meta.direction}</span>}
                    </p>
                  </div>
                  <select
                    value={r.action_override}
                    onChange={(e) => setRule(i, { action_override: e.target.value })}
                    disabled={!r.enabled}
                    className="rounded border border-border bg-surface px-2 py-1 text-xs text-fg disabled:opacity-40"
                    aria-label={`Action override for ${r.rule_id}`}
                  >
                    {ACTION_OVERRIDES.map((a) => (
                      <option key={a} value={a}>
                        {a ? titleCase(a) : 'Default'}
                      </option>
                    ))}
                  </select>
                </div>
              );
            })}
          </div>
        </div>

        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}
