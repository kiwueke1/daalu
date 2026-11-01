FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --legacy-peer-deps

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# Where the Next.js rewrites send /api, /sse, /ide and /ws. Baked in at build
# time. Under Kubernetes the ingress routes those paths straight to the API,
# so the default only matters for Docker Compose (which passes http://api:8000).
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
# node:20-alpine already ships a `node` user/group at UID/GID 1000 — reuse
# it instead of trying to create another at the same id (the original
# `addgroup -g 1000 nodejs` failed because GID 1000 was already taken).
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
USER node
EXPOSE 3000
ENV PORT=3000
CMD ["node", "server.js"]
