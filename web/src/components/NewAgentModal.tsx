import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { useLocale } from "../i18n";
import { Button, cream, dname, inputStyle, Modal, ModalSub, ModalTitle, toast } from "./ui";

const slugify = (s: string) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);

export function NewAgentModal({ onClose, onCreated, initialParent = null }: { onClose: () => void; onCreated: () => void; initialParent?: string | null }) {
  const { t } = useLocale();
  const [display, setDisplay] = useState("");
  const [id, setId] = useState("");
  const [desc, setDesc] = useState("");
  // 从轮盘的「+ 添加项目」进来时父级预填为那一列的父节点（批次七 d 第 5 条）。
  const [parent, setParent] = useState(initialParent ?? "default");
  const [parents, setParents] = useState<{ name: string; label?: string }[]>([]);
  const [parentsErr, setParentsErr] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const idEdited = useRef(false);

  useEffect(() => {
    apiGet<{ profiles: { name: string; label?: string; is_draft?: boolean }[] }>("/api/profiles")
      .then((d) => {
        setParentsErr(null);
        setParents(d.profiles.filter((p) => !p.is_draft)
          .map((p) => ({ name: p.name, label: p.label }))
          .sort((a, b) => (a.name === "default" ? -1 : b.name === "default" ? 1 : a.name.localeCompare(b.name))));
      })
      .catch((e) => setParentsErr(String(e?.message ?? e)));
  }, []);

  const onDisplay = (v: string) => {
    setDisplay(v);
    if (!idEdited.current) setId(slugify(v));
  };

  const create = async () => {
    const name = id.trim();
    if (!/^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/.test(name)) { setErr(t("newAgent.idInvalid")); return; }
    setBusy(true); setErr(null);
    try {
      await apiPost("/api/agent/create-empty", { name, description: desc.trim(), display: display.trim() || null });
      if (parent) await apiPost("/api/move", { node: name, new_parent: parent });
      const parentLabel = parents.find((p) => p.name === parent)?.label || parent;
      toast(parent
        ? t("newAgent.created", { name: display || name, parent: parentLabel })
        : t("newAgent.createdDraft", { name: display || name }), "ok");
      onCreated();
    } catch (e) { setErr(t("newAgent.createFailed", { error: e instanceof Error ? e.message : String(e) })); setBusy(false); }
  };

  return (
    <Modal onClose={onClose} width={520}>
      <ModalTitle>{t("newAgent.title")}</ModalTitle>
      <ModalSub>{t("newAgent.sub")}</ModalSub>
      <div className="mb-1 text-[0.7rem]" style={{ color: cream(55) }}>{t("newAgent.field.display")}</div>
      <input value={display} onChange={(e) => onDisplay(e.target.value)} autoFocus placeholder={t("newAgent.placeholder.display")} style={inputStyle()} />
      <div className="mb-1 mt-3 text-[0.7rem]" style={{ color: cream(55) }}>{t("newAgent.field.id")}</div>
      <input value={id} onChange={(e) => { idEdited.current = true; setId(e.target.value); }} spellCheck={false} placeholder={t("newAgent.placeholder.id")} style={inputStyle()} />
      <div className="mb-1 mt-3 text-[0.7rem]" style={{ color: cream(55) }}>{t("newAgent.field.parent")}</div>
      <select value={parent} onChange={(e) => setParent(e.target.value)} style={{ ...inputStyle(), padding: "6px 8px" }}>
        {parents.map((p) => <option key={p.name} value={p.name}>{dname(p)}</option>)}
        <option value="">{t("newAgent.noParent")}</option>
      </select>
      {parentsErr && <div className="mt-1 text-[0.68rem]" style={{ color: "var(--danger)" }}>{t("newAgent.parentsError", { error: parentsErr })}</div>}
      <div className="mb-1 mt-3 text-[0.7rem]" style={{ color: cream(55) }}>{t("newAgent.field.desc")}</div>
      <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t("newAgent.placeholder.desc")} style={inputStyle()} />
      {err && <div className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="mt-4 flex justify-end gap-3"><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={busy} onClick={create}>{t("common.create")}</Button></div>
    </Modal>
  );
}
