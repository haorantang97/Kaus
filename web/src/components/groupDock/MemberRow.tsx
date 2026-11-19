import { useEffect, useRef, useState } from "react";
import { Pencil, Terminal, UserMinus, UserPlus } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { useLocale, type DictKey } from "../../i18n";
import { EngineIcon } from "../EngineIcon";
import { conversationPath } from "../../lib/routes";
import { requestExternalHandoff } from "../../lib/externalSurface";
import { hasCapability } from "../cards/capabilities";
import { backendDisplay } from "../../lib/backend-display";
import { minimizeGroupDock } from "../../lib/groupStore";
import { fetchBackendUiCapabilities } from "../../lib/sessionApi";
import type { GroupMemberWire } from "../../lib/groupsApi";
import { memberDisplayName } from "../GroupWorkspace";
import { RowMenu, type RowMenuItem, type RowMenuPoint } from "../ConversationRowMenu";
import { useDraftEscape } from "../../hooks/useDraftEscape";

/* 协作组浮窗（`../GroupDock.tsx`）里的成员行：运行点、显示名、⋯ 菜单与第二行元信息。 */

/** 浮层里的小菜单统一的收起方式：点外面、按 Esc。两处都用它，行为不分叉。 */
export function useDismiss(open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    const onDown = (event: MouseEvent) => {
      // 菜单自己里面的点击由按钮各自处理；点到别处就收起。
      if (!(event.target as HTMLElement | null)?.closest?.(".kaus-group-menu")) close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open, close]);
}


/** 成员 ⋯ 菜单（与对话行同一份 `RowMenu`，右键同一菜单，2026-11-17）：终端、改名、移出 / 重新加入。
 *  点名一位成员用 @（2026-11-03 起不再有「定向发送」「暂停 / 恢复」与「提升为普通会话」）。 */
function MemberActions({
  member,
  onRejoin,
  onRemove,
  onRename,
  point,
  threadRunning = false,
}: {
  member: GroupMemberWire;
  onRejoin: () => void;
  onRemove: () => void;
  /** batch52 第 5 件：在本组叫什么（写 `roleLabel`）。 */
  onRename: () => void;
  point: RowMenuPoint | null;
  threadRunning?: boolean;
}) {
  const { t } = useLocale();
  const [terminalSupported, setTerminalSupported] = useState(false);
  const navigate = useNavigate();
  const left = member.participationState === "left";
  const conversation = member.conversation;
  const running = threadRunning || conversation?.runState === "running" || conversation?.state?.startsWith("running") === true;

  useEffect(() => {
    if (!conversation?.backendId) return;
    let current = true;
    void fetchBackendUiCapabilities(conversation.backendId)
      .then(caps => { if (current) setTerminalSupported(hasCapability(caps.externalCli.supported)); })
      .catch(() => {});
    return () => { current = false; };
  }, [conversation?.backendId]);

  const items: RowMenuItem[] = [
    ...(terminalSupported && conversation && !left && conversation.surface !== "external-cli" ? [{
      id: "terminal", icon: <Terminal size={16} aria-hidden />, label: t("surface.action.openExternal"), disabled: running,
      title: running ? t("surface.action.stopFirst") : undefined,
      onSelect: () => {
        requestExternalHandoff(member.conversationId);
        window.dispatchEvent(new CustomEvent("kaus:external-handoff"));
        navigate(conversationPath(member.conversationId));
        minimizeGroupDock();
      },
    }] : []),
    ...(left ? [{ id: "rejoin", icon: <UserPlus size={16} aria-hidden />, label: t("group.member.rejoin"), onSelect: onRejoin }] : []),
    // batch52 第 5 件（AD-173）：改他在**本组**的名字。已经走了的人也给——他还可能被重新加入。
    { id: "rename", icon: <Pencil size={16} aria-hidden />, label: t("group.member.rename"), onSelect: onRename },
    ...(!left ? [{ id: "remove", icon: <UserMinus size={16} aria-hidden />, label: t("group.member.remove"), destructive: true, onSelect: onRemove }] : []),
  ];
  return <RowMenu triggerLabel={t("group.member.actions")} menuLabel={t("group.member.actions")} items={items} point={point}
    testId={`member-actions-${member.id}`} />;
}

export type MemberAction = "rejoin" | "remove" | "leader" | "rename";

