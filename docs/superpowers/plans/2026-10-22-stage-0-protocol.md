# Этап 0 (0.2–0.4). Протокол, генерация типов клиента и CI — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pydantic-модели всех сообщений сигналинга, генерация из них `protocol/schema.json` и zod-схем клиента одной командой `npm run gen:protocol` и CI на GitHub Actions, который проверяет обе части и актуальность сгенерированного.

**Architecture:** Источник истины — `server/app/protocol.py`. `server/app/protocol_schema.py` выгружает JSON Schema. Свой генератор `client/scripts/gen-protocol.ts` превращает её в `client/src/protocol.gen.ts`, затем Prettier форматирует оба файла. Общий файл `protocol/examples.json` читают тесты pytest и vitest, поэтому сервер и клиент принимают и отклоняют одно и то же.

**Tech Stack:** Python 3.13, Pydantic 2.14, pytest; Node 24 (запускает `.ts` без сборки), TypeScript 6.0, zod 4.6, vitest 5, Prettier 3.9; GitHub Actions, actionlint в Docker.

**Spec:** `docs/superpowers/specs/2026-10-21-stage-0-protocol-design.md`. Пункт 0.5 — действия человека, в плане его нет: `docs/superpowers/specs/2026-10-21-stage-0.5-code-signing-checklist.md`.

## Global Constraints

Из CLAUDE.md, дословно:

- «**Источник истины — Pydantic-модели в `server/app/protocol.py`.** Из них генерируется `protocol/schema.json`, а из него — TypeScript-типы и zod-схемы клиента в `client/src/protocol.gen.ts`. Сгенерированные файлы руками не правят: меняется модель, затем запускается `gen:protocol`.»
- «Каждое входящее сообщение разбирается Pydantic-моделью — discriminated union по `type`. Ошибка — `bad-request`.»
- «В Python — snake_case, в JSON протокола — camelCase: модели с `alias_generator=to_camel` и `populate_by_name=True`.»
- «Не логировать и не хранить IP, SDP и ICE-кандидаты — даже в сообщениях об ошибках.»
- «`data` в `signal` — это `{description}`, `{candidate}` или `{relayTrack: {mid, from}}`. Сервер его не читает и пересылает только внутри комнаты отправителя.»
- «**Доступ по ссылке** со случайным id комнаты: не короче 10 символов, из криптостойкого генератора.»
- «`net` — итог проверки сети клиента: `{nat, upnp, cgnat, doubleNat, vpn}`.»
- «**Версии инструментов:** TypeScript закреплён на `~6.0.3` … Node ≥ 24.15 (`engines`, `.nvmrc`).»
- «**Хуки.** Ручная правка `protocol/schema.json` и `client/src/protocol.gen.ts` блокируется. Флаги `--no-verify` и `--no-gpg-sign` блокируются.» Эти файлы появляются только запуском генерации.

Строки таблицы «Константы»: «Участников в комнате — 5»; «Длина id комнаты — ≥ 10 символов»; «Максимальный размер сообщения — 64 КБ (`--ws-max-size`)».

Из спецификации:

