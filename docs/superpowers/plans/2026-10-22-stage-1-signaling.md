# Этап 1. Сервер сигналинга — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WebSocket-сигналинг `/ws`: комнаты в памяти, пересылка `signal`, `mute`, ожидание после обрыва и возврат по `resumeToken`, лимиты на IP и на частоту, логи без личных данных, модульные и интеграционные тесты жизненного цикла комнаты.

**Architecture:** Логика комнат (`app/rooms.py`, класс `Hub`) синхронная и без сети: она получает разобранные сообщения и отдаёт готовый JSON соединениям через узкий протокол `Conn`. Лимиты (`app/limits.py`) — отдельный объект со счётчиками по ключу IP. Слой WebSocket (`app/ws.py`) держит на каждое соединение очередь исходящих с задачей-писателем и токен-бакеты, а уборка идёт фоновой задачей из lifespan (`app/main.py`). Время везде берётся из `Clock`.

**Tech Stack:** Python 3.13, FastAPI 0.156, Starlette 1.9, uvicorn 0.54 (реализация WebSocket — websockets-sansio), Pydantic 2.14, websockets 17.3 (клиент в тестах), pytest 9 + pytest-asyncio 1.4, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-22-stage-1-signaling-design.md`. Исполнитель читает её целиком: таблица `join`, «Лимиты», «Логи» и «Решения исполнителя» — часть требований каждой задачи.

## Global Constraints

Из CLAUDE.md, дословно:

- «**Бэкенд — Python и FastAPI**, один процесс uvicorn.»
- «Строго один воркер: комнаты живут в памяти процесса. `--no-access-log` обязателен: журнал запросов uvicorn содержит IP клиентов.»
- Запуск: «`uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1 --ws-ping-interval 15 --ws-ping-timeout 15 --ws-max-size 65536 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log`».
- «Не логировать и не хранить IP, SDP и ICE-кандидаты — даже в сообщениях об ошибках. IP используется только в момент подключения: для лимитов и определения ASN. Берётся из `websocket.client.host` (uvicorn с `--proxy-headers`).»
- «Каждое входящее сообщение разбирается Pydantic-моделью — discriminated union по `type`. Ошибка — `bad-request`.»
- «В Python — snake_case, в JSON протокола — camelCase: модели с `alias_generator=to_camel` и `populate_by_name=True`.»
- «Всё асинхронное, никаких блокирующих вызовов в обработчиках. STUN и фоновые задачи (уборка, запись статистики) запускаются и останавливаются в lifespan приложения.»
- «Время берётся из подменяемых часов, чтобы таймауты тестировались без ожидания.»
- «`data` в `signal` … Сервер его не читает и пересылает только внутри комнаты отправителя.»
- «`join` на несуществующий id создаёт комнату. Пустая комната удаляется сразу, ссылка продолжает работать.»
- «`resumeToken` = HMAC(`RESUME_SECRET`, roomId, peerId). В памяти он не хранится, поэтому переживает перезапуск сервера. В `join` нет поля `peerId`, поэтому токен несёт `peerId` в себе.»
- «**Хуки.** Ручная правка `protocol/schema.json` и `client/src/protocol.gen.ts` блокируется. Флаги `--no-verify` и `--no-gpg-sign` блокируются.»

Константы CLAUDE.md: «Участников в комнате — 5»; «id участника — 8 символов из того же алфавита»; «WebSocket-ping сервера — раз в 15 с, ожидание ответа 15 с (флаги uvicorn)»; «Максимальный размер сообщения — 64 КБ (`--ws-max-size`)»; «Ожидание после обрыва — 15 с, затем `peer-left`»; «Очередь `signal` на время ожидания — ≤ 50 сообщений»; «Уборка комнат — раз в 1 с».

Из спецификации:

| Что | Значение |
| --- | --- |
| Мест на ключ IP (соединения + ждущие места) | 20 |
| Рукопожатий на ключ | 30 за 60 с, считаются все попытки |
| Входов (`create` + `join`) на ключ | 30 за 60 с |
| Созданий комнат на ключ | 10 за 60 с, отклонённые не считаются; возврат по действующему токену не считается |
| Бакет сообщений соединения | запас 200, 20 в секунду |
| Бакет байтов соединения | запас 256 КБ, 32 КБ/с |
| Бакет байтов ключа | запас 512 КБ, 64 КБ/с |
| Глобально | 1000 соединений, 500 комнат, 128 МБ во всех очередях |
| Очередь исходящих соединения | 256 сообщений и 1 МБ |
| Очередь ждущего | 50 сообщений, 256 КБ, от одного отправителя 25 |
| Ключ IP | IPv4 целиком; IPv6 — /64; `::ffff:a.b.c.d` — как IPv4; нет адреса или мусор — `"unknown"` |
| `RESUME_SECRET` | обязателен, не короче 32 символов |
| Токен | `<peerId>.<base64url без "=" от HMAC-SHA256(secret, "v1\n" + roomId + "\n" + peerId)>`, 52 символа |
| Коды закрытия | `4000` — место занял новый вход; `4001` — переполнена очередь исходящих; `1008` — лимит частоты; `1012` (uvicorn) — остановка; `1009` (uvicorn) — больше 64 КБ |

- Логи: ни IP, ни SDP, ни кандидатов, ни `data`, ни id комнат, ни имён, ни токенов. Исключения пишутся только как `type(exc).__name__`. Ни одной записи на каждое сообщение.
- Новых зависимостей, кроме `websockets` в dev-группе (задача 8), не добавлять.
- Сообщения коммитов — как в истории: `feat(server): …`, `test(server): …`, по-русски. Последняя строка — `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Стиль кода — как в соседних файлах `server/app/`: `Final` для констант, `type X = …`, короткие docstring.

