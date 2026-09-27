import { useEffect, useState } from "react";
import { Plus, Search } from "lucide-react";
import { apiGet, apiPost, fetchNetwork, type NetworkResp } from "../lib/api";
import { t, type DictKey } from "../i18n";
import { AsyncState, Button, confirmAsync, cream, dname, Modal, ModalSub, ModalTitle, Overlay, Pill, Spinner, toast } from "./ui";

interface WHItem {
  type: string; kind: string; name: string; rel: string; desc: string; group: string; source: string;
  config_key: string | null; merge: string | null;
}
interface WHSource { source: string; origin: string; count: number; types: Record<string, number>; groups: string[]; items: WHItem[] }
interface WarehouseResp { root: string; source_count: number; item_total: number; type_totals: Record<string, number>; sources: WHSource[] }
interface AllocResult { target: string; ok: boolean; msg?: string; dest?: string }
interface AllocResp { ok: boolean; copied: number; total: number; removed_from_warehouse: boolean; results: AllocResult[] }

/* 条目类型名走词典（批次十一第 4 件）：`skill` / `mcp` / `config` 之外的类型
   原样显示（后端可能加新类型，不能因为没词条就渲染空白）。 */
const typeLabel = (type: string) =>
  type === "skill" || type === "mcp" || type === "config" ? t(`warehouse.type.${type}` as DictKey) : type;

export function WarehouseOverlay({ onClose, refreshKey = 0 }: { onClose: () => void; refreshKey?: number }) {
  const [d, setD] = useState<WarehouseResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [picking, setPicking] = useState<WHItem | null>(null);
  const load = () => { setD(null); setErr(null); apiGet<WarehouseResp>("/api/warehouse").then(setD).catch((e) => setErr(String(e?.message ?? e))); };
  useEffect(() => { load(); }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // 仅当"同时从仓库移除"成功时才从列表剔除（默认复制=保留）
  const onRemoved = (it: WHItem) =>
    setD((prev) => prev ? {
      ...prev,
      item_total: prev.item_total - 1,
      type_totals: { ...prev.type_totals, [it.type]: Math.max(0, (prev.type_totals[it.type] || 1) - 1) },
      sources: prev.sources.map((s) => s.source === it.source ? { ...s, count: s.count - 1, items: s.items.filter((x) => x.rel !== it.rel) } : s),
    } : prev);

  const ql = q.trim().toLowerCase();
  const match = (it: WHItem) =>
    (typeFilter === "all" || it.type === typeFilter) &&
    (!ql || it.name.toLowerCase().includes(ql) || it.desc.toLowerCase().includes(ql));
  const types = d ? Object.keys(d.type_totals) : [];

  return (
    <Overlay title={t("warehouse.title")} onClose={onClose} onReload={() => load()}
      actions={d && <Pill tone="gold">{t("warehouse.badge", { sources: d.source_count, items: d.item_total })}</Pill>}>
      <AsyncState data={d} err={err}>{(d) => (
          <>
            <p className="mb-4 text-xs leading-relaxed" style={{ color: cream(50) }}>
              {t("warehouse.sub")}
            </p>
            <div className="mb-5 flex flex-wrap items-center gap-2">
              <div className="flex items-center gap-2 rounded-md px-2.5 py-1.5" style={{ border: `1px solid ${cream(15)}`, background: cream(4), minWidth: 280 }}>
                <Search className="size-4" style={{ color: cream(45) }} />
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("warehouse.search")} className="w-full bg-transparent text-xs outline-none" style={{ color: "var(--midground-base)" }} />
              </div>
              <div className="flex items-center gap-1">
                {["all", ...types].map((ty) => (
                  <button key={ty} onClick={() => setTypeFilter(ty)}
                    className="rounded-[2px] px-2.5 py-1 text-[0.66rem] uppercase tracking-[0.1em] transition-colors"
                    style={{ border: `1px solid ${typeFilter === ty ? "var(--gold-deep)" : cream(15)}`, background: typeFilter === ty ? "var(--gold-soft)" : "transparent", color: typeFilter === ty ? "var(--accent-text)" : cream(55) }}>
                    {ty === "all" ? t("warehouse.filter.all", { count: d.item_total }) : t("warehouse.filter.type", { label: typeLabel(ty), count: d.type_totals[ty] })}
                  </button>
                ))}
              </div>
            </div>
            {d.item_total === 0 ? (
              <div className="py-16 text-center text-sm" style={{ color: cream(45) }}>{t("warehouse.empty")}</div>
            ) : d.sources.map((s) => <SourceSection key={s.source} src={s} match={match} onPick={setPicking} />)}
          </>
        )}</AsyncState>
      {picking && <AgentPicker item={picking} onClose={() => setPicking(null)} onRemoved={onRemoved} refreshKey={refreshKey} />}
    </Overlay>
  );
}

