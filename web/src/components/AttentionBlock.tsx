import "./attention.css";
/* 「需要处理」（原概览页第 ② 段，DESIGN ★L-1）。
 *
 * 2026-09-13 用户裁决把概览页整页取消，三段里「继续」「项目」是侧栏的复印件，
 * 跟着一起删了；**这一段留下**——它是唯一"不看会出事"的一段，侧栏里没有一个
 * 位置在说"这几条停着等你"。抽成组件搬到草稿页上，摆在铭牌下面、草稿表单上面。
 *
 * 一条硬规则照旧：**空了整块不在 DOM 里**（不是空态文案）。行的形状一个字没改：
 * 状态点 · 标题 · 项目 · 引擎 · 「等你处理 / 上一次没发出去」· 相对时间，点行进会话。
 *
 * 一条 AD-71：索引行不带模型 id，所以行上只写引擎名，不编一个模型出来。
 */

import { useMemo } from "react";
import { AlertCircle, ChevronDown } from "lucide-react";

import { useLocale } from "../i18n";
import { backendDisplay } from "../lib/backend-display";
import { buildAttentionItems, isRunning, type AttentionItem, type SidebarGroup } from "../lib/conversationIndex";
import { relativeTime } from "../lib/shellPrefs";

function AttentionRow({ item, onOpen }: { item: AttentionItem; onOpen: (id: string) => void }) {
  const { t } = useLocale();
  const engine = item.backendId ? backendDisplay(item.backendId) : null;
  const running = isRunning(item.state);
  return (
    <button
      type="button"
      className="kaus-overview-row"
      data-testid="overview-conversation"
      data-conversation-id={item.id}
      title={item.title}
      onClick={() => onOpen(item.id)}
    >
      {running ? (
        <span className="kaus-run-dot kaus-accent" data-accent="run" aria-label={t("sidebar.running")} />
      ) : (
        <span className="kaus-run-dot is-idle" aria-hidden />
      )}
      <span className="kaus-overview-title">{item.title}</span>
      <span className="kaus-overview-meta">{item.projectDisplayName}</span>
      {engine && <span className="kaus-overview-meta">{engine.displayName}</span>}
      <span
        className="kaus-overview-state"
        data-testid="overview-attention-kind"
        data-kind={item.kind}
        style={item.kind === "failed" ? { color: "var(--danger)" } : undefined}
      >
        {t(`overview.attention.${item.kind}`)}
      </span>
      <span className="kaus-overview-time">{relativeTime(item.updatedAt)}</span>
    </button>
  );
}

export interface AttentionBlockProps {
  groups: SidebarGroup[];
  onOpenConversation: (conversationId: string) => void;
}

export function AttentionBlock({ groups, onOpenConversation }: AttentionBlockProps) {
  const attention = useMemo(() => buildAttentionItems(groups), [groups]);
  const { t } = useLocale();
  // 一条都没有 = 整块不渲染（不是一句"目前没有需要处理的"）。
  if (attention.length === 0) return null;
  return (
    <details className="kaus-attention-disclosure">
      <summary><AlertCircle size={15} /><span>{t("overview.attention.title")}</span><span>{attention.length}</span><ChevronDown size={14} /></summary>
      <div className="kaus-overview-list" data-testid="overview-attention">
        {attention.map((item) => (
          <AttentionRow key={item.id} item={item} onOpen={onOpenConversation} />
        ))}
      </div>
    </details>
  );
}
