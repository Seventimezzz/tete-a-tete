import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const root = fileURLToPath(new URL('../../', import.meta.url));

function prettierCheck(file: string): boolean {
  try {
    execFileSync(process.execPath, ['node_modules/prettier/bin/prettier.cjs', '--check', file], {
      cwd: root,
      stdio: 'pipe',
    });
    return true;
  } catch {
    return false;
  }
}

describe('files the CI client job lints (npm run lint runs prettier --check .)', () => {
  it('protocol/schema.json and the generated zod file are formatted', () => {
    expect(prettierCheck('protocol/schema.json')).toBe(true);
    expect(prettierCheck('client/src/protocol.gen.ts')).toBe(true);
    expect(prettierCheck('protocol/examples.json')).toBe(true);
  });

  it.fails('QA-5: .github/workflows/ci.yml passes prettier --check (double quotes in it)', () => {
    expect(prettierCheck('.github/workflows/ci.yml')).toBe(true);
  });
});
