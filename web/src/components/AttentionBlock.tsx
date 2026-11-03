import { EngineIcon } from "./EngineIcon";
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
import { AlertCircle, Blocks, ChevronDown, Users } from "lucide-react";

import { useLocale } from "../i18n";
import { backendDisplay } from "../lib/backend-display";
import { buildAttentionItems, isRunning, type AttentionItem, type SidebarGroup } from "../lib/conversationIndex";
import { relativeTime } from "../lib/shellPrefs";
import type { ModuleAttentionItem } from "../lib/moduleAttention";
import { openGroupDock, useGroupState } from "../lib/groupStore";
import { isGroupOpen, type GroupWire } from "../lib/groupsApi";

function AttentionRow({ item, onOpen }: { item: AttentionItem; onOpen: (id: string) => void }) {
  const { t, locale } = useLocale();
  const engine = item.backendId ? backendDisplay(item.backendId) : null;
  const running = isRunning(item.state);
  return (
    <button
      type="button"
      className="kaus-overview-row"
      data-testid="overview-conversation"
      data-conversation-id={item.id}
      title={item.kind === "terminal" && item.terminalWaiting ? `${item.title} · ${item.terminalWaiting}` : item.title}
      onClick={() => onOpen(item.id)}
    >
      {running ? (
        <span className="kaus-run-dot kaus-accent" data-accent="run" aria-label={t("sidebar.running")} />
      ) : (
        <span className="kaus-run-dot is-idle" aria-hidden />
      )}
      <span className="kaus-overview-title">{item.title}</span>
      <span className="kaus-overview-meta">{item.projectDisplayName}</span>
      {engine && <span className="kaus-overview-meta"><EngineIcon engine={item.backendId} size={13} />{engine.displayName}</span>}
      <span
        className="kaus-overview-state"
        data-testid="overview-attention-kind"
        data-kind={item.kind}
        style={item.kind === "failed" ? { color: "var(--danger)" } : undefined}
      >
        {t(`overview.attention.${item.kind}`)}
      </span>
      <span className="kaus-overview-time">{relativeTime(item.updatedAt, locale)}</span>
    </button>
  );
}

export interface AttentionBlockProps {
  groups: SidebarGroup[];
  /** 模块任务里等用户处理的那一步（宿主统一列出）。 */
  modules?: ModuleAttentionItem[];
  onOpenConversation: (conversationId: string) => void;
}

function ModuleAttentionRow({ item, onOpen }: { item: ModuleAttentionItem; onOpen: (id: string) => void }) {
  const { t, locale } = useLocale();
  const title = [item.moduleName, item.stepTitle].filter(Boolean).join(" · ");
  return (
    <button type="button" className="kaus-overview-row" data-testid="overview-module-attention" title={title} onClick={() => onOpen(item.conversationId)}>
      <span className="kaus-run-dot is-idle" aria-hidden />
      <span className="kaus-overview-title">{title}</span>
      <span className="kaus-overview-meta"><Blocks size={13} aria-hidden />{t("workbench.title")}</span>
      <span className="kaus-overview-state" data-kind="module">{t("overview.attention.awaiting")}</span>
      <span className="kaus-overview-time">{item.updatedAt ? relativeTime(item.updatedAt, locale) : null}</span>
    </button>
  );
}

/** 协作组里有成员或组长在等审批 / 提问：点开就是那个组的浮窗。 */
function GroupAttentionRow({ group }: { group: GroupWire }) {
  const { t, locale } = useLocale();
  return (
    <button type="button" className="kaus-overview-row" data-testid="overview-group-attention" title={group.title} onClick={() => openGroupDock(group.id)}>
      <span className="kaus-run-dot is-idle" aria-hidden />
      <span className="kaus-overview-title">{group.title}</span>
      <span className="kaus-overview-meta"><Users size={13} aria-hidden />{t("group.pill")}</span>
      <span className="kaus-overview-state" data-kind="group">{t("overview.attention.awaiting")}</span>
      <span className="kaus-overview-time">{group.updatedAt ? relativeTime(group.updatedAt, locale) : null}</span>
    </button>
  );
}

export function AttentionBlock({ groups, modules = [], onOpenConversation }: AttentionBlockProps) {
  const attention = useMemo(() => buildAttentionItems(groups), [groups]);
  const groupState = useGroupState();
  const waitingGroups = useMemo(
    () => groupState.groups.filter((group) => isGroupOpen(group) && (group.awaitingConversationIds ?? []).length > 0),
    [groupState.groups],
  );
  const { t } = useLocale();
  const total = attention.length + modules.length + waitingGroups.length;
  // 一条都没有 = 整块不渲染（不是一句"目前没有需要处理的"）。
  if (total === 0) return null;
  return (
    <details className="kaus-attention-disclosure">
      <summary><AlertCircle size={15} /><span>{t("overview.attention.title")}</span><span>{total}</span><ChevronDown size={14} /></summary>
      <div className="kaus-overview-list" data-testid="overview-attention">
        {attention.map((item) => (
          <AttentionRow key={item.id} item={item} onOpen={onOpenConversation} />
        ))}
        {waitingGroups.map((group) => <GroupAttentionRow key={group.id} group={group} />)}
        {modules.map((item) => <ModuleAttentionRow key={item.jobId} item={item} onOpen={onOpenConversation} />)}
      </div>
    </details>
  );
}
