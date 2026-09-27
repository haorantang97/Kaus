import { useEffect, useState } from "react";
import { GitCompare } from "lucide-react";
import { apiGet, apiPost } from "../lib/api";
import { Button, Card, confirmAsync, cream, Modal, ModalSub, ModalTitle, Pill, toast } from "./ui";

interface Copy { agent: string; label: string; baseline_fp: string | null; current_fp: string | null; present: boolean; changed: boolean; ts: number }
interface Group { kind: string; name: string; n_copies: number; shared: boolean; drifted: boolean; n_changed: number; copies: Copy[] }
interface DistResp { groups: Group[]; total: number; drifted_count: number }

const KIND_LABEL: Record<string, string> = { skill: "技能", mcp: "MCP" };

/* 被动漂移提醒：嵌进仪表盘。无分化时返回 null（不占位）；有分化才显示一张卡 + 对齐入口。 */
export function DriftAlert() {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [recon, setRecon] = useState<Group | null>(null);
  const load = () => { apiGet<DistResp>("/api/distribution").then((r) => setGroups(r.groups.filter((g) => g.drifted))).catch(() => setGroups([])); };
  useEffect(load, []);

  if (!groups || groups.length === 0) return null;   // 关键：空闲零占位

  return (
    <Card title="技能分化 · 待对齐" icon={GitCompare} right={<Pill tone="gold">{groups.length}</Pill>} className="mb-4">
      <p className="mb-2 text-[0.68rem]" style={{ color: cream(48) }}>同一配置的多个副本内容已分化（某 agent 就地改/进化了）。挑一个版本，选择性覆盖到指定 agent——想留局部就别动它。</p>
      <div className="flex flex-col">
        {groups.map((g, i) => (
          <div key={`${g.kind}:${g.name}`} className="flex flex-wrap items-center gap-2 py-2" style={{ borderTop: i ? `1px solid ${cream(10)}` : undefined }}>
            <Pill tone="muted">{KIND_LABEL[g.kind] ?? g.kind}</Pill>
            <span className="text-[0.82rem] font-semibold" style={{ color: "var(--midground-base)" }}>{g.name}</span>
            <span className="inline-flex items-center gap-1"><span className="size-1.5 rounded-full" style={{ background: "var(--gold)" }} /><span className="text-[0.66rem]" style={{ color: "var(--accent-text)" }}>{g.n_changed} 个进化 / {g.n_copies} 份</span></span>
            <span className="flex flex-wrap gap-1">
              {g.copies.map((c) => (
                <span key={c.agent} className="rounded-[2px] px-1.5 py-0.5 text-[0.62rem]" style={{ background: !c.present ? "rgba(179,87,63,.1)" : c.changed ? "var(--gold-soft)" : cream(6), color: !c.present ? "var(--danger)" : c.changed ? "var(--accent-text)" : cream(50) }}>
                  {(c.label || c.agent)}{!c.present ? " 缺失" : c.changed ? " 已改" : ""}
                </span>
              ))}
            </span>
            <span className="ml-auto"><Button variant="primary" onClick={() => setRecon(g)}>对齐…</Button></span>
          </div>
        ))}
      </div>
      {recon && <ReconcileModal g={recon} onClose={() => setRecon(null)} onDone={() => { setRecon(null); load(); }} />}
    </Card>
  );
}

function ReconcileModal({ g, onClose, onDone }: { g: Group; onClose: () => void; onDone: () => void }) {
  const present = g.copies.filter((c) => c.present);
  const [source, setSource] = useState<string>(present.find((c) => c.changed)?.agent ?? present[0]?.agent ?? "");
  const [targets, setTargets] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const toggle = (n: string) => setTargets((s) => { const c = new Set(s); c.has(n) ? c.delete(n) : c.add(n); return c; });
  const tgts = [...targets].filter((t) => t !== source);

  const run = async () => {
    if (!source || tgts.length === 0) return;
    if (!(await confirmAsync(`用「${(present.find((x) => x.agent === source)?.label || source)}」的版本覆盖 ${tgts.length} 个 agent？未勾选的 agent 保持原样。`, {
      danger: true,
      okLabel: "确认覆盖",
    }))) return;
    setBusy(true);
    try {
      const r = await apiPost<{ aligned: number; total: number }>("/api/reconcile", { kind: g.kind, name: g.name, source, targets: tgts });
      toast(`已用「${(present.find((x) => x.agent === source)?.label || source)}」的版本对齐 ${r.aligned}/${r.total} 个 agent`, "ok");
      onDone();
    } catch (e) { toast(`对齐失败：${e instanceof Error ? e.message : e}`, "bad"); }
    setBusy(false);
  };

  return (
    <Modal onClose={onClose} width={560}>
      <ModalTitle>对齐「{g.name}」</ModalTitle>
      <ModalSub>挑一个版本当<b>源</b>，把它覆盖到你勾选的 agent（只在已持有该配置者中选）。没勾的保持原样——进化想留在局部就别勾它。</ModalSub>
      <div className="mb-1 mt-2 text-[0.62rem] uppercase tracking-[0.16em]" style={{ color: cream(45) }}>① 用谁的版本（源）</div>
      <div className="flex flex-col gap-1">
        {present.map((c) => (
          <label key={c.agent} className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-1.5 text-xs" style={{ border: `1px solid ${source === c.agent ? "var(--gold-deep)" : cream(14)}`, background: source === c.agent ? "var(--gold-soft)" : cream(2) }}>
            <input type="radio" name="recon-src" checked={source === c.agent} onChange={() => { setSource(c.agent); setTargets((s) => { const x = new Set(s); x.delete(c.agent); return x; }); }} />
            <span className="font-medium" style={{ color: "var(--midground-base)" }}>{(c.label || c.agent)}</span>
            {c.changed ? <Pill tone="gold">已进化</Pill> : <span className="text-[0.65rem]" style={{ color: cream(42) }}>基线</span>}
            <span className="ml-auto font-mono text-[0.6rem]" style={{ color: cream(38) }}>{(c.current_fp ?? "").slice(0, 8)}</span>
          </label>
        ))}
      </div>
      <div className="mb-1 mt-3 text-[0.62rem] uppercase tracking-[0.16em]" style={{ color: cream(45) }}>② 覆盖到哪些 agent（目标）</div>
      <div className="flex flex-col gap-1">
        {present.filter((c) => c.agent !== source).map((c) => (
          <label key={c.agent} className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-1.5 text-xs" style={{ border: `1px solid ${targets.has(c.agent) ? "var(--gold-deep)" : cream(14)}`, background: targets.has(c.agent) ? "var(--gold-soft)" : cream(2) }}>
            <input type="checkbox" checked={targets.has(c.agent)} onChange={() => toggle(c.agent)} />
            <span className="font-medium" style={{ color: "var(--midground-base)" }}>{(c.label || c.agent)}</span>
            {c.changed && <Pill tone="gold">已进化</Pill>}
            <span className="ml-auto font-mono text-[0.6rem]" style={{ color: cream(38) }}>{(c.current_fp ?? "").slice(0, 8)}</span>
          </label>
        ))}
      </div>
      <div className="mt-4 flex justify-end gap-3">
        <Button onClick={onClose}>取消</Button>
        <Button variant="primary" disabled={!source || tgts.length === 0 || busy} onClick={run}>{busy ? "对齐中…" : `用此版本覆盖 ${tgts.length} 个`}</Button>
      </div>
    </Modal>
  );
}
