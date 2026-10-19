// Agent worker (`xnb agent`): pi's agent loop next to the kernel.
//
// It has no network. Model requests go through `Tunnel` to the page and the host, which
// adds the API key. Tool calls go to the page, which runs them on the kernel. Only the
// page talks to this worker; the kernel worker cannot reach it.

import { Agent, type AgentEvent } from '@earendil-works/pi-agent-core';
import type { Api, AssistantMessage, Model } from '@earendil-works/pi-ai';
import { streamSimple as anthropicStream } from '@earendil-works/pi-ai/api/anthropic-messages';
import { streamSimple as openaiStream } from '@earendil-works/pi-ai/api/openai-completions';
import { ANTHROPIC_MODELS } from '@earendil-works/pi-ai/providers/anthropic.models';
import { TOOL_GUIDELINES, makeTools } from './agent/tools';
import { TUNNEL_BASE, Tunnel } from './agent/tunnel';
import type { IAgentConfig, IAgentInit } from './types';

const ctx = self as any;
const post = (msg: any) => ctx.postMessage(msg);

const tunnel = new Tunnel(
  req => post({ xnb: 'llm', ...req }),
  reqId => post({ xnb: 'llm-abort', reqId })
);

let nextCall = 0;
const calls = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void }>();

function rpc(op: string, args: Record<string, any>): Promise<any> {
  const id = ++nextCall;
  return new Promise((resolve, reject) => {
    calls.set(id, { resolve, reject });
    post({ xnb: 'call', id, op, args });
  });
}

function emit(event: Record<string, any>): void {
  post({ xnb: 'agent-event', event });
}

const NO_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };

/** The session's model; its baseUrl only means something to the tunnel. */
function makeModel(config: IAgentConfig): Model<Api> {
  if (config.provider === 'anthropic') {
    const known = (ANTHROPIC_MODELS as Record<string, Model<Api>>)[config.model];
    const base: Model<Api> = known ?? {
      id: config.model,
      name: config.model,
      api: 'anthropic-messages',
      provider: 'anthropic',
      baseUrl: TUNNEL_BASE,
      reasoning: true,
      input: ['text', 'image'],
      cost: NO_COST,
      contextWindow: 200_000,
      maxTokens: 64_000
    };
    return {
      ...base,
      baseUrl: TUNNEL_BASE,
      input: config.images ? base.input : ['text'],
      compat: { ...(base.compat as any), ...config.compat }
    } as Model<Api>;
  }
  // Servers other than OpenAI's own rarely support the `developer` role or `store`.
  const compat = config.openaiOfficial ? {} : { supportsDeveloperRole: false, supportsStore: false };
  return {
    id: config.model,
    name: config.model,
    api: 'openai-completions',
    provider: 'openai',
    baseUrl: TUNNEL_BASE,
    reasoning: !!config.effort,
    input: config.images ? ['text', 'image'] : ['text'],
    cost: NO_COST,
    contextWindow: 128_000,
    maxTokens: config.maxTokens ?? 16_384,
    compat: { ...compat, ...config.compat }
  } as Model<Api>;
}

function systemPrompt(init: IAgentInit): string {
  const mounts = init.mounts.length
    ? init.mounts
        .map(m => `- ${m.dst} (${m.mode === 'rw' ? 'read-write: changes are saved to the host' : 'read-only: changes are not saved'})`)
        .join('\n')
    : '- none';
  const lines = [
    `You are an agent working in a sandboxed ${init.kernel.display_name} kernel (WebAssembly, inside a browser).`,
    '',
    'The sandbox:',
    '- No network: code cannot download anything or call any API.',
    '- No subprocesses and no shell: run code with the execute tool instead.',
    `- Installed packages (fixed for this session; nothing else can be installed): ${init.packages.join(', ') || 'none'}.`,
    `- Working directory: ${init.cwd}.`,
    '- Host files are only visible through these mounts:',
    mounts,
    '- Files outside read-write mounts exist only in the kernel and are lost when it restarts.',
    '',
    'Guidelines:',
    ...TOOL_GUIDELINES.map(g => `- ${g}`)
  ];
  if (init.config.system) {
    lines.push('', init.config.system);
  }
  return lines.join('\n');
}

/**
 * Drop a trailing reply that failed or was aborted (and any tool results after it): it was
 * never complete, and pi would send back rewritten partial thinking blocks.
 */