- id комнаты: 12 символов из `abcdefghijkmnpqrstuvwxyz23456789`, через `secrets.choice`. id участника: 8 символов из того же алфавита.
- Каждое поле `net` может быть `null` — «не определено». Все пять ключей обязательны.
- Новых зависимостей, кроме `zod` `^4.6.7` в `client`, не добавлять. Генератор пишется без библиотек JSON Schema.
- Сгенерированные файлы — UTF-8 и LF. Повторная генерация без изменений модели не меняет ни байта.
- Сообщения коммитов — как в истории: `feat(server): …`, `test(client): …`, `ci: …`, по-русски. Последняя строка — `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Генерация на Windows и на Linux.** У разработчика `core.autocrlf=true`, CI на Linux. Ожидается: `npm run gen:protocol` на Windows даёт файлы без `\r`, и задание `protocol` в CI видит пустой diff. Проверяется в задаче 4, шаг 6, и в задаче 5, шаг 5.
2. **Глубоко вложенный `data`.** `signal` с `data` вложенности 10 000 укладывается в 64 КБ. Ожидается `BadRequestError`, а не `RecursionError` и не падение процесса. Обычный SDP на 60 000 символов разбирается. Задача 1, шаг 1.
3. **Байты не в UTF-8 и не JSON.** WebSocket может прислать бинарный кадр. Ожидается `BadRequestError`. Задача 1, шаг 1.
4. **Регулярные выражения в Python и JavaScript.** Шаблоны исполняют движок Rust в pydantic-core и `RegExp` с флагом `u`. Ожидается одинаковый ответ на эмодзи, кириллицу, C1-управляющий `\u0085` и `\u007f`. Эти случаи есть в `protocol/examples.json` (задача 1), его проходят обе стороны (задачи 1 и 4).
5. **Новое поле в сообщении сервера.** Будущий сервер добавит поле. Ожидается: сгенерированная `ServerMessage` его пропускает и отбрасывает, а лишнее поле в сообщении клиента — ошибка. Задача 4, шаг 1.

---

### Task 1: Модели протокола, разбор, генераторы id и общие примеры

**Агент:** `server-engineer`

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`; из корня: `npx prettier --check protocol/examples.json`.

Протокол меняется впервые. Скилл `protocol-change`, шаги 1–2, применим целиком. Шаг 3 (генерация) делают задачи 2–4.

**Files:**
- Create: `server/app/protocol.py`, `server/tests/test_protocol.py`, `protocol/examples.json`

**Interfaces:**
- Produces — модуль `app.protocol`:
  - константы `ID_ALPHABET: Final = "abcdefghijkmnpqrstuvwxyz23456789"`, `ROOM_ID_LENGTH: Final = 12`, `PEER_ID_LENGTH: Final = 8`, `ROOM_CAPACITY: Final = 5`, `NAME_MAX_LENGTH: Final = 32`, `RESUME_TOKEN_MAX_LENGTH: Final = 128`, `PAIR_MS_MAX: Final = 3_600_000`, `PAIR_RESTARTS_MAX: Final = 100`;
  - именованные типы через `type X = …` (PEP 695): `RoomId`, `PeerId`, `Name`, `ResumeToken`, `NatType` (`open` | `cone` | `symmetric` | `no-udp`), `CandidateType` (`host` | `srflx` | `prflx` | `upnp`), `PairResult` (`direct` | `failed`), `IpFamily` (`ipv4` | `ipv6`), `ErrorCode` (`room-full` | `bad-request`);
  - модели `NetInfo`, `Route`, `PeerInfo`; `ClientCreate`, `ClientJoin`, `ClientSignal`, `ClientMute`, `ClientPairReport`, `ClientPing`, `ClientLeave`; `ServerJoined`, `ServerPeerJoined`, `ServerPeerLeft`, `ServerSignal`, `ServerMute`, `ServerRelay`, `ServerPong`, `ServerError`. Поля и правила — таблицы раздела 0.2 спецификации, имена полей в Python — snake_case (`room_id`, `self_id`, `resume_token`, `same_ip`, `double_nat`, `from_` с алиасом `from`);
  - `type ClientMessage = Annotated[ClientCreate | … | ClientLeave, Field(discriminator="type")]` и `type ServerMessage = …` из восьми серверных классов;
  - `client_message_adapter: Final[TypeAdapter[ClientMessage]]`, `server_message_adapter: Final[TypeAdapter[ServerMessage]]`;
  - `class BadRequestError(Exception)` без аргументов;
  - `parse_client_message(raw: str | bytes) -> ClientMessage`, `dump_server_message(message: ServerMessage) -> str`, `new_room_id() -> str`, `new_peer_id() -> str`.

- [ ] **Step 1: Написать `protocol/examples.json` и падающие тесты**

Формат файла — из спецификации: `{"client": {"valid": [...], "invalid": [{"why", "message"}]}, "server": {...}}`. Каждое из 15 сообщений — хотя бы один корректный пример. Отдельно корректными должны быть: `join` с `resumeToken` и без него; `net` со всеми `null`; `pair-report` с `failed` и `route`, `family`, `ms`, `sameIp` = `null`; имя из 32 эмодзи `😀`; кириллическое имя; `relay` с `via: null`; `joined` с 4 участниками. Некорректные — с пояснением `why`:

| Направление | Случай |
| --- | --- |
| client | `roomId` из 11 и из 13 символов; с `l`, с `o`, с `0`, с `1`; с заглавной буквой |
| client | `name` пустое; из 33 символов; из 33 эмодзи; с `\u0001`; с `\u007f`; с `\u0085` |
| client | `ms` = −1 и 3 600 001; `restarts` = 101; `ms` = 1.5 |
| client | `nat` = `"full-cone"`; `net` без ключа `vpn`; `muted` = `"true"` |
| client | `data` — массив; `data` — строка; `to` из 7 символов |
| client | `resumeToken` из 129 символов; с пробелом |
| client | лишнее поле; неизвестный `type`; нет `type`; `type` в другом регистре (`"Ping"`) |
| server | `relay` с `via: 5`; `joined` с 5 участниками; `error` с `code: "kicked"` |

Затем `server/tests/test_protocol.py`. Примеры читаются из `Path(__file__).parents[2] / "protocol" / "examples.json"`.

```python
@pytest.mark.parametrize("message", CLIENT_VALID, ids=type_ids)
def test_valid_client_example_parses_to_its_class(message): ...
    # isinstance(parse_client_message(json.dumps(message)), CLASS_BY_TYPE[message["type"]])

@pytest.mark.parametrize("case", CLIENT_INVALID, ids=lambda c: c["why"])
def test_invalid_client_example_is_bad_request(case): ...  # pytest.raises(BadRequestError)

@pytest.mark.parametrize("raw", ["", "{", "[]", "42", "null", '"ping"', '{"type": "ping"', b"\xff\xfe\x00"])
def test_malformed_input_is_bad_request(raw): ...

def test_deeply_nested_signal_data_is_bad_request():
    raw = '{"type":"signal","to":"abcdefgh","data":' + '{"a":' * 10_000 + "1" + "}" * 10_000 + "}"
    # len(raw) < 65_536; pytest.raises(BadRequestError)

def test_signal_with_large_sdp_parses():
    # data = {"description": {"type": "offer", "sdp": "v=0\r\n" + "a" * 60_000}} → ClientSignal, data сохранён как есть

def test_bad_request_carries_no_input():
    # signal с to="bad" и data.description.sdp = "v=0 SECRET-SDP 192.0.2.1"
    # для пойманного e: "SECRET-SDP" и "192.0.2.1" нет в str(e), repr(e), repr(e.args);
    # e.__cause__ is None и e.__context__ is None

def test_server_messages_serialize_by_alias():
    # json.loads(dump_server_message(ServerJoined(room_id=..., self_id=..., resumed=False,
    #   resume_token="t.x", peers=[PeerInfo(id=..., name="Аня", muted=True)])))
    #   == {"type": "joined", "roomId": ..., "selfId": ..., "resumed": False, "resumeToken": "t.x",
    #       "peers": [{"id": ..., "name": "Аня", "muted": True}]}
    # ServerRelay(..., via=None) → в JSON есть "via": null; ServerSignal(from_=...) → ключ "from"

@pytest.mark.parametrize("message", SERVER_VALID, ids=type_ids)
def test_valid_server_example_round_trips(message): ...
    # json.loads(dump_server_message(server_message_adapter.validate_python(message))) == message

@pytest.mark.parametrize("case", SERVER_INVALID, ids=lambda c: c["why"])
def test_invalid_server_example_is_rejected(case): ...  # pytest.raises(ValidationError)

@pytest.mark.parametrize(("generate", "length"), [(new_room_id, 12), (new_peer_id, 8)])
def test_generated_ids_have_format_and_do_not_repeat(generate, length):
    # 10_000 штук: re.fullmatch(f"[{ID_ALPHABET}]{{{length}}}", id) для каждого, len(set(ids)) == 10_000

def test_generated_room_id_is_accepted_in_join(): ...  # ClientJoin с new_room_id() проходит разбор
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `uv run pytest tests/test_protocol.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.protocol'`.

