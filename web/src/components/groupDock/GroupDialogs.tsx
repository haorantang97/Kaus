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
