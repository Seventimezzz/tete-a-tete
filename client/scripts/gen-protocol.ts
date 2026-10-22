import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export class UnsupportedSchemaError extends Error {
  readonly path: string;

  constructor(path: string, reason: string) {
    super(`${path}: ${reason}`);
    this.name = 'UnsupportedSchemaError';
    this.path = path;
  }
}

type Node = Record<string, unknown>;

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const REF_PREFIX = '#/$defs/';
const ANNOTATIONS = ['title', 'description', 'default'];
const BY_TYPE = new Map<string, string[]>([
  ['string', ['pattern', 'minLength', 'maxLength']],
  ['integer', ['minimum', 'maximum']],
  ['boolean', []],
  ['object', ['properties', 'required', 'additionalProperties']],
  ['array', ['items', 'minItems', 'maxItems']],
]);
// Имена, которые нельзя занимать определениям: зарезервированные слова ECMAScript и TypeScript
// (в том числе строгого режима) и идентификаторы, которые использует сам сгенерированный код.
const RESERVED_NAMES = new Set([
  'await',
  'break',
  'case',
  'catch',
  'class',
  'const',
  'continue',
  'debugger',
  'default',
  'delete',
  'do',
  'else',
  'enum',
  'export',
  'extends',
  'false',
  'finally',
  'for',
  'function',
  'if',
  'implements',
  'import',
  'in',
  'instanceof',
  'interface',
  'let',
  'new',
  'null',
  'package',
  'private',
  'protected',
  'public',
  'return',
  'static',
  'super',
  'switch',
  'this',
  'throw',
  'true',
  'try',
  'typeof',
  'var',
  'void',
  'while',
  'with',
  'yield',
  'z',
  'RegExp',
  'loneSurrogate',
]);

const HEADER = `// Сгенерировано из protocol/schema.json командой npm run gen:protocol. Не править руками.
// Источник истины — Pydantic-модели в server/app/protocol.py.
import { z } from 'zod';
`;
// Одиночный суррогат: Pydantic такую строку отклоняет, а regex с флагом u и [...value] пропускают.
// Регулярное выражение без флага u работает по кодовым единицам UTF-16.
const WELL_FORMED = `
const loneSurrogate =
  /[\\ud800-\\udbff](?![\\udc00-\\udfff])|(?<![\\ud800-\\udbff])[\\udc00-\\udfff]/;
`;
let currentDefs: Node = {};

function pointer(path: string, key: string): string {
  return `${path}/${key.replaceAll('~', '~0').replaceAll('/', '~1')}`;
}

function isNode(value: unknown): value is Node {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asNode(value: unknown, path: string): Node {
  if (!isNode(value)) throw new UnsupportedSchemaError(path, 'expected a schema object');
  return value;
}

function allowOnly(node: Node, path: string, allowed: string[]): void {
  for (const key of Object.keys(node)) {
    if (!allowed.includes(key) && !ANNOTATIONS.includes(key)) {
      throw new UnsupportedSchemaError(pointer(path, key), `unsupported keyword "${key}"`);
    }
  }
}

function asCount(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new UnsupportedSchemaError(path, 'expected a non-negative integer');
  }
  return value;
}

function asInteger(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value)) {
    throw new UnsupportedSchemaError(path, 'expected an integer');
  }
  return value;
}

function refName(value: unknown, path: string): string {
  if (typeof value !== 'string' || !value.startsWith(REF_PREFIX)) {
    throw new UnsupportedSchemaError(path, `expected a reference like "${REF_PREFIX}Name"`);
  }
  const name = value.slice(REF_PREFIX.length);
  if (!NAME_RE.test(name)) throw new UnsupportedSchemaError(path, 'bad reference name');
  return name;
}

function isNullType(value: unknown): boolean {
  return isNode(value) && Object.keys(value).length === 1 && value['type'] === 'null';
}

