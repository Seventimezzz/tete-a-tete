import { describe, expect, it } from 'vitest';
import examples from '../../protocol/examples.json' with { type: 'json' };
import { ClientMessage, ServerMessage, ServerPeerJoined } from './protocol.gen';

function describeServerMessage(message: ServerMessage): string {
  switch (message.type) {
    case 'joined':
    case 'peer-joined':
    case 'peer-left':
    case 'signal':
    case 'mute':
    case 'relay':
    case 'pong':
    case 'error':
      return message.type;
    default:
      return message satisfies never;
  }
}

describe('protocol examples', () => {
  it.each(examples.client.valid)('accepts valid client message %#', (m) => {
    expect(ClientMessage.safeParse(m).success).toBe(true);
  });
  it.each(examples.client.invalid)('rejects client message: $why', (c) => {
    expect(ClientMessage.safeParse(c.message).success).toBe(false);
  });
  it.each(examples.server.valid)('accepts valid server message %#', (m) => {
    expect(ServerMessage.safeParse(m).success).toBe(true);
  });
  it.each(examples.server.invalid)('rejects server message: $why', (c) => {
    expect(ServerMessage.safeParse(c.message).success).toBe(false);
  });
});

describe('protocol rules', () => {
  it('counts name length in Unicode characters', () => {
    const make = (name: string) => ({ type: 'peer-joined', id: 'abcdefgh', name });
    expect(ServerPeerJoined.safeParse(make('😀'.repeat(32))).success).toBe(true);
    expect(ServerPeerJoined.safeParse(make('😀'.repeat(33))).success).toBe(false);
    expect(ServerPeerJoined.safeParse(make('a'.repeat(32))).success).toBe(true);
  });
  it('drops unknown fields in server messages', () => {
    const parsed = ServerMessage.parse({ type: 'pong', extra: 1 });
    expect(parsed).not.toHaveProperty('extra');
  });
  it('rejects unknown fields in client messages', () => {
    expect(ClientMessage.safeParse({ type: 'ping', extra: 1 }).success).toBe(false);
  });
  it('narrows ServerMessage by type exhaustively', () => {
    expect(describeServerMessage({ type: 'pong' })).toBe('pong');
  });
});
