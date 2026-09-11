FROM oven/bun:1
WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile
COPY index.ts .

# The bridge defaults to 127.0.0.1:8787; compose overrides HOST so the
# gateway container can reach it over the internal network.
ENV HOST=0.0.0.0 PORT=8787
EXPOSE 8787

CMD ["bun", "index.ts"]
