/** A missing or rejected Clipboard API must never report success. */
export async function copyText(text: string): Promise<void> {
  if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
    throw new Error("clipboard_unavailable");
  }
  await navigator.clipboard.writeText(text);
}
