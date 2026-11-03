import { ErrorNotice } from "./ui";
/* Group 浮窗（DESIGN ★ 定稿 J，batch23 第 2 件）。
 *
 * 右下角常驻一枚中性 Pill「协作组 · N」；点开是一个固定在右下、宽 420px、最高 60vh
 * 的浮层——**不是抽屉，不占主区**。开/最小化状态记 sessionStorage（`lib/groupStore.ts`）。
 *
 * 两级导航（2026-11-03）：第一级是组列表（新建、最小化；每行有 ⋯ / 右键菜单：重命名、
 * 删除）；点一行进入第二级——那个组的工作区，头部是返回、组名、删除组、最小化。
 * 停在哪一级、哪个组也记在同一份 sessionStorage 里。
 *
 * 四条硬规则：
 *  ① **全中性**（★H）：这一片没有任何强调色，唯一的例外是成员行的运行点。
 *  ② **缺的不渲染**（AD-71）：会话被单独删过的成员行只给一句「会话已不在」而不是
 *     空壳；项目没有默认 Binding 时「启动新成员」不给引擎名，交给后端的
 *     `binding_required` 说话；组一个都没有时列表位置只有一句话，没有假骨架。
 *  ③ **写操作一律有回执**：成功 toast 一句人话，失败按 **code** 分支（`docs/ops/groups.md` §5
 *     的码表），不按文案——`member_elsewhere` 还要点名那个组（`detail.groupTitle`）。
 *  ④ **列表靠推送刷新**：组级 SSE 的任一条变更都只做一件事——重取 `GET /api/groups`
 *     （那条流刻意没有重放，AD-148 ③ 补）。断线重连同样重取。
 */

import { CoordinatorSettings } from "./CoordinatorSettings";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, Minus, Pencil, Plus, Trash2, UserPlus, Users } from "lucide-react";
import "../styles/conversation-polish.css";

import { useLocale, type DictKey } from "../i18n";
import {
  GroupWorkspace,
  memberDisplayName,
} from "./GroupWorkspace";
import { Button, toast } from "./ui";
import { RowMenu, type RowMenuPoint } from "./ConversationRowMenu";
import {
  addGroupMember,
  closeGroup,
  conflictGroupTitle,
  createGroup,
  GROUP_ERROR_KEYS,
  initialMessageFailure,
  patchGroup,
  removeGroupMember,
  // batch52
  renameGroupMember,
  resumeGroupMember,
  spawnGroupMember,
  subscribeGroupEvents,
  type GroupMemberWire,
  type GroupWire,
  isGroupOpen,
} from "../lib/groupsApi";
import { groupDropTarget, useGroupDrop, type GroupDropPayload } from "../lib/groupDrop";
import { matchProjectId } from "../lib/projectRef";
import {
  forgetGroup,
  minimizeGroupDock,
  openGroupDock,
  refreshGroups,
  selectGroup,
  showGroupList,
  useDockState,
  useGroupState,
} from "../lib/groupStore";
import {
  fetchProjects,
  SessionApiError,
  type ProjectWire,
} from "../lib/sessionApi";
import { MemberRow, useDismiss, type MemberAction } from "./groupDock/MemberRow";
import { GroupDropItem, MembersPane } from "./groupDock/MembersPane";
import { ExistingPicker, SpawnForm } from "./groupDock/AddMemberForms";
import { CloseGroupDialog, RenameMemberDialog } from "./groupDock/GroupDialogs";

/** 失败 → 一句人话。**按 code 分支**；表里没有的 code 用后端原文（它已经是人话）。 */
export function describeGroupFailure(
  failure: unknown,
  t: (key: DictKey, vars?: Record<string, string | number>) => string,
): string {
  if (!(failure instanceof SessionApiError)) return String((failure as Error)?.message ?? failure);
  const key = GROUP_ERROR_KEYS[failure.code];
  if (!key) return failure.message;
  if (failure.code === "member_elsewhere") {
    const title = conflictGroupTitle(failure);
    // 组名拿不到时退回一句不点名的（比显示 "undefined" 强）。
    if (!title) return t("group.error.memberElsewhereUnnamed");
    return t(key as DictKey, { title });
  }
  return t(key as DictKey);
}

