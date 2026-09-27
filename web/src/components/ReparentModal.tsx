import { useEffect, useState } from "react";
import { apiPost, fetchNetwork, type NetworkResp } from "../lib/api";
import { useLocale } from "../i18n";
import { Button, cream, Modal, Spinner, toast } from "./ui";

/* 改父级:影响预览 + 确认提交。从旧关系图抽出,供 Agents 治理模式 + 侧栏共用。
   逐字保留旧版的预览维度:位置变化 / 配置 diff / 技能 diff / 分身警告 / 后代连带 / warnings。 */
export interface ReparentPreview {
  from: { parent: string | null }; to: { parent: string | null };
  config_diff: { items: { key: string; change: string; before: string; after: string }[] };
  skill_diff: { skill_inherit_on: boolean; converted: boolean };
  constitution: { subscribed: boolean };
  descendants: string[]; is_main_twin: boolean; warnings: string[];
}

export function ReparentModal({ node, target, onClose, onDone }: { node: string; target: string | null; onClose: () => void; onDone: () => void }) {
  const { t } = useLocale();
  const [d, setD] = useState<ReparentPreview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [net, setNet] = useState<NetworkResp | null>(null);
  // 花名映射:labels.json 的显示名在 net.nodes[*].label 里,这里没有 OrgNode 上下文,轻拉一次组织树
  const lb = (n: string | null | undefined) => (n ? (net?.nodes[n]?.label || n) : n);
  useEffect(() => {
    apiPost<ReparentPreview>("/api/reparent-preview", { node, new_parent: target }).then(setD).catch((e) => setErr(String(e?.message ?? e)));
    fetchNetwork().then(setNet).catch(() => setNet(null));
  }, [node, target]);
  const move = async () => {
    setBusy(true);
    try {
      await apiPost("/api/move", { node, new_parent: target });
      toast(t("reparent.moved", { name: String(lb(node)), target: target ? String(lb(target)) : t("reparent.topLevel") }), "ok");
      onDone();
    }
    catch (e) { toast(t("reparent.moveFailed", { error: e instanceof Error ? e.message : String(e) }), "bad"); setBusy(false); }
  };
  return (
    <Modal onClose={onClose} width={560}>
      <h3 className="mb-1 text-sm font-semibold uppercase tracking-[0.12em]">{t("reparent.title", { name: String(lb(node)) })}</h3>
      {err ? <div className="py-4 text-sm" style={{ color: "var(--danger)" }}>{t("reparent.previewFailed", { error: err })}</div>
        : d === null ? <div className="flex items-center gap-2 py-4 text-sm" style={{ color: cream(50) }}><Spinner /> {t("reparent.previewing")}</div>
        : (
          <>
            <div className="mb-3 mt-2 text-xs" style={{ color: cream(60) }}>
              {t("reparent.position")}: {d.from.parent ? lb(d.from.parent) : t("reparent.topLevel")} → <span style={{ color: "var(--accent-text)" }}>{d.to.parent ? lb(d.to.parent) : t("reparent.topLevel")}</span>
              {d.descendants.length > 0 && <span> · {t("reparent.affects", { count: d.descendants.length })}</span>}
            </div>
            {d.is_main_twin && <div className="mb-2 rounded p-2 text-[0.7rem]" style={{ background: "var(--gold-soft)", color: "var(--accent-text)" }}>{t("reparent.twinWarning")}</div>}
            <div className="mb-2 text-[0.7rem]" style={{ color: cream(50) }}>
              <div className="mb-1 font-semibold" style={{ color: "var(--accent-text)" }}>{t("reparent.configChanges")}</div>
              {d.config_diff.items.length === 0 ? <div>{t("reparent.noConfigChanges")}</div>
                : d.config_diff.items.map((it) => <div key={it.key}>· {it.key}：{it.before} → {it.after}</div>)}
            </div>
            <div className="mb-2 text-[0.7rem]" style={{ color: cream(50) }}>
              <div className="mb-1 font-semibold" style={{ color: "var(--accent-text)" }}>{t("reparent.skills")}</div>
              <div>{d.skill_diff.skill_inherit_on ? t("reparent.inheritOn") : t("reparent.inheritOff")}{d.skill_diff.converted ? ` · ${t("reparent.compacted")}` : ""}</div>
            </div>
            {d.warnings.length > 0 && d.warnings.map((w, i) => <div key={i} className="text-[0.7rem]" style={{ color: "var(--danger)" }}>⚠ {w}</div>)}
            <p className="mt-2 text-[0.65rem]" style={{ color: cream(45) }}>{t("reparent.footnote")}</p>
          </>
        )}
      <div className="mt-4 flex justify-end gap-3">
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" disabled={busy || d === null} onClick={move}>{t("reparent.confirm")}</Button>
      </div>
    </Modal>
  );
}
