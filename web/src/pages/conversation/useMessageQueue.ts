import { useCallback, useEffect, useRef, useState } from "react";

import { fetchMessageQueue, saveMessageQueue, type QueuedMessageWire } from "../../lib/sessionApi";

export const QUEUE_LIMIT = 10;

/* 排队发送的队列：存在后端（手机与电脑一致），这里只做读写与乐观更新。
   什么时候发由会话页决定（这一轮结束、且不是用户手动停止）。 */
export function useMessageQueue(conversationId: string) {
  const [items, setItems] = useState<QueuedMessageWire[]>([]);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  useEffect(() => {
    setItems([]);
    const controller = new AbortController();
    fetchMessageQueue(conversationId, controller.signal)
      .then(result => { if (Array.isArray(result?.items)) setItems(result.items); })
      .catch(() => {});
    return () => controller.abort();
  }, [conversationId]);
  const write = useCallback((next: QueuedMessageWire[]) => {
    setItems(next);
    saveMessageQueue(conversationId, next).catch(() => {});
  }, [conversationId]);
  const add = useCallback((item: QueuedMessageWire) => {
    if (itemsRef.current.length >= QUEUE_LIMIT) return false;
    write([...itemsRef.current, item]);
    return true;
  }, [write]);
  const remove = useCallback((id: string) => write(itemsRef.current.filter(item => item.id !== id)), [write]);
  return { items, add, remove };
}
