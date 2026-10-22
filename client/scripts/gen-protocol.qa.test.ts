import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';
import { z } from 'zod';
import { generateZod, UnsupportedSchemaError } from './gen-protocol.ts';

type Exports = Record<string, z.ZodType>;

// Evaluates generated code: drops the import and type exports, hands zod in as `z`.
function load(schema: unknown): Exports {
  const code = generateZod(schema);
  const names = [...code.matchAll(/^export const (\w+) =/gm)].map((m) => m[1] ?? '');
  const body = code
    .split('\n')
    .filter((line) => !line.startsWith('import ') && !line.startsWith('export type '))
    .join('\n')
    .replaceAll('export const ', 'const ');
  return runInNewContext(
    `${body}
({ ${names.join(', ')} })`,
    { z },
  ) as Exports;
}

function pick(exports: Exports, name: string): z.ZodType {
  const found = exports[name];
  if (!found) throw new Error(`no export ${name}`);
  return found;
}

function thrown(fn: () => unknown): unknown {
  try {
    fn();
  } catch (error) {
    return error;
  }
  return undefined;
}

describe('generated code semantics', () => {
  it('escapes quotes, backslashes, newlines and template syntax in enum, const and pattern', () => {
    const tricky = [
      'a"b',
      "c'd",
      'e\\f',
      'g\nh',
      '`${1+1}`',
      '</script>',
      '\u2028\u2029',
      '*/ x /*',
    ];
    const exports = load({
      $defs: {
        E: { type: 'string', enum: tricky },
        C: { const: 'a"b' },
        P: { type: 'string', pattern: '^a"\\\\b\\u2028$' },
      },
    });
    for (const value of tricky) expect(pick(exports, 'E').safeParse(value).success).toBe(true);
    expect(pick(exports, 'E').safeParse('other').success).toBe(false);
    expect(pick(exports, 'C').safeParse('a"b').success).toBe(true);
    expect(pick(exports, 'P').safeParse('a"\\b\u2028').success).toBe(true);
  });

  it('optional key accepts absence but not null; nullable key accepts null but not absence', () => {
    const exports = load({
      $defs: {
        O: {
          type: 'object',
          properties: {
            a: { type: 'string' },
            b: { anyOf: [{ type: 'string' }, { type: 'null' }] },
          },
          required: ['b'],
        },
      },
    });
    const o = pick(exports, 'O');
    expect(o.safeParse({ b: null }).success).toBe(true);
    expect(o.safeParse({ b: 'x' }).success).toBe(true);
    expect(o.safeParse({}).success).toBe(false);
    expect(o.safeParse({ a: null, b: null }).success).toBe(false);
  });

  it('strict object rejects extra keys, open object drops them', () => {
    const exports = load({
      $defs: {
        S: { type: 'object', properties: {}, additionalProperties: false },
        L: { type: 'object', properties: {} },
      },
    });
    expect(pick(exports, 'S').safeParse({ x: 1 }).success).toBe(false);
    expect(pick(exports, 'L').parse({ x: 1 })).toEqual({});
  });

  it('length bounds count code points at the limits', () => {
    const exports = load({ $defs: { S: { type: 'string', minLength: 2, maxLength: 3 } } });
    const s = pick(exports, 'S');
    expect(s.safeParse('😀').success).toBe(false);
    expect(s.safeParse('😀😀').success).toBe(true);
    expect(s.safeParse('😀😀😀').success).toBe(true);
    expect(s.safeParse('😀😀😀😀').success).toBe(false);
  });

  it('integer and array bounds are inclusive and reject fractions', () => {
    const exports = load({
      $defs: {
        I: { type: 'integer', minimum: 0, maximum: 3 },
        A: { type: 'array', items: { type: 'boolean' }, minItems: 1, maxItems: 2 },
      },
    });
    const i = pick(exports, 'I');
    expect([-1, 0, 3, 4, 1.5].map((v) => i.safeParse(v).success)).toEqual([
      false,
      true,
      true,
      false,
      false,
    ]);
    const a = pick(exports, 'A');
    const verdicts = [[], [true], [true, false], [true, false, true]].map(
      (v) => a.safeParse(v).success,
    );
    expect(verdicts).toEqual([false, true, true, false]);
  });

  it('the output does not depend on the order of definitions in the input', () => {
    const defs = {
      B: { type: 'object', properties: { a: { $ref: '#/$defs/A' } }, required: ['a'] },
      A: { type: 'string' },
      C: { type: 'integer' },
    };
    const reversed = Object.fromEntries(Object.entries(defs).reverse());
    expect(generateZod({ $defs: reversed })).toBe(generateZod({ $defs: defs }));
  });
});

