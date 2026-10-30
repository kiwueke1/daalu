// Tool implementations the assistant can invoke.
//
// The served coding model now supports OpenAI function-calling
// (vLLM --enable-auto-tool-choice, see core/model_catalog.py), so the
// agent loop in chatProvider lets the model call these tools directly
// rather than us regex-parsing text pragmas. Two classes of tool:
//
//   * Read-only (read_file, list_dir, search_files, repo_map) — run
//     immediately inside the loop and feed their output back to the
//     model so it can plan against the real workspace.
//   * Mutating (write_file, run_command) — NEVER executed by the loop.
//     They are surfaced to the user as "Apply"/"Run" approval cards;
//     the human is always the one who commits a change. That gate is a
//     product stance, not a missing feature.
//
// Everything that touches the filesystem or runs a process passes
// through this class so the call surface is auditable in one place.
// Every path is resolved against (and confined to) the workspace root.

import * as path from 'path';
import * as vscode from 'vscode';

export interface ProposedWrite {
  filePath: string;          // workspace-relative
  language: string;          // for syntax-highlight in the diff view
  newContent: string;
}

// Defaults; the read caps are overridable from settings via the caller.
const DEFAULT_MAX_READ_BYTES = 16_000;
const MAX_DIR_ENTRIES = 200;
const MAX_SEARCH_FILES = 400;
const MAX_REPO_MAP_FILES = 600;
const SKIP_DIRS = new Set([
  '.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build',
  '.next', '.cache', 'target', '.mypy_cache', '.pytest_cache', '.idea', '.vscode-test',
]);
const CODE_EXT = new Set([
  '.py', '.ts', '.tsx', '.js', '.jsx', '.go', '.rs', '.java', '.rb', '.php',
  '.c', '.h', '.cpp', '.hpp', '.cs', '.kt', '.swift', '.scala', '.sh', '.yaml', '.yml',
]);
// Top-level symbol shapes for the lightweight repo map (Aider-style, but
// regex not tree-sitter): function/class/method/export/const declarations.
const SYMBOL_RE =
  /^\s*(?:export\s+)?(?:async\s+)?(?:def|class|func|function|interface|type|struct|impl|fn)\s+([A-Za-z_][\w]*)|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][\w]*)\s*=/;

export class Tools {
  private terminal: vscode.Terminal | undefined;

  // Backing store for the proposed-change diff. Both sides of the diff are
  // served as READ-ONLY virtual documents under the `daalu-diff:` scheme, so
  // VS Code renders a real side-by-side red/green diff (no stray editable
  // untitled tabs, nothing written to disk until the user clicks Apply).
  // Keyed by full URI string; a per-open sequence token in the path keeps
  // each diff fresh (VS Code caches content-provider output per URI).
  private readonly diffDocs = new Map<string, string>();
  private diffSeq = 0;

  constructor(private readonly out: vscode.OutputChannel) {
    vscode.workspace.registerTextDocumentContentProvider('daalu-diff', {
      provideTextDocumentContent: (uri) => this.diffDocs.get(uri.toString()) ?? '',
    });
  }

  // ── read-only tools (executed by the agent loop) ────────────────

  /**
   * Read a workspace file as UTF-8, truncated to `maxBytes`. Used both
   * by the read_file tool and when the user @-mentions a file.
   */
  async readForTool(workspaceRelativePath: string, maxBytes = DEFAULT_MAX_READ_BYTES): Promise<string> {
    const uri = this.resolveInsideWorkspace(workspaceRelativePath);
    const bytes = await vscode.workspace.fs.readFile(uri);
    const text = Buffer.from(bytes).toString('utf8');
    if (text.length <= maxBytes) return text;
    return text.slice(0, maxBytes) + `\n…[truncated at ${maxBytes} bytes of ${text.length}]`;
  }

  /** Back-compat alias used by the @-attach path. */
  async readFile(workspaceRelativePath: string): Promise<string> {
    return this.readForTool(workspaceRelativePath, Number.MAX_SAFE_INTEGER);
  }

