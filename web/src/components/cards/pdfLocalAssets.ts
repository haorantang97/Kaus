// Kept in the PDF-only dynamic chunk. Vite emits these package resources locally;
// a PDF may name a resource but cannot turn that name into an arbitrary URL.
const cMaps = import.meta.glob<string>("/node_modules/pdfjs-dist/cmaps/*.bcmap", {
  query: "?url", import: "default", eager: true,
});
// With useSystemFonts, PDF.js only requests the Symbol and ZapfDingbats fallbacks.
// These Foxit/PDFium resources have a permissive BSD notice. No Liberation fonts
// or WASM codecs are included in this preview build.
const fonts = import.meta.glob<string>("/node_modules/pdfjs-dist/standard_fonts/Foxit{Symbol,Dingbats}.pfb", {
  query: "?url", import: "default", eager: true,
});

export function createPdfBinaryDataFactory(signal: AbortSignal) {
  return class LocalPdfBinaryDataFactory {
    async fetch({ kind, filename }: { kind: string; filename: string }): Promise<Uint8Array> {
      const url = kind === "cMapUrl"
        ? cMaps[`/node_modules/pdfjs-dist/cmaps/${filename}`]
        : kind === "standardFontDataUrl"
          ? fonts[`/node_modules/pdfjs-dist/standard_fonts/${filename}`]
          : undefined;
      if (!url) throw new Error("Unsupported local PDF resource");
      const response = await fetch(url, { signal, credentials: "omit" });
      if (!response.ok) throw new Error("Could not load local PDF resource");
      return new Uint8Array(await response.arrayBuffer());
    }
  };
}
