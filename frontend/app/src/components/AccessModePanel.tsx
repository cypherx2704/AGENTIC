'use client';

import { useState } from 'react';
import { Badge, Button, Input, Select, Spinner, useToast } from '@/components/ui';
import type { AccessMode, AccessResolution } from '@/lib/types';

const MODES: AccessMode[] = ['none', 'ask', 'automated'];
const MODE_LABEL: Record<AccessMode, string> = {
  none: 'None',
  ask: 'Ask',
  automated: 'Automated',
};

/**
 * Per-agent access-mode control for a registry entry (tool or skill). Generic over the
 * service: the parent passes `resolve`/`apply` bound to the tools- or skills-registry
 * functions, so this one component serves both. Pick an agent → see its effective mode
 * (none | ask | automated) + restricted flag → set a new mode.
 */
export function AccessModePanel({
  resourceLabel,
  agents,
  resolve,
  apply,
}: {
  /** Singular noun for copy, e.g. 'tool' or 'skill'. */
  resourceLabel: string;
  /** Agents to choose from; when empty the control falls back to a free-text agent id. */
  agents: Array<{ agent_id: string; name: string }>;
  resolve: (agentId: string) => Promise<AccessResolution>;
  apply: (agentId: string, mode: AccessMode) => Promise<unknown>;
}) {
  const toast = useToast();
  const [agentId, setAgentId] = useState('');
  const [mode, setMode] = useState<AccessMode>('ask');
  const [current, setCurrent] = useState<AccessResolution | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  async function selectAgent(id: string) {
    setAgentId(id);
    setCurrent(null);
    if (!id.trim()) return;
    setLoading(true);
    try {
      const res = await resolve(id.trim());
      setCurrent(res);
      setMode((res.access_mode as AccessMode) ?? 'ask');
    } catch {
      toast.error('Could not resolve the current access mode for that agent.');
    } finally {
      setLoading(false);
    }
  }

  async function onApply() {
    if (!agentId.trim()) return;
    setSaving(true);
    try {
      await apply(agentId.trim(), mode);
      const res = await resolve(agentId.trim());
      setCurrent(res);
      toast.success(`Access set to “${MODE_LABEL[mode]}” for this ${resourceLabel}.`);
    } catch {
      toast.error('Could not update the access mode.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {agents.length > 0 ? (
          <Select
            label="Agent"
            value={agentId}
            onChange={(e) => selectAgent(e.target.value)}
            hint="The agent whose access to this resource you want to set."
          >
            <option value="">Select an agent…</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.name}
              </option>
            ))}
          </Select>
        ) : (
          <Input
            label="Agent ID"
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            onBlur={(e) => selectAgent(e.target.value)}
            placeholder="agent uuid…"
          />
        )}
        <Select label="Access Mode" value={mode} onChange={(e) => setMode(e.target.value as AccessMode)}>
          {MODES.map((m) => (
            <option key={m} value={m}>
              {MODE_LABEL[m]}
            </option>
          ))}
        </Select>
      </div>

      <div className="flex items-center gap-3">
        <Button size="md" onClick={onApply} loading={saving} disabled={!agentId.trim()}>
          Apply Access
        </Button>
        {loading ? (
          <span className="flex items-center gap-1.5 text-xs text-muted">
            <Spinner size="sm" /> Resolving…
          </span>
        ) : current ? (
          <span className="flex items-center gap-1.5 text-xs text-muted">
            Current:
            <Badge tone={current.access_mode === 'none' ? 'neutral' : current.access_mode === 'automated' ? 'success' : 'info'}>
              {MODE_LABEL[(current.access_mode as AccessMode) ?? 'none']}
            </Badge>
            {current.restricted ? <Badge tone="warning">Restricted</Badge> : null}
          </span>
        ) : null}
      </div>
    </div>
  );
}
