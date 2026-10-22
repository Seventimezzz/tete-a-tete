import { describe, expect, it } from 'vitest';
import fixture from '../../server/tests/fixtures/agreement_cases.json' with { type: 'json' };
import { ClientMessage, ServerMessage } from './protocol.gen';

// The same raw text is checked by server/tests/test_protocol_qa.py with Pydantic.
interface Case {
  dir: 'client' | 'server';
  label: string;
  raw: string;
  expect: 'accept' | 'reject';
  expectZod?: 'accept' | 'reject';
  bug?: { py?: string; zod?: string };
}

const cases = fixture as Case[];

function accepted(c: Case): boolean {
  let value: unknown;
  try {
    value = JSON.parse(c.raw);
  } catch {
    return false;
  }
  const schema = c.dir === 'client' ? ClientMessage : ServerMessage;
  return schema.safeParse(value).success;
}

describe('zod agrees with the shared fixture (same input as Pydantic)', () => {
  for (const [index, c] of cases.entries()) {
    const name = `${String(index)} ${c.dir}: ${c.label}`;
    const wanted = (c.expectZod ?? c.expect) === 'accept';
    const run = (): void => {
      expect(accepted(c)).toBe(wanted);
    };
    if (c.bug?.zod) {
      it.fails(`${c.bug.zod}: ${name}`, run);
    } else {
      it(name, run);
    }
  }

  it('has cases for both directions', () => {
    expect(new Set(cases.map((c) => c.dir))).toEqual(new Set(['client', 'server']));
  });
});
