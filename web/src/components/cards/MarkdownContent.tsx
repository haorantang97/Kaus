import { Fragment, useMemo, type ReactNode } from "react";
import { marked, type Token, type Tokens } from "marked";

import { useLocale } from "../../i18n";
import { localArtifactPath, inlineImageUrl, remoteArtifactUrl } from "../../lib/artifactApi";
import { safeUrl } from "../../lib/md";
import { FileReference } from "./ArtifactPreview";
import { CopyButton } from "./CopyButton";
import { useContentText } from "./contentStrings";
import "./conversationContent.css";

/** Decode only to text; this value is always escaped again by React. */
function decodeText(value: string): string {
  const node = document.createElement("textarea");
  node.innerHTML = value;
  return node.value;
}

function CodeBlock({ text, language }: { text: string; language?: string }) {
  const c = useContentText();
  const { t } = useLocale();
  return <div className="kaus-code-block"><div className="kaus-code-header"><span>{language?.split(/\s+/)[0] || ""}</span><CopyButton text={text} label={c("chat.content.copyCode")} copiedLabel={t("conversation.action.copied")} /></div><pre><code>{text}</code></pre></div>;
}

export function MarkdownContent({ text, conversationId }: { text: string; conversationId?: string }) {
  // The parser only tokenizes. Raw HTML is rendered as text; it never enters innerHTML.
  const tokens = useMemo(() => marked.lexer(text, { gfm: true, breaks: true }), [text]);
  const render = (items: Token[]): ReactNode => items.map((token, index) => {
    const key = `${token.type}:${index}`;
    const childTokens: Token[] | undefined = "tokens" in token ? token.tokens : undefined;
    const inline = () => render(childTokens || []);
    switch (token.type) {
      case "space": case "def": case "checkbox": return null;
      case "paragraph": return <p key={key}>{inline()}</p>;
      case "text": return <Fragment key={key}>{token.tokens ? inline() : decodeText(token.text || "")}</Fragment>;
      case "escape": return <Fragment key={key}>{decodeText(token.text || "")}</Fragment>;
      case "strong": return <strong key={key}>{inline()}</strong>;
      case "em": return <em key={key}>{inline()}</em>;
      case "del": return <del key={key}>{inline()}</del>;
      case "codespan": return <code key={key}>{token.text || ""}</code>;
      case "code": return <CodeBlock key={key} text={token.text} language={token.lang} />;
      case "br": return <br key={key} />;
      case "hr": return <hr key={key} />;
      case "heading": {
        const depth = Math.min(6, Math.max(1, token.depth));
        const Tag = `h${depth}` as "h1";
        return <Tag key={key}>{inline()}</Tag>;
      }
      case "blockquote": return <blockquote key={key}>{inline()}</blockquote>;
      case "list": {
        const list = token as Tokens.List;
        const children = list.items.map((item, itemIndex) => <li key={itemIndex} className={item.task ? "kaus-task-list-item" : undefined}>{item.task && <input type="checkbox" checked={!!item.checked} readOnly disabled aria-label={item.text} />}{render(item.tokens)}</li>);
        return list.ordered ? <ol key={key} start={typeof list.start === "number" ? list.start : undefined}>{children}</ol> : <ul key={key}>{children}</ul>;
      }
      case "table": {
        const table = token as Tokens.Table;
        return <div className="kaus-markdown-table" key={key} tabIndex={0}><table><thead><tr>{table.header.map((cell, column) => <th key={column} style={{ textAlign: table.align[column] || undefined }}>{render(cell.tokens)}</th>)}</tr></thead><tbody>{table.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, column) => <td key={column} style={{ textAlign: table.align[column] || undefined }}>{render(cell.tokens)}</td>)}</tr>)}</tbody></table></div>;
      }
      case "link": {
        const href = decodeText(token.href || "");
        if (localArtifactPath(href)) return <FileReference key={key} source={{ uri: href, title: token.text }} conversationId={conversationId} compact>{inline()}</FileReference>;
        const url = safeUrl(href);
        return <a key={key} href={url || "#"} target={url?.startsWith("#") ? undefined : "_blank"} rel="noopener noreferrer" referrerPolicy="no-referrer" onClick={url ? undefined : (event) => event.preventDefault()}>{inline()}</a>;
      }
      case "image": {
        const uri = decodeText(token.href || "");
        if (!localArtifactPath(uri) && !remoteArtifactUrl(uri) && !inlineImageUrl(uri)) return <span key={key}>{token.text || token.raw}</span>;
        return <FileReference key={key} source={{ uri, title: token.text || undefined, mimeType: localArtifactPath(uri) ? undefined : "image/png" }} conversationId={conversationId} />;
      }
      case "html": return <Fragment key={key}>{token.text || token.raw}</Fragment>;
      default: return <Fragment key={key}>{childTokens ? inline() : token.raw}</Fragment>;
    }
  });
  return <div className="kaus-markdown-content">{render(tokens)}</div>;
}
