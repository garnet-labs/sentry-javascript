import type { IntegrationFn } from '@sentry/core';
import { createFetchIntegration, debug, defineIntegration, getClient, hasSpansEnabled } from '@sentry/core';
import { DEBUG_BUILD } from '../../debug-build';
import type { NodeClientOptions } from '../../types';
import type { NodeFetchOptions } from './types';
import { instrumentUndici } from './undici-instrumentation';

// Created once, because it holds the in-flight spans that every instance of the integration shares.
const globalFetchIntegration = createFetchIntegration({
  name: 'NodeFetch',
  spanOrigin: 'auto.http.node_fetch',
});

/**
 * Bun's and Deno's `fetch` do not use undici, so the undici diagnostics channels never fire there.
 * On these runtimes the integration patches the global `fetch` instead.
 */
function isFetchWithoutUndici(): boolean {
  const globalAny = globalThis as { Bun?: unknown; Deno?: unknown };
  return typeof globalAny.Bun !== 'undefined' || typeof globalAny.Deno !== 'undefined';
}

const _nativeNodeFetchIntegration = ((options: NodeFetchOptions = {}) => {
  const { ignoreOutgoingRequests, spans } = options;
  const fetchFallback = globalFetchIntegration({
    breadcrumbs: options.breadcrumbs,
    tracePropagation: options.tracePropagation,
    shouldCreateSpanForRequest: url => spans !== false && !ignoreOutgoingRequests?.(url),
  });

  return {
    name: 'NodeFetch' as const,
    setupOnce() {
      if (isFetchWithoutUndici()) {
        if (DEBUG_BUILD && (options.requestHook || options.responseHook || options.headersToSpanAttributes)) {
          debug.warn(
            '[NodeFetch] `requestHook`, `responseHook` and `headersToSpanAttributes` are not supported on Bun and Deno.',
          );
        }
        fetchFallback.setupOnce?.();
        return;
      }

      const clientOptions = getClient()?.getOptions();
      instrumentUndici({
        ...options,
        spans: _shouldInstrumentSpans(options, clientOptions),
      });
    },
    setup(client) {
      if (isFetchWithoutUndici()) {
        fetchFallback.setup?.(client);
      }
    },
  };
}) satisfies IntegrationFn;

/**
 * Instrument outgoing fetch requests made through the native node `fetch` API.
 * This emits (depending on the integration options) spans and breadcrumbs, as well as injecting trace propagation headers into the request.
 */
export const nativeNodeFetchIntegration = defineIntegration(_nativeNodeFetchIntegration);

function _shouldInstrumentSpans(options: NodeFetchOptions, clientOptions: Partial<NodeClientOptions> = {}): boolean {
  // If `spans` is passed in, it takes precedence. Otherwise emit spans whenever tracing is enabled;
  // fetch instrumentation is channel-based and does not depend on a Sentry OpenTelemetry tracer provider.
  return options.spans ?? hasSpansEnabled(clientOptions);
}
