"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Compact markdown renderer for copilot answers (command bar, right
 * rail, etc.). The host project doesn't have @tailwindcss/typography,
 * so we ship just enough CSS to make ###/`**`/lists/tables/code render
 * legibly inside a tight panel. Scoped via `.copilot-md`.
 */
export function CopilotMarkdown({ children }: { children: string }) {
  return (
    <div className="copilot-md text-[13px] leading-relaxed">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
      <style jsx global>{`
        .copilot-md > :first-child {
          margin-top: 0;
        }
        .copilot-md > :last-child {
          margin-bottom: 0;
        }
        .copilot-md p {
          margin: 0.4em 0;
        }
        .copilot-md h1,
        .copilot-md h2,
        .copilot-md h3,
        .copilot-md h4 {
          color: var(--text);
          font-weight: 600;
          line-height: 1.25;
          margin: 0.9em 0 0.35em;
        }
        .copilot-md h1 {
          font-size: 1.15em;
        }
        .copilot-md h2 {
          font-size: 1.05em;
        }
        .copilot-md h3 {
          font-size: 0.98em;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: color-mix(in srgb, var(--text) 85%, transparent);
        }
        .copilot-md h4 {
          font-size: 0.95em;
        }
        .copilot-md strong {
          color: var(--text);
          font-weight: 600;
        }
        .copilot-md em {
          font-style: italic;
        }
        .copilot-md ul,
        .copilot-md ol {
          padding-left: 1.2em;
          margin: 0.35em 0;
        }
        .copilot-md ul {
          list-style: disc;
        }
        .copilot-md ol {
          list-style: decimal;
        }
        .copilot-md li {
          margin: 0.15em 0;
        }
        .copilot-md li > p {
          margin: 0;
        }
        .copilot-md hr {
          border: 0;
          border-top: 1px solid var(--line);
          margin: 0.8em 0;
        }
        .copilot-md a {
          color: var(--accent);
          text-decoration: underline;
          text-underline-offset: 2px;
        }
        .copilot-md blockquote {
          border-left: 2px solid var(--line);
          padding-left: 0.7em;
          color: color-mix(in srgb, var(--text) 75%, transparent);
          margin: 0.5em 0;
        }
        .copilot-md code {
          font-family:
            ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, monospace;
          font-size: 0.88em;
          background: var(--bg-elevated);
          border: 1px solid var(--line);
          padding: 0.05em 0.35em;
          border-radius: 4px;
        }
        .copilot-md pre {
          background: var(--bg-elevated);
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 0.6rem 0.8rem;
          margin: 0.5em 0;
          overflow-x: auto;
        }
        .copilot-md pre code {
          background: transparent;
          border: 0;
          padding: 0;
          font-size: 0.85em;
        }
        .copilot-md table {
          width: 100%;
          border-collapse: collapse;
          margin: 0.5em 0;
          font-size: 0.92em;
        }
        .copilot-md th,
        .copilot-md td {
          border: 1px solid var(--line);
          padding: 0.35em 0.55em;
          text-align: left;
          vertical-align: top;
        }
        .copilot-md th {
          background: var(--bg-elevated);
          font-weight: 600;
          color: var(--text);
        }
      `}</style>
    </div>
  );
}
