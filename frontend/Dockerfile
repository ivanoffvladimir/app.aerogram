FROM node:22-alpine AS builder

RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build


# Статика отдаётся Caddy из общего тома, поэтому финальный образ — только файлы.
FROM alpine:3.20 AS runtime
WORKDIR /srv/frontend
COPY --from=builder /app/dist ./
CMD ["true"]
