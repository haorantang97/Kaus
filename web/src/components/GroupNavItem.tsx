import { useCallback } from "react";
import { Users } from "lucide-react";

import { useLocale } from "../i18n";
import { addGroupMember, isGroupOpen, spawnGroupMember } from "../lib/groupsApi";
import { useGroupDrop, type GroupDropPayload } from "../lib/groupDrop";
import { matchProjectId } from "../lib/projectRef";
import { minimizeGroupDock, openGroupDock, refreshGroups, useDockState, useGroupState } from "../lib/groupStore";
import { fetchProjects } from "../lib/sessionApi";
import { describeGroupFailure } from "./GroupDock";
import { toast } from "./ui";

/* 侧栏导航里的「协作组」入口。
 *
 * 点它 = 打开 / 收起协作组浮窗（与右下角胶囊同一个浮窗）。它同时是拖入组的放置目标，
 * 规则与胶囊一致（★J-4）：
 *   ① 浮窗记着一个展开的组 → 加入它；
 *   ② 没有展开的组但只开着一个组 → 就是它；
 *   ③ 开着多个组 → 打开浮窗摊开第一个组，提示拖到具体那个组的成员栏上；
 *   ④ 一个组都没有 → 提示先建组，不给虚线框。
 * 拖会话 = `addGroupMember`；拖项目 = `spawnGroupMember`（只传 projectId，用默认绑定）。 */
export function GroupNavItem() {
  const { t } = useLocale();
  const { groups } = useGroupState();
  const dock = useDockState();
  const openGroups = groups.filter(isGroupOpen);

  const join = useCallback(
    async (groupId: string, dropped: GroupDropPayload) => {
      try {
        if (dropped.kind === "conversation") {
          await addGroupMember(groupId, { conversationId: dropped.conversationId });
          toast(
            dropped.label ? t("group.toast.memberAddedNamed", { title: dropped.label }) : t("group.toast.memberAdded"),
            "ok",
          );
        } else {
          const { projects } = await fetchProjects();
          const projectId = matchProjectId(projects, dropped.projectId);
          const known = projects.find((project) => project.id === projectId);
          const name = dropped.label || known?.displayName || known?.slug || "";
          await spawnGroupMember(groupId, { projectId });
          toast(name ? t("group.toast.memberSpawnedFrom", { name }) : t("group.toast.memberSpawned"), "ok");
        }
      } catch (failure) {
        toast(describeGroupFailure(failure, t), "bad");
      } finally {
        await refreshGroups();
      }
    },
    [t],
  );

  const drop = useGroupDrop(
    useCallback(
      (dropped: GroupDropPayload) => {
        const target = dock.expandedGroupId ?? (openGroups.length === 1 ? openGroups[0].id : null);
        if (target) {
          void join(target, dropped);
          return;
        }
        if (openGroups.length === 0) {
          toast(t("group.drop.noGroup"), "bad");
          return;
        }
        openGroupDock(openGroups[0].id);
        toast(t("group.drop.pickGroup", { count: openGroups.length }), "bad");
      },
      [dock.expandedGroupId, openGroups, join, t],
    ),
    openGroups.length === 0,
  );

  const label = t("group.pill");
  return (
    <button
      type="button"
      className={`shell-nav-item ${dock.open ? "is-active" : ""} ${drop.isOver ? "is-drop-target" : ""}`}
      data-testid="nav-groups"
      aria-expanded={dock.open}
      title={label}
      {...drop.dragProps}
      onClick={() => (dock.open ? minimizeGroupDock() : openGroupDock())}
    >
      <span className="shell-nav-icon"><Users /></span>
      <span className="shell-nav-label">{label}</span>
      {openGroups.length > 0 && <span className="shell-nav-count">{openGroups.length}</span>}
    </button>
  );
}