## Review Focus

1. **Клиент рвёт TCP посреди отправки.** Ожидается: задача-писатель и цикл чтения завершаются без traceback в логе, слот IP освобождается ровно один раз, место уходит в ожидание. Задача 8, `test_abrupt_disconnects_leave_no_traceback`.
2. **Кириллица и эмодзи в именах и `data` по настоящему WebSocket.** Ожидается: `peer-joined` и `signal` доходят байт в байт. Задача 8, сценарий A/B/C с именами `"Аня"`, `"Борис 🎧"`.
3. **Остановка сервера при подключённых клиентах.** Ожидается: клиенты получают закрытие `1012`, уборка останавливается без ошибок в логе. Задача 8, `test_graceful_shutdown_closes_with_1012` (только POSIX: на Windows `terminate()` убивает процесс без сигнала).
4. **Токен от комнаты, которую удалили, потому что все ушли.** Ожидается (решение исполнителя 10): комната создаётся заново, `resumed: true`, лимит создания не тратится. Задача 5, `test_token_for_deleted_room_recreates_it`.
5. **Ждущее место и уборка на границе.** Возврат на 15,0 с до прохода уборки. Ожидается `resumed: false`, а не воскрешение места, и ровно один `peer-left`. Задача 5, `test_resume_at_exactly_15s_before_sweep_is_new_peer`.

---

### Task 1: Код ошибки `rate-limited` и коды закрытия

**Агент:** `server-engineer`. Параллельно ревью — `invariants-reviewer` (протокол).

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`; из корня: `npm run gen:protocol`, `npm test`, `npm run lint`, затем `git status` — изменились только перечисленные файлы.

Скилл `protocol-change`, шаги 1–3 и 5. Шаг 6 (документация) делает задача 9.

**Files:**
- Modify: `server/app/protocol.py`, `protocol/examples.json`, `server/tests/test_protocol.py`
- Generated: `protocol/schema.json`, `client/src/protocol.gen.ts` (только через `npm run gen:protocol`)

**Interfaces:**
- Produces в `app.protocol`: `ErrorCode = Literal["room-full", "bad-request", "rate-limited"]`; константы `CLOSE_REPLACED: Final = 4000`, `CLOSE_SLOW_READER: Final = 4001`, `CLOSE_RATE_LIMIT: Final = 1008`. Коды закрытия в схему не попадают.

- [ ] **Step 1:** Добавить в `protocol/examples.json` корректный серверный пример `{"type": "error", "code": "rate-limited"}`. Тесты, которые читают файл, должны упасть на сервере (pytest) — модель ещё не знает кода.
- [ ] **Step 2:** Запустить `uv run pytest tests/test_protocol.py -q`. Ожидается: FAIL у `test_valid_server_example_round_trips[error]`.
- [ ] **Step 3:** Расширить `ErrorCode`, добавить три константы закрытия.
- [ ] **Step 4:** `uv run pytest -q` — PASS. Из корня `npm run gen:protocol`, `npm test` — PASS, в `client/src/protocol.gen.ts` появился `"rate-limited"`.
- [ ] **Step 5:** Коммит одним набором: модель, примеры, схема, сгенерированный клиент.

```bash
git add server/app/protocol.py protocol/examples.json protocol/schema.json client/src/protocol.gen.ts
git commit -m "feat(server): код ошибки rate-limited и коды закрытия WebSocket"
```

---

### Task 2: Часы, настройки и `resumeToken`

**Агент:** `server-engineer`.

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.

**Files:**
- Create: `server/app/clock.py`, `server/app/config.py`, `server/app/tokens.py`, `server/tests/fakes.py`, `server/tests/test_clock_config.py`, `server/tests/test_tokens.py`
- Modify: `server/pyproject.toml` (`per-file-ignores`: `"app/clock.py" = ["TID251"]`)

**Interfaces:**
- Produces:
  - `app.clock`: `class Clock(Protocol): def now(self) -> float` (монотонные секунды); `class MonotonicClock` — на `time.monotonic()`.
  - `tests.fakes`: `class FakeClock` с `now()` и `advance(seconds: float) -> None`, старт с `1000.0`.
  - `app.config`: `RESUME_SECRET_MIN_LENGTH: Final = 32`; `@dataclass(frozen=True) class Settings: resume_secret: bytes = field(repr=False)`; `class ConfigError(Exception)`; `load_settings(env: Mapping[str, str]) -> Settings`.
  - `app.tokens`: `issue_token(secret: bytes, room_id: str, peer_id: str) -> str`; `verify_token(secret: bytes, room_id: str, token: str) -> str | None` — возвращает `peer_id` или `None`.

- [ ] **Step 1: Падающие тесты**

```python
def test_fake_clock_advances(): ...          # FakeClock().now() == 1000.0; advance(2.5) → 1002.5
def test_monotonic_clock_does_not_go_back(): ...

