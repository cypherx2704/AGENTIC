'use client';

import { useState } from 'react';
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
  const [hovered, setHovered] = useState<string | null>(null);

  function toggle(scope: string) {
    if (disabled) return;
    onChange(value.includes(scope) ? value.filter((s) => s !== scope) : [...value, scope]);
  }

  if (options.length === 0) {
    return <p className={cn('text-sm text-muted', className)}>No grantable scopes are available.</p>;
  }

  const active = hovered ?? null;
  const activeMeta = active ? SCOPE_META[active] : null;

  return (
    <div className={cn('flex flex-col gap-2.5', className)} role="group" aria-label="Allowed scopes">
      {/* Clickable pills — click to select/unselect; selected ones are highlighted. */}
      <div className="flex flex-wrap gap-1.5">
        {options.map((scope) => {
          const checked = value.includes(scope);
          return (
            <button
              key={scope}
              type="button"
              role="checkbox"
              aria-checked={checked}
              disabled={disabled}
              title={SCOPE_META[scope] ? `${scope} — ${SCOPE_META[scope].hint}` : scope}
              onClick={() => toggle(scope)}
              onMouseEnter={() => setHovered(scope)}
              onMouseLeave={() => setHovered((h) => (h === scope ? null : h))}
              onFocus={() => setHovered(scope)}
              onBlur={() => setHovered((h) => (h === scope ? null : h))}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/50',
                checked
                  ? 'border-brand bg-brand/15 text-brand'
                  : 'border-border bg-surface text-muted hover:border-brand/40 hover:text-fg',
                disabled && 'cursor-not-allowed opacity-60',
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  'grid h-3 w-3 shrink-0 place-items-center rounded-[3px] border',
                  checked ? 'border-brand bg-brand text-brand-fg' : 'border-border',
                )}
              >
                {checked ? (
                  <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="4">
                    <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                ) : null}
              </span>
              {scopeLabel(scope)}
            </button>
          );
        })}
      </div>

      {/* Hover/focus a pill to read its detailed description here (no tooltip clipping). */}
      <div className="min-h-[34px] rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-xs">
        {active ? (
          <span className="text-muted">
            <span className="font-mono text-[11px] text-faint">{active}</span>
            {activeMeta ? <> — {activeMeta.hint}</> : null}
          </span>
        ) : (
          <span className="text-faint">Click a scope to select it; hover any scope to read what it grants.</span>
        )}
      </div>
    </div>
  );
}
