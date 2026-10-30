// Daalu Assistant webview controller.
//
// Vanilla JS — kept dependency-free so the build is `tsc` only.
// Responsibilities:
//   * Render the message list.
//   * Parse assistant output for:
//       (a) fenced code blocks with `// daalu:write <path>` pragma →
//           render "Apply" button per block.
//       (b) lines starting with "$ " → render "Run" button per line.
//   * Forward user actions (Send, Apply, Run, Attach, Reset) to the
//     extension host via postMessage.

(function () {
  // @ts-ignore — provided by VS Code in webview context
  const vscode = acquireVsCodeApi();
  const messages = document.getElementById('messages');
  const input = document.getElementById('input');
  const form = document.getElementById('composer');
  const sendBtn = document.getElementById('send-btn');
  const attachBtn = document.getElementById('attach-btn');
  const resetBtn = document.getElementById('reset-btn');

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function addMessage(role, text, meta) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    if (role === 'assistant') {
      div.innerHTML = renderAssistant(text);
    } else {
      div.textContent = text;
    }
    if (meta) {
      const m = document.createElement('div');
      m.className = 'meta';
      m.textContent = meta;
      div.appendChild(m);
    }
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function renderAssistant(text) {
    // Split on fenced code blocks. Each block becomes a styled
    // codeblock element with optional Apply button when the first
    // line is "// daalu:write <path>".
    let out = '';
    const blockRe = /```(\w*)\n([\s\S]*?)```/g;
    let lastIdx = 0;
    let m;
    while ((m = blockRe.exec(text)) !== null) {
      const before = text.slice(lastIdx, m.index);
      out += renderProse(before);
      out += renderCodeBlock(m[1] || '', m[2] || '');
      lastIdx = blockRe.lastIndex;
    }
    out += renderProse(text.slice(lastIdx));
    return out;
  }

  function renderProse(text) {
    // Within prose: lines starting with "$ " become run-lines.
    return text
      .split('\n')
      .map((line) => {
        if (line.startsWith('$ ')) {
          const cmd = line.slice(2);
          const id = 'rl' + Math.random().toString(36).slice(2, 8);
          return (
            `<div class="run-line"><code>${escapeHtml(cmd)}</code>` +
            `<button data-action="run" data-cmd="${escapeHtml(cmd)}">Run</button></div>`
          );
        }
        return escapeHtml(line);
      })
      .join('\n');
  }

  function renderCodeBlock(lang, body) {
    // Detect the daalu:write pragma. Accept either "//", "#", or
    // ";" comment leaders so the model can match the language.
    const lines = body.split('\n');
    let writePath = '';
    let contentStart = 0;
    if (lines.length > 0) {
      const m = lines[0].match(/^\s*(?:\/\/|#|;)\s*daalu:write\s+(.+?)\s*$/);
      if (m) {
        writePath = m[1];
        contentStart = 1;
      }
    }
    const content = lines.slice(contentStart).join('\n');
    const label = writePath
      ? `${escapeHtml(lang)} — ${escapeHtml(writePath)}`
      : escapeHtml(lang || 'code');
    const applyBtn = writePath
      ? `<button data-action="apply"
                 data-path="${escapeHtml(writePath)}"
                 data-lang="${escapeHtml(lang)}">Apply</button>`
      : '';
    return (
      `<div class="codeblock">` +
      `  <div class="codeblock-header"><span>${label}</span>${applyBtn}</div>` +
      `  <pre>${escapeHtml(content)}</pre>` +
      `</div>`
    );
  }

  // Read-only tool activity (read_file / list_dir / search_files / repo_map)
  // shown as a compact one-line card so the user can see the agent working.
  const TOOL_ICON = {
    read_file: '📄',
    list_dir: '📁',
    search_files: '🔎',
    repo_map: '🗺️',
  };
  function addToolCard(name, summary) {
    const div = document.createElement('div');
    div.className = 'tool-card';
    const icon = TOOL_ICON[name] || '🔧';
    div.innerHTML =
      `<span class="tool-name">${icon} ${escapeHtml(name)}</span>` +
      (summary ? ` <code>${escapeHtml(summary)}</code>` : '');
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  // A model-proposed file write → Apply card with the full content in a
  // <pre> (the click handler reads it back). The human approves.
  function addProposeWrite(filePath, language, content) {
    const div = document.createElement('div');
    div.className = 'msg assistant';
    const label = `${escapeHtml(language || 'code')} — ${escapeHtml(filePath)}`;
    div.innerHTML =
      `<div class="codeblock">` +
      `<div class="codeblock-header"><span>${label}</span>` +
      `<button data-action="apply" data-path="${escapeHtml(filePath)}" data-lang="${escapeHtml(language)}">Apply</button></div>` +
      `<pre>${escapeHtml(content)}</pre>` +
      `</div>`;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  // A model-proposed shell command → Run card. The human approves.
  function addProposeRun(command) {
    const div = document.createElement('div');
    div.className = 'msg assistant';
    div.innerHTML =
      `<div class="run-line"><code>${escapeHtml(command)}</code>` +
      `<button data-action="run" data-cmd="${escapeHtml(command)}">Run</button></div>`;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function send() {
    const text = input.value;
    if (!text.trim()) return;
    input.value = '';
    vscode.postMessage({ type: 'send', text });
  }

  // Event wiring.
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    send();
  });
  input.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      send();
    }
  });
  resetBtn.addEventListener('click', () => {
    messages.innerHTML = '';
    vscode.postMessage({ type: 'reset' });
  });
  attachBtn.addEventListener('click', () => {
    const path = window.prompt('Workspace-relative file path to attach as context:');
    if (path) vscode.postMessage({ type: 'attach', path });
  });
  messages.addEventListener('click', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    const action = t.dataset.action;
    if (action === 'apply') {
      // Pull the code content out of the sibling <pre>.
      const card = t.closest('.codeblock');
      const pre = card && card.querySelector('pre');
      const newContent = pre ? pre.textContent || '' : '';
      vscode.postMessage({
        type: 'apply',
        write: {
          filePath: t.dataset.path,
          language: t.dataset.lang || '',
          newContent,
        },
      });
    } else if (action === 'run') {
      vscode.postMessage({ type: 'run', command: t.dataset.cmd });
    }
  });

  window.addEventListener('message', (event) => {
    const msg = event.data;
    switch (msg.type) {
      case 'user':
        addMessage('user', msg.text);
        break;
      case 'assistant': {
        const meta = `${msg.model} · ${msg.promptTokens} prompt + ${msg.completionTokens} completion tokens`;
        addMessage('assistant', msg.text, meta);
        break;
      }
      case 'tool':
        addToolCard(msg.name, msg.summary || '');
        break;
      case 'propose-write':
        addProposeWrite(msg.filePath, msg.language || '', msg.content || '');
        break;
      case 'propose-run':
        addProposeRun(msg.command || '');
        break;
      case 'system':
      case 'context':
        addMessage(msg.type, msg.text || `Attached: ${msg.filePath} (${msg.snippetLength} chars)`);
        break;
      case 'error':
        addMessage('error', '⚠️  ' + msg.text);
        break;
    }
  });

  // Greet the user once on first render. The MVP doesn't persist
  // chat history across IDE restarts (the host class keeps it for
  // the lifetime of the extension process); a future iteration
  // would write conversations to /workspace/.daalu/chats/.
  addMessage('system', 'Daalu Coding Agent ready. Ask about your code, or request changes.');
})();