def test_missing_secret_is_config_error(): ...        # load_settings({}) → ConfigError
def test_short_secret_error_does_not_contain_it(): ...
    # secret = "Zq7-" + "s" * 27 (31 символ); str(exc), repr(exc) и traceback.format_exception(exc) без "Zq7-"
def test_secret_of_32_chars_is_accepted(): ...         # Settings.resume_secret == secret.encode()
def test_settings_repr_hides_secret(): ...

TOKEN_RE = r"^[a-km-np-z2-9]{8}\.[A-Za-z0-9_-]{43}$"
def test_issued_token_format_and_length(): ...         # re.fullmatch(TOKEN_RE, t), len(t) == 52
def test_token_is_deterministic(): ...
def test_verify_returns_peer_id(): ...
@pytest.mark.parametrize("case", ["other room", "other secret", "flipped signature char",
    "no dot", "peer id outside alphabet", "128 chars garbage", "empty", "signature with padding"])
def test_bad_token_is_none(case): ...
```

- [ ] **Step 2:** `uv run pytest tests/test_clock_config.py tests/test_tokens.py -q` — FAIL (модулей нет).
- [ ] **Step 3:** Реализовать. Подпись — `hmac.new(secret, f"v1\n{room_id}\n{peer_id}".encode(), hashlib.sha256)`, base64url без `=`; сравнение — `hmac.compare_digest`. `peerId` проверяется по алфавиту и длине из `app.protocol` до HMAC. Сообщение `ConfigError` — `"RESUME_SECRET is missing"` или `"RESUME_SECRET must be at least 32 characters"`.
- [ ] **Step 4:** `uv run pytest -q`, `uv run ruff check`, `uv run mypy` — PASS.
- [ ] **Step 5:** Коммит `feat(server): подменяемые часы, настройки и resumeToken`.

---

### Task 3: Лимиты

**Агент:** `server-engineer`. Параллельно ревью — `invariants-reviewer` (IP).

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.

**Files:**
- Create: `server/app/limits.py`, `server/tests/test_limits.py`

**Interfaces:**
- Consumes: `Clock`, `FakeClock` (задача 2).
- Produces в `app.limits`:
  - `ip_key(host: str | None) -> str`.
  - `@dataclass(frozen=True) class LimitConfig` с полями и значениями из таблицы Global Constraints: `seats_per_key=20`, `handshakes_per_window=30`, `joins_per_window=30`, `creates_per_window=10`, `window=60.0`, `msg_burst=200`, `msg_rate=20.0`, `bytes_burst=262_144`, `bytes_rate=32_768.0`, `key_bytes_burst=524_288`, `key_bytes_rate=65_536.0`, `max_connections=1000`, `max_rooms=500`, `max_queued_bytes=134_217_728`.
  - `class TokenBucket(capacity: float, rate: float, now: float)` с `take(amount: float, now: float) -> bool`.
  - `class Limits(config: LimitConfig, clock: Clock)`:
    - `try_open(key: str) -> bool` — рукопожатие: записывает попытку в окно рукопожатий всегда; при успехе +1 соединение глобально и +1 слот ключа;
    - `connection_closed() -> None` — −1 соединение глобально;
    - `hold_seat(key: str) -> None`, `release_seat(key: str) -> None` — слоты ключа; ключ удаляется на нуле;
    - `try_join(key: str) -> bool`, `try_create(key: str) -> bool` — окна входов и созданий;
    - `take_key_bytes(key: str, n: int) -> bool`;
    - `reserve(n: int) -> bool`, `free(n: int) -> None` — глобальный бюджет очередей;
    - `sweep() -> None` — чистит окна и полные бакеты ключей без слотов;
    - свойства `connections: int`, `queued_bytes: int`, `seats(key: str) -> int` для тестов и сводной строки.
  - IP не попадает ни в `repr`, ни в исключения: словари по ключу — приватные поля с `repr=False`.

- [ ] **Step 1: Падающие тесты**

```python
@pytest.mark.parametrize(("host", "key"), [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:bbbb::9", "2001:db8:1:2::/64"),
    ("::ffff:203.0.113.7", "203.0.113.7"),
    (None, "unknown"), ("testclient", "unknown"), ("", "unknown")])
