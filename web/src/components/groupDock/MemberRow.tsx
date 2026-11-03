import { useCallback, useEffect, useState } from "react";
import { Ellipsis, Terminal } from "lucide-react";
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


/** 成员「…」菜单：终端、改名、移出 / 重新加入。
 *  点名一位成员用 @（2026-11-03 起不再有「定向发送」「暂停 / 恢复」与「提升为普通会话」）。 */
function MemberActions({
  member,
  onRejoin,
  onRemove,
  onRename,
  threadRunning = false,
}: {
  member: GroupMemberWire;
  onRejoin: () => void;
  onRemove: () => void;
  /** batch52 第 5 件：在本组叫什么（写 `roleLabel`）。 */
  onRename: () => void;
  threadRunning?: boolean;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const [terminalSupported, setTerminalSupported] = useState(false);
  const navigate = useNavigate();
  const left = member.participationState === "left";
  const conversation = member.conversation;
  const running = threadRunning || conversation?.runState === "running" || conversation?.state?.startsWith("running") === true;

  useEffect(() => {
    if (!open || !conversation?.backendId) return;
    let current = true;
    setTerminalSupported(false);
    void fetchBackendUiCapabilities(conversation.backendId)
      .then(caps => { if (current) setTerminalSupported(hasCapability(caps.externalCli.supported)); })
      .catch(() => {});
    return () => { current = false; };
  }, [open, conversation?.backendId]);

  useDismiss(open, useCallback(() => setOpen(false), []));

  const pick = (run: () => void) => () => {
    setOpen(false);
    run();
  };

  return (
    <span className="kaus-group-menu">
      <button
        type="button"
        className="kaus-group-icon-btn"
        aria-label={t("group.member.actions")}
        title={t("group.member.actions")}
        aria-expanded={open}
        data-testid={`member-actions-${member.id}`}
        onClick={() => setOpen((current) => !current)}
      >
        <Ellipsis size={16} aria-hidden />
      </button>
      {open && (
        <span className="kaus-bar-pop" role="menu" aria-label={t("group.member.actions")}>
          {terminalSupported && conversation && !left && conversation.surface !== "external-cli" && (
            <button type="button" role="menuitem" className="kaus-bar-pop-item" disabled={running}
              title={running ? t("surface.action.stopFirst") : undefined}
              onClick={pick(() => {
                requestExternalHandoff(member.conversationId);
                window.dispatchEvent(new CustomEvent("kaus:external-handoff"));
                navigate(conversationPath(member.conversationId));
                minimizeGroupDock();
              })}>
              <Terminal size={14} aria-hidden />{t("surface.action.openExternal")}
            </button>
          )}
          {left && (
            <button type="button" role="menuitem" className="kaus-bar-pop-item" onClick={pick(onRejoin)}>
              {t("group.member.rejoin")}
            </button>
          )}
          {/* batch52 第 5 件（AD-173）：改他在**本组**的名字。已经走了的人也给
              ——他还可能被重新加入，而那时叫什么已经定好了。 */}
          <button
            type="button"
            role="menuitem"
            className="kaus-bar-pop-item"
            data-testid={`member-rename-${member.id}`}
            onClick={pick(onRename)}
          >
            {t("group.member.rename")}
          </button>
          {!left && (
            <button type="button" role="menuitem" className="kaus-bar-pop-item" onClick={pick(onRemove)}>
              {t("group.member.remove")}
            </button>
          )}
        </span>
      )}
    </span>
  );
}

export type MemberAction = "rejoin" | "remove" | "leader" | "rename";

export function MemberRow({
  member,
  members,
  projectName,
  onAction,
  firstMessageFailed = false,
  threadRunning = false,
}: {
  member: GroupMemberWire;
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

  return (
    <div className="kaus-group-member" data-testid={`group-member-${member.id}`}>
      {/* ★H 的唯一例外：运行点。其余全中性。 */}
      {running ? (
        <span className="kaus-run-dot kaus-accent" title={t("group.member.running")} />
      ) : (
        <span className="kaus-run-dot is-idle" aria-hidden="true" />
      )}
      {/* batch46（PRD §B10-2）：成员栏叫他的名字 = 他在房间说明里被告知的名字。
          此前这里直接用会话标题，于是两个都叫 media 的成员在这一栏长得一模一样，
          而它们自己知道谁是 media#2。`title=` 仍给会话标题（悬停看得到原始标题）。 */}
      <span className="kaus-group-member-title" title={conversation?.title ?? undefined}>
        {conversation
          ? memberDisplayName(members, member.id)
          : t("group.member.conversationGone")}
      </span>
      <MemberActions
        member={member}
        threadRunning={threadRunning}
        onRejoin={() => onAction("rejoin", member)}
        onRemove={() => onAction("remove", member)}
        onRename={() => onAction("rename", member)}
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
