/* Group 浮窗（DESIGN ★ 定稿 J，batch23 第 2 件）。
 *
 * 右下角常驻一枚中性 Pill「协作组 · N」；点开是一个固定在右下、宽 420px、最高 60vh
 * 的浮层——**不是抽屉，不占主区**。开/最小化状态记 sessionStorage（`lib/groupStore.ts`）。
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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Archive, ChevronDown, ChevronRight, Ellipsis, Minus, Plus, Terminal, UserPlus, Users, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { conversationPath } from "../lib/routes";
import { requestExternalHandoff } from "../lib/externalSurface";
import { hasCapability } from "./cards/capabilities";
import "../styles/conversation-polish.css";

import { useLocale, type DictKey } from "../i18n";
import { BarMenu, type BarMenuOption } from "./ComposerBar";
import {
  GroupWorkspace,
  memberDisplayName,
} from "./GroupWorkspace";
import { Button, Modal, ModalSub, ModalTitle, toast } from "./ui";
import { backendDisplay } from "../lib/backend-display";
import {
  addGroupMember,
  closeGroup,
  conflictGroupTitle,
  createGroup,
  GROUP_ERROR_KEYS,
  initialMessageFailure,
  patchGroup,
  pauseGroupMember,
  promoteGroupMember,
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
import { useGroupDrop, type GroupDropPayload } from "../lib/groupDrop";
import { matchProjectId } from "../lib/projectRef";
import {
  closeGroupDock,
  forgetGroup,
  minimizeGroupDock,
  openGroupDock,
  refreshGroups,
  toggleGroupExpanded,
  useDockState,
  useGroupState,
} from "../lib/groupStore";
import {
  fetchProjectBindings,
  fetchRecentConversations,
  fetchProjects,
  fetchModelCatalog,
  fetchBackendUiCapabilities,
  SessionApiError,
  type BindingWire,
  type ProjectWire,
} from "../lib/sessionApi";

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

/** 浮层里的小菜单统一的收起方式：点外面、按 Esc。两处都用它，行为不分叉。 */
function useDismiss(open: boolean, close: () => void) {
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

/** 「…」菜单五动作（batch26 加了「定向发送」）。已经走了的成员只剩
 *  「恢复（重新加入）」——暂停一个 left 的人是 409；定向发送也只给 active 的人。 */
function MemberActions({
  member,
  onPause,
  onResume,
  onRemove,
  onPromote,
  onDirected,
  onRename,
  threadRunning = false,
}: {
  member: GroupMemberWire;
  onPause: () => void;
  onResume: () => void;
  onRemove: () => void;
  onPromote: () => void;
  onDirected: () => void;
  /** batch52 第 5 件：在本组叫什么（写 `roleLabel`）。 */
  onRename: () => void;
  threadRunning?: boolean;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const [terminalSupported, setTerminalSupported] = useState(false);
  const navigate = useNavigate();
  const left = member.participationState === "left";
  const paused = member.participationState === "paused";
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
          {/* 定向发送：只发给这一个人（走 `/members/{id}/send`，不是把广播缩到一人）。 */}
          {!left && !paused && (
            <button type="button" role="menuitem" className="kaus-bar-pop-item" onClick={pick(onDirected)}>
              {t("group.member.directed")}
            </button>
          )}
          {!left && !paused && (
            <button type="button" role="menuitem" className="kaus-bar-pop-item" onClick={pick(onPause)}>
              {t("group.member.pause")}
            </button>
          )}
          {(paused || left) && (
            <button type="button" role="menuitem" className="kaus-bar-pop-item" onClick={pick(onResume)}>
              {left ? t("group.member.rejoin") : t("group.member.resume")}
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
          <button type="button" role="menuitem" className="kaus-bar-pop-item" onClick={pick(onPromote)}>
            {t("group.member.promote")}
          </button>
        </span>
      )}
    </span>
  );
}

function MemberRow({
  member,
  members,
  projectName,
  onAction,
  onDirected,
  firstMessageFailed = false,
  threadRunning = false,
}: {
  member: GroupMemberWire;
  /** 整组的成员行——显示名要看得见组里有没有第二个同名的（`#2` 后缀）。 */
  members: GroupMemberWire[];
  projectName: string | null;
  onAction: (
    // batch52：多一条 "rename"（在本组叫什么，AD-173）。
    action: "pause" | "resume" | "remove" | "promote" | "leader" | "rename",
    member: GroupMemberWire,
  ) => void;
  onDirected: (member: GroupMemberWire) => void;
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
        onPause={() => onAction("pause", member)}
        onResume={() => onAction("resume", member)}
        onRemove={() => onAction("remove", member)}
        onPromote={() => onAction("promote", member)}
        onDirected={() => onDirected(member)}
        onRename={() => onAction("rename", member)}
      />
      {/* batch26：左栏只有 216px，标题与元信息挤在一行会把标题截成「文…」。
          第二行放角色/状态标、项目、引擎、表面图标——标题永远读得完整。 */}
      <span className="kaus-group-member-meta">
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

/* batch42 第 2 件（★J-4）：**成员栏 = 放置目标**。
 *
 * 这一栏是展开态浮窗的左栏，同时也就是组工作区的成员栏（`GroupWorkspace` 把它
 * 原样摆在三栏的第一栏），所以这一枚组件同时覆盖了那两处。放置的动作与浮窗里
 * 手点的那两条路是同一条调用（会话 → 加入，项目 → 启动新成员），虚线框按
 * DESIGN §5.9：2px `--k-accent` 虚线，不加别的。 */
function MembersPane({
  title,
  emptyNote,
  onDropConversation,
  children,
}: {
  title: string;
  emptyNote: React.ReactNode;
  onDropConversation: (dropped: GroupDropPayload) => void;
  children: React.ReactNode;
}) {
  const { dragProps, isOver } = useGroupDrop(onDropConversation);
  return (
    <div
      className={`kaus-group-pane ${isOver ? "is-drop-target" : ""}`}
      data-pane="members"
      data-testid="group-pane-members"
      {...dragProps}
    >
      <div className="kaus-group-pane-head">
        <span>{title}</span>
      </div>
      {emptyNote}
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 添加成员：两条路
 * ------------------------------------------------------------------ */

/** 「启动新成员」的小表单：项目 ▾ → 引擎（默认绑定，只读）→ 模型 ▾ → 首句（可选）。 */
function SpawnForm({
  projects,
  defaultProjectId,
  onSubmit,
  onCancel,
}: {
  projects: ProjectWire[];
  defaultProjectId: string | null;
  onSubmit: (body: { projectId: string; bindingId: string | null; modelId: string | null; initialMessage: string | null }) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const [projectId, setProjectId] = useState(defaultProjectId ?? projects[0]?.id ?? "");
  const [bindings, setBindings] = useState<BindingWire[] | null>(null);
  const [models, setModels] = useState<BarMenuOption[]>([]);
  const [modelId, setModelId] = useState<string | null>(null);
  const [first, setFirst] = useState("");
  const [busy, setBusy] = useState(false);

  /* 引擎**不允许在这里切**（用户已定的口味）：只显示该项目的默认 Binding。
     项目没有默认 Binding 时这一行整个不渲染，交给后端的 `binding_required` 说话。 */
  const binding = useMemo(
    () => bindings?.find((row) => row.isDefault) ?? bindings?.[0] ?? null,
    [bindings],
  );

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setBindings(null);
    setModels([]);
    setModelId(null);
    fetchProjectBindings(projectId, controller.signal)
      .then((payload) => setBindings(payload.bindings))
      .catch(() => setBindings([]));
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (!binding) return;
    const controller = new AbortController();
    fetchModelCatalog(binding.backendId, binding.id, controller.signal)
      .then((catalog) => {
        setModels(
          catalog.models.map((model) => ({ value: model.modelId, label: model.displayName || model.modelId })),
        );
        setModelId(catalog.defaultModelId ?? catalog.models[0]?.modelId ?? null);
      })
      // 目录取不到就不给模型下拉：后端会用 Binding 的默认模型（AD-71）。
      .catch(() => setModels([]));
    return () => controller.abort();
  }, [binding]);

  const submit = async () => {
    if (!projectId || busy) return;
    setBusy(true);
    try {
      await onSubmit({
        projectId,
        bindingId: binding?.id ?? null,
        modelId,
        initialMessage: first.trim() || null,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="kaus-group-form" data-testid="group-spawn-form">
      <BarMenu
        label={t("group.spawn.project")}
        value={projectId}
        display={projects.find((project) => project.id === projectId)?.displayName ?? projectId}
        options={projects.map((project) => ({ value: project.id, label: project.displayName || project.slug }))}
        onSelect={setProjectId}
      />
      {/* 引擎只读（会话里不允许切引擎）。没有默认 Binding 就整枚不渲染。 */}
      {binding && (
        <span className="kaus-bar-pill" data-testid="group-spawn-engine">
          {backendDisplay(binding.backendId).displayName}
        </span>
      )}
      {models.length > 0 && modelId && (
        <BarMenu
          label={t("group.spawn.model")}
          value={modelId}
          display={models.find((model) => model.value === modelId)?.label ?? modelId}
          options={models}
          onSelect={setModelId}
        />
      )}
      <input
        className="kaus-group-input"
        data-testid="group-spawn-first"
        aria-label={t("group.spawn.first")}
        placeholder={t("group.spawn.firstPlaceholder")}
        value={first}
        onChange={(event) => setFirst(event.target.value)}
      />
      <span className="kaus-group-form-actions">
        <Button onClick={onCancel}>{t("ui.cancel")}</Button>
        <Button variant="primary" disabled={busy || !projectId} onClick={() => void submit()}>
          {t("group.spawn.submit")}
        </Button>
      </span>
    </div>
  );
}

/** 「选择已有会话」：当前项目下的会话，走 `BarMenu`（项数多了它自己带过滤框）。 */
function ExistingPicker({
  projectId,
  projectName,
  onPick,
  onCancel,
}: {
  projectId: string | null;
  /** batch52：`projectId → 项目名`（取不到就是 null，那一段不出现）。 */
  // batch52
  projectName: (projectId: string | null | undefined) => string | null;
  onPick: (conversationId: string) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const [options, setOptions] = useState<BarMenuOption[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    /* 缺省的 `GET /api/conversations` **不含** `group_only` 的那些——它们本来就已经
       在某个组里，列出来只会让人点到一个必然 409 的选项。 */
    fetchRecentConversations(controller.signal, projectId ? { project: projectId } : {})
      .then((payload) =>
        setOptions(
          payload.conversations.map((row) => ({
            value: row.id,
            /* batch52 第 5 件（真机「藏过头了」第 1 条）：一行由「只有标题」改成
               「标题 · 引擎 · 项目」。长列表里几条同名的旧会话此前长得一模一样，
               靠标题分不出该点哪一条。缺的那一段**整个不出现**（AD-71：不写
               「未知引擎」），所以只有标题时这一行与从前一个字不差。 */
            label: [
              row.title,
              row.backendId ? backendDisplay(row.backendId).displayName : null,
              projectName(row.projectId),
            ]
              .filter(Boolean)
              .join(" · "),
          })),
        ),
      )
      .catch(() => setOptions([]));
    return () => controller.abort();
  }, [projectId, projectName]);

  return (
    <div className="kaus-group-form" data-testid="group-existing-picker">
      <BarMenu
        label={t("group.add.existing")}
        value={null}
        display={t("group.add.pick")}
        options={options ?? []}
        emptyNote={t("group.add.noConversations")}
        onSelect={(conversationId) => void onPick(conversationId)}
      />
      <span className="kaus-group-form-actions">
        <Button onClick={onCancel}>{t("ui.cancel")}</Button>
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 关闭组：处置弹窗
 * ------------------------------------------------------------------ */

function CloseGroupDialog({
  group,
  members,
  onConfirm,
  onCancel,
}: {
  group: GroupWire;
  members: GroupMemberWire[];
  onConfirm: (onClose: "keep" | "archive") => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const [choice, setChoice] = useState<"keep" | "archive">("keep");
  const [busy, setBusy] = useState(false);
  /* 「这次关组会做什么」不靠猜：三档 retention 各有几条，就地数给用户看。 */
  const counts = useMemo(() => {
    const tally = { persistent: 0, decide: 0, ephemeral: 0 };
    for (const member of members) {
      const retention = member.conversation?.retention;
      if (retention === "persistent") tally.persistent += 1;
      else if (retention === "ephemeral") tally.ephemeral += 1;
      else if (retention === "decide_on_group_close") tally.decide += 1;
    }
    return tally;
  }, [members]);

  return (
    <Modal onClose={onCancel} width={460}>
      <ModalTitle>{t("group.close.title")}</ModalTitle>
      <ModalSub>{t("group.close.sub", { title: group.title })}</ModalSub>
      <div className="kaus-group-choices">
        {(["keep", "archive"] as const).map((option) => (
          <label key={option} className={`kaus-group-choice ${choice === option ? "is-current" : ""}`}>
            <input
              type="radio"
              name="group-close"
              value={option}
              checked={choice === option}
              onChange={() => setChoice(option)}
            />
            <span>
              <b>{t(`group.close.${option}` as DictKey)}</b>
              <em>{t(`group.close.${option}.body` as DictKey)}</em>
            </span>
          </label>
        ))}
      </div>
      <p className="kaus-group-close-counts" data-testid="group-close-counts">
        {t("group.close.counts", {
          persistent: counts.persistent,
          decide: counts.decide,
          ephemeral: counts.ephemeral,
        })}
      </p>
      <div className="kaus-return-actions">
        <Button onClick={onCancel}>{t("ui.cancel")}</Button>
        <Button
          variant="danger"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            void onConfirm(choice).finally(() => setBusy(false));
          }}
        >
          {t("group.close.confirm")}
        </Button>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ *
 * batch52 第 5 件（AD-173）：在本组叫什么
 * ------------------------------------------------------------------ */

/** 改这位成员**在本组**的名字（写 `roleLabel`），会话自己的标题不动。
 *
 *  为什么是 `roleLabel` 而不是会话标题：显示名的回退链是
 *  `roleLabel > 会话标题 > id 尾段`，第一位正好是「这位在这个房间里的名字」。
 *  改会话标题会跟着改到别处去——同一条会话可以在这个组里叫「评审」，在侧栏仍旧
 *  叫它本来的名字。
 *
 *  **留空 = 恢复成会话标题**，不是「叫空名字」：所以这枚按钮在空值时照样可点。 */
// batch52
function RenameMemberDialog({
  name,
  current,
  onSubmit,
  onCancel,
}: {
  name: string;
  current: string;
  onSubmit: (roleLabel: string) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const [value, setValue] = useState(current);
  const [busy, setBusy] = useState(false);
  const save = () => {
    if (busy) return;
    setBusy(true);
    void onSubmit(value.trim()).finally(() => setBusy(false));
  };
  return (
    <Modal onClose={onCancel} width={420}>
      <ModalTitle>{t("group.member.renameTitle", { name })}</ModalTitle>
      <ModalSub>{t("group.member.renameHint")}</ModalSub>
      <input
        autoFocus
        className="kaus-group-input"
        aria-label={t("group.member.renameField")}
        data-testid="group-rename-input"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || event.key !== "Enter") return;
          event.preventDefault();
          save();
        }}
      />
      <div className="kaus-return-actions">
        <Button onClick={onCancel}>{t("ui.cancel")}</Button>
        <Button variant="primary" disabled={busy} onClick={save}>
          {t("group.member.renameSave")}
        </Button>
      </div>
    </Modal>
  );
}

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
  const [addMode, setAddMode] = useState<AddMode>(null);
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [closing, setClosing] = useState<GroupWire | null>(null);
  /** batch52 第 5 件：正在给谁改组内名字（null = 没开那个弹窗）。 */
  // batch52
  const [renaming, setRenaming] = useState<GroupMemberWire | null>(null);
  const [directed, setDirected] = useState<string | null>(null);
  /** 组级流每来一条变更就 +1：展开态的时间线拿它当重取的信号。 */
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
    async (action: () => Promise<unknown>, okKey: DictKey, vars?: Record<string, string | number>) => {
      try {
        await action();
        toast(t(okKey, vars), "ok");
      } catch (failure) {
        toast(describeGroupFailure(failure, t), "bad");
      } finally {
        await refreshGroups();
      }
    },
    [t],
  );

  const expanded = dock.expandedGroupId;
  const expandedGroup = groups.find((group) => group.id === expanded) ?? null;
  const expandedMembers = expanded ? (members[expanded] ?? []) : [];

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
      );
    },
    [run, projects],
  );
  /* 收起态的胶囊：drop 到胶囊上加入哪个组？
       ① 浮窗记着一个活动组（`expandedGroupId`）→ 加入它；
       ② 没有活动组但只开着一个组 → 就是它（用户真机第一次试拖就卡在这里：刚刷新
          的页面没有"活动组"，拖过去一点反应都没有——一个组都不用选的场景不该让人选）；
       ③ 没有活动组且开着多个组 → 打开浮窗、把第一个组摊开，并告诉用户拖到具体的
          组上（不替用户猜是哪个）；
       ④ 一个组都没有 → 告诉用户先建组。
     只有④不加虚线框（没有可放的目标）；②③都要有反馈，静默是最坏的结果。 */
  const openGroups = groups.filter(isGroupOpen);
  const pillDrop = useGroupDrop(
    useCallback(
      (dropped: GroupDropPayload) => {
        const target = expanded ?? (openGroups.length === 1 ? openGroups[0].id : null);
        if (target) {
          joinByDrop(target, dropped);
          return;
        }
        if (openGroups.length === 0) {
          toast(t("group.drop.noGroup"), "bad");
          return;
        }
        openGroupDock(openGroups[0].id);
        toast(t("group.drop.pickGroup", { count: openGroups.length }), "bad");
      },
      [expanded, openGroups, joinByDrop, t],
    ),
    openGroups.length === 0,
  );

  const onMemberAction = useCallback(
    (
      action: "pause" | "resume" | "remove" | "promote" | "leader" | "rename",
      member: GroupMemberWire,
    ) => {
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
      const map = {
        pause: [pauseGroupMember, "group.toast.paused"],
        resume: [resumeGroupMember, "group.toast.resumed"],
        remove: [removeGroupMember, "group.toast.removed"],
        promote: [promoteGroupMember, "group.toast.promoted"],
      } as const;
      const [call, okKey] = map[action];
      void run(() => call(member.groupId, member.id), okKey);
    },
    [members, run],
  );

  const pill = (
    <button
      type="button"
      className={`kaus-group-pill ${pillDrop.isOver ? "is-drop-target" : ""}`}
      data-testid="group-dock-pill"
      aria-expanded={dock.open}
      {...pillDrop.dragProps}
      onClick={() => openGroupDock()}
    >
      <Users className="size-3" aria-hidden />
      {groups.length > 0 ? t("group.pill.count", { count: groups.length }) : t("group.pill")}
    </button>
  );

  if (!dock.open) {
    return <div className="kaus-group-dock">{pill}</div>;
  }

  return (
    <div className="kaus-group-dock">
      {/* 展开一个组之后浮层变宽（三栏要放得下）：宽度是**同一个浮层**换了个尺寸，
          不是另开一块面板——这一片要读起来像一件工具（DESIGN ★J 追加节）。 */}
      <div
        className={`kaus-group-panel ${expandedGroup ? "is-wide" : ""}`}
        data-testid="group-dock-panel"
        role="dialog"
        aria-label={t("group.pill")}
      >
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
          <button
            type="button"
            className="kaus-group-icon-btn"
            aria-label={t("common.close")}
            title={t("common.close")}
            data-testid="group-close-dock"
            onClick={closeGroupDock}
          >
            <X className="size-3.5" />
          </button>
        </div>

        {creating && (
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
                const title = newTitle.trim();
                if (!title) return;
                setNewTitle("");
                setCreating(false);
                void run(() => createGroup({ title }), "group.toast.created", { title });
              }}
            />
            <span className="kaus-group-form-actions">
              <Button onClick={() => setCreating(false)}>{t("ui.cancel")}</Button>
              <Button
                variant="primary"
                disabled={newTitle.trim() === ""}
                onClick={() => {
                  const title = newTitle.trim();
                  if (!title) return;
                  setNewTitle("");
                  setCreating(false);
                  void run(() => createGroup({ title }), "group.toast.created", { title });
                }}
              >
                {t("group.new.submit")}
              </Button>
            </span>
          </div>
        )}

        <div className={`kaus-group-body ${expandedGroup ? "is-workspace" : ""}`}>
          {error && <div className="kaus-group-error">{error}</div>}
          {!error && groups.length === 0 && <div className="kaus-group-empty">{t("group.empty")}</div>}

          {groups.map((group) => {
            const open = group.id === expanded;
            const rows = members[group.id] ?? [];
            const count = group.memberCount ?? rows.length;
            const active = rows.filter((row) => row.participationState === "active").length;
            return (
              <div key={group.id} className="kaus-group-item" data-testid={`group-item-${group.id}`}>
                <button
                  type="button"
                  className="kaus-group-row"
                  aria-expanded={open}
                  onClick={() => {
                    toggleGroupExpanded(group.id);
                    setAddMode(null);
                    setAddMenuOpen(false);
                    setDirected(null);
                  }}
                >
                  {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
                  {/* 组的状态点：有人在跑就亮，其余中性（★H）。 */}
                  <span
                    className={`kaus-run-dot ${active > 0 ? "kaus-accent" : "is-idle"}`}
                    aria-hidden="true"
                  />
                  <span className="kaus-group-member-title">{group.title}</span>
                  <span className="kaus-group-dim">{t("group.memberCount", { count })}</span>
                </button>

                {open && (
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
                                setAddMode("spawn");
                              }}
                            >
                              {t("group.add.spawn")}
                            </button>
                          </span>
                        )}
                      </span>
                      <button
                        type="button"
                        className="kaus-group-icon-btn kaus-group-close-action"
                        data-testid="group-close"
                        aria-label={t("group.close")}
                        title={t("group.close")}
                        onClick={() => setClosing(group)}
                      >
                        <Archive size={15} aria-hidden />
                      </button>
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
                        defaultProjectId={group.homeProjectId}
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

                    {/* batch26：展开态 = 三栏。左栏（成员）在这里拼好交给
                        `GroupWorkspace`——成员的五动作在这一层，时间线与 Context
                        Packet 的取数在那一层，谁也不越界。 */}
                    <GroupWorkspace
                      group={group}
                      members={rows}
                      changeToken={changeToken}
                      notices={notices.filter((notice) => notice.groupId === group.id)}
                      onDismissNotice={(id) =>
                        setNotices((current) => current.filter((notice) => notice.id !== id))
                      }
                      directedMemberId={directed}
                      onClearDirected={() => setDirected(null)}
                      membersPane={
                        <MembersPane
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
                              onDirected={(picked) => setDirected(picked.id)}
                              firstMessageFailed={notices.some((notice) => notice.memberId === member.id)}
                              threadRunning={group.thread?.status === "running" || group.thread?.status === "closing"}
                            />
                          ))}
                        </MembersPane>
                      }
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {closing && (
        <CloseGroupDialog
          group={closing}
          members={members[closing.id] ?? []}
          onCancel={() => setClosing(null)}
          onConfirm={async (onClose) => {
            const group = closing;
            setClosing(null);
            await run(async () => {
              const result = await closeGroup(group.id, { onClose });
              /* batch27 第 4 件（真机 C5）：关成功的那一刻就把这个组从共享 store 里
                 抹掉，并让会话索引重取一次——否则侧栏那枚组名小标要等下一轮
                 30s 轮询才消失。 */
              forgetGroup(group.id);
              return result;
            }, "group.toast.closed", { title: group.title });
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
