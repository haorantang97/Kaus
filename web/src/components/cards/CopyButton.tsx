import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { copyText } from "./clipboard";
import { toast } from "../ui";
import { useContentText } from "./contentStrings";

export function CopyButton({ text, label, copiedLabel, className }: {
  text: string; label: string; copiedLabel: string; className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const c = useContentText();
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const copy = async () => {
    try {
      await copyText(text);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    } catch {
      toast(c("chat.content.copyFailed"), "bad");
    }
  };
  return <button type="button" className={className} aria-label={label} title={copied ? copiedLabel : label} onClick={() => void copy()}>
    {copied ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
  </button>;
}
