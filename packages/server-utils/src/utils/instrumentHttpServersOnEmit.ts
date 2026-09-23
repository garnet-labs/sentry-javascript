import * as http from 'node:http';
import * as https from 'node:https';

let hasPatched = false;

/**
 * Hands every `node:http` and `node:https` server to `instrumentServer` on its first `'request'`
 * event, by patching `Server.prototype.emit`. This is for runtimes that do not publish the
 * `http.server.request.start` diagnostics channel (Bun, see https://github.com/oven-sh/bun/issues/29586),
 * where a subscription to that channel never runs. The prototype is patched (not `createServer`)
 * because frameworks often create and start their server before `Sentry.init()` runs.
 *
 * `instrumentServer` is expected to install its own `emit` on the server instance, which shadows
 * this patch for all later requests to that server. It patches at most once per process.
 */
export function instrumentHttpServersOnEmit(instrumentServer: (server: http.Server) => void): void {
  if (hasPatched) {
    return;
  }
  hasPatched = true;

  const instrumented = new WeakSet<object>();

  const patchEmitOn = (ServerClass: typeof http.Server): void => {
    // oxlint-disable-next-line typescript/unbound-method
    const originalEmit = ServerClass.prototype.emit;
    ServerClass.prototype.emit = function (this: http.Server, event: string, ...args: unknown[]): boolean {
      if (event === 'request' && !instrumented.has(this)) {
        instrumented.add(this);
        instrumentServer(this);
        // Re-dispatch the in-flight request through the instance `emit` that `instrumentServer` installed.
        return this.emit(event, ...args);
      }
      return originalEmit.call(this, event, ...args) as boolean;
    } as typeof originalEmit;
  };

  patchEmitOn(http.Server);
  // In Bun `https.Server` reuses `http.Server`, but patch it explicitly in case that ever diverges.
  if (https.Server !== http.Server) {
    patchEmitOn(https.Server);
  }
}
