import { useState } from "react";

import { useLocale } from "../../i18n";
import { Button, Modal, ModalSub, ModalTitle } from "../ui";
import type { GroupWire } from "../../lib/groupsApi";

/* ------------------------------------------------------------------ *
 * 删除组：删除确认（2026-11-01 起关组即删除；2026-11-03 改称删除组）
 * ------------------------------------------------------------------ */

export function CloseGroupDialog({
  group,
  onConfirm,
  onCancel,
}: {
  group: GroupWire;
  onConfirm: () => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const [busy, setBusy] = useState(false);
  return (
    <Modal onClose={onCancel} width={440}>
      <ModalTitle>{t("group.delete.title")}</ModalTitle>
      <ModalSub>{t("group.delete.sub", { title: group.title })}</ModalSub>
      <div className="kaus-return-actions">
        <Button onClick={onCancel}>{t("ui.cancel")}</Button>
        <Button
          variant="danger"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            void onConfirm().finally(() => setBusy(false));
          }}
        >
          {t("group.delete.confirm")}
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
export function RenameMemberDialog({
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
