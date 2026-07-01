'use client';

import Link from 'next/link';
import { PageHeader } from '@/components/AppShell';
import { useSession } from '@/components/SessionProvider';
import { Badge, Card, CardBody, CardHeader } from '@/components/ui';

interface Shortcut {
  href: string;
  title: string;
  body: string;
}

const SHORTCUTS: Shortcut[] = [
  { href: '/agents', title: 'Agents', body: 'Browse agents, edit runtime config, and publish with the Agent Builder.' },
  { href: '/keys', title: 'API Keys', body: 'Issue and revoke agent keys. Raw secrets are shown exactly once.' },
  { href: '/tasks/run', title: 'Task Runner', body: 'Submit a task, stream the live timeline, and see real cost + tokens.' },
  { href: '/tasks', title: 'Task Feed', body: 'Live feed of tasks with a 5s long-poll and status/agent filters.' },
  { href: '/guardrails', title: 'Guardrails', body: 'Manage policies and review the violation log.' },
  { href: '/usage', title: 'LLM Usage & Cost', body: 'Token usage, cost by model, and the cache-token breakdown.' },
  { href: '/rag', title: 'Knowledge Bases', body: 'KB status and an ad-hoc test-query box.' },
  { href: '/audit', title: 'Audit Log', body: 'Tamper-evident audit entries with a chain-verify button.' },
  { href: '/health', title: 'Platform Health', body: 'livez / readyz of every platform service via the BFF.' },
];

export default function DashboardPage() {
  const { session } = useSession();

  return (
    <div>
      <PageHeader
        title="Operator console"
        description="Everything routes through the BFF — the browser never holds a token."
        actions={
          session?.scopes.length ? (
            <div className="flex flex-wrap gap-1">
              {session.scopes.slice(0, 4).map((s) => (
                <Badge key={s} tone="info">
                  {s}
                </Badge>
              ))}
              {session.scopes.length > 4 && <Badge>+{session.scopes.length - 4}</Badge>}
            </div>
          ) : null
        }
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {SHORTCUTS.map((s) => (
          <Link key={s.href} href={s.href} className="block">
            <Card className="h-full transition-colors hover:border-brand/50">
              <CardHeader title={s.title} />
              <CardBody>
                <p className="text-sm text-muted">{s.body}</p>
              </CardBody>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