  /** List one directory level (dirs first), capped. */
  async listDir(workspaceRelativePath: string): Promise<string> {
    const rel = (workspaceRelativePath || '.').trim();
    const uri = this.resolveInsideWorkspace(rel === '.' ? '' : rel);
    const entries = await vscode.workspace.fs.readDirectory(uri);
    const norm = entries
      .filter(([name]) => !SKIP_DIRS.has(name))
      .sort((a, b) => {
        const da = a[1] === vscode.FileType.Directory ? 0 : 1;
        const db = b[1] === vscode.FileType.Directory ? 0 : 1;
        return da - db || a[0].localeCompare(b[0]);
      });
    const shown = norm.slice(0, MAX_DIR_ENTRIES);
    const lines = shown.map(
      ([name, type]) => (type === vscode.FileType.Directory ? `${name}/` : name),
    );
    const header = `${rel || '.'} (${norm.length} entries${norm.length > shown.length ? `, showing ${shown.length}` : ''})`;
    return [header, ...lines].join('\n');
  }

  /**
   * Substring search across workspace files (bounded). Returns
   * `path:line: text` matches. Not a full ripgrep — VS Code exposes no
   * grep API — so we scan a capped set of files. `glob` narrows the set.
   */
  async searchFiles(query: string, glob = '**/*', maxResults = 60): Promise<string> {
    const q = (query || '').trim();
    if (!q) return 'search_files: empty query';
    const exclude = '{' + [...SKIP_DIRS].map((d) => `**/${d}/**`).join(',') + '}';
    const uris = await vscode.workspace.findFiles(glob, exclude, MAX_SEARCH_FILES);
    const needle = q.toLowerCase();
    const hits: string[] = [];
    for (const uri of uris) {
      if (hits.length >= maxResults) break;
      let text: string;
      try {
        const bytes = await vscode.workspace.fs.readFile(uri);
        if (bytes.length > 512_000) continue; // skip very large/binary files
        text = Buffer.from(bytes).toString('utf8');
      } catch {
        continue;
      }
      const rel = this.toRelative(uri);
      const lines = text.split('\n');
      for (let i = 0; i < lines.length && hits.length < maxResults; i++) {
        if (lines[i].toLowerCase().includes(needle)) {
          hits.push(`${rel}:${i + 1}: ${lines[i].trim().slice(0, 200)}`);
        }
      }
    }
    if (hits.length === 0) return `No matches for "${q}".`;
    return `Matches for "${q}" (capped ${maxResults}):\n` + hits.join('\n');
  }

  /**
   * A compact map of the repo: a depth-bounded file tree plus, for code
   * files, the top-level symbols (regex-extracted). Borrowed from
   * Aider's repo-map idea — gives the model orientation without reading
   * every file. Lightweight: no tree-sitter/PageRank ranking.
   */
  async repoMap(maxFiles = MAX_REPO_MAP_FILES): Promise<string> {
    const root = this.workspaceRootOrThrow();
    const exclude = '{' + [...SKIP_DIRS].map((d) => `**/${d}/**`).join(',') + '}';
    const uris = await vscode.workspace.findFiles('**/*', exclude, maxFiles);
    uris.sort((a, b) => this.toRelative(a).localeCompare(this.toRelative(b)));
    const out: string[] = [`repo map for ${path.basename(root.fsPath)} (${uris.length} files):`];
    for (const uri of uris) {
      const rel = this.toRelative(uri);
      const ext = path.extname(rel);
      if (!CODE_EXT.has(ext)) {
        out.push(rel);
        continue;
      }
      const symbols = await this.topSymbols(uri);
      out.push(symbols.length ? `${rel}  ⟦${symbols.slice(0, 12).join(', ')}⟧` : rel);
    }
    return out.join('\n');
  }

  // ── mutating tools (surfaced for user approval, not auto-run) ────

  /**
   * Open a read-only red/green diff of a proposed change WITHOUT writing it.
   * Called the moment the model proposes a write, so the user reviews the
   * actual changes (current ↔ new) before deciding to Apply — the same
   * gesture a coding agent gives you.
   */
  async previewWrite(p: ProposedWrite): Promise<void> {
    const root = this.workspaceRootOrThrow();
    const safePath = this.assertSafePath(p.filePath);
    const target = vscode.Uri.joinPath(root, safePath);
    const before = await this.readBytesOrEmpty(target);
    await this.openDiff(safePath, before, p.newContent);
  }

  /**
   * Apply a proposed write — the "accept" gesture is the UI clicking Apply,
   * so by the time we arrive the user has reviewed the diff (auto-opened at
   * propose time) and consented. This just commits the bytes to disk.
   */
  async applyWrite(p: ProposedWrite): Promise<void> {
    const root = this.workspaceRootOrThrow();
    const safePath = this.assertSafePath(p.filePath);
    const target = vscode.Uri.joinPath(root, safePath);
    await vscode.workspace.fs.writeFile(target, Buffer.from(p.newContent, 'utf8'));
    this.out.appendLine(`[daalu] applyWrite → ${safePath} (${p.newContent.length} chars)`);
  }

