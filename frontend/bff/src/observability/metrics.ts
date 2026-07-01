/**
 * Minimal, dependency-free Prometheus metrics registry.
 *
 * Supports labelled counters and gauges and renders the Prometheus text exposition
 * format for `GET /metrics`. Kept intentionally tiny — the BFF only needs a handful
 * of counters (notably `csrf_violations_total`, mandated by the WP13 spec) plus a
 * couple of request/proxy counters.
 */

type Labels = Readonly<Record<string, string>>;

interface Series {
  labels: Labels;
  value: number;
}

type MetricType = 'counter' | 'gauge';

class Metric {
  private readonly series = new Map<string, Series>();

  constructor(
    readonly name: string,
    readonly help: string,
    readonly type: MetricType,
  ) {}

  private keyFor(labels: Labels): string {
    const keys = Object.keys(labels).sort();
    return keys.map((k) => `${k}=${labels[k]}`).join(',');
  }

  inc(labels: Labels = {}, amount = 1): void {
    const key = this.keyFor(labels);
    const existing = this.series.get(key);
    if (existing) existing.value += amount;
    else this.series.set(key, { labels, value: amount });
  }

  set(labels: Labels, value: number): void {
    const key = this.keyFor(labels);
    this.series.set(key, { labels, value });
  }

  /** Current value for a label-set (0 if never observed) — used by tests. */
  get(labels: Labels = {}): number {
    return this.series.get(this.keyFor(labels))?.value ?? 0;
  }

  render(): string {
    const lines: string[] = [`# HELP ${this.name} ${this.help}`, `# TYPE ${this.name} ${this.type}`];
    if (this.series.size === 0) {
      lines.push(`${this.name} 0`);
      return lines.join('\n');
    }
    for (const s of this.series.values()) {
      const labelStr = renderLabels(s.labels);
      lines.push(`${this.name}${labelStr} ${s.value}`);
    }
    return lines.join('\n');
  }
}

function renderLabels(labels: Labels): string {
  const keys = Object.keys(labels);
  if (keys.length === 0) return '';
  const inner = keys
    .map((k) => `${k}="${escapeLabelValue(String(labels[k]))}"`)
    .join(',');
  return `{${inner}}`;
}

function escapeLabelValue(v: string): string {
  return v.replace(/\\/g, '\\\\').replace(/\n/g, '\\n').replace(/"/g, '\\"');
}

export class MetricsRegistry {
  private readonly metrics = new Map<string, Metric>();

  counter(name: string, help: string): Metric {
    return this.getOrCreate(name, help, 'counter');
  }

  gauge(name: string, help: string): Metric {
    return this.getOrCreate(name, help, 'gauge');
  }

  private getOrCreate(name: string, help: string, type: MetricType): Metric {
    const existing = this.metrics.get(name);
    if (existing) return existing;
    const m = new Metric(name, help, type);
    this.metrics.set(name, m);
    return m;
  }

  render(): string {
    return Array.from(this.metrics.values())
      .map((m) => m.render())
      .join('\n\n') + '\n';
  }
}

/** The well-known metrics the BFF maintains. Built once and shared via the app context. */
export interface BffMetrics {
  readonly registry: MetricsRegistry;
  readonly csrfViolations: Metric;
  readonly httpRequests: Metric;
  readonly proxyRequests: Metric;
  readonly loginAttempts: Metric;
  readonly cacheHits: Metric;
}

export function createMetrics(): BffMetrics {
  const registry = new MetricsRegistry();
  return {
    registry,
    csrfViolations: registry.counter(
      'csrf_violations_total',
      'Total number of rejected requests due to a missing or mismatched CSRF token',
    ),
    httpRequests: registry.counter(
      'bff_http_requests_total',
      'Total HTTP requests handled by the BFF, by method and status class',
    ),
    proxyRequests: registry.counter(
      'bff_proxy_requests_total',
      'Total downstream proxy requests, by upstream and outcome',
    ),
    loginAttempts: registry.counter(
      'bff_login_attempts_total',
      'Total login attempts, by outcome',
    ),
    cacheHits: registry.counter(
      'bff_dashboard_cache_total',
      'Dashboard cache lookups, by result (hit/miss)',
    ),
  };
}

export type { Metric };