export function MemberRow({
  member,
  members,
  projectName,
  onAction,
  firstMessageFailed = false,
  threadRunning = false,
  renaming = false,
  onRenameSubmit,
  onRenameCancel,
}: {
  member: GroupMemberWire;
  /** 组内名称原位编辑（2026-11-19，取代弹窗）：Enter 保存，Esc / 失焦取消；留空则回到对话标题。 */
  renaming?: boolean;
  onRenameSubmit?: (roleLabel: string) => void;
  onRenameCancel?: () => void;
  /** 整组的成员行——显示名要看得见组里有没有第二个同名的（`#2` 后缀）。 */
  members: GroupMemberWire[];
  projectName: string | null;
  onAction: (action: MemberAction, member: GroupMemberWire) => void;
  /** batch27：这位成员是 spawn 出来的，而首句没发出去——成员行上留一枚小标，
   *  详情（原因 + 提示）在组时间线顶上那条常驻通知里。 */
  firstMessageFailed?: boolean;
  threadRunning?: boolean;
}) {
  const { t } = useLocale();
  const conversation = member.conversation;
  const running = conversation?.runState === "running" || conversation?.state?.startsWith("running") === true;
  const external = conversation?.surface === "external-cli";
  const engine = conversation ? backendDisplay(conversation.backendId).displayName : null;
  const navigate = useNavigate();
  const [point, setPoint] = useState<RowMenuPoint | null>(null);
  const menuSeq = useRef(0);
  const [draft, setDraft] = useState<string | null>(null);
  const draftEscape = useDraftEscape();
  useEffect(() => { setDraft(renaming ? member.roleLabel ?? "" : null); }, [renaming, member.roleLabel]);
  const displayName = memberDisplayName(members, member.id);

  return (
    <div className="kaus-group-member kaus-row-with-menu" data-testid={`group-member-${member.id}`}
      onContextMenu={event => { event.preventDefault(); menuSeq.current += 1; setPoint({ x: event.clientX, y: event.clientY, seq: menuSeq.current }); }}>
      {/* ★H 的唯一例外：运行点。其余全中性。 */}
      {running ? (
        <span className="kaus-run-dot kaus-accent" title={t("group.member.running")} />
      ) : (
        <span className="kaus-run-dot is-idle" aria-hidden="true" />
      )}
      {/* batch46（PRD §B10-2）：成员栏叫他的名字 = 他在房间说明里被告知的名字。
          此前这里直接用会话标题，于是两个都叫 media 的成员在这一栏长得一模一样，
          而它们自己知道谁是 media#2。`title=` 仍给会话标题（悬停看得到原始标题）。 */}
      {/* 单击打开这位成员的对话（与对话行一致，2026-11-17）。 */}
      {renaming && draft !== null
        ? <input className="kaus-sidebar-title-input kaus-group-member-title" autoFocus data-testid="group-rename-input"
            aria-label={t("group.member.renameField")} placeholder={conversation?.title ?? undefined} value={draft}
            onChange={event => setDraft(event.target.value)} onFocus={event => event.currentTarget.select()}
            onBlur={() => onRenameCancel?.()}
            onKeyDown={event => {
              if (event.nativeEvent.isComposing) return;
              if (draftEscape(event, draft !== (member.roleLabel ?? ""), () => onRenameCancel?.())) return;
              if (event.key === "Enter") { event.preventDefault(); onRenameSubmit?.(draft.trim()); }
            }} />
        : conversation
        ? <button type="button" className="kaus-group-member-title kaus-row-open" title={conversation.title ? `${displayName} · ${conversation.title}` : displayName}
            onClick={() => { navigate(conversationPath(member.conversationId)); minimizeGroupDock(); }}>
            {displayName}
          </button>
        : <span className="kaus-group-member-title">{t("group.member.conversationGone")}</span>}
      <MemberActions
        member={member}
        threadRunning={threadRunning}
        onRejoin={() => onAction("rejoin", member)}
        onRemove={() => onAction("remove", member)}
        onRename={() => onAction("rename", member)}
        point={point}
      />
      {/* batch26：左栏只有 216px，标题与元信息挤在一行会把标题截成「文…」。
          第二行放角色/状态标、项目、引擎、表面图标——标题永远读得完整。 */}
      <span className="kaus-group-member-meta">
        {conversation && <EngineIcon engine={conversation.backendId} size={13} />}
        {/* batch48：组长的标记。**沿用角色/状态那一枚小标**（`kaus-group-tag`），
            不新造一种强调——这一片唯一的强调色仍旧只有运行点（★H / ★J-2）。 */}
        {member.isLeader && (
          <span className="kaus-group-tag" data-testid={`member-leader-${member.id}`}>
            {t("group.member.leader")}
          </span>
        )}
        {firstMessageFailed && (
          <span className="kaus-group-bad" data-testid={`member-first-failed-${member.id}`}>
            {t("group.member.firstMessageFailed")}
          </span>
        )}
        {member.participationState !== "active" && (
          <span className="kaus-group-tag" data-testid={`member-state-${member.id}`}>
            {t(`group.state.${member.participationState}` as DictKey)}
          </span>
        )}
        {projectName && <span className="kaus-group-dim">{projectName}</span>}
        {engine && <span className="kaus-group-dim">{engine}</span>}
        {external && <Terminal className="size-3 shrink-0" aria-label={t("surface.sidebar.external")} />}
      </span>
    </div>
  );
}
