'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { register, type RegisterResult } from '@/lib/bff-client';
import { useSession } from '@/components/SessionProvider';
import { Button, Card, CardBody, CardHeader, ErrorBanner, Input, PasswordInput } from '@/components/ui';
import { config } from '@/lib/config';

export default function RegisterPage() {
  const router = useRouter();
  const { refresh } = useSession();
  const [tenantName, setTenantName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<RegisterResult | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const r = await register({
        email: email.trim(),
        password,
        tenant_name: tenantName.trim() || undefined,
      });
      setResult(r);
      // The BFF auto-logs the new user in (session cookie already set) — refresh session state.
      if (r.authenticated) await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg p-4">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand text-lg font-bold text-brand-fg">
            C
          </span>
          <span className="text-lg font-semibold text-fg">{config.appName}</span>
        </div>

        {result ? (
          <Card className="w-full max-w-md">
            <CardHeader
              title="Account created"
              description="Your workspace and orchestrator agent are ready."
            />
            <CardBody>
              <div className="flex flex-col gap-4">
                <p className="text-sm text-muted">
                  We created your tenant and its mandatory <span className="font-medium text-fg">orchestrator</span> agent.
                  Save the orchestrator&apos;s API key below — it is shown <span className="font-medium text-fg">only once</span> and
                  is used for SDK / programmatic access.
                </p>
                <div className="rounded-md border border-border bg-surface p-3">
                  <div className="text-xs text-muted">Orchestrator API key</div>
                  <code className="block break-all text-sm text-fg">{result.api_key}</code>
                </div>
                <Button size="lg" onClick={() => router.replace('/')}>
                  Continue to console
                </Button>
              </div>
            </CardBody>
          </Card>
        ) : (
          <Card className="w-full max-w-md">
            <CardHeader
              title="Create your account"
              description="Sign up for a new CypherX workspace. We create your orchestrator agent automatically."
            />
            <CardBody>
              <form onSubmit={onSubmit} className="flex flex-col gap-4">
                <Input
                  label="Workspace / tenant name"
                  placeholder="Acme Inc."
                  value={tenantName}
                  onChange={(e) => setTenantName(e.target.value)}
                  autoComplete="organization"
                  hint="Optional — defaults to your email handle. You can change it later."
                />
                <Input
                  label="Email"
                  type="email"
                  placeholder="you@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email"
                  required
                />
                <PasswordInput
                  label="Password"
                  placeholder="At least 8 characters"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password"
                  required
                  hint="Used to sign in to the console."
                />
                {error ? <ErrorBanner error={error} /> : null}
                <Button
                  type="submit"
                  loading={submitting}
                  disabled={!email.trim() || password.length < 8}
                  size="lg"
                >
                  Create account
                </Button>
              </form>
              <p className="mt-4 text-center text-sm text-muted">
                Already have an account?{' '}
                <Link href="/login" className="text-brand hover:underline">
                  Sign in
                </Link>
              </p>
            </CardBody>
          </Card>
        )}
      </div>
    </div>
  );
}