function node(raw: unknown, path: string, deps: Set<string>): string {
  const n = asNode(raw, path);

  if ('$ref' in n) {
    allowOnly(n, path, ['$ref']);
    const name = refName(n['$ref'], pointer(path, '$ref'));
    deps.add(name);
    return name;
  }
  if ('const' in n) {
    allowOnly(n, path, ['const', 'type']);
    const value = n['const'];
    if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
      throw new UnsupportedSchemaError(pointer(path, 'const'), 'unsupported const value');
    }
    return `z.literal(${JSON.stringify(value)})`;
  }
  if ('enum' in n) {
    allowOnly(n, path, ['enum', 'type']);
    const values = n['enum'];
    if (
      !Array.isArray(values) ||
      values.length === 0 ||
      !values.every((v) => typeof v === 'string')
    ) {
      throw new UnsupportedSchemaError(pointer(path, 'enum'), 'only non-empty string enums');
    }
    return `z.enum([${values.map((v) => JSON.stringify(v)).join(', ')}])`;
  }
  if ('anyOf' in n) {
    allowOnly(n, path, ['anyOf']);
    const variants = n['anyOf'];
    const at = pointer(path, 'anyOf');
    if (!Array.isArray(variants) || variants.length !== 2) {
      throw new UnsupportedSchemaError(at, 'anyOf must be a schema and {"type": "null"}');
    }
    const nullIndex = variants.findIndex(isNullType);
    if (nullIndex === -1 || isNullType(variants[1 - nullIndex])) {
      throw new UnsupportedSchemaError(at, 'anyOf must be a schema and {"type": "null"}');
    }
    const otherIndex = 1 - nullIndex;
    return `${node(variants[otherIndex], pointer(at, String(otherIndex)), deps)}.nullable()`;
  }
  if ('oneOf' in n) {
    allowOnly(n, path, ['oneOf', 'discriminator']);
    const variants = n['oneOf'];
    const at = pointer(path, 'oneOf');
    const disc = n['discriminator'];
    if (!Array.isArray(variants) || variants.length === 0) {
      throw new UnsupportedSchemaError(at, 'oneOf must be a non-empty array');
    }
    if (!isNode(disc) || typeof disc['propertyName'] !== 'string') {
      throw new UnsupportedSchemaError(
        pointer(path, 'discriminator'),
        'oneOf needs a discriminator',
      );
    }
    const items = variants.map((v, i) => {
      const item = asNode(v, pointer(at, String(i)));
      if (!('$ref' in item)) {
        throw new UnsupportedSchemaError(pointer(at, String(i)), 'oneOf items must be references');
      }
      checkVariant(item, disc['propertyName'] as string, pointer(at, String(i)));
      return node(item, pointer(at, String(i)), deps);
    });
    return `z.discriminatedUnion(${JSON.stringify(disc['propertyName'])}, [${items.join(', ')}])`;
  }
  if (Object.keys(n).every((k) => ANNOTATIONS.includes(k))) return 'z.unknown()';

  const type = n['type'];
  const allowedKeys = typeof type === 'string' ? BY_TYPE.get(type) : undefined;
  if (typeof type !== 'string' || allowedKeys === undefined) {
    throw new UnsupportedSchemaError(
      pointer(path, 'type'),
      `unsupported type ${JSON.stringify(type)}`,
    );
  }
  allowOnly(n, path, ['type', ...allowedKeys]);

  switch (type) {
    case 'boolean':
      return 'z.boolean()';
    case 'string':
      return stringNode(n, path);
    case 'integer':
      return integerNode(n, path);
    case 'array':
      return arrayNode(n, path, deps);
    default:
      return objectNode(n, path, deps);
  }
}

function checkVariant(item: Node, property: string, path: string): void {
  const ref = typeof item['$ref'] === 'string' ? item['$ref'] : '';
  const name = ref.startsWith(REF_PREFIX) ? ref.slice(REF_PREFIX.length) : '';
  const target = Object.hasOwn(currentDefs, name) ? currentDefs[name] : undefined;
  const props = isNode(target) && target['type'] === 'object' ? target['properties'] : undefined;
  const tag = isNode(props) && Object.hasOwn(props, property) ? props[property] : undefined;
  if (!isNode(tag) || typeof tag['const'] !== 'string') {
    throw new UnsupportedSchemaError(
      path,
      `oneOf variant must be an object whose "${property}" property is a const string`,
    );
  }
}

function stringNode(n: Node, path: string): string {
  let out = 'z.string().refine((value) => !loneSurrogate.test(value))';
  if ('pattern' in n) {
    const pattern = n['pattern'];
    if (typeof pattern !== 'string') {
      throw new UnsupportedSchemaError(pointer(path, 'pattern'), 'expected a string');
    }
    try {
      new RegExp(pattern, 'u');
    } catch {
      throw new UnsupportedSchemaError(
        pointer(path, 'pattern'),
        'pattern is not a valid ECMAScript regular expression',
      );
    }
    out += `.regex(new RegExp(${JSON.stringify(pattern)}, 'u'))`;
  }
  if ('minLength' in n) {
    const min = asCount(n['minLength'], pointer(path, 'minLength'));
    out += `.refine((value) => [...value].length >= ${String(min)})`;
  }
  if ('maxLength' in n) {
    const max = asCount(n['maxLength'], pointer(path, 'maxLength'));
    out += `.refine((value) => [...value].length <= ${String(max)})`;
  }
  return out;
}

function integerNode(n: Node, path: string): string {
  let out = 'z.number().int()';
  if ('minimum' in n) out += `.min(${String(asInteger(n['minimum'], pointer(path, 'minimum')))})`;
  if ('maximum' in n) out += `.max(${String(asInteger(n['maximum'], pointer(path, 'maximum')))})`;
  return out;
}

