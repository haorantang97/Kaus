import { useState, type ReactNode, type RefObject } from "react";
import { Paperclip, X } from "lucide-react";

import { useLocale } from "../i18n";
import { useComposerAutosize, useComposerInputGuard } from "../hooks/useComposerInput";
import { AttachmentFilename } from "./AttachmentFilename";
import { ComposerBar, type ComposerBarProps } from "./ComposerBar";

/* 输入区（会话页与草稿页共用）：拖放区 + 工具栏 + 隐藏的文件选择框 + 附件托盘 +
 * 文本框。两页的差别全部由 props 说出来——附件的生命周期（草稿页先攒 `File[]`
 * 发送时再传；会话页立刻上传成 `ConversationAttachment`）留在各自页面里，这里只
 * 负责把它们画成同一种 chip。
 *
 * 键位（第 4 件①）：Enter 发送、Shift+Enter 换行，⌘/Ctrl+Enter 继续可用；输入法
 * 组字期间的 Enter 是选词，不发送（`useComposerInputGuard`）。 */

export interface ComposerChip {
  key: string;
  /** chip 的 `title`（完整文件名；会话页的附件可能没有名字）。 */
  title: string | undefined;
  icon: ReactNode;
  name: string;
  /** 文件大小或状态词（「就绪」「上传中」）。 */
  detail: string;
  removeLabel: string;
  removeDisabled?: boolean;
  onRemove: () => void;
}

export interface ComposerProps {
  /** 工具栏（上下文行 + 控制行 + 右侧动作）原样透传给 `ComposerBar`。 */
  bar: Omit<ComposerBarProps, "children">;
  /** 文件进来的三条路（拖放 / 粘贴 / 文件选择）都汇到这里。 */
  onFiles: (files: File[]) => void;
  /** 只读 / 发送中：拖放与粘贴一律不接（不 `preventDefault`，拖进来的高亮也不亮）。 */
  filesBlocked: boolean;
  /** 文件选择框的 `accept`；不传 = 不限。 */
  accept?: string;
  fileInputRef: RefObject<HTMLInputElement | null>;
  fileInputDisabled?: boolean;
  chips: ComposerChip[];
  /** 托盘里跟在 chips 后面的额外节点（会话页的「上传中」chip）。 */
  extraChips?: ReactNode[];
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  text: string;
  onTextChange: (value: string) => void;
  placeholder: string;
  rows: number;
  disabled: boolean;
  onSend: () => void;
}

export function Composer({
  bar,
  onFiles,
  filesBlocked,
  accept,
  fileInputRef,
  fileInputDisabled,
  chips,
  extraChips = [],
  textareaRef,
  text,
  onTextChange,
  placeholder,
  rows,
  disabled,
  onSend,
}: ComposerProps) {
  const { t } = useLocale();
  const [dragging, setDragging] = useState(false);
  useComposerAutosize(textareaRef, text);
  const composerInput = useComposerInputGuard();
  return (
    <div
      className={`kaus-composer is-stacked${dragging ? " is-dragging" : ""}`}
      onDragOver={event => {
        if (!event.dataTransfer.types.includes("Files") || filesBlocked) return;
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={event => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={event => {
        if (!event.dataTransfer.files.length || filesBlocked) return;
        event.preventDefault();
        setDragging(false);
        onFiles(Array.from(event.dataTransfer.files));
      }}
    >
      <ComposerBar {...bar}>
        <input ref={fileInputRef} type="file" multiple hidden aria-label={t("bar.attach")} accept={accept} disabled={fileInputDisabled} onChange={event => { const files = Array.from(event.target.files || []); event.target.value = ""; onFiles(files); }} />
        {(chips.length > 0 || extraChips.length > 0) && (
          <div className="kaus-attachment-tray" aria-label={t("conversation.attach.list")}>
            {chips.map(chip => (
              <div className="kaus-attachment-chip" key={chip.key} title={chip.title}>
                {chip.icon}
                <span><strong><AttachmentFilename name={chip.name} /></strong><small>{chip.detail}</small></span>
                <button type="button" disabled={chip.removeDisabled} onClick={chip.onRemove} aria-label={chip.removeLabel}><X size={14} /></button>
              </div>
            ))}
            {extraChips}
          </div>
        )}
        {dragging && <div className="kaus-drop-hint"><Paperclip size={18} />{t("conversation.attach.drop")}</div>}
        <div className="kaus-composer-row">
          <textarea
            ref={textareaRef}
            aria-label={t("conversation.message.label")}
            rows={rows}
            disabled={disabled}
            placeholder={placeholder}
            value={text}
            onChange={(event) => onTextChange(event.target.value)}
            onPaste={event => {
              if (!event.clipboardData.files.length || filesBlocked) return;
              event.preventDefault();
              onFiles(Array.from(event.clipboardData.files));
            }}
            onCompositionStart={composerInput.onCompositionStart}
            onCompositionEnd={composerInput.onCompositionEnd}
            onKeyDown={(event) => {
              if (composerInput.guardKeyDown(event)) return;
              if (event.key !== "Enter" || event.shiftKey || event.altKey) return;
              event.preventDefault();
              /* 运行态的判定住在页面的 `send()` 里（batch39 第 1 件）：按钮和 Enter
                 走同一条路，才不会各说各的。 */
              onSend();
            }}
          />
        </div>
      </ComposerBar>
    </div>
  );
}
