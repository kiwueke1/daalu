// HTTP client to the Daalu inference-gateway.
//
// The gateway is an OpenAI-compatible front-end with the auth +
// quota + metering wrapper around vLLM. We talk to it the way we'd
// talk to OpenAI:
//
//   POST <gatewayUrl>/v1/chat/completions
//        Authorization: Bearer <service-token JWT>
//        body: { model, messages, max_tokens, tools?, tool_choice? }
//
// Tool-calling: when `tools` are passed we set tool_choice:"auto" and
// the served model (Qwen2.5-Coder via the `hermes` parser, or
// Qwen3-Coder via `qwen3_coder` — see core/model_catalog.py) returns
// OpenAI-style `tool_calls`. The agent loop in chatProvider executes
// the read-only ones and surfaces writes/commands for user approval.
//
// The service-token JWT is minted by the token-minter sidecar and
// written to a file (see config `daalu.tokenFile`). We read the file
// on every request — the kernel page-caches it so the overhead is
// negligible, and we don't need a refresh loop.
//
// Streaming is intentionally not used yet: mixing SSE deltas with
// tool-call accumulation is fiddly and a separate enhancement. The
// loop here is request/response per round.

import * as fs from 'fs';
import * as vscode from 'vscode';

export type Role = 'system' | 'user' | 'assistant' | 'tool';

export interface ToolCall {
  id: string;
  type: 'function';
  function: { name: string; arguments: string }; // arguments is a JSON string
}

export interface ChatMessage {
  role: Role;
  // Assistant turns that only call tools carry null content; tool
  // results carry the tool output as a string.
  content: string | null;
  tool_calls?: ToolCall[]; // present on assistant turns that call tools
  tool_call_id?: string; // present on role:"tool" results
  name?: string; // tool name on role:"tool" results
}

export interface ToolSpec {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, unknown>; // JSON Schema
  };
}

export interface CompletionResult {
  text: string; // assistant natural-language content ('' when only tool calls)
  toolCalls: ToolCall[]; // [] when the model emitted no tool calls
  promptTokens: number;
  completionTokens: number;
  model: string;
  finishReason: string;
}

export class InferenceClient {
  private cachedToken: string | null = null;
  private cachedTokenAt: number = 0;

  constructor(private readonly out: vscode.OutputChannel) {}

  reloadToken(): void {
    this.cachedToken = null;
    this.cachedTokenAt = 0;
  }

  private get gatewayUrl(): string {
    const cfg = vscode.workspace.getConfiguration('daalu').get<string>('gatewayUrl') ?? '';
    return (cfg || process.env.DAALU_GATEWAY_URL || '').replace(/\/+$/, '');
  }

  private get tokenFile(): string {
    return vscode.workspace.getConfiguration('daalu').get<string>('tokenFile')
      ?? '/var/run/daalu/tokens/service.jwt';
  }

  get model(): string {
    // Precedence: explicit setting → DAALU_MODEL (the workspace pod
    // injects this from the model the user picked at create time) →
    // a safe coder default.
    const setting = vscode.workspace.getConfiguration('daalu').get<string>('model');
    return setting
      || process.env.DAALU_MODEL
      || 'qwen/qwen2.5-coder-14b-instruct';
  }

  private get maxTokens(): number {
    return vscode.workspace.getConfiguration('daalu').get<number>('maxTokens') ?? 2048;
  }

  private async readToken(): Promise<string> {
    // 30-second cache. The token-minter writes a new token every 30 s
    // with a 60 s TTL, so this cache + refresh strategy keeps us
    // always carrying a valid token without per-call file IO.
    const now = Date.now();
    if (this.cachedToken && now - this.cachedTokenAt < 30_000) {
      return this.cachedToken;
    }
    try {
      const text = await fs.promises.readFile(this.tokenFile, 'utf8');
      this.cachedToken = text.trim();
      this.cachedTokenAt = now;
      return this.cachedToken;
    } catch (err) {
      throw new Error(
        `Daalu Assistant: cannot read service token from ${this.tokenFile} — ${err}. ` +
        `The token-minter sidecar may not be running. Try the "Daalu: Reload Token" command.`,
      );
    }
  }

  /**
   * One chat-completions round. Pass `tools` to enable function-calling
   * (tool_choice defaults to "auto"); omit them to force a plain
   * natural-language answer (used for the final "budget exhausted" turn).
   */
  async complete(messages: ChatMessage[], tools?: ToolSpec[]): Promise<CompletionResult> {
    const url = this.gatewayUrl;
    if (!url) {
      throw new Error(
        'Daalu Assistant: no gateway URL configured. Set daalu.gatewayUrl or the ' +
        'DAALU_GATEWAY_URL env var (the workspace pod injects this automatically).',
      );
    }
    const token = await this.readToken();
    const body: Record<string, unknown> = {
      model: this.model,
      messages,
      max_tokens: this.maxTokens,
      // No stream:true — streaming is a separate enhancement (mixing SSE
      // deltas with tool-call accumulation is its own piece of work).
    };
    if (tools && tools.length > 0) {
      body.tools = tools;
      body.tool_choice = 'auto';
    }
    this.out.appendLine(
      `[daalu] POST ${url}/v1/chat/completions (model=${this.model}, tools=${tools?.length ?? 0})`,
    );
    const resp = await fetch(`${url}/v1/chat/completions`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      // 401 usually means the token rotated and our cache is stale;
      // bust the cache and let the caller retry once.
      if (resp.status === 401) {
        this.reloadToken();
      }
      throw new Error(`Daalu Assistant: gateway returned ${resp.status} — ${text.slice(0, 200)}`);
    }
    const data = (await resp.json()) as {
      choices: {
        message: { content: string | null; tool_calls?: ToolCall[] };
        finish_reason?: string;
      }[];
      usage?: { prompt_tokens?: number; completion_tokens?: number };
      model?: string;
    };
    const choice = data.choices?.[0];
    const message = choice?.message;
    const rawToolCalls = message?.tool_calls;
    return {
      text: message?.content ?? '',
      toolCalls: Array.isArray(rawToolCalls) ? rawToolCalls : [],
      promptTokens: data.usage?.prompt_tokens ?? 0,
      completionTokens: data.usage?.completion_tokens ?? 0,
      model: data.model ?? this.model,
      finishReason: choice?.finish_reason ?? '',
    };
  }
}
