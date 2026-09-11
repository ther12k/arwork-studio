# --- build: static frontend via Vite -------------------------------------
FROM oven/bun:1 AS web-build
WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile
COPY . .
RUN bun run build

# --- serve: Caddy serves dist/ and reverse-proxies the mini-services ------
FROM caddy:2-alpine
COPY docker/Caddyfile /etc/caddy/Caddyfile
COPY --from=web-build /app/dist /srv/web

EXPOSE 81
