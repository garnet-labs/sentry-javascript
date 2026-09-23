import { afterEach, describe, expect, it, vi } from 'vitest';

const { instrumentUndici } = vi.hoisted(() => ({ instrumentUndici: vi.fn() }));

vi.mock('../../src/integrations/node-fetch/undici-instrumentation', () => ({ instrumentUndici }));

import { nativeNodeFetchIntegration } from '../../src/integrations/node-fetch';

describe('nativeNodeFetchIntegration', () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    delete (globalThis as { Deno?: unknown }).Deno;
    globalThis.fetch = originalFetch;
    instrumentUndici.mockClear();
  });

  it('instruments undici on Node', () => {
    nativeNodeFetchIntegration().setupOnce?.();

    expect(instrumentUndici).toHaveBeenCalledTimes(1);
    expect(globalThis.fetch).toBe(originalFetch);
  });

  // Bun's and Deno's `fetch` do not use undici, so its diagnostics channels never fire there.
  it('patches the global `fetch` instead of instrumenting undici on Deno', () => {
    (globalThis as { Deno?: unknown }).Deno = {};

    nativeNodeFetchIntegration().setupOnce?.();

    expect(instrumentUndici).not.toHaveBeenCalled();
    expect(globalThis.fetch).not.toBe(originalFetch);
  });
});