- [ ] **Step 3: Реализовать `server/app/protocol.py`**

Две базовые конфигурации:
- у клиентских моделей, `NetInfo` и `Route` — `alias_generator=to_camel`, `populate_by_name=True`, `extra="forbid"`, `strict=True`;
- у серверных моделей и `PeerInfo` — те же алиасы, без `forbid` и `strict`.

Шаблоны `RoomId` и `PeerId` собираются из `ID_ALPHABET`: `^[a-km-np-z2-9]{12}$` и `^[a-km-np-z2-9]{8}$`. `Name` — `min_length=1`, `max_length=NAME_MAX_LENGTH`, `pattern=r"^[^\x00-\x1f\x7f-\x9f]+$"`. `ResumeToken` — `pattern=r"^[A-Za-z0-9_.-]+$"`, `max_length=RESUME_TOKEN_MAX_LENGTH`. `ServerJoined.peers` — `max_length=ROOM_CAPACITY - 1`.

`parse_client_message`: `client_message_adapter.validate_json(raw)` в `try`; в `except ValidationError` только ставится флаг, а `raise BadRequestError()` идёт после блока. Так в `__context__` не остаётся исключения с входом. Ошибку UTF-8 в байтах pydantic-core тоже выдаёт как `ValidationError`. Если окажется иначе, ловить и её тем же способом.

`dump_server_message`: `server_message_adapter.dump_json(message, by_alias=True).decode()`, поля `None` не выбрасываются.

- [ ] **Step 4: Запустить проверки**

Run: из `server/` — `uv run pytest -q`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`; из корня — `npx prettier --write protocol/examples.json`, затем `npx prettier --check protocol/examples.json`.
Expected: все тесты проходят, ruff и mypy без ошибок, Prettier — «All matched files use Prettier code style!».

- [ ] **Step 5: Commit**

```bash
git add server/app/protocol.py server/tests/test_protocol.py protocol/examples.json
git commit -m "feat(server): модели протокола сигналинга, разбор с bad-request и генераторы id"
```

---

### Task 2: Выгрузка JSON Schema и тест её актуальности

**Агент:** `server-engineer`

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.

**Files:**
- Create: `server/app/protocol_schema.py`, `server/tests/test_protocol_schema.py`, `protocol/schema.json` (создаётся запуском модуля, не Write)

**Interfaces:**
- Consumes: `ClientMessage`, `ServerMessage` из задачи 1.
- Produces:
  - `build_schema() -> dict[str, Any]` — словарь ровно с ключами `$schema` (`"https://json-schema.org/draft/2020-12/schema"`), `title` (`"Tete-a-tete signaling protocol"`), `$defs`. В `$defs` лежат все модели и именованные типы, а также `ClientMessage` и `ServerMessage` с `oneOf` и `discriminator`. Все `$ref` вида `#/$defs/<Имя>`. Объединения попадают в `$defs` под своими именами, если взять схему обёрточной модели с полями типов `ClientMessage` и `ServerMessage` и оставить только её `$defs` (проверено на Pydantic 2.14.1).
  - `write_schema(path: Path) -> None` — `json.dumps(..., indent=2, ensure_ascii=False) + "\n"`, `path.write_text(..., encoding="utf-8", newline="\n")`.
  - запуск: `python -m app.protocol_schema <путь>` из `server/`.

- [ ] **Step 1: Написать падающие тесты `server/tests/test_protocol_schema.py`**

