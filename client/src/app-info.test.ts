import { describe, expect, it } from 'vitest';
import { APP_NAME } from './app-info';

describe('app-info', () => {
  it('names the application as in CLAUDE.md', () => {
    expect(APP_NAME).toBe('Tete-a-tete');
  });
});
