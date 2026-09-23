import * as nodeDiagnosticsChannel from 'node:diagnostics_channel';

// Bun garbage-collects a diagnostics channel that no code references, together with its
// subscribers, so a subscription made at `init()` can stop receiving messages after the next GC.
// Node and Deno keep a channel alive while it has subscribers. These wrappers of the
// `node:diagnostics_channel` functions keep a reference to every channel they return, so the SDK's
// subscriptions stay active on Bun too.

const channelsByName = new Map<string | symbol, nodeDiagnosticsChannel.Channel>();
const tracingChannelsByName = new Map<string, nodeDiagnosticsChannel.TracingChannel>();

/** `diagnostics_channel.channel()`, with the returned channel kept referenced. */
export const channel: typeof nodeDiagnosticsChannel.channel = name => {
  let result = channelsByName.get(name);
  if (!result) {
    result = nodeDiagnosticsChannel.channel(name);
    channelsByName.set(name, result);
  }
  return result;
};

/** `diagnostics_channel.subscribe()`, on a channel kept referenced by {@link channel}. */
export const subscribe: typeof nodeDiagnosticsChannel.subscribe = (name, onMessage) => {
  channel(name).subscribe(onMessage);
};

/**
 * `diagnostics_channel.tracingChannel()`, with the returned tracing channel (and so its five
 * channels) kept referenced when it is created by name. `undefined` where the runtime has no
 * `tracingChannel` (Node < 18.19), like the original export.
 */
export const tracingChannel: typeof nodeDiagnosticsChannel.tracingChannel = nodeDiagnosticsChannel.tracingChannel
  ? (((nameOrChannels: Parameters<typeof nodeDiagnosticsChannel.tracingChannel>[0]) => {
      if (typeof nameOrChannels !== 'string') {
        return nodeDiagnosticsChannel.tracingChannel(nameOrChannels);
      }
      let result = tracingChannelsByName.get(nameOrChannels);
      if (!result) {
        result = nodeDiagnosticsChannel.tracingChannel(nameOrChannels);
        tracingChannelsByName.set(nameOrChannels, result);
      }
      return result;
    }) as typeof nodeDiagnosticsChannel.tracingChannel)
  : (undefined as unknown as typeof nodeDiagnosticsChannel.tracingChannel);