def test_ip_key(host, key): ...

def test_twenty_seats_per_key_then_refused(): ...      # 20 × try_open → True, 21-й → False; другой ключ → True
def test_seat_held_by_waiting_place_counts(): ...      # hold_seat снижает остаток так же, как соединение
def test_handshakes_30_per_60s_counts_refused_too(): ...  # окно сдвигается: на +60,0 с снова можно
def test_global_1000_connections(): ...                # LimitConfig(max_connections=3) для скорости
def test_creates_10_per_60s_refused_not_counted(): ...
def test_joins_30_per_60s(): ...
def test_token_bucket_burst_then_refill(): ...         # (200, 20/с): 200 × take(1) → True, 201-й → False; +0,05 с → True
def test_key_bytes_bucket(): ...                       # 512 КБ сразу, потом 64 КБ/с
def test_reserve_and_free_global_budget(): ...
def test_sweep_drops_empty_keys_and_state_repr_has_no_ip(): ...
```

- [ ] **Step 2:** `uv run pytest tests/test_limits.py -q` — FAIL.
- [ ] **Step 3:** Реализовать. Окна — `collections.deque` отметок времени на ключ. `ip_key` — через `ipaddress.ip_address`; IPv6 — `ipaddress.ip_network(f"{addr}/64", strict=False)` в строковой форме; `ipv4_mapped` разворачивается в IPv4.
- [ ] **Step 4:** Проверки — PASS.
- [ ] **Step 5:** Коммит `feat(server): лимиты на IP, токен-бакеты и глобальные пределы`.

---

### Task 4: Комнаты — вход, выход, пересылка

**Агент:** `server-engineer`.

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.

**Files:**
- Create: `server/app/rooms.py`, `server/tests/test_rooms.py`
- Modify: `server/tests/fakes.py` (добавить `FakeConn`)

**Interfaces:**
- Consumes: `app.protocol` (модели, `parse_client_message`, `dump_server_message`, `new_room_id`, `new_peer_id`, `ROOM_CAPACITY`), `Clock`, `Limits`, `issue_token`, `verify_token`.
- Produces в `app.rooms`:
  - `class Conn(Protocol)`: свойство `key: str`; `send(text: str) -> None` — ставит готовый JSON в очередь, не блокирует; `close(code: int) -> None`. Объекты сравниваются по идентичности.
  - константы `WAIT_SECONDS: Final = 15.0`, `WAIT_QUEUE_MAX: Final = 50`, `WAIT_QUEUE_BYTES: Final = 262_144`, `WAIT_QUEUE_PER_SENDER: Final = 25`.
  - `class Hub(*, secret: bytes, clock: Clock, limits: Limits)`:
    - `handle(conn: Conn, message: ClientMessage) -> None`;
    - `disconnected(conn: Conn) -> None` — задача 5; в этой задаче — заготовка, которая только отпускает слот: `limits.release_seat(conn.key)`;
    - `sweep() -> None` — задача 5;
    - `stats() -> HubStats`, где `@dataclass(frozen=True) class HubStats: rooms: int; peers: int; waiting: int`.
  - Датаклассы `Room` и `Peer` — по модели состояния из спецификации, без `pairs`, `relays` и `asn` (этапы 6 и 9). Поля `name`, `ws`/`conn`, `queue`, `net` — `repr=False`. У `Peer` — `seat_key: str | None = field(default=None, repr=False)` для задачи 5.
  - Сообщения сервера сериализуются один раз (`dump_server_message`) и отдаются строкой.
- `tests.fakes.FakeConn(key: str = "k")`: список `sent: list[dict]` (JSON разобран), `closed: int | None`, хелпер `of_type(t: str) -> list[dict]`.

- [ ] **Step 1: Падающие тесты** — `Hub` с `FakeClock`, настоящими `Limits` и секретом `b"t" * 32`, хелпер `join(hub, conn, room_id=..., name=..., token=None)` собирает `ClientJoin` через модель.

```python
def test_create_returns_joined_with_new_ids_and_token(): ...
    # joined: resumed False, peers [], roomId по формату, selfId 8 символов, verify_token(...) == selfId
def test_join_unknown_room_creates_it(): ...
def test_join_existing_room_notifies_others(): ...
    # A получает peer-joined {id, name}; joined у B: peers == [{"id": A, "name": "Аня", "muted": False}]
