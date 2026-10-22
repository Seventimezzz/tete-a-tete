import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { generateZod, UnsupportedSchemaError } from './gen-protocol.ts';

const HEADER = `// Сгенерировано из protocol/schema.json командой npm run gen:protocol. Не править руками.
// Источник истины — Pydantic-модели в server/app/protocol.py.
`;

function catchError(fn: () => unknown): unknown {
  try {
    fn();
  } catch (error) {
    return error;
  }
  return undefined;
}

describe('generateZod', () => {
  it('rejects an unsupported keyword and names its path', () => {
    const error = catchError(() =>
      generateZod({ $defs: { A: { type: 'integer', exclusiveMinimum: 0 } } }),
    );
    expect(error).toBeInstanceOf(UnsupportedSchemaError);
    expect((error as UnsupportedSchemaError).path).toBe('#/$defs/A/exclusiveMinimum');
  });

  it('rejects a reference cycle', () => {
    const schema = {
      $defs: { A: { $ref: '#/$defs/B' }, B: { $ref: '#/$defs/A' } },
    };
    expect(() => generateZod(schema)).toThrow(UnsupportedSchemaError);
  });

  it('rejects anyOf other than nullable', () => {
    const schema = { $defs: { A: { anyOf: [{ type: 'string' }, { type: 'integer' }] } } };
    expect(() => generateZod(schema)).toThrow(UnsupportedSchemaError);
  });

  it('rejects a definition name that is not an identifier', () => {
    expect(() => generateZod({ $defs: { 'a-b': { type: 'string' } } })).toThrow(
      UnsupportedSchemaError,
    );
  });

  it('orders definitions by dependency, then by name', () => {
    const out = generateZod({
      $defs: {
        C: { $ref: '#/$defs/B' },
        B: { $ref: '#/$defs/A' },
        A: { type: 'string' },
        D: { type: 'string' },
      },
    });
    const at = (name: string): number => out.indexOf(`export const ${name}`);
    expect(at('A')).toBeLessThan(at('B'));
    expect(at('B')).toBeLessThan(at('C'));
    expect(at('D')).toBeGreaterThan(at('C'));
  });

  it('is deterministic', () => {
    const schema: unknown = JSON.parse(
      readFileSync(new URL('../../protocol/schema.json', import.meta.url), 'utf8'),
    );
    expect(generateZod(schema)).toBe(generateZod(structuredClone(schema)));
  });

  it('generates the real protocol schema', () => {
    const schema: unknown = JSON.parse(
      readFileSync(new URL('../../protocol/schema.json', import.meta.url), 'utf8'),
    );
    const out = generateZod(schema);
    expect(out).toContain('z.discriminatedUnion("type", [');
    expect(out).toContain('export const ClientMessage');
    expect(out).toContain('export type ServerMessage = z.infer<typeof ServerMessage>;');
  });

  it('emits strict objects only for additionalProperties: false', () => {
    const strict = generateZod({
      $defs: { A: { type: 'object', additionalProperties: false, properties: {} } },
    });
    const loose = generateZod({ $defs: { A: { type: 'object', properties: {} } } });
    expect(strict).toContain('z.strictObject(');
    expect(loose).toContain('z.object(');
    expect(loose).not.toContain('z.strictObject(');
  });

  it('counts Unicode code points for string length and compiles the pattern with u', () => {
    const out = generateZod({
      $defs: { N: { type: 'string', minLength: 1, maxLength: 32, pattern: '^a+$' } },
    });
    expect(out).toContain('[...value].length');
    expect(out).toContain('new RegExp("^a+$", \'u\')');
  });

  it('marks fields outside required optional and nullable anyOf as nullable', () => {
    const out = generateZod({
      $defs: {
        A: {
          type: 'object',
          properties: {
            x: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
            y: { type: 'boolean' },
          },
          required: ['x'],
        },
      },
    });
    expect(out).toContain('"x": z.boolean().nullable()');
    expect(out).toContain('"y": z.boolean().optional()');
  });

  it('starts with the generated-file header', () => {
    const out = generateZod({ $defs: { A: { type: 'string' } } });
    expect(out.startsWith(`${HEADER}import { z } from 'zod';`)).toBe(true);
  });

  it('names the path of an uncompilable pattern', () => {
    const error = catchError(() =>
      generateZod({ $defs: { A: { type: 'string', pattern: '(?i)a' } } }),
    );
    expect((error as UnsupportedSchemaError).path).toBe('#/$defs/A/pattern');
  });
  it('names the path of a reserved definition name', () => {
    const error = catchError(() => generateZod({ $defs: { class: { type: 'string' } } }));
    expect((error as UnsupportedSchemaError).path).toBe('#/$defs/class');
  });
  it('names the path of a union variant without the discriminator literal', () => {
    const error = catchError(() =>
      generateZod({
        $defs: {
          B: { type: 'object', properties: { x: { type: 'string' } }, required: ['x'] },
          U: { oneOf: [{ $ref: '#/$defs/B' }], discriminator: { propertyName: 'type' } },
        },
      }),
    );
    expect((error as UnsupportedSchemaError).path).toBe('#/$defs/U/oneOf/0');
  });
  it('emits the lone-surrogate check once and only when a string is used', () => {
    const withString = generateZod({ $defs: { A: { type: 'string' }, B: { type: 'string' } } });
    expect(withString.match(/const loneSurrogate/g)).toHaveLength(1);
    expect(generateZod({ $defs: { A: { type: 'boolean' } } })).not.toContain('loneSurrogate');
  });
});
