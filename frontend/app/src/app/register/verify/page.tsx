'use client';

import { Suspense, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { BffError, verifyTenant, type VerifyResult } from '@/lib/bff-client';
import { useSession } from '@/components/SessionProvider';
import { Button, Card, CardBody, CardHeader, ErrorBanner, Spinner, useToast } from '@/components/ui';
import { config } from '@/lib/config';

type Phase = 'verifying' | 'done' | 'error';

// A transient failure (BFF/Auth unreachable, gateway timeout) is worth retrying — verify is now
// idempotent server-side, so a replay safely returns the provisioned tenant + a fresh key. A 4xx
// (410 expired/used, 422 bad token) is terminal and fails fast.
const TRANSIENT_STATUSES = new Set([0, 502, 503, 504]);
function isTransient(err: unknown): boolean {
  return err instanceof BffError && TRANSIENT_STATUSES.has(err.status);
}

/** Verify with bounded backoff retries on transient failures; rethrows terminal/!transient errors. */
async function verifyWithRetry(
  token: string,
  onRetry: (attempt: number) => void,
  signal?: AbortSignal,
): Promise<VerifyResult> {
  const delays = [1500, 3000, 5000, 8000];
  let lastErr: unknown;
  for (let i = 0; i <= delays.length; i++) {
    try {
      return await verifyTenant(token, signal);
    } catch (err) {
      lastErr = err;
      if (i === delays.length || !isTransient(err)) throw err;
      onRetry(i + 1);
      await new Promise((resolve) => setTimeout(resolve, delays[i]));
    }
  }
  throw lastErr;
}

function VerifyInner() {
  const router = useRouter();
  const params = useSearchParams();
  const { refresh } = useSession();
  const toast = useToast();
  const token = params.get('token') ?? '';

  const [phase, setPhase] = useState<Phase>('verifying');
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  // Auto-login establishes the session from the freshly-issued api_key. If it ever fails we still
  // show the credentials so the user can sign in manually.
  const [loggedIn, setLoggedIn] = useState(false);
  const [copied, setCopied] = useState(false);
  const [retrying, setRetrying] = useState(0);
  // The verification token is single-use (the backend claims it atomically) — guard against
  // React's dev double-invoke so we never fire the consume twice and trip a spurious 410.
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    void (async () => {
      if (!token.trim()) {
        setError(new Error('This verification link is missing its token. Open the link from your email again.'));
        setPhase('error');
        return;
      }
      try {
        const res = await verifyWithRetry(token.trim(), (n) => setRetrying(n));
        setResult(res);
        // Console login is now email/password (or Google). This email-verification flow yields the
        // orchestrator's api_key for SDK/programmatic use — it is shown once below; no auto-login.
        setLoggedIn(false);
        setPhase('done');
      } catch (err) {
        setError(err);
        setPhase('error');
      }
    })();
  }, [token, refresh]);

  async function copyKey() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.api_key);
      setCopied(true);
      toast.success('API key copied to clipboard.');
    } catch {
      toast.error('Clipboard unavailable — select and copy the key manually.');
    }
  }

  if (phase === 'verifying') {
    return (
      <Card className="w-full max-w-md">
        <CardBody>
          <div className="flex flex-col items-center gap-3 py-6 text-center">
            <Spinner size="md" />
            <p className="text-sm text-muted">Verifying your email and provisioning your tenant…</p>
            {retrying > 0 && (
              <p className="text-xs text-muted">Taking a little longer than usual — retrying (attempt {retrying + 1})…</p>
            )}
          </div>
        </CardBody>
      </Card>
    );
  }

  if (phase === 'error') {
    return (
      <Card className="w-full max-w-md">
        <CardHeader title="Verification failed" description="This link could not be used." />
        <CardBody>
          <div className="flex flex-col gap-4">
            <ErrorBanner error={error} />
            <p className="text-sm text-muted">
              The link may have expired or already been used. You can request a new one.
            </p>
            <div className="flex items-center gap-2">
              <Link href="/register">
                <Button size="sm">Start over</Button>
              </Link>
              <Link href="/login" className="text-sm text-brand hover:underline">
                Back to sign in
              </Link>
            </div>
          </div>
        </CardBody>
      </Card>
    );
  }

  // phase === 'done'
  return (
    <Card className="w-full max-w-md">
      <CardHeader
        title="Your tenant is ready 🎉"
        description={loggedIn ? "You're signed in. Save your admin API key below." : 'Save your admin API key, then sign in.'}
      />
      <CardBody>
        <div className="flex flex-col gap-4">
          <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            This admin API key is shown only once. Store it securely — it&apos;s how you authenticate
            programmatically and recover console access.
          </div>

          {result && (
            <>
              <div>
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">Admin API key</p>
                <code className="block break-all rounded-md border border-border bg-surface-2 px-3 py-3 font-mono text-sm text-fg">
                  {result.api_key}
                </code>
                <div className="mt-2">
                  <Button variant="secondary" size="sm" onClick={copyKey}>
                    {copied ? 'Copied' : 'Copy key'}
                  </Button>
                </div>
              </div>

              <dl className="grid grid-cols-1 gap-2 text-xs text-muted sm:grid-cols-2">
                <Field label="Tenant" value={result.tenant_name} />
                <Field label="Plan" value={result.plan} />
                <Field label="Tenant ID" value={<span className="font-mono">{result.tenant_id}</span>} />
                <Field label="Agent ID" value={<span className="font-mono">{result.agent_id}</span>} />
              </dl>
            </>
          )}

          {loggedIn ? (
            <Button size="lg" onClick={() => router.replace('/')}>
              Enter console
            </Button>
          ) : (
            <div className="flex flex-col gap-2">
              <p className="text-sm text-muted">
                Sign in with the tenant id, agent id, and the API key above.
              </p>
              <Link href="/login">
                <Button size="lg" className="w-full">
                  Go to sign in
                </Button>
              </Link>
            </div>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="font-medium text-fg">{label}</dt>
      <dd className="mt-0.5 break-all">{value}</dd>
    </div>
  );
}

export default function VerifyPage() {
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
          <VerifyInner />
        </Suspense>
      </div>
    </div>
  );
}
