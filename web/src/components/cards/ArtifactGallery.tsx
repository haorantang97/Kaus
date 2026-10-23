import { useEffect, useRef, useState, type ReactNode } from "react";
import { Image as ImageIcon } from "lucide-react";
import { artifactName, fetchArtifact, inlineImageUrl, localArtifactPath, previewKind, type ArtifactSource } from "../../lib/artifactApi";

/** Preload only authorized local images in the two-neighbour window. Remote images stay opt-in. */
export function ArtifactGallery({ sources, index, conversationId, onSelect, children }: {
  sources: ArtifactSource[]; index: number; conversationId?: string; onSelect: (index: number) => void; children: ReactNode;
}) {
  const cache = useRef(new Map<string, string>());
  const [urls, setUrls] = useState<Record<string, string>>({});
  useEffect(() => {
    const controller = new AbortController();
    const nearby = sources.slice(Math.max(0, index - 2), index + 3);
    const keep = new Set(nearby.map(source => source.uri));
    for (const [uri, url] of cache.current) if (!keep.has(uri)) { URL.revokeObjectURL(url); cache.current.delete(uri); }
    setUrls(Object.fromEntries(cache.current));
    if (conversationId) for (const source of nearby) {
      if (source === sources[index] || cache.current.has(source.uri) || !localArtifactPath(source.uri) || (source.sizeBytes ?? 0) > 30 * 1024 * 1024) continue;
      void fetchArtifact(conversationId, source, { signal: controller.signal }).then(blob => {
        if (controller.signal.aborted || blob.size > 30 * 1024 * 1024 || previewKind(source, blob.type) !== "image") return;
        cache.current.set(source.uri, URL.createObjectURL(blob)); setUrls(Object.fromEntries(cache.current));
      }).catch(() => {});
    }
    return () => controller.abort();
  }, [sources, index, conversationId]);
  useEffect(() => () => { for (const url of cache.current.values()) URL.revokeObjectURL(url); cache.current.clear(); }, []);
  return <div className="kaus-image-gallery" aria-roledescription="gallery">
    {sources.map((source, slot) => Math.abs(slot - index) <= 2 && <div key={source.uri} className="kaus-gallery-slide" data-active={slot === index || undefined}
      style={{ transform: `translateX(calc(${slot - index} * (75vw + 32px)))` }}>
      {slot === index ? children : <button type="button" tabIndex={-1} onClick={() => onSelect(slot)} aria-label={source.title || artifactName(source)}>
        {(inlineImageUrl(source.uri) || urls[source.uri]) ? <img src={inlineImageUrl(source.uri) || urls[source.uri]} alt="" draggable={false} referrerPolicy="no-referrer" /> : <span><ImageIcon size={24} /><span>{source.title || artifactName(source)}</span></span>}
      </button>}
    </div>)}
  </div>;
}
