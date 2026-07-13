'use client';

import { useState, type FormEvent } from 'react';
import { Button, ErrorBanner, Input, Modal, Select, Textarea, useToast } from '@/components/ui';
import { createCustomRule, updateCustomRule } from '@/lib/services';
import type {
  CustomRule,
  CustomRuleInput,
  CustomRuleType,
  RuleAction,
  RuleDirection,
  RuleFailMode,
  RuleSeverity,
} from '@/lib/types';

const TYPES: Array<{ value: CustomRuleType; label: string }> = [
  { value: 'regex', label: 'Regex' },
  { value: 'classifier-threshold', label: 'Classifier Threshold' },
];
const DIRECTIONS: RuleDirection[] = ['input', 'output', 'both'];
const SEVERITIES: RuleSeverity[] = ['info', 'low', 'medium', 'high', 'critical'];
const ACTIONS: RuleAction[] = ['allow', 'warn', 'redact', 'block'];
const FAIL_MODES: RuleFailMode[] = ['closed', 'open'];

function titleCase(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function clamp(n: number, lo: number, hi: number): number {
  if (!Number.isFinite(n)) return lo;
  return Math.min(hi, Math.max(lo, n));
}

/**
 * Create or update a tenant-authored custom rule. Regex rules carry a `pattern` (server-side
 * ReDoS-checked); classifier-threshold rules carry a `classifier_category` + `threshold`.
 * API errors (ReDoS 422 / quota 409) surface inline via ErrorBanner.
 */
export function CustomRuleEditor({
  open,
  rule,
  onClose,
  onSaved,
}: {
  open: boolean;
  rule: CustomRule | null; // null => create
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const isEdit = rule !== null;

  const [name, setName] = useState(rule?.name ?? '');
  const [type, setType] = useState<CustomRuleType>((rule?.type as CustomRuleType) ?? 'regex');
  const [direction, setDirection] = useState<RuleDirection>((rule?.direction as RuleDirection) ?? 'input');
  const [category, setCategory] = useState(rule?.category ?? '');
  const [severity, setSeverity] = useState<RuleSeverity>((rule?.severity as RuleSeverity) ?? 'medium');
  const [action, setAction] = useState<RuleAction>((rule?.default_action as RuleAction) ?? 'block');
  const [failMode, setFailMode] = useState<RuleFailMode>((rule?.default_fail_mode as RuleFailMode) ?? 'closed');
  const [timeoutMs, setTimeoutMs] = useState<string>(String(rule?.timeout_ms ?? 10));
  const [pattern, setPattern] = useState(rule?.pattern ?? '');
  const [classifierCategory, setClassifierCategory] = useState(rule?.classifier_category ?? '');
  const [threshold, setThreshold] = useState<string>(rule?.threshold != null ? String(rule.threshold) : '0.8');

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const canSubmit =
    name.trim().length > 0 &&
    category.trim().length > 0 &&
    (type === 'regex' ? pattern.trim().length > 0 : classifierCategory.trim().length > 0);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);

    const body: CustomRuleInput = {
      name: name.trim(),
      type,
      direction,
      category: category.trim(),
      severity,
      default_action: action,
      default_fail_mode: failMode,
      timeout_ms: clamp(Number(timeoutMs), 1, 5000),
      ...(type === 'regex'
        ? { pattern: pattern }
        : { classifier_category: classifierCategory.trim(), threshold: clamp(Number(threshold), 0, 1) }),
    };

    try {
      // NOTE: create/update return the wrapped {rule:{…}} envelope; we never read the value —
      // the parent reloads the list, and the edit form is seeded from the list row instead.
      if (isEdit && rule) await updateCustomRule(rule.id, body);
      else await createCustomRule(body);
      toast.success(isEdit ? 'Custom rule updated.' : 'Custom rule created.');
      onSaved();
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
      title={isEdit ? 'Edit Custom Rule' : 'New Custom Rule'}
      description="Regex rules are ReDoS-checked on save; classifier rules score a category against a threshold."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="custom-rule-form" type="submit" loading={busy} disabled={!canSubmit}>
            {isEdit ? 'Save Changes' : 'Create Rule'}
          </Button>
        </>
      }
    >
      <form id="custom-rule-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required autoFocus />

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Select label="Type" value={type} onChange={(e) => setType(e.target.value as CustomRuleType)}>
            {TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </Select>
          <Select label="Direction" value={direction} onChange={(e) => setDirection(e.target.value as RuleDirection)}>
            {DIRECTIONS.map((d) => (
              <option key={d} value={d}>
                {titleCase(d)}
              </option>
            ))}
          </Select>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Input
            label="Category"
            hint="e.g. pii, security, compliance"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            required
          />
          <Select label="Severity" value={severity} onChange={(e) => setSeverity(e.target.value as RuleSeverity)}>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {titleCase(s)}
              </option>
            ))}
          </Select>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Select label="Default Action" value={action} onChange={(e) => setAction(e.target.value as RuleAction)}>
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {titleCase(a)}
              </option>
            ))}
          </Select>
          <Select label="Default Fail Mode" value={failMode} onChange={(e) => setFailMode(e.target.value as RuleFailMode)}>
            {FAIL_MODES.map((f) => (
              <option key={f} value={f}>
                {titleCase(f)}
              </option>
            ))}
          </Select>
          <Input
            label="Timeout (ms)"
            type="number"
            min={1}
            max={5000}
            value={timeoutMs}
            onChange={(e) => setTimeoutMs(e.target.value)}
          />
        </div>

        {type === 'regex' ? (
          <Textarea
            label="Pattern"
            hint="Regular expression source. Checked for ReDoS safety on save."
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
            placeholder="\\b\\d{3}-\\d{2}-\\d{4}\\b"
            required
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input
              label="Classifier Category"
              hint="The target classifier category to score."
              value={classifierCategory}
              onChange={(e) => setClassifierCategory(e.target.value)}
              required
            />
            <Input
              label="Threshold"
              type="number"
              min={0}
              max={1}
              step={0.01}
              hint="Score threshold (0–1)."
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
            />
          </div>
        )}

        {error ? <ErrorBanner error={error} title="Could not save the rule" /> : null}
      </form>
    </Modal>
  );
}