  /**
   * Open `vscode.diff` between the file's current content (left, red) and the
   * proposed content (right, green). Both sides are read-only virtual docs;
   * the real basename is kept in the path so the diff gets the right syntax
   * highlighting. Focus stays on the chat so the user can Apply/keep talking.
   */
  private async openDiff(safePath: string, before: Uint8Array, after: string): Promise<void> {
    try {
      const left = this.diffUri(safePath, 'current', Buffer.from(before).toString('utf8'));
      const right = this.diffUri(safePath, 'proposed', after);
      const verb = before.length === 0 ? 'new file' : 'current ↔ proposed';
      await vscode.commands.executeCommand(
        'vscode.diff',
        left,
        right,
        `${path.basename(safePath)} — Daalu (${verb})`,
        { preview: true, preserveFocus: true },
      );
    } catch (err) {
      this.out.appendLine(`[daalu] diff preview failed: ${err}`);
    }
  }

  private diffUri(rel: string, side: 'current' | 'proposed', content: string): vscode.Uri {
    const seq = ++this.diffSeq;
    const uri = vscode.Uri.from({ scheme: 'daalu-diff', path: `/${side}/${seq}/${rel}` });
    this.diffDocs.set(uri.toString(), content);
    // Bound memory over a long session — evict the oldest snapshots.
    while (this.diffDocs.size > 40) {
      const oldest = this.diffDocs.keys().next().value;
      if (!oldest) break;
      this.diffDocs.delete(oldest);
    }
    return uri;
  }

  /**
   * Run a shell command in a dedicated, persistent VS Code terminal.
   * Output is not streamed back into the chat — the terminal is the
   * source of truth.
   */
  runInTerminal(command: string): void {
    if (!this.terminal || this.terminal.exitStatus !== undefined) {
      this.terminal = vscode.window.createTerminal({
        name: 'Daalu',
        cwd: this.workspaceRoot()?.fsPath,
      });
    }
    this.terminal.show(false);
    this.terminal.sendText(command, true);
    this.out.appendLine(`[daalu] runInTerminal → ${command}`);
  }

  // ── helpers ─────────────────────────────────────────────────────

  private workspaceRoot(): vscode.Uri | null {
    const folders = vscode.workspace.workspaceFolders;
    return folders && folders.length > 0 ? folders[0].uri : null;
  }

  private workspaceRootOrThrow(): vscode.Uri {
    const root = this.workspaceRoot();
    if (!root) throw new Error('no workspace open');
    return root;
  }

  private toRelative(uri: vscode.Uri): string {
    const root = this.workspaceRootOrThrow();
    return path.posix.relative(root.path, uri.path);
  }

  private async topSymbols(uri: vscode.Uri): Promise<string[]> {
    try {
      const bytes = await vscode.workspace.fs.readFile(uri);
      if (bytes.length > 256_000) return [];
      const text = Buffer.from(bytes).toString('utf8');
      const names: string[] = [];
      for (const line of text.split('\n')) {
        const m = SYMBOL_RE.exec(line);
        if (m) {
          const name = m[1] || m[2];
          if (name && !names.includes(name)) names.push(name);
        }
        if (names.length >= 12) break;
      }
      return names;
    } catch {
      return [];
    }
  }

  /**
   * Resolve a workspace-relative path to an absolute URI, rejecting any
   * attempt to escape the workspace root. The model can emit anything;
   * we defend at the call site rather than trusting the prompt.
   */
  private resolveInsideWorkspace(rel: string): vscode.Uri {
    const root = this.workspaceRootOrThrow();
    const safe = this.assertSafePath(rel);
    return safe ? vscode.Uri.joinPath(root, safe) : root;
  }

  private assertSafePath(p: string): string {
    const norm = path.posix.normalize((p || '').replace(/^\/+/, ''));
    if (norm === '.' ) return '';
    if (norm.startsWith('..') || norm.includes('/../')) {
      throw new Error(`refusing to access path outside workspace: ${p}`);
    }
    return norm;
  }

  private async readBytesOrEmpty(uri: vscode.Uri): Promise<Uint8Array> {
    try {
      return await vscode.workspace.fs.readFile(uri);
    } catch {
      return new Uint8Array();
    }
  }

}