function arrayNode(n: Node, path: string, deps: Set<string>): string {
  if (!('items' in n)) {
    throw new UnsupportedSchemaError(pointer(path, 'items'), 'items is required');
  }
  let out = `z.array(${node(n['items'], pointer(path, 'items'), deps)})`;
  if ('minItems' in n) out += `.min(${String(asCount(n['minItems'], pointer(path, 'minItems')))})`;
  if ('maxItems' in n) out += `.max(${String(asCount(n['maxItems'], pointer(path, 'maxItems')))})`;
  return out;
}

function objectNode(n: Node, path: string, deps: Set<string>): string {
  const extra = n['additionalProperties'];
  const hasProps = 'properties' in n;

  if (isNode(extra)) {
    if (hasProps || 'required' in n) {
      throw new UnsupportedSchemaError(
        pointer(path, 'additionalProperties'),
        'additionalProperties schema together with properties',
      );
    }
    return `z.record(z.string(), ${node(extra, pointer(path, 'additionalProperties'), deps)})`;
  }
  if ('additionalProperties' in n && extra !== false) {
    throw new UnsupportedSchemaError(
      pointer(path, 'additionalProperties'),
      'expected false or a schema',
    );
  }

  const props = hasProps ? asNode(n['properties'], pointer(path, 'properties')) : {};
  const requiredRaw = n['required'] ?? [];
  if (!Array.isArray(requiredRaw) || !requiredRaw.every((r) => typeof r === 'string')) {
    throw new UnsupportedSchemaError(pointer(path, 'required'), 'expected an array of strings');
  }
  const required = new Set<string>(requiredRaw);
  for (const key of required) {
    if (!Object.hasOwn(props, key)) {
      throw new UnsupportedSchemaError(pointer(path, 'required'), `unknown property "${key}"`);
    }
  }
  const fields = Object.entries(props).map(([key, value]) => {
    const expr = node(value, pointer(pointer(path, 'properties'), key), deps);
    return `${JSON.stringify(key)}: ${expr}${required.has(key) ? '' : '.optional()'}`;
  });
  const ctor = extra === false ? 'z.strictObject' : 'z.object';
  return `${ctor}({${fields.join(', ')}})`;
}

export function generateZod(schema: unknown): string {
  const root = asNode(schema, '#');
  allowOnly(root, '#', ['$schema', '$defs']);
  const defs = asNode(root['$defs'] ?? {}, '#/$defs');
  currentDefs = defs;

  const exprs = new Map<string, string>();
  const deps = new Map<string, Set<string>>();
  for (const [name, def] of Object.entries(defs)) {
    const path = pointer('#/$defs', name);
    if (!NAME_RE.test(name)) {
      throw new UnsupportedSchemaError(path, `definition name "${name}" is not an identifier`);
    }
    if (RESERVED_NAMES.has(name)) {
      throw new UnsupportedSchemaError(path, `definition name "${name}" is reserved`);
    }
    const used = new Set<string>();
    exprs.set(name, node(def, path, used));
    deps.set(name, used);
  }
  for (const [name, used] of deps) {
    for (const dep of used) {
      if (!exprs.has(dep)) {
        throw new UnsupportedSchemaError(pointer('#/$defs', name), `unknown reference "${dep}"`);
      }
    }
  }

  const order: string[] = [];
  const done = new Set<string>();
  while (done.size < exprs.size) {
    const ready = [...exprs.keys()]
      .filter((name) => !done.has(name) && [...(deps.get(name) ?? [])].every((d) => done.has(d)))
      .sort();
    const next = ready[0];
    if (next === undefined) {
      const stuck = [...exprs.keys()].filter((name) => !done.has(name)).sort();
      throw new UnsupportedSchemaError(
        pointer('#/$defs', stuck[0] ?? ''),
        `reference cycle among ${stuck.join(', ')}`,
      );
    }
    done.add(next);
    order.push(next);
  }

  const body = order.map(
    (name) =>
      `export const ${name} = ${exprs.get(name) ?? ''};\nexport type ${name} = z.infer<typeof ${name}>;`,
  );
  const text = body.join('\n\n');
  return `${HEADER}${text.includes('loneSurrogate') ? WELL_FORMED : ''}\n${text}\n`;
}

if (import.meta.main) {
  const input = fileURLToPath(new URL('../../protocol/schema.json', import.meta.url));
  const output = fileURLToPath(new URL('../src/protocol.gen.ts', import.meta.url));
  const schema: unknown = JSON.parse(readFileSync(input, 'utf8'));
  writeFileSync(output, generateZod(schema), { encoding: 'utf8' });
}
