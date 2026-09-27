import { authorizedFetch } from "./authorizedFetch";

export interface ArtifactSource {
  uri: string;
  title?: string | null;
  mimeType?: string | null;
  sizeBytes?: number | null;
}

export type PreviewKind = "image" | "markdown" | "text" | "html" | "pdf" | "download";

/** Classifying a link is not permission to read it; the API enforces the workspace boundary. */
export function localArtifactPath(uri: string): string | null {
  if (!uri || /[\u0000-\u001f\u007f]/.test(uri)) return null;
  let path = uri;
  if (/^file:/i.test(uri)) {
    try {
      const url = new URL(uri);
      if (url.hostname && url.hostname !== "localhost") return null;
      path = url.pathname;
    } catch { return null; }
  } else if (/^[a-z][a-z\d+.-]*:/i.test(uri) || uri.startsWith("//")) {
    return null;
  } else if (!uri.startsWith("/") && !uri.startsWith("./") && !uri.startsWith("../") && !/^[^?#]+\.[a-z\d]{1,12}(?::\d+)?$/i.test(uri)) {
    return null;
  }
  try { path = decodeURIComponent(path); } catch { return null; }
  // Editor line suffixes identify a position, not a different filesystem path.
  path = path.replace(/(?:#L\d+(?:C\d+)?|:\d+(?::\d+)?)$/, "");
  return /[\u0000-\u001f\u007f]/.test(path) ? null : path;
}

export function artifactName(source: ArtifactSource): string {
  if (source.uri.startsWith("data:")) return source.title || source.mimeType || /^data:([^;,]+)/.exec(source.uri)?.[1] || "file";
  return source.title || localArtifactPath(source.uri)?.split("/").pop() || source.uri.split(/[/?#]/).filter(Boolean).pop() || source.uri;
}

export function previewKind(source: ArtifactSource, responseType = ""): PreviewKind {
  const mime = (source.mimeType || responseType || /^data:([^;,]+)/.exec(source.uri)?.[1] || "").split(";")[0].toLowerCase();
  const name = (localArtifactPath(source.uri) || (source.uri.startsWith("data:") ? source.title || "" : source.uri)).toLowerCase().split(/[?#]/)[0];
  // SVG is text, never inserted into the host document as active markup.
  if (mime === "image/svg+xml" || responseType.startsWith("image/svg+xml") || /\.svg$/.test(name)) return "text";
  if (source.uri.startsWith("data:") && mime === "text/html") return "text";
  if (/\.html?$/.test(name)) return "html";
  if (/^image\/(?:png|jpeg|gif|webp|avif|bmp)$/.test(mime) || /\.(?:png|jpe?g|gif|webp|avif|bmp)$/.test(name)) return "image";
  if (mime === "text/html" || /\.html?$/.test(name)) return "html";
  if (mime === "text/markdown" || /\.(?:md|markdown)$/.test(name)) return "markdown";
  if (mime === "application/pdf" || /\.pdf$/.test(name)) return "pdf";
  if (/^text\//.test(mime) || /^(?:application\/(?:json|xml|javascript|yaml))$/.test(mime) || /\.(?:txt|json|jsonl|csv|tsv|yaml|yml|xml|log|py|js|jsx|ts|tsx|css|sql|sh|rs|go|toml)$/.test(name)) return "text";
  return "download";
}

export function remoteArtifactUrl(uri: string): string | null {
  if (/[\u0000-\u0020\u007f-\u009f]/.test(uri)) return null;
  try {
    const url = new URL(uri);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function inlineImageUrl(uri: string): string | null {
  return uri.length <= 14 * 1024 * 1024 && /^data:image\/(?:png|jpeg|gif|webp|avif);base64,[a-z\d+/=\r\n]+$/i.test(uri) ? uri : null;
}

export function inlineArtifactBlob(uri: string): Blob | null {
  if (uri.length > 14 * 1024 * 1024) return null;
  const match = /^data:([a-z\d.+/-]+);base64,([a-z\d+/=\r\n]*)$/i.exec(uri);
  if (!match) return null;
  try {
    const decoded = atob(match[2]);
    if (decoded.length > 10 * 1024 * 1024) return null;
    return new Blob([Uint8Array.from(decoded, (character) => character.charCodeAt(0))], { type: match[1] });
  } catch { return null; }
}

export async function fetchArtifact(
  conversationId: string,
  source: ArtifactSource,
  options: { signal?: AbortSignal; download?: boolean } = {},
): Promise<Blob> {
  const path = localArtifactPath(source.uri);
  if (!path) throw new Error("Unsupported file reference");
  const params = new URLSearchParams({ path });
  if (options.download) params.set("download", "true");
  const response = await authorizedFetch(`/api/conversations/${encodeURIComponent(conversationId)}/files?${params}`, {
    headers: { Accept: "*/*" }, signal: options.signal, tokenMode: "required",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { error?: { message?: string } } | null;
    throw new Error(payload?.error?.message || `HTTP ${response.status}`);
  }
  return response.blob();
}

/** Keep downloads as downloads even when the source claims to be HTML or SVG. */
export function downloadArtifactBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(new Blob([blob], { type: "application/octet-stream" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.replace(/[\\/\u0000-\u001f]/g, "_") || "download";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** An opaque sandbox plus a leading CSP prevents active content and network requests. */
export function sandboxedHtml(source: string): string {
  // A template stays inert while parsing, including its image/resource elements.
  // Do not use an ordinary detached HTMLDocument that can start resource requests.
  const template = document.createElement("template");
  template.innerHTML = source;
  template.content.querySelectorAll("script,iframe,frame,frameset,object,embed,applet,base,meta,link,form,input,button,textarea,select,svg,math,template,noscript").forEach((node) => node.remove());
  for (const element of template.content.querySelectorAll("*")) {
    for (const attr of Array.from(element.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on") || ["href", "src", "srcset", "action", "formaction", "target", "ping", "download", "background", "poster", "data", "xlink:href"].includes(name)) element.removeAttribute(attr.name);
    }
  }
  const policy = "default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; style-src 'unsafe-inline'; font-src 'none'; form-action 'none'; base-uri 'none'";
  return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${policy}"><meta charset="utf-8"><style>body{font:14px/1.65 system-ui,sans-serif;margin:24px;overflow-wrap:anywhere;color:#242424;background:#fff}img{max-width:100%}pre{white-space:pre-wrap}</style></head><body>${template.innerHTML}</body></html>`;
}
