'use client';

import { Suspense, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { googleLoginUrl, login } from '@/lib/bff-client';
import { useSession } from '@/components/SessionProvider';
import { Button, Card, CardBody, CardHeader, ErrorBanner, Input, PasswordInput } from '@/components/ui';
import { config } from '@/lib/config';

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { refresh } = useSession();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const next = params.get('next') || '/';
  // The Google callback bounces back here with ?error=google on a failed exchange.
  const googleError = params.get('error') === 'google';

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password);
      await refresh();
      // Only follow a relative `next` (never an absolute URL — open-redirect guard).
      router.replace(next.startsWith('/') ? next : '/');
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader title={`Sign in to ${config.appName}`} description="Sign in with your email and password, or continue with Google." />
      <CardBody>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Input
            label="Email"
            type="email"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
          <PasswordInput
            label="Password"
            placeholder="••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
          {error ? <ErrorBanner error={error} /> : null}
          {googleError ? (
            <p className="text-sm text-danger">Google sign-in failed. Please try again.</p>
          ) : null}
          <Button type="submit" loading={submitting} disabled={!email.trim() || !password} size="lg">
            Sign in
          </Button>
        </form>

        <div className="my-4 flex items-center gap-3 text-xs text-muted">
          <span className="h-px flex-1 bg-border" />
          OR
          <span className="h-px flex-1 bg-border" />
        </div>

        {/* Full-page navigation to the BFF Google start (it 302s to Google's consent screen). */}
        <a
          href={googleLoginUrl()}
          className="flex w-full items-center justify-center gap-2 rounded-md border border-border bg-surface px-4 py-2.5 text-sm font-medium text-fg hover:bg-bg"
        >
          <span className="font-bold text-brand">G</span> Continue with Google
        </a>

        <p className="mt-4 text-center text-sm text-muted">
          New to {config.appName}?{' '}
          <Link href="/register" className="text-brand hover:underline">
            Create an account
          </Link>
        </p>
      </CardBody>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-bg p-4">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand text-lg font-bold text-brand-fg">
            C
          </span>
          <span className="text-lg font-semibold text-fg">{config.appName}</span>
        </div>
        <Suspense fallback={<div className="text-center text-sm text-muted">Loading…</div>}>
          <LoginForm />
        </Suspense>
      </div>
    </div>
  );
}
