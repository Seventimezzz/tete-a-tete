# Этап 0 (0.2–0.5). Протокол сигналинга, генерация типов клиента и CI

Дата: 2026-10-21. Пункты плана из CLAUDE.md:

> - 0.2 `server/app/protocol.py`: Pydantic-модели всех сообщений с camelCase в JSON, генератор id комнаты.
> - 0.3 Генерация протокола: `protocol/schema.json` из Pydantic, из него — TypeScript-типы и zod-схемы в `client/src/protocol.gen.ts`. Скрипт `gen:protocol`. Сгенерированные файлы пишутся с LF и не должны падать на `npm run lint`: исключить их из Prettier и ESLint или прогонять Prettier в генераторе. Скрипт генератора на TypeScript должен входить в `include` какого-нибудь tsconfig.
> - 0.4 CI: ruff, mypy и pytest для сервера; lint, typecheck и vitest для клиента; проверка, что сгенерированный протокол актуален (`gen:protocol`, затем `git diff --exit-code`).
> - 0.5 Заказать сертификат подписи кода: выпуск занимает время, а нужен он к этапу 11.

«Готово, когда» этапа 0: `uv run pytest` и `npm test` проходят, модели протокола покрыты тестами, CI зелёный.

0.1 выполнен и влит в `main` (269a057). 0.5 — действия человека, агенты его не реализуют, см. раздел «0.5».

> **Изменено после исполнения.** По итогам QA и финального ревью:
> - `data` в `signal` ограничен вложенностью 32 уровня, а `NaN`, `Infinity` и числа, которые переполняются в бесконечность, дают `bad-request`. Иначе сервер принял бы сообщение, которое не может переслать без искажений. Клиентская схема `data` по-прежнему не проверяет.
> - Сгенерированные zod-схемы отвергают строки с одиночными суррогатами, как и Pydantic. Генератор отвергает имена определений `z`, `RegExp`, `loneSurrogate` и зарезервированные слова ECMAScript.
> - `ValidationError` моделей и адаптеров не показывает входные данные (`hide_input_in_errors`). Поля с `data`, id комнаты и токеном не попадают в `repr`.
> - Общий набор QA лежит в `protocol/agreement_cases.json` рядом с `protocol/examples.json`.

## Зачем

Сервер (этап 1) и клиент (этап 4) говорят одним протоколом. Если описать его дважды, на Python и на TypeScript, описания разойдутся, и ошибка проявится только в живом звонке. Поэтому у протокола один источник — Pydantic-модели сервера, а клиент получает типы и проверку сообщений генерацией. CI ловит рассинхрон: модель поменяли, а генерацию не запустили.

## Цели

1. `server/app/protocol.py`: модели всех 15 сообщений из CLAUDE.md, общие типы, разбор входящего сообщения с единственной ошибкой `bad-request`, сериализация исходящего, генераторы id комнаты и участника.
2. `protocol/schema.json` и `client/src/protocol.gen.ts` создаются одной командой `npm run gen:protocol`. Повторный запуск без изменений модели не меняет ни байта.
3. Сервер и клиент принимают и отклоняют одни и те же сообщения. Это проверяется общим набором примеров, по которому идут тесты pytest и vitest.
4. CI на GitHub Actions запускает все проверки сервера и клиента и проверяет актуальность сгенерированных файлов. CI зелёный на ветке этапа.
5. У человека есть чек-лист заказа сертификата подписи кода.

## Не цели

- Эндпоинт `/ws`, обработчики сообщений, комнаты, очереди, лимиты — этап 1. Модели сообщений здесь есть, обработчиков нет.
- Подпись и проверка `resumeToken` — 1.5. Здесь только формат строки токена в модели.
- Клиент сигналинга и разбор сообщений сервера в ядре звонка — 4.2. Здесь только сгенерированные схемы и тесты к ним.
- Типы содержимого `data` в `signal` (`description`, `candidate`, `relayTrack`) — дело клиента, 4.4 и 9.2. Сервер `data` не читает.
- Сборка установщика на `windows-latest` в CI — 11.1.
- Electron, Angular, esbuild — 4.1.

