// Webview provider for the chat sidebar + the agent loop.
//
// We use a vanilla HTML/JS webview (no React) so the bundle stays
// small and the build is just `tsc`. The webview communicates with
// the extension host via postMessage:
//
//   webview → ext: { type: 'send', text }
//                 { type: 'apply', write: ProposedWrite }
//                 { type: 'run', command }
//                 { type: 'attach', path } | { type: 'reset' }
//
//   ext → webview: { type: 'user', text }
//                 { type: 'assistant', text, promptTokens, completionTokens, model }
//                 { type: 'tool', name, summary }            (read-only activity)
//                 { type: 'propose-write', filePath, language, content }
//                 { type: 'propose-run', command }
//                 { type: 'system'|'context'|'error', text }
//
// Agent loop: the served model supports OpenAI function-calling
// (vLLM --enable-auto-tool-choice). Each round we send the history +
// tool specs; the model either answers in natural language (done) or
// emits tool_calls. Read-only tools run immediately and feed their
// output back for another round (bounded by daalu.maxToolIterations).
// Mutating tools (write_file/run_command) are NOT executed — they are
// surfaced as Apply/Run approval cards and the turn ends so the human
// stays in control of every change.

import * as vscode from 'vscode';
import {
  InferenceClient,
  ChatMessage,
  ToolCall,
  ToolSpec,
} from './inferenceClient';
import { Tools, ProposedWrite } from './tools';

const READONLY_TOOLS = new Set(['read_file', 'list_dir', 'search_files', 'repo_map']);

const TOOL_SPECS: ToolSpec[] = [
  {
    type: 'function',
    function: {
      name: 'read_file',
      description:
        'Read a UTF-8 text file from the workspace (truncated to a byte cap). ' +
        'Use this to inspect code before answering or proposing changes.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Workspace-relative file path.' },
        },
        required: ['path'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'list_dir',
      description: 'List one directory level in the workspace (directories first).',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Workspace-relative dir path. "." for root.' },
        },
        required: ['path'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'search_files',
      description:
        'Case-insensitive substring search across workspace files. Returns ' +
        'path:line: text matches (bounded). Optionally narrow with a glob.',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: 'Substring to search for.' },
          glob: { type: 'string', description: 'Optional glob to narrow files, e.g. "**/*.py".' },
        },
        required: ['query'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'repo_map',
      description:
        'Return a compact map of the repository: the file tree plus the top-level ' +
        'symbols of each code file. Call this first for orientation in an unfamiliar repo.',
      parameters: { type: 'object', properties: {} },
    },
  },
  {
    type: 'function',
    function: {
      name: 'write_file',
      description:
        'Propose writing/replacing a workspace file. This does NOT write immediately — ' +
        'the user sees an Apply button with a diff and approves it. Provide the FULL new file content.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Workspace-relative file path.' },
          content: { type: 'string', description: 'The complete new file contents.' },
          language: { type: 'string', description: 'Language id for the diff view (optional).' },
        },
        required: ['path', 'content'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'run_command',
      description:
        'Propose a shell command. This does NOT run immediately — the user sees a Run ' +
        'button and approves it. Use for builds, tests, git status, etc.',
      parameters: {
        type: 'object',
        properties: {
          command: { type: 'string', description: 'The shell command to propose.' },
        },
        required: ['command'],
      },
    },
  },
];

const SYSTEM_PROMPT = `You are Daalu Coding Agent, an in-IDE coding agent running on the user's own GPU (or the Daalu-hosted pool).

You have tools. Use them — you start with NO knowledge of the workspace contents:
  * read_file, list_dir, search_files, repo_map — inspect the workspace. Call repo_map or list_dir first to orient yourself, then read the specific files you need before answering or editing.
  * write_file — propose a change. It is NOT applied automatically; the user reviews a diff and clicks Apply. Always pass the COMPLETE new file content, not a fragment.
  * run_command — propose a shell command (build/test/git). It is NOT run automatically; the user clicks Run.

Rules:
  * Prefer reading real files over guessing. Don't claim a file's contents without reading it.
  * Make minimal, targeted edits that match the surrounding code style.
  * You cannot reach the public internet or push to a remote. Your only network is the inference API itself.
  * Only read or modify files inside the open workspace.
  * When you're done, give a short plain-language summary of what you found or proposed.`;

