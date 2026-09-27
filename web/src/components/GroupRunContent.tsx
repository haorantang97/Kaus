import { useEffect, useState } from "react";
import { EventTransport } from "../lib/eventTransport";
import { conversationEventsUrl, fetchEventsSnapshot, resolveInteraction } from "../lib/sessionApi";
import { initialTimelineState, reduceEvent, messageText, messageReasoningText, type AgentEventEnvelope, type InteractionItem, type TimelineItem } from "../lib/timeline/reducer";
import { CardRenderer } from "./cards/CardRenderer";
import { ReasoningCard } from "./cards/ReasoningCard";

export function publicGroupText(text: string): string {
  const block = text.search(/(?:^|\n)```kaus-control/);
  if (block >= 0) return text.slice(0, block).trimEnd();
  const line = text.slice(text.lastIndexOf("\n") + 1);
  if (line.startsWith("```") && "```kaus-control".startsWith(line) && (text.match(/```/g)?.length ?? 0) % 2 === 1) return text.slice(0, text.length - line.length).trimEnd();
  return text;
}

export function GroupRunContent({ conversationId, after = 0, runId, live = false }: {
  conversationId: string; after?: number; runId?: string | null; live?: boolean;
}) {
  const [timeline, setTimeline] = useState(() => initialTimelineState(conversationId));
  const [error, setError] = useState("");
  useEffect(() => {
    setTimeline(initialTimelineState(conversationId)); setError("");
    let disposed = false;
    const consume = (event: AgentEventEnvelope) => {
      if (!disposed && event.sequence > after) setTimeline(state => reduceEvent(state, event));
    };
    if (!live) {
      const replay = async () => {
        let since = after;
        do {
          const page = await fetchEventsSnapshot(conversationId, since);
          if (disposed) return;
          for (const event of page.events) consume(event as unknown as AgentEventEnvelope);
          if (!page.truncated || page.lastSequence <= since) return;
          since = page.lastSequence;
        } while (!disposed);
      };
      void replay().catch(failure => { if (!disposed) setError((failure as Error).message); });
      return () => { disposed = true; };
    }
    const transport = new EventTransport<AgentEventEnvelope>({
      openStream: ({ after: cursor, onEvent, onOpen, onError }) => {
        if (!globalThis.EventSource || !live) {
          const timer = window.setTimeout(onError, 0);
          return { close: () => window.clearTimeout(timer) };
        }
        const stream = new EventSource(conversationEventsUrl(conversationId, cursor));
        stream.onopen = onOpen;
        stream.onmessage = event => { try { onEvent(JSON.parse(event.data)); } catch { /* Ignore malformed transport frames. */ } };
        stream.onerror = onError;
        return { close: () => stream.close() };
      },
      fetchSnapshot: async since => {
        const page = await fetchEventsSnapshot(conversationId, since);
        return { ...page, events: page.events as unknown as AgentEventEnvelope[] };
      },
      sequenceOf: event => event.sequence, onEvent: consume,
      isRunActive: () => live,
      onPollSettled: ok => { setError(ok ? "" : "连接中断，正在重试"); },
    }, { after });
    transport.start();
    return () => { disposed = true; transport.stop(); };
  }, [conversationId, after, live]);
  const respond = async (item: InteractionItem, answer: { optionId?: string; optionIds?: string[]; text?: string; cancelled?: boolean }) => {
    setError("");
    try {
      await resolveInteraction(conversationId, item.itemId.slice("interaction:".length), {
        kind: item.interactionKind, optionId: answer.optionId ?? null, text: answer.text ?? null,
        optionIds: answer.optionIds, cancelled: answer.cancelled,
      });
    } catch (failure) { setError((failure as Error).message); }
  };
  const items = timeline.items.filter(item => (!runId || item.runId === runId) &&
    (item.kind !== "message" || (item.role === "assistant" && (live || Boolean(messageReasoningText(item))))) &&
    !["lifecycle", "extension"].includes(item.kind));
  return <div className="kaus-group-run-content">
    {items.map(original => {
      if (!live && original.kind === "message") return <ReasoningCard key={original.itemId} text={messageReasoningText(original)} />;
      const item: TimelineItem = original.kind === "message"
        ? { ...original, finalText: publicGroupText(messageText(original)), deltaChunks: {} } : original;
      return <CardRenderer key={item.itemId} item={item} conversationId={conversationId} live={live && !item.terminal}
        callbacks={live ? {
          onPermissionRespond: (interaction, optionId) => void respond(interaction, { optionId }),
          onQuestionRespond: (interaction, answer) => void respond(interaction, answer),
        } : {}} />;
    })}
    {error && <div className="kaus-group-error" role="alert">{error}</div>}
  </div>;
}
