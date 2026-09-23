import * as nodeDiagnosticsChannel from 'node:diagnostics_channel';
import { describe, expect, it } from 'vitest';
import { channel, subscribe, tracingChannel } from '../../src/utils/diagnosticsChannel';

describe('diagnosticsChannel', () => {
  it('returns the same channel object for a name', () => {
    expect(channel('sentry-test:channel')).toBe(channel('sentry-test:channel'));
  });

  it('returns the same tracing channel object for a name', () => {
    expect(tracingChannel('sentry-test:tracing')).toBe(tracingChannel('sentry-test:tracing'));
  });

  it('delivers messages to a handler subscribed by name', () => {
    const messages: unknown[] = [];
    subscribe('sentry-test:subscribe', message => messages.push(message));

    nodeDiagnosticsChannel.channel('sentry-test:subscribe').publish('hello');

    expect(messages).toEqual(['hello']);
  });

  it('delivers tracing events to subscribers of a tracing channel created by name', () => {
    const events: string[] = [];
    tracingChannel('sentry-test:tracing-events').subscribe({
      start: () => events.push('start'),
      end: () => events.push('end'),
      asyncStart: () => undefined,
      asyncEnd: () => undefined,
      error: () => undefined,
    });

    nodeDiagnosticsChannel.tracingChannel('sentry-test:tracing-events').traceSync(() => undefined, {});

    expect(events).toEqual(['start', 'end']);
  });

  it('creates a tracing channel from channel objects without caching it', () => {
    const channels = {
      start: nodeDiagnosticsChannel.channel('sentry-test:objects:start'),
      end: nodeDiagnosticsChannel.channel('sentry-test:objects:end'),
      asyncStart: nodeDiagnosticsChannel.channel('sentry-test:objects:asyncStart'),
      asyncEnd: nodeDiagnosticsChannel.channel('sentry-test:objects:asyncEnd'),
      error: nodeDiagnosticsChannel.channel('sentry-test:objects:error'),
    };

    expect(tracingChannel(channels).start).toBe(channels.start);
  });
});
