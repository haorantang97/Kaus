import { useCallback, useState } from "react";
import { ChevronDown, ChevronRight, Terminal } from "lucide-react";
import { useLocale } from "../i18n";
import { cream } from "./ui";
import { useExternalSurfaceIds } from "../lib/externalSurface";
import { isRunning, type SidebarGroup } from "../lib/conversationIndex";
import { CONVERSATION_DRAG_MIME, PROJECT_DRAG_MIME } from "../lib/groupDrop";
import { backendDisplay } from "../lib/backend-display";
import { readCollapsedGroups, relativeTime, writeCollapsedGroups } from "../lib/shellPrefs";

/* 侧栏 = 会话列表（★定稿 B / AD-79）。
 *
 * - 「新会话」在侧栏导航列表最上面（批次七 d 第 4 条），这里不再重复一个入口；
 * - 按项目分组（组头可折叠，折叠状态存 localStorage）；
 * - 运行中的会话置顶并带脉冲点；
 * - 每条：标题 · 引擎字母标 · 相对时间；当前会话用现有 `.is-current` 选中态。
 *
 * 项目**本身不进侧栏**（项目从概览图谱进），所以这里没有树、没有展开箭头以外的层级。
 */

/* 协作组名称与组内角色归 GroupDock；侧栏始终显示会话自己的身份。 */

export interface ConversationSidebarProps {
  groups: SidebarGroup[];
  loading: boolean;
  error: string | null;
  activeConversationId: string | null;
  onOpenConversation: (conversationId: string) => void;
}

export function ConversationSidebar({
  groups,
  loading,
  error,
  activeConversationId,
  onOpenConversation,
}: ConversationSidebarProps) {
  const { t } = useLocale();
  const [collapsed, setCollapsed] = useState<string[]>(() => readCollapsedGroups());
  /* Phase 4 第 2 件：在外部终端跑的那几条各带一枚小终端图标。会话索引那一行
     （`GET /api/conversations`）把 running-card / running-external 合并成一个
     `running`，所以这份名单由会话页登记（见 lib/externalSurface.ts）。 */
  const externalIds = useExternalSurfaceIds();
  /* 正在被拖的那一条：只用来把行压淡一点（不发明新样式）。 */
  const [draggingId, setDraggingId] = useState<string | null>(null);

  const toggle = useCallback((projectId: string) => {
    setCollapsed((current) => {
      const next = current.includes(projectId)
        ? current.filter((id) => id !== projectId)
        : [...current, projectId];
      writeCollapsedGroups(next);
      return next;
    });
  }, []);

  return (
    <div className="sidebar-context kaus-conversation-sidebar" data-testid="conversation-sidebar">
      {/* 「新会话」入口搬到了导航列表最上面（批次七 d 第 4 条）：一个入口就够。 */}
      <div className="sidebar-context-title">{t("sidebar.title")}</div>

      <nav className="sidebar-agent-tree is-compact flex-1 overflow-y-auto">
        {error && (
          <div className="px-2 py-2 text-xs" style={{ color: "var(--danger)" }}>
            {t("sidebar.error", { error })}
          </div>
        )}
        {!error && loading && groups.length === 0 && (
          <div className="px-2 py-2 text-xs" style={{ color: cream(45) }}>{t("sidebar.loading")}</div>
        )}
        {!error && !loading && groups.length === 0 && (
          <div className="px-2 py-2 text-xs leading-relaxed" style={{ color: cream(45) }}>
            {t("sidebar.empty")}
          </div>
        )}
        {groups.map((group) => {
          const open = !collapsed.includes(group.projectId);
          return (
            <div key={group.projectId} className="kaus-sidebar-group">
              <button
                type="button"
                className="kaus-sidebar-group-head"
                aria-expanded={open}
                /* batch43 第 2 件（★J-4 追加）：项目分组表头也是拖源——把它拖到
                   组胶囊 / 成员栏上 = 从这个项目起一条新会话加入组（路径 B）。
                   MIME 与会话那条**不同**（`application/x-kaus-project`），放置
                   那头据此分流，两种拖拽互不误触发。 */
                draggable
                onDragStart={(event) => {
                  event.dataTransfer.setData(PROJECT_DRAG_MIME, group.projectId);
                  // 顺手带一份显示名：回执要说「已从 <项目名> 启动一名成员」。
                  event.dataTransfer.setData("text/plain", group.displayName);
                  event.dataTransfer.effectAllowed = "link";
                }}
                /* 拖拽本身是看不见的——悬停时一句话把它说出来（顺带保住被截断的
                   项目全名）。 */
                title={t("group.drop.projectHint", { name: group.displayName })}
                onClick={() => toggle(group.projectId)}
              >
                {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
                <span className="flex-1 truncate">{group.displayName}</span>
                <span style={{ color: cream(38) }}>{group.conversations.length}</span>
              </button>
              {open &&
                group.conversations.map((conversation) => {
                  const running = isRunning(conversation.state);
                  // 引擎名只在真有 backendId 时才拼进 title（AD-71：不编「未知引擎」）。
                  const engineName = conversation.backendId ? backendDisplay(conversation.backendId).displayName : "";
                  const selected = conversation.id === activeConversationId;
                  const dragging = draggingId === conversation.id;
                  return (
                    <div
                      key={conversation.id}
                      role="button"
                      tabIndex={0}
                      data-conversation-id={conversation.id}
                      /* batch42 第 2 件（★J-4）：每条会话行都是拖源。行内的按钮上
                         起拖要拦下来（参照 Sidebar.tsx 的老写法），否则点开一个
                         菜单再一拖就把整行拖走了。 */
                      draggable
                      onDragStart={(event) => {
                        if ((event.target as HTMLElement).closest("button")) {
                          event.preventDefault();
                          return;
                        }
                        event.dataTransfer.setData(CONVERSATION_DRAG_MIME, conversation.id);
                        // 顺手带一份标题：放置那头的回执要说「已加入 <标题>」。
                        event.dataTransfer.setData("text/plain", conversation.title);
                        event.dataTransfer.effectAllowed = "link";
                        setDraggingId(conversation.id);
                      }}
                      onDragEnd={() => setDraggingId(null)}
                      onClick={() => onOpenConversation(conversation.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onOpenConversation(conversation.id);
                        }
                      }}
                      className={`sidebar-agent-row kaus-conversation-row flex cursor-pointer items-center gap-1.5 ${selected ? "sel-accent is-current" : ""} ${dragging ? "is-dragging" : ""}`}
                      /* batch40（★L 第 6 条）：引擎从行上撤了，改挂在这条 title 上
                         ——一行里同时有标题、组名、终端图标、引擎字母和时间时，
                         最先被挤掉的恰恰是标题（真机上就是这样）。 */
                      title={engineName ? `${conversation.title} · ${engineName}` : conversation.title}
                    >
                      {running ? (
                        <span className="kaus-run-dot" title={t("sidebar.running")} aria-label={t("sidebar.running")} />
                      ) : (
                        <span className="kaus-run-dot is-idle" aria-hidden="true" />
                      )}
                      <span className="flex-1 truncate">{conversation.title}</span>
                      {externalIds.includes(conversation.id) && (
                        <Terminal
                          className="size-3 shrink-0"
                          data-testid="sidebar-external-icon"
                          aria-label={t("surface.sidebar.external")}
                        />
                      )}
                      {/* 一行只显示会话标题与相对时间，引擎在悬停提示里。 */}
                      <span className="kaus-conversation-time">{relativeTime(conversation.updatedAt)}</span>
                    </div>
                  );
                })}
            </div>
          );
        })}
      </nav>
    </div>
  );
}