function SourceSection({ src, match, onPick }: { src: WHSource; match: (s: WHItem) => boolean; onPick: (s: WHItem) => void }) {
  const shown = src.items.filter(match);
  if (shown.length === 0) return null;
  const byGroup: Record<string, WHItem[]> = {};
  for (const it of shown) (byGroup[it.group] ||= []).push(it);
  return (
    <div className="mb-7">
      <div className="mb-3 flex items-baseline gap-2.5">
        <span className="gold-bar inline-block shrink-0" style={{ width: 3, height: 14, borderRadius: 1 }} />
        <span className="text-[0.9rem] font-bold uppercase tracking-[0.12em]">{src.source}</span>
        <Pill tone="muted">{shown.length}/{src.count}</Pill>
        {src.origin && <span className="truncate text-[0.62rem]" style={{ color: cream(38) }} title={src.origin}>{src.origin.replace(/^https?:\/\//, "").replace(/\.git$/, "")}</span>}
      </div>
      {Object.keys(byGroup).sort().map((g) => (
        <div key={g} className="mb-4">
          <div className="mb-2 text-[0.62rem] uppercase tracking-[0.16em]" style={{ color: cream(42) }}>{g} · {byGroup[g].length}</div>
          <div className="grid gap-2.5" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(232px, 1fr))" }}>
            {byGroup[g].map((it) => <ItemCard key={it.rel} it={it} onPick={onPick} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

function ItemCard({ it, onPick }: { it: WHItem; onPick: (s: WHItem) => void }) {
  const isCfg = it.type !== "skill";
  return (
    <div className="flex flex-col gap-1.5 p-2.5 transition-shadow hover:shadow-[0_2px_12px_rgba(60,50,20,0.08)]" style={{ border: `1px solid ${cream(15)}`, background: cream(2) }}>
      <div className="flex items-start justify-between gap-1.5">
        <span className="break-all text-[0.78rem] font-semibold leading-tight" style={{ color: "var(--midground-base)" }}>{it.name}</span>
        <button onClick={() => onPick(it)} title={t("warehouse.assign")} className="grid size-6 shrink-0 place-items-center rounded transition-colors hover:bg-black/[0.05]" style={{ border: `1px solid color-mix(in srgb, var(--gold) 40%, transparent)`, color: "var(--accent-text)" }}>
          <Plus className="size-3.5" />
        </button>
      </div>
      {isCfg && <span className="w-fit"><Pill tone="gold">{typeLabel(it.type)}</Pill></span>}
      <p className="line-clamp-3 text-[0.68rem] leading-snug" style={{ color: cream(50), minHeight: "2.4em" }}>{it.desc || t("warehouse.item.noDesc")}</p>
    </div>
  );
}

/* 点 + 后：组织树，多选 agent，一次复制多份。 */
function AgentPicker({ item, onClose, onRemoved, refreshKey = 0 }: { item: WHItem; onClose: () => void; onRemoved: (s: WHItem) => void; refreshKey?: number }) {
  const [net, setNet] = useState<NetworkResp | null>(null);
  const [netErr, setNetErr] = useState<string | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [remove, setRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setNet(null);
    setNetErr(null);
    fetchNetwork().then((r) => { setNetErr(null); setNet(r); }).catch((e) => setNetErr(String(e?.message ?? e)));
  }, [refreshKey]);
  const isCfg = item.type !== "skill";
  const toggle = (n: string) => setSel((s) => { const c = new Set(s); c.has(n) ? c.delete(n) : c.add(n); return c; });

  const run = async () => {
    if (sel.size === 0) return;
    if (remove && !(await confirmAsync(t("warehouse.removeConfirm", { name: item.name }), { danger: true }))) return;
    setBusy(true);
    try {
      const r = await apiPost<AllocResp>("/api/warehouse/allocate", { source: item.source, rel: item.rel, targets: [...sel], remove_from_warehouse: remove });
      const failed = r.results.filter((x) => !x.ok);
      if (r.copied > 0) toast(`${t("warehouse.copied", { name: item.name, copied: r.copied, total: r.total })}${failed.length ? t("warehouse.copiedSkipped", { count: failed.length }) : ""}`, "ok");
      if (failed.length) toast(t("warehouse.copyFailedSome", { list: failed.map((f) => `${dname({ name: f.target })}(${f.msg})`).join("、") }), "bad");
      if (r.removed_from_warehouse) onRemoved(item);
      onClose();
    } catch (e) { toast(t("warehouse.allocFailed", { error: e instanceof Error ? e.message : String(e) }), "bad"); }
    setBusy(false);
  };

  return (
    <Modal onClose={onClose} width={580}>
      <ModalTitle>{isCfg
        ? t("warehouse.picker.titleTyped", { name: item.name, type: typeLabel(item.type) })
        : t("warehouse.picker.title", { name: item.name })}</ModalTitle>
      <ModalSub>
        {isCfg
          ? t("warehouse.picker.subConfig", { key: item.config_key ?? "" })
          : t("warehouse.picker.subSkill", { source: item.source })}
      </ModalSub>
      <div className="max-h-[48vh] overflow-y-auto py-1">
        {netErr
          ? <div className="py-6 text-sm" style={{ color: "var(--danger)" }}>{t("warehouse.picker.treeFailed", { error: netErr })}</div>
          : net ? net.roots.map((r) => <AgentNode key={r} name={r} net={net} sel={sel} onToggle={toggle} depth={0} />)
            : <div className="flex items-center gap-2 py-6 text-sm" style={{ color: cream(50) }}><Spinner /> {t("warehouse.picker.treeLoading")}</div>}
      </div>
      <label className="mt-3 flex cursor-pointer items-center gap-2 text-xs" style={{ color: cream(55) }}>
        <input type="checkbox" checked={remove} onChange={(e) => setRemove(e.target.checked)} />
        {t("warehouse.picker.removeAfter")}
      </label>
      <div className="mt-3 flex items-center justify-between">
        <span className="text-[0.7rem]" style={{ color: cream(45) }}>{t("warehouse.picker.selected", { count: sel.size })}</span>
        <div className="flex gap-3">
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" disabled={sel.size === 0 || busy} onClick={run}>{busy ? t("warehouse.picker.copying") : t("warehouse.picker.copy", { count: sel.size })}</Button>
        </div>
      </div>
    </Modal>
  );
}

function AgentNode({ name, net, sel, onToggle, depth }: { name: string; net: NetworkResp; sel: Set<string>; onToggle: (n: string) => void; depth: number }) {
  const n = net.nodes[name];
  if (!n) return null;
  const on = sel.has(name);
  const role = name === "default" ? t("warehouse.role.main")
    : n.main_twin ? t("warehouse.role.twin")
    : n.role === "source" ? t("warehouse.role.source")
    : n.role === "shared" ? t("warehouse.role.shared")
    : t("warehouse.role.standalone");
  return (
    <div>
      <button onClick={() => onToggle(name)} className="mb-1 flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left transition-colors"
        style={{ marginLeft: depth * 18, border: `1px solid ${on ? "var(--gold-deep)" : cream(14)}`, background: on ? "var(--gold-soft)" : cream(2) }}>
        <span className="grid size-4 shrink-0 place-items-center rounded-[3px] text-[0.6rem]" style={{ border: `1px solid ${on ? "var(--gold-deep)" : cream(30)}`, background: on ? "var(--gold-deep)" : "transparent", color: "#fff" }}>{on ? "✓" : ""}</span>
        <span className="text-[0.8rem] font-medium" style={{ color: "var(--midground-base)" }}>{dname(n)}</span>
        <span className="text-[0.62rem] uppercase tracking-[0.12em]" style={{ color: cream(42) }}>{role} · {t("warehouse.node.skills", { count: n.skill_count })}</span>
      </button>
      {(n.children || []).map((c) => <AgentNode key={c} name={c} net={net} sel={sel} onToggle={onToggle} depth={depth + 1} />)}
    </div>
  );
}
