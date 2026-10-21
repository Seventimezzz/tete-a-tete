---
name: webrtc-call-engine
description: Правила и образцы кода ядра звонка Tete-a-tete (client/renderer/src/app/core) — кто создаёт RTCPeerConnection и отправляет offer, perfect negotiation, буфер ICE-кандидатов, restartIce, сверка peers после joined, mute, индикатор говорящего, getStats для pair-report, ретрансляция трека через участника. Используй при любой работе с WebRTC в клиенте.
---

# Ядро звонка: соединения между участниками

Ниже — правила из CLAUDE.md и дизайн-документа, разложенные по коду. Если образец расходится с ними, правы они.

## Старт

1. Микрофон запрашивается до первого `RTCPeerConnection`. Без разрешения Chromium прячет адреса за именами `*.local`.
2. Конфигурация соединения — только свой STUN:

   ```ts
   new RTCPeerConnection({ iceServers: [{ urls: `stun:${host}:3478` }] });
   ```

   TURN и сторонних STUN нет. STUN спрашивается для каждого соединения отдельно: у каждого свой порт.
3. Политику IP и диапазон портов задаёт главный процесс до загрузки страницы. Ядро это не настраивает.

## Кто начинает

- **Новичок** — тот, кто получил `joined`. Для каждого id из `peers` он создаёт соединение, добавляет трек микрофона, и `negotiationneeded` отправляет offer.
- **Старые участники** на `peer-joined` только запоминают имя. Соединение создаётся, когда приходит первый `signal` от новичка: создать соединение, добавить трек микрофона, затем обработать offer. Трек, добавленный до `setRemoteDescription(offer)`, займёт аудио-секцию offer, и ответ получится `sendrecv` без лишнего согласования.
- **Роль polite** — сравнение id, одно правило на всё приложение (например, `polite = selfId < peerId`). Зафиксируй его функцией и тестом.
- **`pair-report`** отправляет участник пары с меньшим id при каждом изменении итога.

## Perfect negotiation и буфер кандидатов

`data` в `signal` — это `{description}`, `{candidate}` или `{relayTrack}`. Образец для одной пары:

```ts
class PeerLink {
  private makingOffer = false;
  private ignoreOffer = false;
  private isSettingRemoteAnswerPending = false;
  private readonly pendingCandidates: RTCIceCandidateInit[] = [];

  constructor(
    private readonly pc: RTCPeerConnection,
    private readonly polite: boolean,
    private readonly send: (data: SignalData) => void,
    private readonly events: PeerLinkEvents,
  ) {
    pc.onnegotiationneeded = async () => {
      try {
        this.makingOffer = true;
        await pc.setLocalDescription();
        this.send({ description: pc.localDescription!.toJSON() });
      } finally {
        this.makingOffer = false;
      }
    };
    pc.onicecandidate = ({ candidate }) => {
      if (candidate) this.send({ candidate: candidate.toJSON() });
    };
  }

  async onDescription(description: RTCSessionDescriptionInit): Promise<void> {
    const readyForOffer =
      !this.makingOffer &&
      (this.pc.signalingState === 'stable' || this.isSettingRemoteAnswerPending);
    const offerCollision = description.type === 'offer' && !readyForOffer;
    this.ignoreOffer = !this.polite && offerCollision;
    if (this.ignoreOffer) return;

    this.isSettingRemoteAnswerPending = description.type === 'answer';
    await this.pc.setRemoteDescription(description); // polite откатывает свой offer сам
    this.isSettingRemoteAnswerPending = false;

    for (const candidate of this.pendingCandidates.splice(0)) {
      await this.addCandidate(candidate);
    }
    if (description.type === 'offer') {
      await this.pc.setLocalDescription();
      this.send({ description: this.pc.localDescription!.toJSON() });
    }
  }

  async onCandidate(candidate: RTCIceCandidateInit): Promise<void> {
    if (!this.pc.remoteDescription) {
      this.pendingCandidates.push(candidate);
      return;
    }
    await this.addCandidate(candidate);
  }

  private async addCandidate(candidate: RTCIceCandidateInit): Promise<void> {
    try {
      await this.pc.addIceCandidate(candidate);
    } catch {
      // Кандидат от отброшенного offer — норма. Наружу только событие, без текста кандидата.
      if (!this.ignoreOffer) this.events.candidateRejected();
    }
  }
}
```

Тесты на поддельном `RTCPeerConnection`: встречные offer с обеих сторон, кандидат раньше описания, кандидаты от отброшенного offer.

## Обрывы и перезапуск ICE

Следи за `connectionState`:

| Состояние | Действие |
| --- | --- |
| `disconnected` | таймер 3 с; если состояние не сменилось — `restartIce()` |
| `failed` | одна попытка `restartIce()`; не помогло — статус «нет прямой связи» и `pair-report` с итогом `failed` |
| `connected` | сбросить таймер и счётчик попыток |

- `restartIce()` вызывает `negotiationneeded`, и новый offer идёт по тому же perfect negotiation.
- Пока сигналинг недоступен, перезапуск откладывается и выполняется после следующего `joined`.
- Обрыв WebSocket соединения между участниками не трогает.

## Сверка после каждого `joined`

- `resumed: true`: закрыть соединения с id, которых нет в `peers`; для id из `peers` без соединения создать соединение и отправить offer, как новичок. Отложенные перезапуски выполнить.
- `resumed: false`: закрыть все соединения и войти как новичок.

**Нерешённый вопрос.** После перезапуска сервера первые вернувшиеся получают неполный `peers`: остальные ещё не переподключились. Буквальная сверка закроет живые P2P-соединения, хотя дизайн обещает, что они продолжают работать. Правило для этого случая решается в спецификации этапа 5 вместе с человеком, а не в коде на ходу.

## Mute и индикатор говорящего

- Mute: `track.enabled = false` и сообщение `mute {muted}`. Без `removeTrack`, `replaceTrack` и пересогласования.
- Индикатор: опрос `receiver.getSynchronizationSources()` и поле `audioLevel` для каждого входящего трека. Наружу — только изменения выше порога. WebAudio не используется.
- Воспроизведение входящего трека и выбор устройства вывода (`setSinkId`) — через внедряемый интерфейс, чтобы ядро тестировалось без DOM.

## `pair-report` из `getStats()`

- Выбранная пара — `transport.selectedCandidatePairId`, из неё локальный и удалённый `candidateType` и семейство адреса.
- `sameIp`, `ms` (от создания соединения до `connected`) и `restarts` считаются на клиенте. На сервер уходят только поля модели протокола, без адресов. Значения `route` и `family` берутся из модели.

## Ретрансляция через участника

Сообщение `relay {a, b, via}` приходит всем троим.

- **Ретранслятор (`via` — это я):** трек, полученный от `b`, добавляется `addTrack` в соединение с `a`, и наоборот. Соединения пересогласуются через perfect negotiation. Когда у нового трансивера появился `mid`, отправь в соединение получателя `signal` с `{relayTrack: {mid, from}}`. При `relay` с `via: null` или с другим ретранслятором — `removeTrack` для этих отправителей.
- **Участник пары:** статус «через B». Событие `track` и сообщение `relayTrack` могут прийти в любом порядке: храни обе стороны и сопоставляй по `mid`. Пересланный трек проигрывается и показывается как голос `from`.
- В комнате на двоих ретранслировать некому.
