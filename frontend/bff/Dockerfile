# syntax=docker/dockerfile:1
# ── CypherX BFF (WP13) — multi-stage production image ─────────────────────────────────
# The security boundary between the browser SPA and the platform services. Fastify +
# TypeScript, compiled to dist/ then run on a slim non-root runtime. Listens on
# BFF_PORT (default 8088) inside the container. Node 22 (engines: ">=22").

# 1) Dependencies (incl. dev) — needed to compile TypeScript with a reproducible lockfile.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

# 2) Build — emit the dist/ JavaScript bundle (tsc -p tsconfig.json).
FROM node:24-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# 3) Production deps only — drop the dev toolchain (tsc/eslint/vitest) from the image.
FROM node:24-alpine AS prod-deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --no-audit --no-fund

# 4) Runner — minimal image, non-root, only the compiled output + prod deps.
FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    BFF_HOST=0.0.0.0 \
    BFF_PORT=8088

# Non-root user to run the server.
RUN addgroup --system --gid 1001 nodejs \
  && adduser --system --uid 1001 bff

COPY --from=prod-deps --chown=bff:nodejs /app/node_modules ./node_modules
COPY --from=builder   --chown=bff:nodejs /app/dist          ./dist
COPY --chown=bff:nodejs package.json ./

USER bff
EXPOSE 8088

# Process liveness via the BFF's own /livez (stdlib http, no curl in the image).
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD node -e "require('http').get('http://127.0.0.1:'+(process.env.BFF_PORT||8088)+'/livez',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "dist/server.js"]