export class ChatViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private history: ChatMessage[] = [];
  private inFlight = false;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly client: InferenceClient,
    private readonly tools: Tools,
    private readonly out: vscode.OutputChannel,
  ) {
    this.history.push({ role: 'system', content: SYSTEM_PROMPT });
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'webview')],
    };
    view.webview.html = this.renderHtml(view.webview);
    view.webview.onDidReceiveMessage((msg) => this.onMessage(msg));
  }

  private async onMessage(msg: { type: string; [k: string]: unknown }): Promise<void> {
    try {
      switch (msg.type) {
        case 'send':
          await this.handleSend(String(msg.text ?? ''));
          break;
        case 'apply':
          await this.tools.applyWrite(msg.write as ProposedWrite);
          this.post({ type: 'system', text: `Applied: ${(msg.write as ProposedWrite).filePath}` });
          break;
        case 'run':
          this.tools.runInTerminal(String(msg.command ?? ''));
          break;
        case 'attach':
          await this.handleAttach(String(msg.path ?? ''));
          break;
        case 'reset':
          this.history = [this.history[0]]; // keep system prompt
          this.post({ type: 'system', text: 'Conversation reset.' });
          break;
        default:
          this.out.appendLine(`[daalu] unknown webview message: ${msg.type}`);
      }
    } catch (err) {
      this.post({ type: 'error', text: String((err as Error).message ?? err) });
    }
  }

  private get toolsEnabled(): boolean {
    return vscode.workspace.getConfiguration('daalu').get<boolean>('toolsEnabled') ?? true;
  }

  private get maxToolIterations(): number {
    return vscode.workspace.getConfiguration('daalu').get<number>('maxToolIterations') ?? 8;
  }

  private get maxReadBytes(): number {
    return vscode.workspace.getConfiguration('daalu').get<number>('maxReadBytes') ?? 16_000;
  }

  private get maxSearchResults(): number {
    return vscode.workspace.getConfiguration('daalu').get<number>('maxSearchResults') ?? 60;
  }

  private async handleSend(text: string): Promise<void> {
    text = text.trim();
    if (!text || this.inFlight) return;
    this.inFlight = true;
    this.history.push({ role: 'user', content: text });
    this.post({ type: 'user', text });
    try {
      if (this.toolsEnabled) {
        await this.runAgentLoop();
      } else {
        const r = await this.client.complete(this.history);
        this.history.push({ role: 'assistant', content: r.text });
        this.postAssistant(r.text, r);
      }
    } finally {
      this.inFlight = false;
    }
  }

  /**
   * The bounded tool-calling loop. Returns when the model gives a
   * tool-free answer, when it proposes a mutating action (human gate),
   * or when the iteration budget is spent.
   */
  private async runAgentLoop(): Promise<void> {
    for (let i = 0; i < this.maxToolIterations; i++) {
      const r = await this.client.complete(this.history, TOOL_SPECS);
      // Record the assistant turn verbatim (content + any tool_calls) so
      // the protocol stays valid for the follow-up tool messages.
      this.history.push({
        role: 'assistant',
        content: r.text || null,
        tool_calls: r.toolCalls.length ? r.toolCalls : undefined,
      });
      if (r.text) this.postAssistant(r.text, r);

      if (r.toolCalls.length === 0) return; // natural-language answer → done

      let proposedMutation = false;
      for (const tc of r.toolCalls) {
        const result = await this.handleToolCall(tc);
        this.history.push({
          role: 'tool',
          tool_call_id: tc.id,
          name: tc.function.name,
          content: result.toolResult,
        });
        if (result.mutating) proposedMutation = true;
      }

      // A proposed write/command needs the human; stop the auto-loop so
      // the model doesn't reason on top of an unapproved change.
      if (proposedMutation) {
        this.post({
          type: 'system',
          text: 'Review the proposed change(s) above — Apply/Run to proceed, then continue the chat.',
        });
        return;
      }
    }
    // Budget exhausted — force a final tool-free answer.
    this.history.push({
      role: 'user',
      content: 'Tool budget exhausted. Give your best final answer now without calling more tools.',
    });
    const final = await this.client.complete(this.history);
    this.history.push({ role: 'assistant', content: final.text });
    this.postAssistant(final.text, final);
  }

  /**
   * Execute (read-only) or surface (mutating) a single tool call.
   * Returns the string fed back to the model as the tool result, and
   * whether it was a mutation (which ends the auto-loop).
   */
  private async handleToolCall(
    tc: ToolCall,
  ): Promise<{ toolResult: string; mutating: boolean }> {
    const name = tc.function.name;
    let args: Record<string, unknown> = {};
    try {
      args = tc.function.arguments ? JSON.parse(tc.function.arguments) : {};
    } catch {
      return { toolResult: `error: tool ${name} got invalid JSON arguments`, mutating: false };
    }

    try {
      switch (name) {
        case 'read_file': {
          const p = String(args.path ?? '');
          this.post({ type: 'tool', name, summary: p });
          return { toolResult: await this.tools.readForTool(p, this.maxReadBytes), mutating: false };
        }
        case 'list_dir': {
          const p = String(args.path ?? '.');
          this.post({ type: 'tool', name, summary: p });
          return { toolResult: await this.tools.listDir(p), mutating: false };
        }
        case 'search_files': {
          const q = String(args.query ?? '');
          const glob = args.glob ? String(args.glob) : undefined;
          this.post({ type: 'tool', name, summary: glob ? `${q}  (${glob})` : q });
          return {
            toolResult: await this.tools.searchFiles(q, glob, this.maxSearchResults),
            mutating: false,
          };
        }
        case 'repo_map': {
          this.post({ type: 'tool', name, summary: '' });
          return { toolResult: await this.tools.repoMap(), mutating: false };
        }
        case 'write_file': {
          const filePath = String(args.path ?? '');
          const content = String(args.content ?? '');
          const language = String(args.language ?? '');
          this.post({ type: 'propose-write', filePath, language, content });
          // Auto-open a read-only red/green diff so the user sees exactly what
          // changes (current ↔ proposed) before clicking Apply — not a bare
          // code block. Best-effort: a diff failure must not break the turn.
          try {
            await this.tools.previewWrite({ filePath, language, newContent: content });
          } catch (err) {
            this.out.appendLine(`[daalu] preview diff failed: ${err}`);
          }
          return {
            toolResult: `Proposed write to ${filePath} (${content.length} chars). A red/green diff was opened for the user, who reviews it and clicks Apply (pending approval).`,
            mutating: true,
          };
        }
        case 'run_command': {
          const command = String(args.command ?? '');
          this.post({ type: 'propose-run', command });
          return {
            toolResult: `Proposed command shown to the user as a Run card (pending approval): ${command}`,
            mutating: true,
          };
        }
        default:
          return { toolResult: `error: unknown tool ${name}`, mutating: false };
      }
    } catch (err) {
      return { toolResult: `error running ${name}: ${(err as Error).message ?? err}`, mutating: false };
    }
  }

  private postAssistant(
    text: string,
    r: { promptTokens: number; completionTokens: number; model: string },
  ): void {
    this.post({
      type: 'assistant',
      text,
      promptTokens: r.promptTokens,
      completionTokens: r.completionTokens,
      model: r.model,
    });
  }

  private async handleAttach(filePath: string): Promise<void> {
    const content = await this.tools.readFile(filePath);
    const wrapped =
      `User attached \`${filePath}\` to the conversation:\n\n` +
      '```\n' + content.slice(0, 16_000) + '\n```';
    this.history.push({ role: 'user', content: wrapped });
    this.post({ type: 'context', filePath, snippetLength: content.length });
  }

  private post(msg: object): void {
    this.view?.webview.postMessage(msg);
  }

  private renderHtml(webview: vscode.Webview): string {
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, 'webview', 'chat.js'),
    );
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, 'webview', 'chat.css'),
    );
    // Strict CSP — only same-origin scripts/styles, no inline.
    const nonce = Math.random().toString(36).slice(2);
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; img-src ${webview.cspSource} https: data:;
                 script-src 'nonce-${nonce}' ${webview.cspSource};
                 style-src ${webview.cspSource} 'unsafe-inline';
                 font-src ${webview.cspSource};">
  <link rel="stylesheet" href="${styleUri}" />
  <title>Daalu Coding Agent</title>
</head>
<body>
  <div id="messages" aria-live="polite"></div>
  <form id="composer">
    <textarea id="input" rows="3" placeholder="Ask Daalu about your code… (Cmd/Ctrl+Enter to send)"></textarea>
    <div class="actions">
      <button type="button" id="attach-btn" title="Attach a file from the workspace">📎 Attach</button>
      <button type="button" id="reset-btn" title="Reset conversation">↺ Reset</button>
      <button type="submit" id="send-btn">Send</button>
    </div>
  </form>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}
