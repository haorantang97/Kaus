import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, MoreHorizontal, Pin } from "lucide-react";
import { type NetworkResp, type OrgNode } from "../lib/api";
import { cream, dname } from "./ui";
import { ReparentModal } from "./ReparentModal";
import { t } from "../i18n";

// Faithful port of the existing dashboard's sidebar agent tree (renderNav):
// parent/child forest, twin-first ordering, collapsible nodes, pinned shortcuts,
// 化身/停用 badges, gateway dot, ⋯ menu. Clicking a node selects the agent.
export function Sidebar({
  net,
  current,
  onSelect,
  onOpenMenu,
  onChanged,
  filter = "",
  compact = false,
}: {
  net: NetworkResp;
  current: string | null;
  onSelect: (name: string) => void;
  onOpenMenu: (name: string, rect: DOMRect) => void;
  onChanged: () => void;
  filter?: string;
  compact?: boolean;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [drag, setDrag] = useState<string | null>(null);
  const [reparent, setReparent] = useState<{ node: string; target: string | null } | null>(null);
  const nodes = net.nodes;
  const all = Object.values(nodes).filter((n) => !n.is_draft);
  const byName: Record<string, OrgNode> = Object.fromEntries(all.map((n) => [n.name, n]));
  const isDesc = (anc: string, x: string) => { let c: string | undefined = x; const seen = new Set<string>(); while (c && !seen.has(c)) { if (c === anc) return true; seen.add(c); c = byName[c]?.parent || undefined; } return false; };
  const askReparent = (n: string | null, target: string | null) => { if (!n || n === target || n === "default" || byName[n]?.main_twin) return; if (target && isDesc(n, target)) return; setReparent({ node: n, target }); };

  // 分身(main_twin)不进 kids —— 它不是"下级域",是本体的"模式",单独挂在本体上渲染。
  const kids: Record<string, string[]> = {};
  for (const n of all) {
    if (n.parent && byName[n.parent] && n.parent !== n.name && !n.main_twin) {
      (kids[n.parent] ||= []).push(n.name);
    }
  }
  Object.values(kids).forEach((a) => a.sort((x, y) => x.localeCompare(y)));

  const roots = (net.roots.length ? net.roots : all.filter((n) => !n.parent).map((n) => n.name))
    .slice()
    .sort((a, b) => (a === "default" ? -1 : b === "default" ? 1 : a.localeCompare(b)));
  const pins = all.filter((n) => n.is_pinned);
  const drafts = Object.values(nodes).filter((n) => n.is_draft);
  const q = filter.trim().toLowerCase();
  const matches = q ? all.filter((n) => n.name.toLowerCase().includes(q) || dname(n).toLowerCase().includes(q)) : [];

  const toggle = (n: string) =>
    setCollapsed((s) => {
      const c = new Set(s);
      if (c.has(n)) c.delete(n);
      else c.add(n);
      return c;
    });

  function renderNode(name: string, depth: number): ReactNode {
    const p = byName[name];
    if (!p) return null;
    const has = (kids[name] || []).length > 0;
    const open = !collapsed.has(name);
    const dead = p.effective_killed;
    const sel = current === name;
    return (
      <div key={name}>
        <div
          role="button"
          tabIndex={0}
          onClick={() => onSelect(name)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(name); } }}
          draggable={name !== "default" && !p.main_twin}
          onDragStart={(e) => { if ((e.target as HTMLElement).closest("button")) { e.preventDefault(); return; } setDrag(name); }}
          onDragEnd={() => { setDrag(null); document.querySelectorAll<HTMLElement>(".sidebar-agent-row").forEach((el) => { el.style.boxShadow = ""; }); }}
          onDragOver={(e) => { if (drag && drag !== name) { e.preventDefault(); (e.currentTarget as HTMLElement).style.boxShadow = "inset 0 0 0 2px var(--gold)"; } }}
          onDragLeave={(e) => ((e.currentTarget as HTMLElement).style.boxShadow = "none")}
          onDrop={(e) => { e.preventDefault(); (e.currentTarget as HTMLElement).style.boxShadow = "none"; askReparent(drag, name); }}
          className={`sidebar-agent-row group flex cursor-pointer items-center gap-1.5 transition-colors ${sel ? "sel-accent is-current" : ""}`}
          style={{
            paddingLeft: 8 + depth * 14,
            color: dead ? cream(38) : "var(--midground-base)",
          }}
        >
          {has ? (
            <button
              onClick={(e) => {
                e.stopPropagation();
                toggle(name);
              }}
              className="grid size-4 shrink-0 place-items-center rounded hover:bg-black/[0.06]"
              style={{ color: cream(55) }}
              title={open ? "Collapse" : "Expand"}
            >
              {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
            </button>
          ) : (
            <span className="size-4 shrink-0" />
          )}
          <span className="flex-1 truncate">{dname(p)}</span>
          {p.main_twin && (
            <span className="shrink-0 text-[0.56rem] uppercase tracking-wider" style={{ color: "var(--accent-text)" }}>
              Twin
            </span>
          )}
          {dead && (
            <span className="shrink-0 text-[0.56rem]" style={{ color: "var(--danger)" }}>
              Disabled
            </span>
          )}
          <span
            className="size-1.5 shrink-0 rounded-full"
            title={`gateway ${p.gateway || "?"}`}
            style={{ background: p.gateway === "running" ? "var(--gold)" : cream(22) }}
          />
          <button
            onClick={(e) => {
              e.stopPropagation();
              onOpenMenu(name, (e.currentTarget as HTMLElement).getBoundingClientRect());
            }}
            className="grid size-5 shrink-0 place-items-center rounded opacity-0 transition-opacity hover:bg-black/[0.06] group-hover:opacity-100"
            style={{ color: cream(70) }}
            title={t("common.more")}
          >
            <MoreHorizontal className="size-3.5" />
          </button>
        </div>
        {/* 分身簇：挂在本体下、不当分叉子。同一个"灵魂"的不同模式,视觉上明确区别于下级域。 */}
        {open && (p.twins || []).length > 0 && (
          <div
            className="mb-1 ml-2 mt-0.5 rounded-md py-1"
            style={{ marginLeft: 8 + (depth + 1) * 14, borderLeft: `2px solid var(--gold-soft)`, paddingLeft: 8 }}
          >
            <div className="px-1 pb-0.5 text-[0.52rem] uppercase tracking-[0.18em]" style={{ color: "var(--accent-text)" }}>
              {t("tree.twinModes")}
            </div>
            {(p.twins || []).map((tn) => {
              const t = byName[tn];
              if (!t) return null;
              const tsel = current === tn;
              return (
                <div
                  key={tn}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSelect(tn)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(tn); } }}
                  className={`sidebar-agent-row sidebar-twin-row group flex cursor-pointer items-center gap-1.5 ${tsel ? "sel-accent is-current" : ""}`}
                  style={{ color: "var(--midground-base)" }}
                >
                  <span className="shrink-0 text-[0.7rem]" style={{ color: "var(--accent-text)" }}>◑</span>
                  <span className="truncate">{dname(t)}</span>
                  {t.twin_mode && (
                    <span className="ml-auto shrink-0 text-[0.56rem] tracking-wide" style={{ color: cream(48) }}>
                      {t.twin_mode}
                    </span>
                  )}
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpenMenu(tn, (e.currentTarget as HTMLElement).getBoundingClientRect());
                    }}
                    className="grid size-5 shrink-0 place-items-center rounded opacity-0 transition-opacity hover:bg-black/[0.06] group-hover:opacity-100"
                    style={{ color: cream(70) }}
                    title="More"
                  >
                    <MoreHorizontal className="size-3.5" />
                  </button>
                </div>
              );
            })}
          </div>
        )}
        {has && open && (kids[name] || []).map((c) => renderNode(c, depth + 1))}
      </div>
    );
  }

  return (
    <>
    <nav className={`sidebar-agent-tree flex-1 overflow-y-auto ${compact ? "is-compact" : ""}`}>
      {q ? (
        matches.length === 0 ? (
          <div className="px-2 py-3 text-xs" style={{ color: cream(45) }}>{t("tree.noMatch")}</div>
        ) : (
          matches.map((p) => (
            <div
              key={p.name}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(p.name)}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(p.name); } }}
              className="flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-sm"
              style={{ background: current === p.name ? "var(--gold-soft)" : "transparent" }}
            >
              <span className="flex-1 truncate">{dname(p)}</span>
              {p.main_twin && <span className="text-[0.56rem] uppercase tracking-wider" style={{ color: "#9fd09f" }}>Twin</span>}
              {p.name !== dname(p) && <span className="text-[0.6rem]" style={{ color: cream(40) }}>{p.name}</span>}
            </div>
          ))
        )
      ) : (
      <>
      {pins.length > 0 && (
        <div className="mb-2 border-b pb-2" style={{ borderColor: cream(10) }}>
          {pins.map((p) => (
            <div
              key={p.name}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(p.name)}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(p.name); } }}
              className="flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-sm"
              style={{ background: current === p.name ? cream(12) : "transparent" }}
            >
              <Pin className="size-3 shrink-0" style={{ color: cream(55) }} />
              <span className="flex-1 truncate">{dname(p)}</span>
            </div>
          ))}
        </div>
      )}
      {roots.map((r) => renderNode(r, 0))}
      {drafts.length > 0 && (
        <div
          className="mt-3 rounded-md border border-dashed px-2 py-1.5 text-xs"
          style={{ borderColor: cream(20), color: cream(55) }}
        >
          {drafts.length} drafts · manage in Agents governance
        </div>
      )}
      </>
      )}
    </nav>
    {reparent && <ReparentModal node={reparent.node} target={reparent.target} onClose={() => setReparent(null)} onDone={() => { setReparent(null); onChanged(); }} />}
    </>
  );
}
