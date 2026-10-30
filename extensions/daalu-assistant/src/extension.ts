// Daalu Coding Agent — in-IDE coding assistant.
//
// Activation:
//   * Registers the ChatViewProvider on the activity-bar sidebar.
//   * Best-effort moves the view to the right (secondary side bar) so
//     it sits where users expect a chat, leaving the editor area for
//     code. VS Code has no declarative API for this, so we drive it
//     with workbench commands at runtime and degrade gracefully (the
//     view simply stays in the activity bar if the build rejects them).
//   * Registers commands so the user can re-open the panel, move it to
//     the right manually, or force-reload the service token.
//
// Everything that talks to the network or touches the filesystem lives
// in the supporting modules (InferenceClient, Tools); this file is
// just wiring.

import * as vscode from 'vscode';
import { ChatViewProvider } from './chatProvider';
import { InferenceClient } from './inferenceClient';
import { Tools } from './tools';

const CONTAINER = 'workbench.view.extension.daaluAssistant';

// Candidate workbench commands that relocate the active view container
// to the secondary side bar. The id has changed across VS Code
// versions and isn't a stable API, so we try each and stop at the
// first that doesn't throw. If none exist the view stays put — the
// user can still drag it, and the manual "Move Chat to Secondary Side
// Bar" command surfaces a hint.
const MOVE_RIGHT_CANDIDATES = [
  'workbench.action.moveFocusedViewToSecondarySideBar',
  'workbench.action.moveViewToSecondarySideBar',
  'workbench.action.moveFocusedViewToAuxiliaryBar',
];

async function tryExec(command: string, ...args: unknown[]): Promise<boolean> {
  try {
    await vscode.commands.executeCommand(command, ...args);
    return true;
  } catch {
    return false;
  }
}

/**
 * Best-effort: open our container and shove it into the secondary side
 * bar. Returns true if a relocate command succeeded. Never throws.
 */
async function openOnRight(out: vscode.OutputChannel): Promise<boolean> {
  await tryExec(CONTAINER);            // reveal/focus our view first
  await tryExec('daaluAssistant.chat.focus');
  for (const cmd of MOVE_RIGHT_CANDIDATES) {
    if (await tryExec(cmd)) {
      out.appendLine(`[daalu] moved chat to secondary side bar via ${cmd}`);
      return true;
    }
  }
  out.appendLine('[daalu] no supported move-to-secondary-side-bar command; chat left in activity bar');
  return false;
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel('Daalu Coding Agent');
  output.appendLine('[daalu] activating extension');

  const client = new InferenceClient(output);
  const tools = new Tools(output);
  const provider = new ChatViewProvider(context.extensionUri, client, tools, output);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('daaluAssistant.chat', provider, {
      // Keep the chat history alive when the user collapses the side
      // panel. Without this, every collapse loses the conversation.
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand('daalu.openChat', () => {
      vscode.commands.executeCommand(CONTAINER);
    }),
    vscode.commands.registerCommand('daalu.moveToRight', async () => {
      const ok = await openOnRight(output);
      if (!ok) {
        vscode.window.showInformationMessage(
          'Daalu Coding Agent: this VS Code build has no command to move a view to the right. ' +
          'Drag the Daalu icon into the secondary side bar (the panel on the right) to dock it there.',
        );
      }
    }),
    vscode.commands.registerCommand('daalu.reload', () => {
      client.reloadToken();
      vscode.window.showInformationMessage('Daalu Coding Agent token reloaded.');
    }),
    output,
  );

  // Auto-place on the right on startup (opt-out via daalu.autoOpenRight).
  // Deferred so the view provider + workbench are fully registered first;
  // running the move too early is a no-op.
  const autoOpenRight = vscode.workspace.getConfiguration('daalu').get<boolean>('autoOpenRight') ?? true;
  if (autoOpenRight) {
    setTimeout(() => { void openOnRight(output); }, 1500);
  }

  output.appendLine('[daalu] activation complete');
}

export function deactivate(): void {
  // No-op — webview retains context when hidden; on full window
  // shutdown VS Code disposes our subscriptions automatically.
}
