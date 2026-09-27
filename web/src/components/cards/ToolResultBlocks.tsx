import { FileReference } from "./ArtifactPreview";
import { MonoBlock, stringify } from "./CardShell";
import { useContentText } from "./contentStrings";
import type { ArtifactSource } from "../../lib/artifactApi";

export type ToolContentBlock = { kind: "text"; text: string } | { kind: "file"; source: ArtifactSource } | { kind: "unknown"; value: unknown };

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

/** Preserve every content block in mixed tool results; media must not vanish behind text. */
export function toolContentBlocks(value: unknown): ToolContentBlock[] {
  if (value == null || value === "") return [];
  if (typeof value === "string") return [{ kind: "text", text: value }];
  if (Array.isArray(value)) return value.flatMap(toolContentBlocks);
  const item = record(value);
  if (!item) return [{ kind: "unknown", value }];
  if (item.type === "content" && item.content) return toolContentBlocks(item.content);
  if (typeof item.text === "string" && (item.type === "text" || item.type === "output_text")) return [{ kind: "text", text: item.text }];
  if (item.type === "resource" && record(item.resource)) {
    const resource = record(item.resource)!;
    const mimeType = typeof resource.mimeType === "string" ? resource.mimeType : "application/octet-stream";
    if (typeof resource.text === "string") return [{ kind: "text", text: resource.text }];
    const uri = typeof resource.blob === "string" ? `data:${mimeType};base64,${resource.blob}` : resource.uri;
    if (typeof uri === "string") return [{ kind: "file", source: { uri, mimeType, title: typeof resource.name === "string" ? resource.name : typeof resource.uri === "string" ? resource.uri.split("/").pop() : undefined } }];
  }
  if (["image", "audio", "resource_link", "image_url"].includes(String(item.type))) {
    const imageUrl = record(item.image_url);
    const mimeType = typeof item.mimeType === "string" ? item.mimeType : item.type === "image" || item.type === "image_url" ? "image/png" : "application/octet-stream";
    const uri = typeof item.data === "string" ? `data:${mimeType};base64,${item.data}` : item.uri || item.url || (typeof item.image_url === "string" ? item.image_url : imageUrl?.url);
    if (typeof uri === "string") return [{ kind: "file", source: { uri, mimeType, title: typeof item.name === "string" ? item.name : typeof item.title === "string" ? item.title : undefined } }];
    return [{ kind: "unknown", value: { type: item.type, mimeType } }];
  }
  if (Array.isArray(item.content)) {
    const blocks = toolContentBlocks(item.content);
    const extra = Object.fromEntries(Object.entries(item).filter(([key]) => !["content", "isError", "type"].includes(key)));
    return Object.keys(extra).length ? [...blocks, { kind: "unknown", value: extra }] : blocks;
  }
  return [{ kind: "unknown", value }];
}

export function ToolResultBlocks({ value, conversationId }: { value: unknown; conversationId?: string }) {
  const c = useContentText();
  return <div className="kaus-media-blocks">{toolContentBlocks(value).map((block, index) => {
    if (block.kind === "file") return <FileReference key={index} source={block.source} conversationId={conversationId} />;
    if (block.kind === "text") return <MonoBlock key={index} max={320}>{block.text}</MonoBlock>;
    const unknown = record(block.value);
    if (unknown && ["image", "audio"].includes(String(unknown.type))) return <p key={index}>{c("chat.content.mediaUnavailable")}</p>;
    return <MonoBlock key={index} max={320}>{stringify(block.value)}</MonoBlock>;
  })}</div>;
}
