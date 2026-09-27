/* 拖拽入组（架构 v1.2 §路径 A「拖入 Group」，batch42 第 2 件 / DESIGN ★J-4）。
 *
 * 「拖入 Group」是 v1.2 就锁定的需求，此前只做了 GroupDock 里那个下拉（「选择已有
 * 会话」），拖拽从来没做过。这里放两件东西，让三处放置目标共用同一套逻辑：
 *
 *   - 两个 MIME：拖源与放置目标之间唯一的约定。**只认这两种**——从别处（文件、
 *     文本、外站链接）拖过来的东西一律不响应，连虚线框都不给。
 *   - `useGroupDrop(onDrop)`：返回 `{ dragProps, isOver }`，调用方把 `dragProps`
 *     摊到那个元素上，按 `isOver` 加 `is-drop-target` 类。
 *
 * batch43 第 2 件：拖源多了一种——**项目**（侧栏的项目分组表头 / 轮盘上的项目节点）。
 * 两种拖源落在同一批放置目标上，但松手之后干的事不同，所以回调里带一个 `kind`：
 *
 *   - `conversation` → 把这条已有会话加进组（`POST /groups/{id}/members`）；
 *   - `project`      → 从这个项目的默认引擎起一条**新**会话加进组，也就是浮窗里
 *                      「启动新成员」那条路（`POST /groups/{id}/spawn`）。
 *
 * 判 kind 靠 MIME 而不是靠值的形状：两个 id 都长得像 `xxx:yyy`，靠猜早晚猜错。
 * 一次拖拽只带一种 MIME（拖源那头就是这么写的），万一两种都在，**会话优先**——
 * 它是更保守的那个动作（加已有的人，不凭空起新会话）。
 *
 * 视觉按 DESIGN.md §5.9 已有的规矩：**2px `--k-accent` 虚线框**，不加别的。
 *
 * 一条诚实边界：两种 drop 都不另开后端路径——会话走「选择已有会话」那条，项目走
 * 「启动新成员」那条，与浮窗里手点的完全同一条调用。否则两个入口的错误码与回执
 * 早晚会长成两套。
 */

import { useCallback, useMemo, useState } from "react";

/** 拖一条会话时 `dataTransfer` 里唯一的那个类型。 */
export const CONVERSATION_DRAG_MIME = "application/x-kaus-conversation";
/** 拖一个项目时 `dataTransfer` 里唯一的那个类型（batch43）。 */
export const PROJECT_DRAG_MIME = "application/x-kaus-project";

/** `types` 在 jsdom 里是普通数组，在浏览器里是 DOMStringList，两边都支持
 *  `includes`，所以按数组读并兜一层。 */
function hasType(transfer: DataTransfer | null | undefined, mime: string): boolean {
  if (!transfer) return false;
  const types = transfer.types;
  if (!types) return false;
  return Array.from(types as unknown as ArrayLike<string>).includes(mime);
}

/** 这次拖拽是不是"一条会话"。 */
export function isConversationDrag(transfer: DataTransfer | null | undefined): boolean {
  return hasType(transfer, CONVERSATION_DRAG_MIME);
}

/** 这次拖拽是不是"一个项目"。 */
export function isProjectDrag(transfer: DataTransfer | null | undefined): boolean {
  return hasType(transfer, PROJECT_DRAG_MIME);
}

/** 这次拖拽我们认不认。`dragover` 阶段只能看 `types`（`getData` 那时按规范是空的），
 *  所以"给不给虚线框"只由 MIME 决定，不看值。 */
export function isGroupDrag(transfer: DataTransfer | null | undefined): boolean {
  return isConversationDrag(transfer) || isProjectDrag(transfer);
}

export interface ConversationDropProps {
  onDragOver: (event: React.DragEvent) => void;
  onDragLeave: (event: React.DragEvent) => void;
  onDrop: (event: React.DragEvent) => void;
}

/** 拖过来的那条会话：id 是唯一要紧的东西，`label` 只用来把回执说成人话
 *  （拖源顺手在 `text/plain` 上带了标题；带不到就空串，回执退回一句通名）。 */
export interface DroppedConversation {
  kind: "conversation";
  conversationId: string;
  label: string;
}

/** 拖过来的那个项目。`projectId` 可能是领域库 id，也可能是轮盘那头只知道的裸名字
 *  ——放置那头用 `lib/projectRef.ts` 的 `matchProjectId` 统一成 id，这里不管。 */
export interface DroppedProject {
  kind: "project";
  projectId: string;
  label: string;
}

export type GroupDropPayload = DroppedConversation | DroppedProject;

/** 从一次 `drop` 的 `dataTransfer` 上读出我们认得的那件东西；认不出来给 null。 */
export function readGroupDrag(transfer: DataTransfer | null | undefined): GroupDropPayload | null {
  if (!transfer) return null;
  const label = transfer.getData("text/plain") || "";
  if (isConversationDrag(transfer)) {
    const conversationId = transfer.getData(CONVERSATION_DRAG_MIME);
    if (conversationId) return { kind: "conversation", conversationId, label };
  }
  if (isProjectDrag(transfer)) {
    const projectId = transfer.getData(PROJECT_DRAG_MIME);
    if (projectId) return { kind: "project", projectId, label };
  }
  return null;
}

export interface ConversationDrop {
  dragProps: ConversationDropProps;
  /** 正悬在这个目标上（且拖的确实是我们认得的东西）。调用方据此加 `is-drop-target`。 */
  isOver: boolean;
}

/**
 * 一个放置目标。
 *
 * @param onDrop   松手时拿到那件东西（会话或项目，外加用来写回执的显示名）。
 * @param disabled 这个目标现在不接受放置（例如收起态的胶囊上一个组都没有）——
 *                 这时连 `preventDefault` 都不发，浏览器会显示"禁止"光标，
 *                 也**不加**虚线框（AD-71：不能做的事不要装得能做）。
 */
export function useGroupDrop(
  onDrop: (dropped: GroupDropPayload) => void,
  disabled = false,
): ConversationDrop {
  const [isOver, setIsOver] = useState(false);

  const handleDragOver = useCallback(
    (event: React.DragEvent) => {
      if (disabled || !isGroupDrag(event.dataTransfer)) return;
      // preventDefault 才算"这里可以放"；不发就等于拒收。
      event.preventDefault();
      event.dataTransfer.dropEffect = "link";
      setIsOver(true);
    },
    [disabled],
  );

  const handleDragLeave = useCallback(() => setIsOver(false), []);

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      setIsOver(false);
      if (disabled || !isGroupDrag(event.dataTransfer)) return;
      event.preventDefault();
      const dropped = readGroupDrag(event.dataTransfer);
      if (dropped) onDrop(dropped);
    },
    [disabled, onDrop],
  );

  return useMemo(
    () => ({
      dragProps: { onDragOver: handleDragOver, onDragLeave: handleDragLeave, onDrop: handleDrop },
      isOver: isOver && !disabled,
    }),
    [handleDragOver, handleDragLeave, handleDrop, isOver, disabled],
  );
}