/* ------------------------------------------------------------------ *
 * 成员行
 * ------------------------------------------------------------------ */

/** 开着组时重取列表的间隔（组内审批不走组级推送）。 */
const AWAITING_POLL_MS = 15_000;
/* ------------------------------------------------------------------ *
 * 浮窗本体
 * ------------------------------------------------------------------ */

type AddMode = null | "existing" | "spawn";

/** batch27（真机 ① 的前端一半）：spawn 的首句没发出去。
 *
 * 这件事此前只发一条 toast——而 toast 紧跟在「已添加」后面弹，真机上后一条把前一条
 * 盖掉了，用户看到的是"成功"。所以现在它是**组时间线里一条常驻通知**（外加成员行
 * 上一枚小标）：不会自己消失，用户读完自己关。 */
export interface SpawnNotice {
  id: string;
  groupId: string;
  memberId: string | null;
  /** 那条新会话的标题（点名是谁的首句没发出去）。 */
  title: string;
  /** 后端那句人话；后端没给就是 null，界面退回一句通用的。 */
  message: string | null;
  /** `error.hint`：接下来该怎么办。没有就不显示（AD-71）。 */
  hint: string | null;
}

export function GroupDock() {
  const { t } = useLocale();
  const { groups, members, error } = useGroupState();
  const dock = useDockState();
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [spawnProjectId, setSpawnProjectId] = useState<string | null>(null);
  const [addMode, setAddMode] = useState<AddMode>(null);
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [closing, setClosing] = useState<GroupWire | null>(null);
  /** batch52 第 5 件：正在给谁改组内名字（null = 没开那个弹窗）。 */
  // batch52
  const [renaming, setRenaming] = useState<GroupMemberWire | null>(null);
  /** 组列表里正在改名的那一行。 */
  const [renamingGroup, setRenamingGroup] = useState<{ id: string; title: string } | null>(null);
  /** 组列表行的右键菜单：落在哪一行、开在哪个位置。 */
  const [menuPoint, setMenuPoint] = useState<{ groupId: string; point: RowMenuPoint } | null>(null);
  const menuSeq = useRef(0);
  /** 组级流每来一条变更就 +1：组内的时间线拿它当重取的信号。 */
  const [changeToken, setChangeToken] = useState(0);
  /** batch27：首句没发出去的那几条常驻通知（不是 toast，见 `SpawnNotice`）。 */
  const [notices, setNotices] = useState<SpawnNotice[]>([]);
  const noticeSeq = useRef(0);
  useDismiss(addMenuOpen, useCallback(() => setAddMenuOpen(false), []));

  /* 第 4 条规则：**推送刷新**。任何一条 `kaus/group.changed` 都只做一件事——
     重取组列表；组级流没有重放，断线重连（`onResync`）走的也是这一条。
     batch26：同一条推送顺带催一次时间线（`message_posted` 也在这条流上）。 */
  useEffect(() => {
    void refreshGroups();
    const bump = () => setChangeToken((value) => value + 1);
    return subscribeGroupEvents({
      onChange: (change) => {
        bump();
        /* batch27 第 4 件：`group_closed` 先**就地**把这个组抹掉，再重取。
           重取是异步的，而侧栏那枚组名小标不该在这段空窗里继续举着一个
           已经关掉的组（真机 C5）。 */
        if (change.change === "group_closed") forgetGroup(change.groupId);
        void refreshGroups();
      },
      onResync: () => {
        bump();
        void refreshGroups();
      },
    });
  }, []);

  /* 组内的审批 / 提问不走组级推送：开着组时定期重取，收起的胶囊才能及时提示。 */
  const hasOpenGroups = groups.some(isGroupOpen);
  useEffect(() => {
    if (!hasOpenGroups) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshGroups();
    }, AWAITING_POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasOpenGroups]);

  useEffect(() => {
    const controller = new AbortController();
    fetchProjects(controller.signal)
      .then((payload) => setProjects(payload.projects))
      .catch(() => setProjects([]));
    return () => controller.abort();
  }, []);

  const projectName = useCallback(
    (projectId: string | null | undefined): string | null =>
      projects.find((project) => project.id === projectId)?.displayName ?? null,
    [projects],
  );

  /** 写操作的统一回执：成功一句 toast，失败按 code 翻人话，末了重取一次列表。 */
  const run = useCallback(
    async (action: () => Promise<unknown>, okKey: DictKey, vars?: Record<string, string | number>, recover?: (failure: unknown) => boolean) => {
      try {
        await action();
        toast(t(okKey, vars), "ok");
      } catch (failure) {
        if (!recover?.(failure)) toast(describeGroupFailure(failure, t), "bad");
      } finally {
        await refreshGroups();
      }
    },
    [t],
  );

  /* 两级导航：`expandedGroupId` 为 null 时是组列表（第一级），否则是那个组（第二级）。
     这个值记在 sessionStorage 里，最小化再打开仍停在同一级。指向的组已经不在了
     （被删 / 被关）就退回组列表。 */
  const expanded = dock.expandedGroupId;
  const expandedGroup = groups.find((group) => group.id === expanded) ?? null;

  const enterGroup = useCallback((groupId: string | null) => {
    selectGroup(groupId);
    setAddMode(null);
    setAddMenuOpen(false);
    setCreating(false);
    setRenamingGroup(null);
  }, []);

  const saveGroupTitle = useCallback(
    (group: GroupWire, value: string) => {
      setRenamingGroup(null);
      const title = value.trim();
      if (!title || title === group.title) return;
      void run(() => patchGroup(group.id, { title }), "group.toast.renamed", { title });
    },
    [run],
  );

  /* batch42 第 2 件（★J-4）：拖一条会话进组。
     **与「选择已有会话」完全同一条调用**（`addGroupMember` → POST /groups/{id}/members），
     不另开路径——成功一句 toast 点名标题，失败仍旧按 code 翻人话（`run` 里那套）。
     不做项目限制：后端本来不限，此前只是那个下拉限在 home project。

     batch43 第 2 件：拖过来的也可能是**一个项目**（用户裁决 2026-09-13：组成员只
     能是会话，但「把项目拖到组上」= 组里现有的「启动新成员」）。所以这里按 `kind`
     分两条，各自走浮窗里手点的那条同一调用：
       - conversation → `addGroupMember`
       - project      → `spawnGroupMember`，只传 `projectId`：binding 不传（用该项目
         的默认绑定，没有默认时后端回 `binding_required`，码表里已经有人话），
         首句也不传（拖拽没地方打字，不替用户编一句）。
     项目 id 先过一遍 `matchProjectId`：侧栏那头给的是领域库 id，轮盘那头只知道裸
     名字，两种都要能落到同一个项目上。 */
  const joinByDrop = useCallback(
    (groupId: string, dropped: GroupDropPayload) => {
      if (dropped.kind === "conversation") {
        void run(
          () => addGroupMember(groupId, { conversationId: dropped.conversationId }),
          dropped.label ? "group.toast.memberAddedNamed" : "group.toast.memberAdded",
          dropped.label ? { title: dropped.label } : undefined,
        );
        return;
      }
      const projectId = matchProjectId(projects, dropped.projectId);
      const known = projects.find((project) => project.id === projectId);
      const name = dropped.label || known?.displayName || known?.slug || "";
      void run(
        () => spawnGroupMember(groupId, { projectId }),
        name ? "group.toast.memberSpawnedFrom" : "group.toast.memberSpawned",
        name ? { name } : undefined,
        failure => {
          if (!(failure instanceof SessionApiError) || !["engine_not_selectable", "binding_required"].includes(failure.code)) return false;
          openGroupDock(groupId); setSpawnProjectId(projectId); setAddMode("spawn");
          return true;
        },
      );
    },
    [run, projects],
  );
  /* 收起态的胶囊 / 浮窗外框：drop 上来加入哪个组？
       ① 浮窗停在某个组里（`expandedGroupId`）→ 加入它；
       ② 停在组列表、但只开着一个组 → 就是它（刚刷新的页面也能拖）；
       ③ 停在组列表且开着多个组 → 打开组列表，并告诉用户拖到具体的组上（不替用户猜）；
       ④ 一个组都没有 → 告诉用户先建组。
     只有④不加虚线框（没有可放的目标）；②③都要有反馈，静默是最坏的结果。 */
  const openGroups = groups.filter(isGroupOpen);
  const pillDrop = useGroupDrop(
    useCallback(
      (dropped: GroupDropPayload) => {
        const target = groupDropTarget(openGroups, expanded);
        if (target) {
          joinByDrop(target, dropped);
          return;
        }
        if (openGroups.length === 0) {
          toast(t("group.drop.noGroup"), "bad");
          return;
        }
        showGroupList();
        toast(t("group.drop.pickGroup", { count: openGroups.length }), "bad");
      },
      [expanded, openGroups, joinByDrop, t],
    ),
    openGroups.length === 0 || (dock.open && expandedGroup !== null && !isGroupOpen(expandedGroup)),
  );

  const onMemberAction = useCallback(
    (action: MemberAction, member: GroupMemberWire) => {
      // batch52 第 5 件：改名要先问叫什么，所以它开一个小弹窗而不是直接调端点。
      if (action === "rename") {
        setRenaming(member);
        return;
      }
      if (action === "leader") {
        // batch48：换组长是改组的一个字段（PATCH /groups/{id}），不是成员上的一个
        // 动作——所以它走的是另一条调用，回执里点名说清楚现在是谁。
        void run(
          () => patchGroup(member.groupId, { leaderMemberId: member.id }),
          "group.member.leaderDone",
          { name: memberDisplayName(members[member.groupId] ?? [], member.id) },
        );
        return;
      }
      // 「重新加入」走的是后端的 resume（left → active，重新过一遍唯一性）。
      const map = {
        rejoin: [resumeGroupMember, "group.toast.rejoined"],
        remove: [removeGroupMember, "group.toast.removed"],
      } as const;
      const [call, okKey] = map[action];
      void run(() => call(member.groupId, member.id), okKey);
    },
    [members, run],
  );

  const awaitingGroup = openGroups.find((group) => (group.awaitingConversationIds ?? []).length > 0) ?? null;
  const pill = (
    <button
      type="button"
      className={`kaus-group-pill ${pillDrop.isOver ? "is-drop-target" : ""}`}
      data-testid="group-dock-pill"
      aria-expanded={dock.open}
      title={awaitingGroup ? t("group.awaiting", { title: awaitingGroup.title }) : undefined}
      {...pillDrop.dragProps}
      onClick={() => openGroupDock(awaitingGroup?.id)}
    >
      <Users className="size-3" aria-hidden />
      {groups.length > 0 ? t("group.pill.count", { count: groups.length }) : t("group.pill")}
      {awaitingGroup && (
        <span className="kaus-group-pill-dot" data-testid="group-pill-awaiting" role="img" aria-label={t("overview.attention.awaiting")} />
      )}
    </button>
  );

  if (!dock.open) {
    return <div className="kaus-group-dock">{pill}</div>;
  }

  const submitNewGroup = () => {
    const title = newTitle.trim();
    if (!title) return;
    setNewTitle("");
    setCreating(false);
    void run(() => createGroup({ title }), "group.toast.created", { title });
  };

  const minimize = (
    <button
      type="button"
      className="kaus-group-icon-btn"
      aria-label={t("group.minimize")}
      title={t("group.minimize")}
      data-testid="group-minimize"
      onClick={minimizeGroupDock}
    >
      <Minus className="size-3.5" />
    </button>
  );

  return (
    <div className="kaus-group-dock">
      {/* 进入一个组之后浮层变宽（工作区要放得下）：宽度是**同一个浮层**换了个尺寸，
          不是另开一块面板——这一片要读起来像一件工具（DESIGN ★J 追加节）。 */}
      <div
        className={`kaus-group-panel ${expandedGroup ? "is-wide" : ""} ${pillDrop.isOver ? "is-drop-target" : ""}`}
        data-testid="group-dock-panel"
        data-level={expandedGroup ? "group" : "list"}
        role="dialog"
        aria-label={expandedGroup ? expandedGroup.title : t("group.pill")}
        {...pillDrop.dragProps}
      >
        {expandedGroup ? (
          <div className="kaus-group-head">
            <button
              type="button"
              className="kaus-group-icon-btn"
              data-testid="group-back"
              aria-label={t("group.back")}
              title={t("group.back")}
              onClick={() => enterGroup(null)}
            >
              <ArrowLeft size={16} aria-hidden />
            </button>
            <span className="kaus-group-head-title is-group" data-testid="group-head-title" title={expandedGroup.title}>
              {expandedGroup.title}
            </span>
            <button
              type="button"
              className="kaus-group-icon-btn"
              data-testid="group-delete"
              aria-label={t("group.delete")}
              title={t("group.delete")}
              onClick={() => setClosing(expandedGroup)}
            >
              <Trash2 size={15} aria-hidden />
            </button>
            {minimize}
          </div>
        ) : (
          <div className="kaus-group-head">
            <span className="kaus-group-head-title">{t("group.pill")}</span>
            <button
              type="button"
              className="kaus-group-icon-btn"
              data-testid="group-new"
              aria-label={t("group.new")}
              title={t("group.new")}
              onClick={() => setCreating((current) => !current)}
            >
              <Plus size={16} aria-hidden />
            </button>
            {minimize}
          </div>
        )}

        {!expandedGroup && creating && (
          <div className="kaus-group-form" data-testid="group-new-form">
            <input
              autoFocus
              className="kaus-group-input"
              aria-label={t("group.new.title")}
              placeholder={t("group.new.placeholder")}
              value={newTitle}
              onChange={(event) => setNewTitle(event.target.value)}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing || event.key !== "Enter") return;
                event.preventDefault();
                submitNewGroup();
              }}
            />
            <span className="kaus-group-form-actions">
              <Button onClick={() => setCreating(false)}>{t("ui.cancel")}</Button>
              <Button variant="primary" disabled={newTitle.trim() === ""} onClick={submitNewGroup}>
                {t("group.new.submit")}
              </Button>
            </span>
          </div>
        )}

        <div className={`kaus-group-body ${expandedGroup ? "is-workspace" : ""}`}>
          {error && <ErrorNotice error={error} className="kaus-group-error" />}

          {expandedGroup ? (
            <GroupDropItem group={expandedGroup} onDropConversation={(dropped) => joinByDrop(expandedGroup.id, dropped)}>
              {(() => {
                const group = expandedGroup;
                const rows = members[group.id] ?? [];
                return (
                  <div className="kaus-group-detail is-workspace">
                    <div className="kaus-group-actions">
                      {group.coordinatorEnabled && <CoordinatorSettings group={group} />}
                      <span className="kaus-group-menu">
                        <button
                          type="button"
                          className="kaus-group-icon-btn"
                          data-testid="group-add-member"
                          aria-label={t("group.add")}
                          title={t("group.add")}
                          aria-expanded={addMenuOpen}
                          onClick={() => setAddMenuOpen((current) => !current)}
                        >
                          <UserPlus size={16} aria-hidden />
                        </button>
                        {addMenuOpen && (
                          <span className="kaus-bar-pop" role="menu" aria-label={t("group.add")}>
                            <button
                              type="button"
                              role="menuitem"
                              className="kaus-bar-pop-item"
                              onClick={() => {
                                setAddMenuOpen(false);
                                setAddMode("existing");
                              }}
                            >
                              {t("group.add.existing")}
                            </button>
                            <button
                              type="button"
                              role="menuitem"
                              className="kaus-bar-pop-item"
                              onClick={() => {
                                setAddMenuOpen(false);
                                setSpawnProjectId(null); setAddMode("spawn");
                              }}
                            >
                              {t("group.add.spawn")}
                            </button>
                          </span>
                        )}
                      </span>
                    </div>

                    {addMode === "existing" && (
                      <ExistingPicker
                        projectId={group.homeProjectId}
                        projectName={projectName}
                        onCancel={() => setAddMode(null)}
                        onPick={async (conversationId) => {
                          setAddMode(null);
                          await run(
                            () => addGroupMember(group.id, { conversationId }),
                            "group.toast.memberAdded",
                          );
                        }}
                      />
                    )}
                    {addMode === "spawn" && (
                      <SpawnForm
                        projects={projects}
                        defaultProjectId={spawnProjectId ?? group.homeProjectId}
                        onCancel={() => setAddMode(null)}
                        onSubmit={async (body) => {
                          setAddMode(null);
                          await run(async () => {
                            const result = await spawnGroupMember(group.id, body);
                            /* 首句失败**不回滚**（AD-148）：会话与成员都在，只是第一句没发出去。
                               batch27：这条回执不再是 toast——它紧跟在「已添加」后面弹，真机上
                               被后一条盖掉了，用户看到的是"成功"。改成组时间线里一条**常驻通知**
                               （外加成员行上一枚小标），带后端的 message 与 hint。 */
                            const failure = initialMessageFailure(result.initialMessage);
                            if (failure) {
                              noticeSeq.current += 1;
                              const id = `notice:${noticeSeq.current}`;
                              setNotices((current) => [
                                ...current,
                                {
                                  id,
                                  groupId: group.id,
                                  memberId: result.member?.id ?? null,
                                  title: result.conversation?.title ?? "",
                                  message: failure.message,
                                  hint: failure.hint,
                                },
                              ]);
                            }
                            return result;
                          }, "group.toast.memberSpawned");
                        }}
                      />
                    )}

                    {/* 成员栏在这里拼好交给 `GroupWorkspace`——成员的动作在这一层，
                        时间线与资料的取数在那一层，谁也不越界。 */}
                    <GroupWorkspace
                      group={group}
                      members={rows}
                      changeToken={changeToken}
                      notices={notices.filter((notice) => notice.groupId === group.id)}
                      onDismissNotice={(id) =>
                        setNotices((current) => current.filter((notice) => notice.id !== id))
                      }
                      membersPane={
                        <MembersPane
                          disabled={!isGroupOpen(group)}
                          title={t("group.tab.members")}
                          emptyNote={
                            rows.length === 0 ? (
                              <div className="kaus-group-empty">{t("group.noMembers")}</div>
                            ) : null
                          }
                          onDropConversation={(dropped) => joinByDrop(group.id, dropped)}
                        >
                          {rows.map((member) => (
                            <MemberRow
                              key={member.id}
                              member={member}
                              members={rows}
                              projectName={projectName(member.conversation?.projectId)}
                              onAction={onMemberAction}
                              firstMessageFailed={notices.some((notice) => notice.memberId === member.id)}
                              threadRunning={group.thread?.status === "running" || group.thread?.status === "closing"}
                            />
                          ))}
                        </MembersPane>
                      }
                    />
                  </div>
                );
              })()}
            </GroupDropItem>
          ) : (
            <>
              {!error && groups.length === 0 && <div className="kaus-group-empty">{t("group.empty")}</div>}
              {groups.map((group) => {
                const rows = members[group.id] ?? [];
                const count = group.memberCount ?? rows.length;
                const active = rows.filter((row) => row.participationState === "active").length;
                const editing = renamingGroup?.id === group.id ? renamingGroup : null;
                return (
                  <GroupDropItem key={group.id} group={group} onDropConversation={(dropped) => joinByDrop(group.id, dropped)}>
                    <div
                      className="kaus-group-row-line"
                      onContextMenu={(event) => {
                        if (editing) return;
                        event.preventDefault();
                        menuSeq.current += 1;
                        setMenuPoint({ groupId: group.id, point: { x: event.clientX, y: event.clientY, seq: menuSeq.current } });
                      }}
                    >
                      {editing ? (
                        <span className="kaus-group-row">
                          <span className={`kaus-run-dot ${active > 0 ? "kaus-accent" : "is-idle"}`} aria-hidden="true" />
                          <input
                            className="kaus-sidebar-title-input"
                            autoFocus
                            data-testid={`group-rename-${group.id}`}
                            aria-label={t("group.new.title")}
                            maxLength={200}
                            value={editing.title}
                            onChange={(event) => setRenamingGroup({ id: group.id, title: event.target.value })}
                            onFocus={(event) => event.currentTarget.select()}
                            onBlur={() => setRenamingGroup(null)}
                            onKeyDown={(event) => {
                              event.stopPropagation();
                              if (event.nativeEvent.isComposing) return;
                              if (event.key === "Escape") { event.preventDefault(); setRenamingGroup(null); }
                              if (event.key === "Enter") { event.preventDefault(); saveGroupTitle(group, editing.title); }
                            }}
                          />
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="kaus-group-row"
                          data-testid={`group-open-${group.id}`}
                          onClick={() => enterGroup(group.id)}
                        >
                          {/* 组的状态点：有人在跑就亮，其余中性（★H）。 */}
                          <span className={`kaus-run-dot ${active > 0 ? "kaus-accent" : "is-idle"}`} aria-hidden="true" />
                          <span className="kaus-group-member-title">{group.title}</span>
                          <span className="kaus-group-dim">{t(count === 1 ? "group.memberCount.one" : "group.memberCount", { count })}</span>
                        </button>
                      )}
                      <RowMenu
                        triggerLabel={t("group.row.menuFor", { title: group.title })}
                        menuLabel={t("group.row.menu")}
                        testId={`group-menu-${group.id}`}
                        point={menuPoint?.groupId === group.id ? menuPoint.point : null}
                        items={[
                          {
                            id: "rename",
                            icon: <Pencil size={16} aria-hidden />,
                            label: t("group.row.rename"),
                            onSelect: () => setRenamingGroup({ id: group.id, title: group.title }),
                          },
                          {
                            id: "delete",
                            icon: <Trash2 size={16} aria-hidden />,
                            label: t("group.row.delete"),
                            separated: true,
                            onSelect: () => setClosing(group),
                          },
                        ]}
                      />
                    </div>
                  </GroupDropItem>
                );
              })}
            </>
          )}
        </div>
      </div>

      {closing && (
        <CloseGroupDialog
          group={closing}
          onCancel={() => setClosing(null)}
          onConfirm={async () => {
            const group = closing;
            setClosing(null);
            await run(async () => {
              const result = await closeGroup(group.id);
              forgetGroup(group.id);
              if (dock.expandedGroupId === group.id) enterGroup(null);
              return result;
            }, "group.toast.deleted", { title: group.title });
          }}
        />
      )}

      {/* batch52 第 5 件（AD-173）：在本组叫什么。回执点名的是**后端重算之后**的
          那个名字——用户敲「写手」而组里已经有一位写手时，他接下来真的会被告知的
          名字是「写手#2」，回执说的必须是后者。 */}
      {renaming && (
        <RenameMemberDialog
          name={memberDisplayName(members[renaming.groupId] ?? [], renaming.id)}
          current={renaming.roleLabel ?? ""}
          onCancel={() => setRenaming(null)}
          onSubmit={async (roleLabel) => {
            const member = renaming;
            setRenaming(null);
            try {
              const result = await renameGroupMember(member.groupId, member.id, roleLabel);
              toast(
                t("group.member.renameDone", {
                  name: result.member.displayName || result.member.conversation?.title || member.id,
                }),
                "ok",
              );
            } catch (failure) {
              toast(
                t("group.member.renameFailed", { error: describeGroupFailure(failure, t) }),
                "bad",
              );
            } finally {
              await refreshGroups();
            }
          }}
        />
      )}
    </div>
  );
}
