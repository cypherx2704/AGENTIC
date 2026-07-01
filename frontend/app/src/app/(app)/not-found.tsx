import Link from 'next/link';
import { Card, CardBody } from '@/components/ui';

/**
 * Not-found UI for the authenticated console — rendered inside the shell when a page
 * calls notFound() (e.g. an unknown agent/task id). Keeps the operator in-context with
 * a clear way back, instead of dead-ending on the bare framework 404.
 */
export default function AppNotFound() {
  return (
    <div className="mx-auto max-w-xl py-12">
      <Card>
        <CardBody className="flex flex-col items-start gap-4">
          <div>
            <h1 className="text-lg font-semibold text-fg">Not found</h1>
            <p className="mt-1 text-sm text-muted">
              The page or resource you&rsquo;re looking for doesn&rsquo;t exist, or may have been
              moved or deleted.
            </p>
          </div>
          <Link
            href="/"
            className="rounded-md bg-brand px-4 py-2 text-sm font-medium text-brand-fg hover:opacity-90"
          >
            Back to dashboard
          </Link>
        </CardBody>
      </Card>
    </div>
  );
}
