// Node suites that do not run on Deno, relative to `node-integration-tests`. A single test that
// fails on Deno is skipped with `test.skipIf` on `RUNTIME` in the Node suite, not listed here.

// Node-only features: ANR and native thread watchdogs, child process and worker thread breadcrumbs.
const NODE_ONLY = ['suites/anr/test.ts', 'suites/breadcrumbs/**', 'suites/thread-blocked-native/test.ts'];

// Deno's `fetch` does not use undici, so `nativeNodeFetchIntegration` patches the global `fetch`
// there. That fallback does not support `requestHook`, `responseHook` and `headersToSpanAttributes`,
// and its breadcrumb and span data differ from the undici instrumentation in these suites.
const FETCH_FALLBACK_DIFFERENCES = [
  'suites/tracing/http-client-spans/fetch-forward-request-hook/test.ts',
  'suites/tracing/http-client-spans/fetch-headers-to-span-attributes/test.ts',
  'suites/tracing/http-client-spans/fetch-strip-query/test.ts',
  'suites/tracing/ignoreSpans-streamed/continued-trace-http-client/test.ts',
  'suites/tracing/requests/fetch-breadcrumbs/test.ts',
  'suites/tracing/requests/fetch-no-trace-propagation/test.ts',
  'suites/tracing/requests/fetch-sampled-no-active-span/test.ts',
];

// In the ESM tests Deno cannot find `PrismaClient`, a CommonJS export of `@prisma/client`.
const PRISMA_ESM_INTEROP = ['suites/tracing/prisma-orm-v5/test.ts', 'suites/tracing/prisma-orm-v6/test.ts'];

// In the CJS tests Deno cannot `require()` a dependency that ships only ES modules: `graphql` 17,
// and `escape-string-regexp` under `mastra`. The ESM tests of `mastra` also check `fetch` spans.
const REQUIRE_OF_ESM_ONLY_DEPENDENCY = ['suites/tracing/graphql-tracing-channel/**', 'suites/tracing/mastra/test.ts'];

// Some or all tests fail on Deno, cause not investigated yet. In most AI suites the span
// streaming test fails. `apollo-graphql` (CJS tests only) and `mongodb` fail on Deno 2.8.3 (the CI
// version) and pass on Deno 2.9.0.
const NOT_TRIAGED = [
  'suites/tracing/anthropic/test.ts',
  'suites/tracing/apollo-graphql/**',
  'suites/tracing/fastify/test.ts',
  'suites/tracing/flue/test.ts',
  'suites/tracing/google-genai/test.ts',
  'suites/tracing/groq/test.ts',
  'suites/tracing/http-client-spans/http-strip-query/test.ts',
  'suites/tracing/ioredis-dc/test.ts',
  'suites/tracing/koa/test.ts',
  'suites/tracing/langchain/test.ts',
  'suites/tracing/langgraph/test.ts',
  'suites/tracing/mistral/test.ts',
  'suites/tracing/mongodb/test.ts',
  'suites/tracing/mongoose-v5/test.ts',
  'suites/tracing/mysql/test.ts',
  'suites/tracing/openai/test.ts',
  'suites/tracing/openai/v7/test.ts',
  'suites/tracing/orchestrion-lazy-registration/test.ts',
  'suites/tracing/prisma-orm-v7/test.ts',
  'suites/tracing/together-ai/test.ts',
  'suites/tracing/vercelai/v6_v7/test.ts',
];

// Passes on Deno when run alone, but failed in about 1 of 3 full runs of this package.
const FLAKY = ['suites/tracing/tracePropagationTargets/**'];

export const NODE_SUITES_EXCLUDE = [
  '**/node_modules/**',
  ...NODE_ONLY,
  ...FETCH_FALLBACK_DIFFERENCES,
  ...PRISMA_ESM_INTEROP,
  ...REQUIRE_OF_ESM_ONLY_DEPENDENCY,
  ...NOT_TRIAGED,
  ...FLAKY,
];
