import type { Metadata } from 'next';
import { Inter, IBM_Plex_Mono } from 'next/font/google';
import './globals.css';
import { SessionProvider } from '@/components/SessionProvider';
import { ToastProvider } from '@/components/ui';
import { config } from '@/lib/config';

// UI face + the monospace face for every id / trace / token / scope in the console.
const inter = Inter({ subsets: ['latin'], variable: '--font-sans', display: 'swap' });
const plexMono = IBM_Plex_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: `${config.appName} Console`,
  description: 'CypherX enterprise agent platform — operator console.',
};

// Set the saved theme before first paint so the toggle preference never flashes.
// Defaults to the OS preference (handled by the CSS media query) when unset.
const noFlashTheme = `(function(){try{var t=localStorage.getItem('cx-theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${plexMono.variable}`} suppressHydrationWarning>
      <body>
        <script dangerouslySetInnerHTML={{ __html: noFlashTheme }} />
        <ToastProvider>
          <SessionProvider>{children}</SessionProvider>
        </ToastProvider>
      </body>
    </html>
  );
}