function dropIncomplete(agent: Agent): void {
  const msgs = agent.state.messages;
  let cut = msgs.length;
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m: any = msgs[i];
    if (m.role === 'toolResult') {
      continue;
    }
    if (m.role === 'assistant' && (m.stopReason === 'aborted' || m.stopReason === 'error')) {
      cut = i;
    }
    break;
  }
  if (cut < msgs.length) {
    agent.state.messages = msgs.slice(0, cut);
  }
}

function forward(event: AgentEvent): void {
  switch (event.type) {
    case 'message_update': {
      const e = event.assistantMessageEvent;
      if (e.type === 'text_delta') {
        emit({ kind: 'text', delta: e.delta });
      } else if (e.type === 'thinking_start') {
        emit({ kind: 'thinking' });
      }
      break;
    }
    case 'message_end': {
      const m = event.message as AssistantMessage;
      if (m.role === 'assistant') {
        emit({ kind: 'message', stopReason: m.stopReason, error: m.errorMessage ?? null, usage: m.usage });
      }
      break;
    }
    case 'tool_execution_start':
      emit({ kind: 'tool_start', id: event.toolCallId, name: event.toolName, args: event.args });
      break;
    case 'tool_execution_end': {
      const content = event.result?.content ?? [];
      emit({
        kind: 'tool_end',
        id: event.toolCallId,
        name: event.toolName,
        isError: event.isError,
        seconds: (event.durationMs ?? 0) / 1000,
        text: content
          .filter((c: any) => c.type === 'text')
          .map((c: any) => c.text)
          .join('\n')
          .slice(0, 4000),
        images: content.filter((c: any) => c.type === 'image').map((c: any) => ({ data: c.data, mimeType: c.mimeType })),
        details: event.result?.details ?? null
      });
      break;
    }
  }
}

let agent: Agent | null = null;

function start(init: IAgentInit): void {
  const config = init.config;
  const model = makeModel(config);
  const stream = config.provider === 'anthropic' ? anthropicStream : openaiStream;
  agent = new Agent({
    initialState: {
      systemPrompt: systemPrompt(init),
      model,
      thinkingLevel: (config.effort as any) ?? (config.provider === 'anthropic' ? 'medium' : 'off'),
      tools: makeTools(rpc, init.cwd, config.images)
    },
    streamFn: (m, context, options) =>
      (stream as any)(m, context, {
        ...options,
        apiKey: 'xnb',
        fetch: tunnel.fetch,
        maxTokens: config.maxTokens ?? undefined
      }),
    sessionId: config.sessionId,
    toolExecution: 'sequential'
  });
  agent.subscribe(forward);
}

async function chat(text: string): Promise<void> {
  if (!agent) {
    emit({ kind: 'error', message: 'the agent is not ready' });
    return;
  }
  if (agent.state.isStreaming) {
    emit({ kind: 'error', message: 'the agent is busy' });
    return;
  }
  try {
    await agent.prompt(text);
  } catch (e: any) {
    emit({ kind: 'error', message: String(e?.message ?? e) });
  } finally {
    dropIncomplete(agent);
    emit({ kind: 'turn_end', messages: agent.state.messages.length });
  }
}

ctx.onmessage = (ev: MessageEvent) => {
  const msg = ev.data;
  switch (msg?.xnb) {
    case 'init':
      try {
        start(msg.init);
        post({ xnb: 'agent-ready' });
      } catch (e: any) {
        post({ xnb: 'agent-fatal', message: String(e?.stack ?? e) });
      }
      break;
    case 'chat':
      void chat(msg.text);
      break;
    case 'abort':
      agent?.abort();
      tunnel.abortAll();
      break;
    case 'reset':
      if (agent && !agent.state.isStreaming) {
        agent.reset();
        emit({ kind: 'reset' });
      } else {
        emit({ kind: 'error', message: 'cannot reset while the agent is working' });
      }
      break;
    case 'reply': {
      const c = calls.get(msg.id);
      calls.delete(msg.id);
      if (c) {
        if (msg.error) {
          c.reject(new Error(msg.error));
        } else {
          c.resolve(msg.value);
        }
      }
      break;
    }
    case 'llm-reply':
      tunnel.receive(msg.reply);
      break;
  }
};


post({ xnb: 'loaded' });