def test_sixth_join_gets_room_full(): ...              # error room-full; пятеро не получили ничего нового
def test_create_or_join_while_in_room_is_bad_request(): ...
@pytest.mark.parametrize("msg", ["signal", "mute", "pair-report"])
def test_room_messages_outside_room_are_bad_request(msg): ...
def test_leave_outside_room_is_noop(): ...
def test_ping_gets_pong_in_any_state(): ...
def test_signal_forwarded_with_from_and_exact_data(): ...   # data с кириллицей и вложенностью 32 — без изменений
@pytest.mark.parametrize("to", ["self", "unknown", "peer of other room"])
def test_signal_to_bad_target_is_dropped_silently(to): ...  # никто ничего не получил, error нет
def test_mute_broadcast_to_others_and_shown_in_later_joined(): ...
def test_pair_report_in_room_is_accepted_silently(): ...
def test_leave_notifies_others_and_frees_place(): ...      # peer-left у остальных; повторный вход — шестой участник проходит
def test_last_leave_deletes_room(): ...                    # stats().rooms == 0; join на тот же id создаёт заново
def test_create_limit_gives_rate_limited(): ...            # 11-й create с ключа за 60 с → error rate-limited
def test_join_limit_gives_rate_limited(): ...              # 31-й вход за 60 с
def test_global_room_limit(): ...                          # LimitConfig(max_rooms=2): третья комната → rate-limited
def test_state_repr_has_no_name_or_data(): ...
```

- [ ] **Step 2:** `uv run pytest tests/test_rooms.py -q` — FAIL.
- [ ] **Step 3:** Реализовать `Hub.handle` для всего, кроме строк таблицы `join` с действующим токеном (их делает задача 5: в этой задаче токен проверяется, и действующий пока ведёт себя как отсутствующий). Порядок проверок `join`: соединение уже в комнате → `bad-request`; `try_join` → `rate-limited`; затем таблица; создание комнаты — `try_create` и `max_rooms` → `rate-limited`; полная комната → `room-full`.
- [ ] **Step 4:** Проверки — PASS.
- [ ] **Step 5:** Коммит `feat(server): комнаты — create, join, leave, signal, mute`.

---

### Task 5: Обрывы, ожидание, возврат и уборка

**Агент:** `server-engineer`. Параллельно ревью — `invariants-reviewer` (токены, IP).

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.

**Files:**
- Modify: `server/app/rooms.py`, `server/tests/test_rooms.py` (или новый `server/tests/test_rooms_resume.py`)

**Interfaces:**
- Consumes: всё из задачи 4.
- Produces: полные `Hub.disconnected(conn)` и `Hub.sweep()`; поведение — таблица `join` и разделы «Обрыв соединения в комнате», «Уборка» спецификации.
  - Слоты: соединение держит слот, пока открыто. При обрыве в комнате слот переходит месту (`peer.seat_key = conn.key`, без `release_seat`). Слот места отпускается при уборке и при возврате новым соединением (у того свой слот). `leave` слотов не трогает: место на связи, слот держит соединение. Обрыв вне комнаты или соединения, которое уже отвязано от места (его заменили), — `release_seat(conn.key)`.
  - Очередь ждущего хранит готовые строки, байты резервируются в `Limits.reserve` и освобождаются при доставке или выбросе.

- [ ] **Step 1: Падающие тесты**

```python
def test_disconnect_keeps_place_silently(): ...             # остальным ничего; stats().waiting == 1
def test_signal_to_waiting_peer_is_queued_and_delivered_in_order(): ...
    # 3 signal во время ожидания; возврат: joined (resumed True), затем 3 signal по порядку, потом новые
def test_waiting_queue_caps(): ...
    # 51-й не встаёт; 256 КБ по байтам; от одного отправителя не больше 25, другой ещё может
def test_sweep_keeps_at_14_9s_removes_at_15s(): ...         # ровно один peer-left; id в left
def test_resume_at_14_99s_keeps_place(): ...
def test_resume_at_exactly_15s_before_sweep_is_new_peer(): ...   # resumed False, новый selfId, один peer-left
def test_resume_after_leave_is_new_peer(): ...
def test_resume_while_old_conn_open_closes_it_with_4000(): ...
    # old.closed == 4000; peer-left никто не получил; new получил joined resumed True
def test_replaced_conn_disconnect_does_not_start_waiting(): ...  # после hub.disconnected(old) место на связи
def test_two_joins_with_same_token_last_wins(): ...         # R3: одно место, первое из новых закрыто 4000
def test_signal_during_switch_arrives_after_queue(): ...     # R4
def test_resume_into_recreated_room_keeps_id_and_notifies(): ...
    # перезапуск: новый Hub с тем же секретом; A возвращается первым (resumed True, peers []),
    # B — с прежним id, A получает peer-joined B
def test_resume_into_full_recreated_room_is_room_full(): ...
def test_token_for_deleted_room_recreates_it(): ...         # resumed True, try_create не потрачен
def test_invalid_token_joins_as_new_peer(): ...
def test_resume_updates_net_keeps_name(): ...
def test_seats_follow_places(): ...
    # обрыв в комнате: seats(key) не меняется; уборка: −1; возврат новым соединением: место отпускает свой слот
