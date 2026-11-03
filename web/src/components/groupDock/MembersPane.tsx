import type { ReactNode } from "react";

import { isGroupOpen, type GroupWire } from "../../lib/groupsApi";
import { useGroupDrop, type GroupDropPayload } from "../../lib/groupDrop";

/* 协作组浮窗里接受「拖会话入组」的两种容器：组条目本身，以及成员分栏。 */

/** Every visible surface of a group accepts membership drops, including its composer. */
export function GroupDropItem({ group, onDropConversation, children }: {
  group: GroupWire;
  onDropConversation: (dropped: GroupDropPayload) => void;
  children: ReactNode;
}) {
  const { dragProps, isOver } = useGroupDrop(onDropConversation, !isGroupOpen(group));
  return (
    <div className={`kaus-group-item ${isOver ? "is-drop-target" : ""}`}
      data-testid={`group-item-${group.id}`} {...dragProps}>
      {children}
    </div>
  );
}

export function MembersPane({
  title,
  emptyNote,
  onDropConversation,
  disabled,
  children,
}: {
  title: string;
  emptyNote: ReactNode;
  onDropConversation: (dropped: GroupDropPayload) => void;
  disabled: boolean;
  children: ReactNode;
}) {
  const { dragProps, isOver } = useGroupDrop(onDropConversation, disabled);
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