```python
def test_committed_schema_is_up_to_date():
    # json.loads((ROOT / "protocol" / "schema.json").read_text("utf-8")) == build_schema(),
    # сообщение при падении: "protocol/schema.json устарел: запустите npm run gen:protocol"

def test_schema_lists_every_message():
    # set(defs["ClientMessage"]["discriminator"]["mapping"]) ==
    #   {"create", "join", "signal", "mute", "pair-report", "ping", "leave"}
    # set(defs["ServerMessage"]["discriminator"]["mapping"]) ==
    #   {"joined", "peer-joined", "peer-left", "signal", "mute", "relay", "pong", "error"}

def test_client_models_forbid_extra_fields_and_server_models_do_not():
    # defs["ClientJoin"]["additionalProperties"] is False; defs["NetInfo"]["additionalProperties"] is False
    # "additionalProperties" not in defs["ServerJoined"]; "additionalProperties" not in defs["PeerInfo"]

def test_named_types_are_definitions():
    # {"RoomId", "PeerId", "Name", "ResumeToken", "NetInfo", "Route", "PeerInfo"} <= set(defs)

def test_write_schema_uses_utf8_and_lf(tmp_path):
    # data = (tmp_path / "s.json").read_bytes() после write_schema; b"\r" not in data;
    # data.endswith(b"}\n"); json.loads(data.decode("utf-8")) == build_schema()
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `uv run pytest tests/test_protocol_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.protocol_schema'`.

- [ ] **Step 3: Реализовать `server/app/protocol_schema.py`**

Автоматические `title` у полей можно убрать своим `GenerateJsonSchema` — на усмотрение исполнителя, спецификация это разрешает.

- [ ] **Step 4: Сгенерировать `protocol/schema.json` и запустить проверки**

Run: из `server/` — `uv run python -m app.protocol_schema ../protocol/schema.json`, затем `uv run pytest -q`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`.
Expected: файл создан; все тесты проходят, включая `test_committed_schema_is_up_to_date`; ruff и mypy без ошибок.

- [ ] **Step 5: Commit**

```bash
git add server/app/protocol_schema.py server/tests/test_protocol_schema.py protocol/schema.json
git commit -m "feat(server): выгрузка JSON Schema протокола и тест её актуальности"
```

---

### Task 3: Генератор zod-схем из JSON Schema

**Агент:** `electron-engineer`

**Проверка:** из корня: `npm run lint`, `npm run typecheck`, `npm test`.

**Files:**
- Create: `client/scripts/gen-protocol.ts`, `client/scripts/gen-protocol.test.ts`, `client/scripts/tsconfig.json`
- Modify: `client/package.json` (скрипт `typecheck`), `client/vitest.config.ts` (`include`)

**Interfaces:**
- Produces — модуль `client/scripts/gen-protocol.ts`:
  - `export class UnsupportedSchemaError extends Error` с полем `readonly path: string` — JSON Pointer узла, например `#/$defs/A/exclusiveMinimum`. Параметры-свойства конструктора не использовать: Node исполняет только стираемый синтаксис;
  - `export function generateZod(schema: unknown): string` — исходный код TypeScript без форматирования, по таблице «JSON Schema → zod 4» из спецификации. Порядок констант — по зависимостям, при равенстве — по имени. Цикл `$ref`, неизвестное ключевое слово, имя `$defs`, которое не подходит под `^[A-Za-z_][A-Za-z0-9_]*$`, `anyOf` не из пары «схема + `null`» — `UnsupportedSchemaError`;
  - при `import.meta.main` (есть в Node 24.18) скрипт читает `protocol/schema.json` и пишет `client/src/protocol.gen.ts`, оба пути — от `import.meta.url`. Текст пишется с LF.
- Первые строки вывода:
  ```ts
  // Сгенерировано из protocol/schema.json командой npm run gen:protocol. Не править руками.
  // Источник истины — Pydantic-модели в server/app/protocol.py.
  import { z } from 'zod';
  ```
- Шаблон `pattern` выводится как `new RegExp(<JSON.stringify(pattern)>, 'u')`. `minLength` и `maxLength` проверяют число символов Unicode (`[...value].length`), а не `.length`.

- [ ] **Step 1: Настроить tsconfig и vitest для `client/scripts`**

`client/scripts/tsconfig.json` расширяет `../../tsconfig.base.json`, `include: ["."]`, `types: ["node"]`, `erasableSyntaxOnly: true`. Типы Node остаются только здесь, `client/tsconfig.json` их не получает. В `client/package.json`: `"typecheck": "tsc --noEmit && tsc --noEmit -p scripts"`. В `client/vitest.config.ts`: `include: ['src/**/*.test.ts', 'scripts/**/*.test.ts']`.

