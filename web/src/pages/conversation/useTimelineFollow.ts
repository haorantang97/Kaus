import { useCallback, useEffect, useRef, useState } from "react";

import { useScrollInputCooldown } from "../../hooks/useConversationPinning";
import { atBottom } from "./pageState";

/* 自动跟随（G-9）与「回到最新 · N 条新消息」（第 7 件）。
 *
 * 在底部附近才跟；用户上翻后不打扰，只亮「回到最新 ↓」。上翻期间到达的新条目
 * 累计成未读数，回到底部清零。跟随状态只住在页面层——它是版式不是时间线内容。 */

export interface TimelineFollowOptions {
  conversationId: string;
  /** 时间线最新 sequence：变了就（在跟随态下）钉到底。 */
  lastSequence: number | null;
  /** 在途占位条数：新插一条占位也要钉到底。 */
  pendingCount: number;
  /** 当前可见条目数：上翻期间增长的那部分就是未读。 */
  itemCount: number;
}

export function useTimelineFollow({ conversationId, lastSequence, pendingCount, itemCount }: TimelineFollowOptions) {
  const [follow, setFollow] = useState(true);
  const [showJump, setShowJump] = useState(false);
  /* 第 7 件：上翻期间到达的新条目数，用在「回到最新 · N 条新消息」上。 */
  const [unread, setUnread] = useState(0);
  const scrollInput = useScrollInputCooldown();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const jumpIntent = useRef(false);
  const userScrollIntent = useRef(false);
  const beginBookmarkJump = useCallback(() => { jumpIntent.current = true; setFollow(false); setShowJump(true); }, []);

  useEffect(() => {
    jumpIntent.current = false;
    userScrollIntent.current = false;
    setFollow(true);
    setUnread(0);
  }, [conversationId]);

  const pinTimelineToEnd = useCallback(() => {
    const pane = scrollRef.current;
    if (!pane) return;
    if (typeof pane.scrollTo === "function") pane.scrollTo({ top: pane.scrollHeight, behavior: "auto" });
    else pane.scrollTop = pane.scrollHeight;
  }, []);
  useEffect(() => { if (follow) setShowJump(false); }, [follow]);

  /* 自动跟随（G-9）：在底部附近才跟；用户上翻后不打扰，只亮「回到最新 ↓」。 */
  const scrollToBottom = useCallback(() => {
    jumpIntent.current = false;
    userScrollIntent.current = false;
    scrollInput.reset();
    // Scroll only the timeline so its growing bottom buffer does not move the page.
    pinTimelineToEnd();
    setFollow(true);
    setUnread(0);
  }, []);

  useEffect(() => {
    if (!follow || jumpIntent.current || !scrollInput.canFollow()) return;
    pinTimelineToEnd();
  }, [follow, lastSequence, pendingCount]);

  useEffect(() => {
    const flow = scrollRef.current?.firstElementChild;
    if (!follow || !flow || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (!jumpIntent.current && scrollInput.canFollow()) pinTimelineToEnd();
    });
    observer.observe(flow);
    return () => observer.disconnect();
  }, [follow]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const bottom = atBottom(el);
    // Explicit navigation keeps following off until a real scroll gesture or
    // “latest”. Layout/resize callbacks during the animation can revisit bottom.
    if (jumpIntent.current && !userScrollIntent.current) return;
    // Content growth and history replay also emit scroll events. Only a user's
    // navigation may turn following off; automatic reflow must keep it on.
    if (bottom) setFollow(true);
    else if (userScrollIntent.current || jumpIntent.current) setFollow(false);
    jumpIntent.current = false;
    userScrollIntent.current = false;
    setShowJump(el.scrollHeight - el.scrollTop - el.clientHeight > 120);
    // 回到底部就把未读清零：那些消息现在正看着。
    if (bottom) setUnread(0);
  }, []);

  /* 第 7 件：上翻期间到达的新条目累计成未读数；回到底部清零。 */
  const itemCountRef = useRef(itemCount);
  useEffect(() => {
    const previous = itemCountRef.current;
    itemCountRef.current = itemCount;
    if (itemCount > previous && !follow) setUnread((count) => count + (itemCount - previous));
  }, [itemCount, follow]);

  /** 用户的真实滚动手势（滚轮 / 触摸 / 键盘 / 点空白处）：只有它能关掉跟随。 */
  const markUserScroll = useCallback(() => {
    userScrollIntent.current = true;
    scrollInput.markInput();
  }, []);

  return { follow, setFollow, showJump, unread, setUnread, scrollRef, beginBookmarkJump, scrollToBottom, onScroll, markUserScroll };
}