def test_random_churn_returns_counters_to_zero(): ...        # R6: random.Random(1), 1000 шагов
    # create/join/leave/обрыв/возврат/уборка; в конце всё закрыть и прогнать уборку после +16 с:
    # stats() == HubStats(0, 0, 0), limits.seats(k) == 0 для всех ключей, limits.queued_bytes == 0
```

- [ ] **Step 2:** `uv run pytest -q` — новые тесты FAIL.
- [ ] **Step 3:** Реализовать. `join` сам считает место с `disconnected_at` ≥ 15 с освобождённым (как после уборки, с `peer-left`), не дожидаясь `sweep`.
- [ ] **Step 4:** Проверки — PASS.
- [ ] **Step 5:** Коммит `feat(server): обрывы, очередь ожидания, возврат по токену и уборка`.

---

### Task 6: Логи без личных данных

**Агент:** `server-engineer`. Параллельно ревью — `invariants-reviewer` (логи).

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.

**Files:**
- Create: `server/app/logs.py` (не `logging.py`: имя затенило бы стандартный модуль), `server/tests/test_logs.py`

**Interfaces:**
- Produces в `app.logs`:
  - `class PrivacyFilter(logging.Filter)` — отбрасывает записи с `record.msg`, начинающимся с `'%s - "WebSocket %s"'`, и записи ниже INFO от логгеров `uvicorn*` и `websockets*`.
  - `install_log_filter() -> None` — вешает фильтр на логгеры `uvicorn`, `uvicorn.error`, `websockets`; повторный вызов не добавляет второй.
  - `log_exception(logger: logging.Logger, where: str, exc: BaseException) -> None` — пишет `"%s failed: %s"` с `where` и `type(exc).__name__`, без `exc_info`.
  - `loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None` — то же для event loop: только имя типа из `context.get("exception")`, без `context["message"]` (там может быть repr задачи).
  - `logger: Final = logging.getLogger("tete")`.

- [ ] **Step 1: Падающие тесты**

```python
@pytest.mark.parametrize("args", [("203.0.113.7:5555", "/ws"), ("203.0.113.7:5555", "/ws?r=abcdefghijkm")])
@pytest.mark.parametrize("fmt", ['%s - "WebSocket %s" [accepted]', '%s - "WebSocket %s" 403', '%s - "WebSocket %s" %d'])
def test_handshake_records_dropped(fmt, args): ...    # через caplog на "uvicorn.error" после install_log_filter()
def test_debug_and_trace_from_uvicorn_and_websockets_dropped(): ...
def test_info_startup_message_kept(): ...              # "Started server process [%d]"
def test_install_is_idempotent(): ...
def test_log_exception_writes_type_name_only(): ...
    # ValueError("Аня abcdefghijkm tok.en 203.0.113.7"): в caplog.text только "ValueError", без этих строк и traceback
def test_loop_exception_handler_writes_type_name_only(): ...
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** Проверки — PASS.
- [ ] **Step 5:** Коммит `feat(server): фильтр логов uvicorn и запись исключений без данных`.

---

### Task 7: Эндпоинт `/ws`, соединение и lifespan