- [ ] **Step 2: Написать падающие тесты `client/scripts/gen-protocol.test.ts`**

```ts
it('rejects an unsupported keyword and names its path', ...)
  // generateZod({ $defs: { A: { type: 'integer', exclusiveMinimum: 0 } } })
  // бросает UnsupportedSchemaError, error.path === '#/$defs/A/exclusiveMinimum'
it('rejects a reference cycle', ...)          // A → B → A: UnsupportedSchemaError
it('rejects anyOf other than nullable', ...)  // anyOf: [{type:'string'},{type:'integer'}]
it('orders definitions by dependency, then by name', ...)
  // $defs { C: {$ref B}, B: {$ref A}, A: string, D: string }:
  // индексы 'export const A' < 'export const B' < 'export const C', 'export const D' после 'export const C'
it('is deterministic', ...)  // generateZod(s) === generateZod(structuredClone(s)) на protocol/schema.json
it('emits strict objects only for additionalProperties: false', ...)
  // в выводе для схемы с false — 'z.strictObject(', без additionalProperties — 'z.object('
it('starts with the generated-file header', ...)  // вывод начинается с двух строк комментария из Interfaces
```

Поведение схем на настоящих сообщениях проверяет задача 4.

- [ ] **Step 3: Запустить тесты и убедиться, что они падают**

Run: `npx vitest run --root client scripts`
Expected: FAIL — не найден модуль `./gen-protocol.ts`.

- [ ] **Step 4: Реализовать `client/scripts/gen-protocol.ts`**

- [ ] **Step 5: Запустить проверки**

Run: из корня — `npm run lint`, `npm run typecheck`, `npm test`.
Expected: всё проходит, тесты генератора есть в выводе vitest.

- [ ] **Step 6: Commit**

```bash
git add client/scripts client/package.json client/vitest.config.ts
git commit -m "feat(client): генератор zod-схем из JSON Schema протокола"
```

---

### Task 4: Команда `gen:protocol`, сгенерированные схемы клиента и их тесты

**Агент:** `electron-engineer`

**Проверка:** из корня: `npm run gen:protocol`, `npm run lint`, `npm run typecheck`, `npm test`; из `server/`: `uv run pytest`.

**Files:**
- Create: `client/src/protocol.test.ts`, `client/src/protocol.gen.ts` (генерацией)
- Modify: `package.json` (скрипт `gen:protocol`), `client/package.json` (`zod`), `package-lock.json`, `eslint.config.js` (игнор `client/src/protocol.gen.ts`), `protocol/schema.json` (генерацией: Prettier переформатирует)

**Interfaces:**
- Consumes: `python -m app.protocol_schema <путь>` (задача 2), `client/scripts/gen-protocol.ts` (задача 3), `protocol/examples.json` (задача 1).
- Produces: `client/src/protocol.gen.ts` экспортирует константу и тип с одним именем на каждое определение из `$defs`. Среди них `ClientMessage` и `ServerMessage` — `z.discriminatedUnion('type', …)`. На эти имена опирается клиент сигналинга в 4.2.

- [ ] **Step 1: Написать падающие тесты `client/src/protocol.test.ts`**

В `client/src` нет типов Node: примеры читаются импортом JSON (`with { type: 'json' }` и `resolveJsonModule` в `client/tsconfig.json`) или другим способом без `node:fs`.

