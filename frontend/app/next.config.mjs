import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The SPA is served standalone (Dockerfile) and proxies every API call to the BFF.
  // `output: standalone` produces a minimal self-contained server bundle for the image.
  output: 'standalone',
  // Pin the file-tracing root to THIS app so a stray lockfile in a parent dir doesn't
  // confuse Next's workspace-root inference (we are a leaf app in a polyrepo).
  outputFileTracingRoot: __dirname,
  // Security headers applied to every response. The BFF owns CORS/CSRF for /bff/*;
  // these harden the SPA shell itself.
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
        ],
      },
    ];
  },
};

export default nextConfig;