**Агент:** `server-engineer`. Параллельно ревью — `invariants-reviewer` (IP, логи).

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`.

**Files:**
- Create: `server/app/ws.py`, `server/tests/test_ws.py`
- Modify: `server/app/main.py`, `server/tests/test_main.py`

**Interfaces:**
- Consumes: `Hub`, `Conn`, `Limits`, `LimitConfig`, `ip_key`, `TokenBucket`, `Settings`, `load_settings`, `ConfigError`, `Clock`, `MonotonicClock`, `install_log_filter`, `log_exception`, `loop_exception_handler`, коды закрытия из `app.protocol`.
- Produces:
  - `app.ws`: `OUTBOX_MAX_MESSAGES: Final = 256`, `OUTBOX_MAX_BYTES: Final = 1_048_576`, `CLOSE_WAIT_SECONDS: Final = 1.0`; `class Connection` — реализует `Conn` поверх Starlette `WebSocket`: очередь исходящих с учётом байтов (и `Limits.reserve/free`), задача-писатель, бакеты сообщений и байтов; `router: APIRouter` с `@router.websocket("/ws")`.
  - `app.main`: `create_app(settings: Settings | None = None, *, clock: Clock | None = None, limit_config: LimitConfig | None = None, hub_factory: Callable[..., Hub] = Hub) -> FastAPI`; модульный `app = create_app()`. `install_log_filter()` вызывается при импорте `app.main`. Без `settings` lifespan вызывает `load_settings(os.environ)`, и `ConfigError` останавливает запуск. Lifespan: `loop.set_exception_handler(loop_exception_handler)`, задача уборки — раз в 1 с `hub.sweep()` и `limits.sweep()`, раз в 60 с строка `"stats rooms=%d peers=%d waiting=%d connections=%d closed_by_limits=%d"`. Состояние — в `app.state`.
- Поведение эндпоинта — раздел «Соединение» спецификации:
  1. `key = ip_key(websocket.client.host if websocket.client else None)`. Если адрес loopback или `"unknown"`, один раз за процесс WARNING `"X-Forwarded-For отсутствует, лимиты общие"`.
  2. `limits.try_open(key)` → иначе `await websocket.close()` до `accept` (uvicorn отвечает 403).
  3. Цикл: `receive()`; текст → бакеты (сообщение, байты соединения, байты ключа) → при отказе закрыть `1008`; разбор → `bad-request` или `hub.handle`. Бинарный кадр — тоже через бакеты, затем `bad-request`.
  4. `finally`: `hub.disconnected(conn)`, `limits.connection_closed()`, остановить писателя, освободить байты очереди. Любое исключение — `log_exception(...)`, наружу не выходит.
  5. `Connection.close(code)`: дальнейшие `send` игнорируются, очередь выбрасывается, писатель отправляет закрытие; если оно не завершилось за `CLOSE_WAIT_SECONDS`, обработчик всё равно возвращается — uvicorn закрывает транспорт.

- [ ] **Step 1: Падающие тесты** — `starlette.testclient.TestClient(create_app(Settings(b"t" * 32), clock=FakeClock(), limit_config=...))`.

```python
def test_ping_pong(): ...
def test_bad_json_and_binary_get_bad_request_and_connection_stays(): ...  # после ошибки ping → pong
def test_two_clients_create_join_signal(): ...
def test_handshake_refused_over_seat_limit(): ...        # LimitConfig(seats_per_key=1): второе подключение отклонено до accept
def test_message_rate_limit_closes_1008(): ...           # LimitConfig(msg_burst=5, msg_rate=0.0001): шестое сообщение → закрытие 1008
def test_outbox_overflow_closes_4001(): ...
    # модульно: Connection с фейковым WebSocket, чей send_text никогда не завершается;
    # 257-й send → close(4001); и по байтам: 17 сообщений по 64 КБ → close(4001)
def test_missing_secret_stops_startup(monkeypatch): ...  # без RESUME_SECRET: with TestClient(create_app()) → ConfigError
def test_forwarded_for_warning_once_without_address(caplog): ...
def test_sweeper_runs_in_lifespan(): ...                  # FakeClock + обрыв + ожидание > 1 с реального времени → peer-left
```

`test_main.py`: существующий тест на 404 для `/docs`, `/redoc`, `/openapi.json` остаётся зелёным.

- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** Проверки — PASS. Ручная проверка: `RESUME_SECRET=<32 символа> uv run uvicorn app.main:app --port 8080 --no-access-log` стартует, без переменной — останавливается с сообщением без значения.
- [ ] **Step 5:** Коммит `feat(server): эндпоинт /ws, очередь исходящих, лимит частоты и уборка в lifespan`.

---

### Task 8: Интеграционные тесты на настоящем uvicorn

**Агент:** `server-engineer`. Параллельно ревью — `invariants-reviewer` (логи, IP).

**Проверка:** из `server/`: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`; время `uv run pytest tests/integration -q` — меньше 60 с.

**Files:**
- Create: `server/tests/integration/__init__.py`, `server/tests/integration/harness.py`, `server/tests/integration/test_app.py` (фабрика для тестов), `server/tests/integration/test_lifecycle.py`, `server/tests/integration/test_limits_and_frames.py`, `server/tests/integration/test_logs.py`
- Modify: `server/pyproject.toml` — `websockets>=17.3` в dev-группе, маркер `integration`; `server/uv.lock`

**Interfaces:**
- Consumes: `create_app`, `Hub`, `LimitConfig`.
- Produces:
  - `harness.start_server(*, app: str = "app.main:app", factory: bool = False, env: Mapping[str, str] = {}) -> RunningServer` — процесс `sys.executable -m uvicorn <app>` с флагами из CLAUDE.md, кроме `--port` (свободный порт); ждёт, пока порт примет соединение (не дольше 10 с). `RunningServer`: `url` (`ws://127.0.0.1:<port>/ws`), `stop() -> str` (возвращает stdout + stderr процесса), `kill()`.
  - `harness.Client` — обёртка над `websockets.asyncio.client.connect` с `recv_json(timeout=2.0)`, `send_json`, `expect(type_)`; запоминает все id комнат, токены, имена и маркеры `data`, которые видел, — для проверки логов.
  - Фикстура `server` — запускает `app.main:app` с тестовым `RESUME_SECRET`; при завершении проверяет лог (ниже).
  - `test_app.make_app()` (`--factory`) — читает `TETE_TEST_LIMITS` (JSON полей `LimitConfig`) и `TETE_TEST_FAULT=1`: тогда `hub_factory` — подкласс `Hub`, который на `join` с именем `"boom"` бросает `ValueError(f"{name} {room_id} {token}")`. В рабочем `app.main` такого крючка нет.
