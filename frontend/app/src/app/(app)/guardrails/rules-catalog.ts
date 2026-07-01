/**
 * The first-cycle built-in rule catalog (mirrors the guardrails seed migration). Used to
 * pre-populate the policy editor's rule toggles. Operators can still enter custom rule
 * ids; the create/edit endpoint validates them (422 with the offending ids).
 */
export interface CatalogRule {
  rule_id: string;
  label: string;
  direction: 'input' | 'output';
}

export const RULE_CATALOG: CatalogRule[] = [
  { rule_id: 'prompt-injection-v1', label: 'Prompt injection', direction: 'input' },
  { rule_id: 'jailbreak-v1', label: 'Jailbreak attempt', direction: 'input' },
  { rule_id: 'pii-email-v1', label: 'PII — email', direction: 'input' },
  { rule_id: 'pii-phone-v1', label: 'PII — phone', direction: 'input' },
  { rule_id: 'pii-credit-card-v1', label: 'PII — credit card', direction: 'input' },
  { rule_id: 'toxicity-v1', label: 'Toxicity', direction: 'input' },
  { rule_id: 'output-pii-email-v1', label: 'Output PII — email', direction: 'output' },
  { rule_id: 'output-pii-credit-card-v1', label: 'Output PII — credit card', direction: 'output' },
  { rule_id: 'output-toxicity-v1', label: 'Output toxicity', direction: 'output' },
  { rule_id: 'output-jailbreak-leak-v1', label: 'Output jailbreak / leak', direction: 'output' },
  { rule_id: 'output-max-length-v1', label: 'Output max length', direction: 'output' },
];

export const ACTION_OVERRIDES = ['', 'allow', 'warn', 'redact', 'block'] as const;
