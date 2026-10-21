---
name: verify
description: Проверки Tete-a-tete по изменённым частям репозитория — ruff, mypy и pytest для server/; lint, typecheck и vitest для client/; актуальность сгенерированного протокола; поиск IP, SDP и кандидатов в логах сервера. Используй перед заявлением, что задача готова, перед финальным коммитом и перед PR.
argument-hint: "[BASE]"
---

# Проверка изменений

Аргумент: `$ARGUMENTS` — базовый коммит. Если пусто, база — `git merge-base HEAD main`.

## 1. Что изменилось

```
git diff --name-only <BASE>
git status --porcelain
```

## 2. Команды по частям

| Изменено | Команды | Откуда |
| --- | --- | --- |
| `server/` | `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest` | `server/` |
| `client/` | `npm run lint`, `npm run typecheck`, `npm test` | корень |
| `server/app/protocol.py`, `protocol/`, `client/src/protocol.gen.ts` | `npm run gen:protocol`, затем `git status --porcelain -- protocol/schema.json client/src/protocol.gen.ts` — вывод должен быть пустым | корень |
| `deploy/` | `docker compose -f deploy/docker-compose.yml config`, `caddy validate --config deploy/Caddyfile`, если инструменты есть | корень |
| `.github/workflows/` | `actionlint`, если есть | корень |

Изменения в протоколе запускают и серверные, и клиентские проверки.

## 3. Приватность сервера

Если менялся `server/`, найди в добавленных строках логи и ошибки, где может оказаться адрес, SDP, кандидат, id комнаты или токен:

```
git diff <BASE> -- server/ | grep -nE '^\+.*(log|print|raise|detail=|warn)' | grep -iE 'host|addr|ip|sdp|candidate|room|token|ValidationError'
```

Каждое совпадение прочитай в коде. Совпадение — ещё не нарушение, но объяснить его нужно в отчёте.

## 4. Правила

- Запускай команду, читай вывод целиком, считай пройденные и упавшие тесты. Успех без вывода команды не заявляется.
- Если команды или скрипта ещё нет (до этапа 0), это «недоступно», а не «прошло».
- Упавший тест не чинится правкой теста под код без причины. Причина ищется по `superpowers:systematic-debugging`.

## 5. Отчёт

Таблица: часть, команда, результат (прошло, упало, недоступно), числа (тесты, ошибки), затем найденное в проверке приватности.
