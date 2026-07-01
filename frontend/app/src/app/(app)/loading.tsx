import { Loading } from '@/components/ui';

/**
 * Route-transition fallback for the authenticated console. Shows an immediate, stable
 * loading state inside the shell while a segment streams in, instead of a frozen page.
 */
export default function AppLoading() {
  return <Loading label="Loading…" />;
}