- Проверка лога в каждой фикстуре: в выводе процесса нет `127.0.0.1`, адресов из `X-Forwarded-For`, ни одного запомненного id комнаты, токена, имени и маркера `data`, нет `Traceback`.

- [ ] **Step 1: Сценарии** (каждый — тест, async)

```python
# test_lifecycle.py
async def test_enter_and_leave(server): ...
async def test_drop_and_return_with_queue(server): ...
    # B рвёт соединение; A шлёт 2 signal B; B возвращается с токеном: joined resumed True, затем 2 signal
async def test_leave_then_token_gives_resumed_false(server): ...
async def test_server_restart_everyone_resumes_with_same_ids(): ...
    # A, B, C в комнате → kill → старт с тем же секретом → все join с токенами:
    # те же selfId, resumed True; A получил peer-joined B и C
async def test_abc_scenario_from_design(server): ...
    # A "Аня" создаёт, B "Борис 🎧" входит, C входит; signal по всем 6 направлениям с кириллицей в data;
    # B leave → A и C получили peer-left B
async def test_half_open_old_connection_replaced_fast(server): ...
    # R1: старый клиент не читает; новый join с токеном → joined быстрее 1 с; старый закрыт 4000; peer-left нет
async def test_abrupt_disconnects_leave_no_traceback(server): ...
    # 50 клиентов входят и рвут TCP (transport.abort) посреди отправки; сервер отвечает pong новому клиенту
@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM")
async def test_graceful_shutdown_closes_with_1012(): ...

# test_limits_and_frames.py — фабрика с малыми пределами через TETE_TEST_LIMITS
async def test_seat_limit_by_forwarded_for(): ...         # seats_per_key=2: третье с X-Forwarded-For 198.51.100.1 → отказ 403; с 198.51.100.2 — проходит
async def test_rightmost_forwarded_for_is_the_key(): ...  # "6.6.6.6, 7.7.7.7" делит лимит с "7.7.7.7"
async def test_create_rate_limited(): ...                  # creates_per_window=2
async def test_oversized_message_closed_1009(server): ...  # 70 000 байт текстом; фрагментами; сжатое, разжимается > 64 КБ
async def test_message_flood_closed_1008_others_still_fast(server): ...
    # signal по 64 КБ из мелких элементов подряд → закрытие 1008; другой клиент: pong быстрее 200 мс
async def test_slow_reader_closed_4001(server): ...        # получатель не читает, отправитель шлёт signal → 4001 у получателя
async def test_short_secret_fails_without_printing_it(): ...

# test_logs.py
async def test_query_string_is_not_logged(server): ...     # /ws?r=<roomId>
async def test_injected_error_logs_type_name_only(): ...   # TETE_TEST_FAULT=1, join name="boom" → в логе "ValueError", без имени, id, токена
```

- [ ] **Step 2:** Запустить `uv run pytest tests/integration -q`. Ожидается PASS: задачи 1–7 уже реализовали поведение. Если сценарий падает, это ошибка в коде задачи 1–7: тест остаётся с `pytest.mark.xfail(strict=True, reason=...)`, а ошибка уходит отдельной задачей исправления.
- [ ] **Step 3:** Прогнать весь набор дважды подряд — без флапающих тестов.
- [ ] **Step 4:** Коммит `test(server): интеграционные тесты сигналинга на настоящем uvicorn`.

---

### Task 9: Документация — основная сессия

**Агент:** основная сессия (субагенты дизайн-документ не правят).

**Проверка:** `npm run lint` из корня (Prettier для markdown, если подключён), чтение изменённых разделов.

- CLAUDE.md:
  - «Протокол сигналинга»: `error {code: room-full | bad-request | rate-limited}`; строка о кодах закрытия `4000`, `4001`, `1008`, `1009`, `1012`;
  - «Константы»: строки лимитов, бакетов, очередей и глобальных пределов из Global Constraints;
  - «Правила кода», клиент: решение 3 спецификации (15 с при `resumed: true`) и решение исполнителя 12 (`mute` после `joined`);
  - пункты 2.3, 3.3, 3.4, 3.5, 4.2, 4.6, 5.2, 9.3 — меры из «Разбора угроз»;
  - `[x]` у 1.1–1.8 — после `verify` (шаг 9 скилла `stage`).
- Дизайн-документ: строка `error` таблицы протокола; «Сверка после каждого `joined`»; лимиты в «Безопасности и приватности».
- Коммит `docs: этап 1 — протокол, константы и правила клиента`.
