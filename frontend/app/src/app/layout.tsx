import type { Metadata } from 'next';
import './globals.css';
import { SessionProvider } from '@/components/SessionProvider';
import { ToastProvider } from '@/components/ui';
import { config } from '@/lib/config';

export const metadata: Metadata = {
  title: `${config.appName} Console`,
  description: 'CypherX enterprise agent platform — operator console.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ToastProvider>
          <SessionProvider>{children}</SessionProvider>
        </ToastProvider>
      </body>
    </html>
  );
}