## Ограничения

Из CLAUDE.md, дословно:

- «**Источник истины — Pydantic-модели в `server/app/protocol.py`.** Из них генерируется `protocol/schema.json`, а из него — TypeScript-типы и zod-схемы клиента в `client/src/protocol.gen.ts`. Сгенерированные файлы руками не правят: меняется модель, затем запускается `gen:protocol`.»
- «Каждое входящее сообщение разбирается Pydantic-моделью — discriminated union по `type`. Ошибка — `bad-request`.»
- «В Python — snake_case, в JSON протокола — camelCase: модели с `alias_generator=to_camel` и `populate_by_name=True`.»
- «Не логировать и не хранить IP, SDP и ICE-кандидаты — даже в сообщениях об ошибках.»
- «`data` в `signal` — это `{description}`, `{candidate}` или `{relayTrack: {mid, from}}`. Сервер его не читает и пересылает только внутри комнаты отправителя.»
- «**Доступ по ссылке** со случайным id комнаты: не короче 10 символов, из криптостойкого генератора.»
- «`net` — итог проверки сети клиента: `{nat, upnp, cgnat, doubleNat, vpn}`.»
- «`resumeToken` = HMAC(`RESUME_SECRET`, roomId, peerId). В памяти он не хранится, поэтому переживает перезапуск сервера.»
- Константы: «Участников в комнате — 5», «Длина id комнаты — ≥ 10 символов», «Максимальный размер сообщения — 64 КБ (`--ws-max-size`)», «Интервалы `ms` / `restarts` в статистике — `<1s`, `1-3s`, `3-10s`, `>10s` / `0`, `1`, `2+`».
- «**Версии инструментов:** TypeScript закреплён на `~6.0.3` … Node ≥ 24.15 (`engines`, `.nvmrc`).»
- Скилл `protocol-change`: «Сообщения сервера разбираются zod-схемой из сгенерированного файла»; «Новое сообщение сервера старый клиент не знает: проверь, что он его пропустит, а не упадёт»; «`protocol/schema.json` и `client/src/protocol.gen.ts` коммитятся вместе с моделью, в одном коммите».
- Агент `infra-engineer`: «Задания: сервер (`uv sync`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`), клиент (`npm ci`, `npm run lint`, `npm run typecheck`, `npm test`), протокол (`npm run gen:protocol`, затем `git diff --exit-code`). … Версии actions закрепляй.»
- Хук проекта блокирует правку `protocol/schema.json` и `client/src/protocol.gen.ts` через Edit и Write. Эти файлы создаёт только `npm run gen:protocol`.

Из дизайн-документа «Tete-a-tete: как работает сервис», дословно:

- Таблица протокола, строка `joined`: «`roomId`, `selfId`, `resumed`, `resumeToken`, `peers: [{id, name, muted}]` — Ответ на `create` или `join`».
- Строка `relay`: «Пара `a`–`b` идёт через участника `via`. `via: null` — ретрансляция закончилась».
- Строка `error`: «`code`: `room-full`, `bad-request` — Отказ».
- Статистика: «`result` — итог: `direct`, `failed`»; «`route` — маршрут: `host`, `srflx`, `upnp`, `prflx` с каждой стороны — выбранная пара кандидатов в `getStats()`»; «`family` — протокол: `ipv4`, `ipv6`»; «`sameIp` — общий внешний IP: да, нет»; «`ms` — время до соединения … от отправки offer до `connected`»; «`restarts` — перезапуски … счётчик клиента»; «`net` — сеть каждой стороны: `nat`: `open`, `cone`, `symmetric`, `no-udp`; флаги `upnp`, `cgnat`, `doubleNat`, `vpn`».
- Обрывы: «`resumeToken` подписан, а не хранится. Токен содержит `roomId` и `peerId` и подписан HMAC секретом `RESUME_SECRET`.»
- Безопасность: «id комнаты не должен угадываться. Доступ идёт по ссылке, поэтому id случайный, из криптостойкого генератора и не короче 10 символов.»

## Решения человека (2026-10-21)

1. **Ветка.** 0.1 влит в `main` перемоткой и отправлен на GitHub. Этап 0 идёт в ветке `stage-0-protocol` от `main`.
2. **id комнаты — 12 символов из алфавита `abcdefghijkmnpqrstuvwxyz23456789`:** строчные латинские буквы и цифры без похожих `l`, `o`, `0`, `1`. Это 32 символа и 60 бит. Сервер принимает в `join` только id этого формата, иначе `bad-request`.
3. **`net` обязателен, но каждое его поле может быть `null`** — «не определено». Так клиент отправляет `net` и до этапов 6 и 8, и когда проверка сети не успела закончиться. В статистике `null` — отдельная группа, не `false`.
4. **Push разрешён:** `main` и ветка этапа отправляются на GitHub, статус CI читается через API GitHub. Слияние в `main` в конце — только с согласия человека.
5. **0.5 делится на два шага.** В 0.5 человек выбирает поставщика и проверяет, что тот его обслужит: страна, форма, имя в сертификате. Заказ сертификата за 3–4 недели до подписи становится строкой пункта 11.2. В обоих пунктах — ссылка на чек-лист.

## Как устроено

### 0.2. Модели: `server/app/protocol.py`

**Общие типы.** Именованные типы объявляются через `type X = Annotated[...]` (PEP 695). Тогда Pydantic выносит их в `$defs` схемы, и у клиента появляются те же имена. Проверено на Pydantic 2.14.1.

| Тип | JSON | Правило |
| --- | --- | --- |
| `RoomId` | строка | `^[a-km-np-z2-9]{12}$`. Алфавит и длина — константы модуля, шаблон собирается из них |
| `PeerId` | строка | `^[a-km-np-z2-9]{8}$`, тот же алфавит, 40 бит *(наше решение)*. Сравнивается как строка: роль polite и отправитель `pair-report` — по сравнению id, а для ASCII сравнение строк в Python и JavaScript совпадает |
| `Name` | строка | от 1 до 32 символов Unicode, без управляющих `\x00-\x1f` и `\x7f-\x9f` *(наше решение)*. Сервер имя не обрезает, пробелы по краям убирает клиент перед отправкой |
| `ResumeToken` | строка | `^[A-Za-z0-9_.-]+$`, не длиннее 128 *(наше решение)*. Устройство токена выбирает 1.5. В `join` нет поля `peerId`, поэтому токен должен содержать `peerId` в себе, как и сказано в дизайне |
| `NetInfo` | объект | `nat`: `open` \| `cone` \| `symmetric` \| `no-udp` \| `null`; `upnp`, `cgnat`, `doubleNat`, `vpn`: `boolean` \| `null`. Все пять ключей обязательны |
| `PeerInfo` | объект | `id: PeerId`, `name: Name`, `muted: boolean` |
| `CandidateType` | строка | `host` \| `srflx` \| `prflx` \| `upnp` |
| `Route` | объект | `local: CandidateType`, `remote: CandidateType`. `local` — сторона отправителя `pair-report` *(наше решение: «с каждой стороны» из дизайна)* |

**Клиент → сервер.** Классы с префиксом `Client`, объединение `ClientMessage`.

| `type` | Класс | Поля |
| --- | --- | --- |
| `create` | `ClientCreate` | `name: Name`, `net: NetInfo` |
| `join` | `ClientJoin` | `roomId: RoomId`, `name: Name`, `net: NetInfo`, `resumeToken?: ResumeToken \| null` |
| `signal` | `ClientSignal` | `to: PeerId`, `data`: JSON-объект с любым содержимым (`dict[str, JsonValue]`) |
| `mute` | `ClientMute` | `muted: boolean` |
| `pair-report` | `ClientPairReport` | `peer: PeerId`, `result: direct \| failed`, `route: Route \| null`, `family: ipv4 \| ipv6 \| null`, `sameIp: boolean \| null`, `ms: integer 0…3 600 000 \| null`, `restarts: integer 0…100` |
| `ping` | `ClientPing` | — |
| `leave` | `ClientLeave` | — |

`pair-report` *(наше решение, уточняется в 6.2)*: при `failed` маршрута, протокола и времени нет, поэтому `route`, `family`, `ms` допускают `null`. `sameIp` — `null`, если srflx-кандидатов нет. `ms` и `restarts` приходят числами, по интервалам их раскладывает сервер в 6.3. Верхние границы защищают от мусора, клиент значения выше границы обрезает. Согласованность полей с `result` модель не проверяет: правило выберет 6.3.

**Сервер → клиент.** Классы с префиксом `Server`, объединение `ServerMessage`.

| `type` | Класс | Поля |
| --- | --- | --- |
| `joined` | `ServerJoined` | `roomId: RoomId`, `selfId: PeerId`, `resumed: boolean`, `resumeToken: ResumeToken`, `peers: PeerInfo[]` не длиннее 4 (вместимость комнаты минус сам участник) |
| `peer-joined` | `ServerPeerJoined` | `id: PeerId`, `name: Name` |
| `peer-left` | `ServerPeerLeft` | `id: PeerId` |
| `signal` | `ServerSignal` | `from: PeerId`, `data`: как у `ClientSignal`. `from` — ключевое слово Python: поле `from_` с явным алиасом `from` |
| `mute` | `ServerMute` | `id: PeerId`, `muted: boolean` |
| `relay` | `ServerRelay` | `a: PeerId`, `b: PeerId`, `via: PeerId \| null` |
| `pong` | `ServerPong` | — |
| `error` | `ServerError` | `code: room-full \| bad-request` |

**Конфигурация моделей.**

- Все модели: `alias_generator=to_camel`, `populate_by_name=True`. Следствие `populate_by_name`: сервер принимает и `room_id`, и `roomId`. Схема и клиент знают только camelCase.
- Модели клиента и вложенные в них `NetInfo` и `Route`: `extra="forbid"` и `strict=True`. Лишнее поле, строка `"true"` вместо `true`, `1.0` вместо `1` дают `bad-request`.
- Модели сервера: лишние поля не запрещаются. Тогда в схеме у них нет `additionalProperties: false`, и клиент пропускает поля, которые добавит будущая версия сервера.
- Вместимость комнаты — константа `ROOM_CAPACITY = 5` в модуле. Из неё же считается предел `peers`. Этап 1 берёт её отсюда.

**Разбор и сериализация.**

- `parse_client_message(raw: str | bytes) -> ClientMessage` разбирает текст `TypeAdapter`-ом объединения. Битый JSON, не объект, неизвестный или пропущенный `type`, любая ошибка полей — исключение `BadRequestError`.
- `BadRequestError` не несёт входных данных: ни в тексте, ни в `args`, ни в цепочке исключений. Текст `ValidationError` содержит вход, а в нём SDP и кандидаты. Поэтому `BadRequestError` поднимается после блока `except`, а не внутри него. `raise ... from None` внутри `except` не годится: он лишь скрывает цепочку при выводе, а `ValidationError` остаётся в `__context__`.
- `dump_server_message(message: ServerMessage) -> str` — JSON по алиасам. Поля со значением `null` не выбрасываются: `via: null` значит «ретрансляция закончилась».

**Генераторы id.** `new_room_id()` и `new_peer_id()` берут символы через `secrets.choice` из алфавита модуля. Уникальность участника внутри комнаты обеспечит этап 1.

**Примеры протокола: `protocol/examples.json`** *(наше решение)*. Файл пишется руками, его читают тесты обеих сторон. В нём корректные и некорректные сообщения для каждого направления, у некорректных — пояснение `why`:

```json
{
  "client": { "valid": [{ "type": "ping" }], "invalid": [{ "why": "лишнее поле", "message": { "type": "ping", "x": 1 } }] },
  "server": { "valid": [{ "type": "pong" }], "invalid": [{ "why": "via не id", "message": { "type": "relay", "a": "…", "b": "…", "via": 5 } }] }
}
```

В корректных — каждое сообщение хотя бы раз и все варианты `null`. В некорректных — границы правил: id комнаты из 11 и 13 символов, с `l`, `o`, `0`, `1` и заглавными буквами; имя из 0 и 33 символов и с управляющим символом; `ms` = −1 и 3 600 001; `restarts` = 101; неизвестное значение `nat`; `net` без ключа; `data` — массив или строка; `resumeToken` из 129 символов; неизвестный `type`. Битый JSON в файл не помещается, его проверяют только тесты pytest.

**Тесты сервера: `server/tests/test_protocol.py`.**

- Каждое корректное сообщение из примеров разбирается в свой класс. Каждое некорректное даёт `BadRequestError`. Битый JSON, JSON-массив, число и пустая строка тоже дают `BadRequestError`.
- Имя из 32 символов, в том числе из 32 эмодзи и кириллицей, принимается.
- `BadRequestError` после ошибки в `signal` с SDP внутри `data`: в `str`, `repr`, `args`, `__cause__` и `__context__` нет текста входа.
- Сообщения сервера сериализуются по алиасам: `selfId`, `resumeToken`, `from`, `sameIp`; `via: null` остаётся в JSON. Корректные серверные примеры проходят круг «разбор → сериализация» без изменений.
- Генераторы: 10 000 id комнаты и участника проходят свои шаблоны, нужной длины и без повторов.

### 0.3. Генерация

**Команда.** `npm run gen:protocol` из корня выполняет три шага:

1. `uv run --directory server python -m app.protocol_schema ../protocol/schema.json` — модуль собирает схему и пишет её в файл: UTF-8, LF, перевод строки в конце. Записывать нужно с `newline="\n"`: на Windows текстовый режим и перенаправление вывода дают CRLF.
2. `node client/scripts/gen-protocol.ts` — генератор читает `protocol/schema.json` и пишет `client/src/protocol.gen.ts`. Node 24 запускает `.ts` сам, без сборки (проверено на 24.18.1). Поэтому в скрипте только стираемый синтаксис TypeScript, без `enum` и `namespace`.
3. `prettier --write protocol/schema.json client/src/protocol.gen.ts`. После этого `prettier --check .` в `npm run lint` проходит, и вывод одинаков на Windows и Linux.

**`protocol/schema.json`.** JSON Schema 2020-12 в режиме validation. В `$defs` лежит каждая модель и каждый именованный тип под своим именем. На верхнем уровне — два discriminated union, `ClientMessage` и `ServerMessage`, в том виде, в каком их выдаёт Pydantic: `oneOf` и `discriminator.mapping`. Схема строится функцией `build_schema() -> dict` в `server/app/protocol_schema.py`, запись в файл — в `__main__` этого модуля.

Тест pytest сравнивает разобранный `protocol/schema.json` с `build_schema()`. Устаревшая схема видна локально, без CI.

**Генератор `client/scripts/gen-protocol.ts`.** Свой, без библиотек *(наше решение)*. Протокол использует малую часть JSON Schema, а готовые конвертеры добавили бы зависимость и выдают zod v3 или не умеют `discriminatedUnion`. Генератор поддерживает только нужное подмножество и падает с путём к узлу на любом другом ключевом слове. Тогда новая конструкция в модели не превратится молча в `z.unknown()`.

| JSON Schema | zod 4 |
| --- | --- |
| `$defs/X` | `export const X = …; export type X = z.infer<typeof X>;` — одно имя для схемы и типа |
| `$ref` | ссылка на константу. Константы выводятся в порядке зависимостей, при равенстве — по имени. Цикл — ошибка |
| `object` с `additionalProperties: false` | `z.strictObject({...})` |
| `object` без `additionalProperties` | `z.object({...})` — лишние ключи отбрасываются |
| `object` с `additionalProperties`-схемой и без `properties` | `z.record(z.string(), …)` |
| поле вне `required` | `.optional()` |
| `anyOf: [X, {type: null}]` | `X.nullable()` |
| `const` | `z.literal(...)` |
| `enum` из строк | `z.enum([...])` |
| `string` + `pattern` | `.regex(new RegExp(pattern, 'u'))` |
| `string` + `minLength` / `maxLength` | проверка числа символов Unicode, а не `.length`: JSON Schema считает code points, Pydantic тоже, а `.length` в JavaScript считает UTF-16. Иначе имя из 20 эмодзи сервер пропустит, а клиент отбросит `peer-joined` с ним |
| `integer` + `minimum` / `maximum` | `z.number().int().min(…).max(…)` |
| `boolean` | `z.boolean()` |
| `array` + `items`, `minItems` / `maxItems` | `z.array(…).min(…).max(…)` |
| `{}` | `z.unknown()` |
| `oneOf` + `discriminator` | `z.discriminatedUnion('type', [...])` |
| `title`, `default`, `description` | пропускаются |

Файл начинается с комментария «сгенерировано, не править, источник — `server/app/protocol.py`, команда — `npm run gen:protocol`». Повторный запуск даёт тот же файл байт в байт.

**Инструменты клиента.**

- `zod` `^4.6.7` — зависимость `client`, она нужна странице во время работы.
- Генератор входит в `include` tsconfig, в котором есть типы Node. Типы Node не должны попасть в будущий код страницы.
- ESLint не проверяет `client/src/protocol.gen.ts`: файл сгенерирован, его проверяют `tsc` и тесты. Сам генератор ESLint проверяет как обычный код.
- vitest находит тесты и в `client/src`, и в `client/scripts`.

**Тесты клиента.**

- `client/src/protocol.test.ts`: каждое корректное сообщение из `protocol/examples.json` проходит `ClientMessage` или `ServerMessage`, каждое некорректное отклоняется. Отдельно: имя из 32 эмодзи проходит, из 33 — нет; лишнее поле в сообщении сервера пропускается, в сообщении клиента — ошибка.
- `client/scripts/gen-protocol.test.ts`: генератор падает на неподдержанном ключевом слове и называет путь; два запуска на одной схеме дают одинаковый текст; порядок констант учитывает зависимости.

### 0.4. CI: `.github/workflows/ci.yml`

- Запуск — `push` в любую ветку и `workflow_dispatch` *(наше решение: открыть PR без `gh` можно только в браузере, а проверка нужна на ветке этапа)*. `permissions: contents: read`, `concurrency` по ветке с отменой устаревшего прогона, `timeout-minutes` у каждого задания.
- Все actions закреплены по полному SHA коммита с комментарием версии. `actions/checkout` — с `persist-credentials: false`.
- Задание `server` на `ubuntu-latest`: `astral-sh/setup-uv` с закреплённой версией uv; из `server/` — `uv sync --locked`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.
- Задание `client` на `ubuntu-latest`: `actions/setup-node` с `node-version-file: .nvmrc` и кэшем npm; `npm ci`, `npm run lint`, `npm run typecheck`, `npm test`.
- Задание `protocol` на `ubuntu-latest`: uv и Node, `npm ci`, `npm run gen:protocol`, затем `git diff --exit-code` и пустой `git status --porcelain`, чтобы поймать и новые неотслеживаемые файлы. Файлы, сгенерированные на Windows, сверяются с генерацией на Linux, и разница в переводах строк тоже станет видна.
- Локально workflow проверяется `actionlint` из образа Docker `rhysd/actionlint` с закреплённой версией. Docker на машине есть.

### 0.5. Сертификат подписи кода — действия человека

Агенты не реализуют этот пункт. Чек-лист с вариантами, ценами, сроками и источниками — [2026-10-21-stage-0.5-code-signing-checklist.md](2026-10-21-stage-0.5-code-signing-checklist.md).

Главное из него:
- EV не нужен: с 2024 года он не даёт мгновенной репутации SmartScreen.
- Нужен OV или IV с ключом в облаке, чтобы подписывать сборки в CI.
- С 2026-03-01 сертификат действует не дольше 460 дней, и срок идёт с выпуска. Поэтому сейчас выбирается поставщик и проверяется, что он обслужит разработчика, а сам заказ делается за 3–4 недели до 11.2.

Пункт отмечает `[x]` человек.

## Зоны и агенты

| Работа | Файлы | Агент |
| --- | --- | --- |
| Модели, разбор, генераторы id, примеры, тесты | `server/app/protocol.py`, `server/tests/test_protocol.py`, `protocol/examples.json` | `server-engineer` |
| Выгрузка схемы и тест её актуальности | `server/app/protocol_schema.py`, `server/tests/` | `server-engineer` |
| Генератор, `gen:protocol`, zod, конфиги, тесты клиента, первый запуск генерации | `client/scripts/`, `client/src/protocol.gen.ts` (генерацией), `protocol/schema.json` (генерацией), `client/`, `package.json`, `eslint.config.js` | `electron-engineer` |
| CI | `.github/workflows/ci.yml` | `infra-engineer` |
| CLAUDE.md и дизайн-документ | — | основная сессия |

## Что остаётся исполнителю

- Внутреннее устройство модуля протокола: имена констант, базовые классы моделей, вспомогательные функции.
- Убирать ли из схемы автоматические `title` (например `"title": "Resumetoken"`) своим `GenerateJsonSchema`. Читаемость выше, но не обязательно.
- Как именно генератор считает символы Unicode в zod: `.refine`, `.check` или вспомогательная функция в сгенерированном файле.
- Раскладка tsconfig для `client/scripts` и способ, которым тесты читают `protocol/examples.json`.
- Точные версии actions и uv на момент работы.

## Как проверить

1. Из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest` — всё проходит, тесты протокола есть.
2. Из корня: `npm run gen:protocol` дважды подряд, затем `git status --porcelain` — пусто после коммита первого запуска.
3. Из корня: `npm run lint`, `npm run typecheck`, `npm test` — всё проходит, тесты протокола есть.
4. Проба рассинхрона (не коммитится): новое поле в модели без запуска генерации — падает тест актуальности схемы в pytest. После `gen:protocol` без коммита `git diff --exit-code` падает.
5. Проба генератора (не коммитится): `exclusiveMinimum` в модели — `gen:protocol` падает и называет путь.
6. `actionlint` из Docker — без ошибок.
7. Ветка отправлена на GitHub, все три задания CI зелёные.

## После исполнения: CLAUDE.md и дизайн-документ

- CLAUDE.md, «Протокол сигналинга»: `peers: [{id, name, muted}]` в `joined`; поля `net` могут быть `null` — «не определено»; формат id комнаты и участника. «Константы»: id комнаты — 12 символов из `a-z2-9` без `l` и `o`; id участника — 8 символов; имя — 1–32 символа. «Команды»: убрать «`gen:protocol` — на 0.3».
- CLAUDE.md, «План разработки»: 0.5 — «Выбрать поставщика сертификата подписи кода и убедиться, что он обслужит разработчика» со ссылкой на чек-лист; в 11.2 — «Сертификат заказывается за 3–4 недели до подписи: срок действия не больше 460 дней идёт с выпуска».
- Дизайн-документ: пример id `k3x9q2` (6 символов) противоречит правилу «не короче 10» — заменить на 12-символьный в «Пути пользователя». В «Протоколе сигналинга» и «Безопасности» — формат id и `null` в `net`.
