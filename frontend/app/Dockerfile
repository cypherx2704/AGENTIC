# ── CypherX SPA — multi-stage production image ────────────────────────────────────────
# Builds the Next.js standalone bundle, then runs it as a non-root user on a slim runtime.
# Node 24 (available in the target environment).

# 1) Dependencies — install with a clean, reproducible lockfile.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

# 2) Build — compile the standalone server bundle.
FROM node:24-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* values are inlined at build time. They contain NO secrets (the BFF holds
# every secret + the httpOnly session cookie); override at build for a fixed BFF origin.
# Empty NEXT_PUBLIC_BFF_URL => same-origin (calls go to /bff/*, e.g. behind the edge proxy).
ARG NEXT_PUBLIC_BFF_URL=""
ARG NEXT_PUBLIC_BFF_PREFIX="/bff"
ENV NEXT_PUBLIC_BFF_URL=${NEXT_PUBLIC_BFF_URL} \
    NEXT_PUBLIC_BFF_PREFIX=${NEXT_PUBLIC_BFF_PREFIX} \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# 3) Runner — minimal image, non-root, only the standalone output.
FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# Create a non-root user to run the server.
RUN addgroup --system --gid 1001 nodejs \
  && adduser --system --uid 1001 nextjs

# The standalone output bundles only the files the server needs.
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000

# Container healthcheck hits the app's own root (served by the standalone server).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:'+(process.env.PORT||3000)+'/login',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