describe('generator rejects what it cannot translate', () => {
  it('an unknown $ref', () => {
    const error = thrown(() => generateZod({ $defs: { A: { $ref: '#/$defs/Missing' } } }));
    expect(error).toBeInstanceOf(UnsupportedSchemaError);
    expect((error as UnsupportedSchemaError).message).toContain('Missing');
  });

  it('a self reference, also through array items and properties', () => {
    for (const def of [
      { $ref: '#/$defs/A' },
      { type: 'array', items: { $ref: '#/$defs/A' } },
      { type: 'object', properties: { a: { $ref: '#/$defs/A' } } },
    ]) {
      expect(() => generateZod({ $defs: { A: def } })).toThrow(UnsupportedSchemaError);
    }
  });

  it('a $ref that points anywhere but $defs/Name', () => {
    for (const ref of [
      '#',
      '#/$defs/A/properties/x',
      'other.json#/$defs/A',
      '#/definitions/A',
      '',
    ]) {
      expect(() => generateZod({ $defs: { A: { type: 'string' }, B: { $ref: ref } } })).toThrow(
        UnsupportedSchemaError,
      );
    }
  });

  it('unsupported keywords inside nested nodes, naming the full path', () => {
    const error = thrown(() =>
      generateZod({
        $defs: {
          A: {
            type: 'object',
            properties: { p: { type: 'array', items: { type: 'string', format: 'uuid' } } },
          },
        },
      }),
    );
    expect((error as UnsupportedSchemaError).path).toBe('#/$defs/A/properties/p/items/format');
  });

  it('escapes "/" and "~" in property names in the reported path', () => {
    const error = thrown(() =>
      generateZod({ $defs: { A: { type: 'object', properties: { 'a/b~c': { type: 'null' } } } } }),
    );
    expect((error as UnsupportedSchemaError).path).toContain('a~1b~0c');
  });

  it.each([
    ['uniqueItems', { type: 'array', items: { type: 'string' }, uniqueItems: true }],
    ['multipleOf', { type: 'integer', multipleOf: 2 }],
    ['exclusiveMaximum', { type: 'integer', exclusiveMaximum: 3 }],
    ['format', { type: 'string', format: 'email' }],
    ['allOf', { allOf: [{ type: 'string' }] }],
    ['not', { not: { type: 'string' } }],
    ['if', { if: { type: 'string' } }],
    ['patternProperties', { type: 'object', patternProperties: { a: { type: 'string' } } }],
    ['prefixItems', { type: 'array', items: { type: 'string' }, prefixItems: [] }],
    ['propertyNames', { type: 'object', propertyNames: { type: 'string' } }],
    ['minProperties', { type: 'object', minProperties: 1 }],
  ])('keyword %s', (_name, def) => {
    expect(() => generateZod({ $defs: { A: def } })).toThrow(UnsupportedSchemaError);
  });

  it('a type list, number, null and an unknown type', () => {
    for (const def of [
      { type: ['string', 'null'] },
      { type: 'number' },
      { type: 'null' },
      { type: 'weird' },
    ]) {
      expect(() => generateZod({ $defs: { A: def } })).toThrow(UnsupportedSchemaError);
    }
  });

  it('a fractional or negative bound', () => {
    for (const def of [
      { type: 'string', minLength: -1 },
      { type: 'string', maxLength: 1.5 },
      { type: 'integer', minimum: 0.5 },
      { type: 'array', items: { type: 'string' }, maxItems: -2 },
    ]) {
      expect(() => generateZod({ $defs: { A: def } })).toThrow(UnsupportedSchemaError);
    }
  });

  it('required naming a property that does not exist', () => {
    expect(() =>
      generateZod({
        $defs: { A: { type: 'object', properties: { a: { type: 'string' } }, required: ['b'] } },
      }),
    ).toThrow(UnsupportedSchemaError);
  });

  it('QA-3: required naming an inherited Object key such as "toString" is rejected', () => {
    expect(() =>
      generateZod({ $defs: { A: { type: 'object', properties: {}, required: ['toString'] } } }),
    ).toThrow(UnsupportedSchemaError);
  });

  it('QA-3: type "constructor" is reported as unsupported, not as a TypeError', () => {
    expect(() => generateZod({ $defs: { A: { type: 'constructor' } } })).toThrow(
      UnsupportedSchemaError,
    );
  });

  it('QA-4: a pattern JavaScript cannot compile fails at generation, not at import', () => {
    for (const pattern of ['(?i)abc', '(?P<x>a)', 'a++', '\\Aabc\\Z']) {
      expect(() => generateZod({ $defs: { A: { type: 'string', pattern } } })).toThrow(
        UnsupportedSchemaError,
      );
    }
  });

  it('QA-4: a definition named like a reserved word or "z" fails at generation', () => {
    for (const name of ['z', 'default', 'class', 'delete']) {
      expect(() => load({ $defs: { [name]: { type: 'string' } } })).toThrow(UnsupportedSchemaError);
    }
  });

  it('QA-4: a union variant without the discriminator literal fails at generation', () => {
    const schema = {
      $defs: {
        A: { type: 'object', properties: { type: { const: 'a' } }, required: ['type'] },
        B: { type: 'object', properties: { x: { type: 'string' } }, required: ['x'] },
        U: {
          oneOf: [{ $ref: '#/$defs/A' }, { $ref: '#/$defs/B' }],
          discriminator: { propertyName: 'type' },
        },
      },
    };
    expect(() => load(schema)).toThrow(UnsupportedSchemaError);
  });
});