```ts
it.each(examples.client.valid)('accepts valid client message %#', (m) => ...)    // ClientMessage.safeParse(m).success === true
it.each(examples.client.invalid)('rejects client message: $why', (c) => ...)      // safeParse(c.message).success === false
it.each(examples.server.valid)('accepts valid server message %#', ...)           // ServerMessage
it.each(examples.server.invalid)('rejects server message: $why', ...)
it('counts name length in Unicode characters', ...)
  // ServerPeerJoined: name '😀'.repeat(32) — success; '😀'.repeat(33) — нет; 'a'.repeat(32) — success
it('drops unknown fields in server messages', ...)
  // ServerMessage.parse({ type: 'pong', extra: 1 }) — успех, в результате нет ключа 'extra'
it('rejects unknown fields in client messages', ...)  // ClientMessage.safeParse({ type: 'ping', extra: 1 }).success === false
it('narrows ServerMessage by type exhaustively', ...)
  // функция со switch по message.type на все 8 значений и `message satisfies never` в default
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `npx vitest run --root client src/protocol.test.ts`
Expected: FAIL — не найден модуль `./protocol.gen`.

- [ ] **Step 3: Подключить команду и зависимость**

- В корневой `package.json`: `"gen:protocol": "uv run --directory server python -m app.protocol_schema ../protocol/schema.json && node client/scripts/gen-protocol.ts && prettier --write protocol/schema.json client/src/protocol.gen.ts"`.
- `npm install zod@^4.6.7 -w client`.
- В `eslint.config.js` добавить `client/src/protocol.gen.ts` в `ignores`.

- [ ] **Step 4: Сгенерировать и запустить тесты**

Run: из корня — `npm run gen:protocol`, затем `npm test`.
Expected: `client/src/protocol.gen.ts` создан, все тесты vitest проходят.

- [ ] **Step 5: Запустить все проверки**

Run: из корня — `npm run lint`, `npm run typecheck`, `npm test`; из `server/` — `uv run pytest -q`.
Expected: всё проходит. `test_committed_schema_is_up_to_date` проходит и после переформатирования Prettier: тест сравнивает разобранный JSON.

- [ ] **Step 6: Commit и проверка повторной генерации**

```bash
git add package.json package-lock.json client/package.json eslint.config.js client/src/protocol.test.ts client/src/protocol.gen.ts protocol/schema.json
git commit -m "feat(client): команда gen:protocol и zod-схемы протокола"
```

Run: `npm run gen:protocol`, затем `git status --porcelain`, затем `git grep -c $'\r' -- protocol/schema.json client/src/protocol.gen.ts`.
Expected: `git status` пуст, `git grep` ничего не находит (код выхода 1).

---

### Task 5: CI на GitHub Actions

**Агент:** `infra-engineer`

**Проверка:** `actionlint` из Docker; после push — все задания зелёные.

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: команды из Global Constraints и задач 1–4; `.nvmrc`, `server/.python-version`, `server/uv.lock`, `package-lock.json`.

- [ ] **Step 1: Написать `.github/workflows/ci.yml`**

По разделу 0.4 спецификации:
- `on: push` (любая ветка) и `workflow_dispatch`;
- `permissions: contents: read`, `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }`, `timeout-minutes: 15` у каждого задания;
- задания `server`, `client`, `protocol` на `ubuntu-latest`. В `protocol` после `npm run gen:protocol` идут `git diff --exit-code` и `test -z "$(git status --porcelain)"`.

Версии actions (`actions/checkout`, `actions/setup-node`, `astral-sh/setup-uv`) — последние релизы, закреплённые полным SHA с комментарием `# vX.Y.Z`. SHA берётся из `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>`. Для аннотированного тега нужен SHA коммита (строка `^{}`), а не тега. У checkout — `persist-credentials: false`. У setup-uv — закреплённая версия uv (локально 0.12.7) и `enable-cache: true`. У setup-node — `node-version-file: .nvmrc` и `cache: npm`.

- [ ] **Step 2: Проверить actionlint**

Run: `docker run --rm -v "$(pwd):/repo" --workdir /repo rhysd/actionlint:<последняя версия> -color`
Expected: пустой вывод, код 0. Если Docker Desktop не запущен, так и написать в отчёте.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: проверки сервера, клиента и актуальности протокола"
```

- [ ] **Step 4 (контроллер): Отправить ветку**

Run: `git push -u origin stage-0-protocol`

- [ ] **Step 5 (контроллер): Дождаться зелёного CI**

Run: `curl -s "https://api.github.com/repos/Seventimezzz/tete-a-tete/actions/runs?branch=stage-0-protocol&per_page=1"`, затем `.../actions/runs/<id>/jobs`.
Expected: у последнего прогона `status: completed` и `conclusion: success`; у заданий `server`, `client`, `protocol` — `success`. При падении — лог задания и исправление агентом зоны.
