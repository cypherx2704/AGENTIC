'use client';

import { cn } from '@/lib/utils';

/**
 * Human labels + one-line descriptions for the platform's agent scopes. Unknown scopes fall
 * back to a Title-Cased rendering of the raw string, so a new scope still shows sensibly.
 */
const SCOPE_META: Record<string, { label: string; hint: string }> = {
  'agent:execute': { label: 'Execute Tasks', hint: 'Run agent tasks through the pipeline.' },
  'agent:create_subagent': { label: 'Create Sub-Agents', hint: 'Provision sub-agents (orchestrator only).' },
  'orchestrator:manage': { label: 'Manage Orchestrator', hint: 'Manage the tenant orchestrator + sub-agents.' },
  'llm:invoke': { label: 'Invoke LLMs', hint: 'Call the LLM gateway (chat/embeddings).' },
  'llm:chat': { label: 'Chat Completions', hint: 'Use chat completions specifically.' },
  'guardrails:check': { label: 'Run Guardrails', hint: 'Evaluate input/output against policies.' },
  'rag:query': { label: 'Query Knowledge', hint: 'Retrieve from knowledge bases.' },
  'rag:ingest': { label: 'Ingest Documents', hint: 'Add documents to knowledge bases.' },
  'rag:admin': { label: 'Manage Knowledge', hint: 'Create/delete KBs and manage access.' },
  'mem:read': { label: 'Read Memory', hint: 'Search and read stored memories.' },
  'mem:write': { label: 'Write Memory', hint: 'Store, update, and delete memories.' },
  'tool:invoke': { label: 'Invoke Tools', hint: 'Call registered MCP tools.' },
  'tool:admin': { label: 'Manage Tools', hint: 'Register tools and set access.' },
  'skill:invoke': { label: 'Invoke Skills', hint: 'Run registered skills.' },
  'skill:admin': { label: 'Manage Skills', hint: 'Register skills and set access.' },
  'tenant:read': { label: 'Read Tenant', hint: 'View tenant settings and quotas.' },
  'tenant:admin': { label: 'Administer Tenant', hint: 'Manage tenant settings, agents, and keys.' },
  'audit:read': { label: 'Read Audit Log', hint: 'View the tamper-evident audit trail.' },
  'platform:admin': { label: 'Platform Admin', hint: 'Cross-tenant platform administration.' },
};

/** Human label for a scope (falls back to Title Case of the raw scope). */
export function scopeLabel(scope: string): string {
  const meta = SCOPE_META[scope];
  if (meta) return meta.label;
  return scope
    .split(/[:_\-]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/**
 * Select an agent's scopes by CHECKBOX from a known set — never free-text (a typo would grant
 * the wrong scope or 422). `available` is the set the caller may grant (default: their own
 * scopes — you can't grant what you don't hold). `value`/`onChange` are the selected scopes.
 */
export function ScopeSelector({
  available,
  value,
  onChange,
  disabled = false,
  exclude = ['orchestrator:manage'],
  className,
}: {
  available: readonly string[];
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  /** Scopes never offered for selection (e.g. the non-delegable orchestrator:manage). */
  exclude?: readonly string[];
  className?: string;
}) {
  const options = Array.from(new Set([...available, ...value])).filter((s) => !exclude.includes(s)).sort();

  function toggle(scope: string) {
    if (disabled) return;
    onChange(value.includes(scope) ? value.filter((s) => s !== scope) : [...value, scope]);
  }

  if (options.length === 0) {
    return <p className={cn('text-sm text-muted', className)}>No grantable scopes are available.</p>;
  }

  return (
    <div className={cn('flex flex-col gap-1.5', className)} role="group" aria-label="Allowed scopes">
      {options.map((scope) => {
        const checked = value.includes(scope);
        const meta = SCOPE_META[scope];
        return (
          <label
            key={scope}
            className={cn(
              'flex cursor-pointer items-start gap-2.5 rounded-md border px-2.5 py-2 transition-colors',
              checked ? 'border-brand/40 bg-brand/5' : 'border-border bg-surface hover:bg-surface-2',
              disabled && 'cursor-not-allowed opacity-60',
            )}
          >
            <input
              type="checkbox"
              className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-brand"
              checked={checked}
              disabled={disabled}
              onChange={() => toggle(scope)}
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium text-fg">{scopeLabel(scope)}</span>
              <span className="block font-mono text-[11px] text-faint">{scope}</span>
              {meta ? <span className="mt-0.5 block text-xs text-muted">{meta.hint}</span> : null}
            </span>
          </label>
        );
      })}
    </div>
  );
}
