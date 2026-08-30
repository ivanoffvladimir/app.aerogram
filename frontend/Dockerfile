# Сборка статики кабинета. Результат забирает Caddy — своего сервера
# у фронта нет (ADR-0012: серверный рендеринг не используется).
FROM node:22-alpine AS build
WORKDIR /app

RUN corepack enable

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
# Секреты в бандл не попадают: сюда не передаётся ни одной переменной,
# кроме режима сборки (фронт-ТЗ, раздел 1).
RUN pnpm build

# Готовая статика копируется в том, который монтирует Caddy.
FROM alpine:3.20
COPY --from=build /app/out /srv/frontend
CMD ["true"]
