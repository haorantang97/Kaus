import { useEffect, useMemo, useState } from "react";
import { Archive, X } from "lucide-react";
import { apiGet, apiPost, fetchNetwork, type NetworkResp } from "../lib/api";
import { AsyncState, Button, confirmAsync, cream, dname, HAIR, inputStyle, Modal, ModalSub, ModalTitle, Overlay, Pill, toast } from "./ui";

// 看板：层级感知(组织树泳道,我们独有)+ 官方深度(多看板/依赖/运行日志/诊断/收回/批量)。
// 全部经我们 /api/kanban/* 端点(hermes kanban CLI 薄封装)。无 tenant、无附件/inspect/terminate(引擎专属)。
interface Task { id: string; title: string; assignee?: string; status: string; priority?: number; created_at?: number; body?: string }
interface KanbanResp { tasks: Task[]; note?: string }
interface Detail { id: string; title?: string; status?: string; assignee?: string; body?: string; comments?: unknown[]; events?: unknown[]; parents?: string[]; children?: string[]; [k: string]: unknown }
interface Board { slug: string; name: string; counts: string; current: boolean }

const COLS = [
  { k: "triage", cn: "待分类", sub: "原始想法 — 规范制定者将完善规格" },
  { k: "todo", cn: "待办", sub: "等待依赖项或未分配" },
  { k: "scheduled", cn: "已调度", sub: "等待时间延迟或已调度的跟进" },
  { k: "ready", cn: "就绪", sub: "依赖已满足；指派配置以便调度" },
  { k: "running", cn: "进行中", sub: "已被工作者认领 — 执行中" },
  { k: "review", cn: "待审", sub: "等待审阅" },
  { k: "blocked", cn: "阻塞", sub: "工作者请求人工输入" },
  { k: "done", cn: "已完成", sub: "已完成" },
  { k: "archived", cn: "已归档", sub: "已从看板清出(勾选「已归档」时可见)" },
];
const COL_CN: Record<string, string> = Object.fromEntries(COLS.map((c) => [c.k, c.cn]));
const COL_SUB: Record<string, string> = Object.fromEntries(COLS.map((c) => [c.k, c.sub]));
const ORDER = COLS.map((c) => c.k);
const VERB_CN: Record<string, string> = { promote: "提为就绪", block: "标记受阻", unblock: "解除受阻", schedule: "排期", complete: "标记完成" };
const LEGAL: Record<string, string[]> = {
  triage: ["block"], todo: ["promote", "block", "schedule"],
  ready: ["block", "schedule"], running: ["block", "complete"],
  review: ["complete", "block"], blocked: ["unblock", "promote"],
  scheduled: ["unblock"], done: [], archived: [],
};
// 拖拽换列只对“能手动直设”的状态有效(其余由 claim/dispatch 驱动)
const STATUS2VERB: Record<string, string> = { ready: "promote", blocked: "block", scheduled: "schedule", done: "complete" };
const f = (o: unknown, ...ks: string[]): string | undefined => { const r = o as Record<string, unknown> | null; for (const k of ks) if (r && r[k] != null) return String(r[k]); return undefined; };
const ageDays = (t: Task) => (t.created_at ? (Date.now() / 1000 - t.created_at) / 86400 : 0);
const staleColor = (t: Task) => { const a = ageDays(t); return a > 7 ? "var(--danger)" : a > 3 ? "#9a7d3f" : "transparent"; };

export function KanbanOverlay({ onClose, onGoProfile, refreshKey = 0 }: { onClose: () => void; onGoProfile: (n: string) => void; refreshKey?: number }) {
  const [data, setData] = useState<KanbanResp | null>(null);
  const [net, setNet] = useState<NetworkResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [out, setOut] = useState("");
  const [view, setView] = useState<"tree" | "flat">("tree");
  const [focus, setFocus] = useState("");
  const [boards, setBoards] = useState<Board[]>([]);
  const [curBoard, setCurBoard] = useState("");
  const [diags, setDiags] = useState<unknown[]>([]);
  const [gw, setGw] = useState<{ running: boolean; summary: string[] } | null>(null);
  const [metaErr, setMetaErr] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);
  const [statusF, setStatusF] = useState("");
  const [assigneeF, setAssigneeF] = useState("");
  const [bulk, setBulk] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailAuxErr, setDetailAuxErr] = useState<string | null>(null);
  const [runs, setRuns] = useState<unknown[]>([]);
  const [log, setLog] = useState("");
  const [comment, setComment] = useState("");
  const [reTo, setReTo] = useState("");
  const [depChild, setDepChild] = useState("");
  const [title, setTitle] = useState("");
  const [assignee, setAssignee] = useState("");
  const [pri, setPri] = useState("");
  const [tenant, setTenant] = useState("");
  const [workspaceKind, setWorkspaceKind] = useState("scratch");
  const [workspacePath, setWorkspacePath] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [maxRuntime, setMaxRuntime] = useState("");
  const [goalMode, setGoalMode] = useState(false);
  const [triage, setTriage] = useState(true);
  const [dispatchMax, setDispatchMax] = useState(3);
  const [showNB, setShowNB] = useState(false);
  // 空态（批次十一第 7 件）：一个任务都没有时先只露「新建任务」，点了才展开建任务那一行。
  const [showCreate, setShowCreate] = useState(false);
  const [nb, setNb] = useState({ slug: "", name: "", desc: "", icon: "" });

  const boardQuery = (board = curBoard) => (board ? `board=${encodeURIComponent(board)}` : "");
  const kb = (path: string, extra = "", board = curBoard) => {
    const params = [boardQuery(board), extra].filter(Boolean).join("&");
    return `/api/kanban${path}${params ? `?${params}` : ""}`;
  };

  const loadTasks = (board = curBoard) => {
    const qs = new URLSearchParams();
    if (board) qs.set("board", board);
    if (archived) qs.set("archived", "true");
    if (statusF) qs.set("status", statusF);
    if (assigneeF) qs.set("assignee", assigneeF);
    const q = qs.toString();
    return apiGet<KanbanResp>(`/api/kanban${q ? `?${q}` : ""}`).then(setData).catch((e) => setErr(String(e?.message ?? e)));
  };
  const loadMeta = (board = curBoard) => {
    setMetaErr(null);
    const report = (label: string, e: unknown) => {
      const msg = `${label}加载失败：${e instanceof Error ? e.message : String(e)}`;
      setMetaErr((current) => (current ? `${current}；${msg}` : msg));
    };
    return Promise.allSettled([
      apiGet<{ boards: Board[]; current: string }>("/api/kanban/boards").then((d) => { setBoards(d.boards || []); if (d.current) setCurBoard(d.current); }).catch((e) => report("看板列表", e)),
      apiGet<{ diagnostics: unknown[] }>(kb("/diagnostics", "", board)).then((d) => setDiags(d.diagnostics || [])).catch((e) => report("诊断", e)),
      apiGet<{ running: boolean; summary: string[] }>("/api/kanban/gateway").then(setGw).catch((e) => report("gateway", e)),
      fetchNetwork().then(setNet).catch((e) => report("组织树", e)),
    ]);
  };
  const load = (board = curBoard) => { setErr(null); return Promise.allSettled([loadTasks(board), loadMeta(board)]); };
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (refreshKey) load(); }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadTasks(); }, [archived, statusF, assigneeF, curBoard]); // eslint-disable-line react-hooks/exhaustive-deps

  const nodes = useMemo(() => net?.nodes ?? {}, [net]);
  const allNames = useMemo(() => Object.keys(nodes), [nodes]);
  useEffect(() => { if (!assignee && allNames[0]) setAssignee(allNames[0]); }, [allNames, assignee]);

  const depthOf = (n: string) => { let d = 0, c = nodes[n]; const seen = new Set<string>(); while (c?.parent && !seen.has(c.parent)) { seen.add(c.parent); d++; c = nodes[c.parent]; } return d; };
  const subtree = (root: string): Set<string> => { const o = new Set<string>(); const st = [root]; while (st.length) { const x = st.pop()!; if (o.has(x)) continue; o.add(x); (nodes[x]?.children || []).forEach((c) => st.push(c)); } return o; };
  const focusSet = useMemo(() => (focus ? subtree(focus) : new Set(allNames)), [focus, nodes, allNames]); // eslint-disable-line react-hooks/exhaustive-deps

  const tasks = data?.tasks || [];
  const inFocus = tasks.filter((t) => t.assignee && focusSet.has(t.assignee));
  const colStats = ORDER.filter((s) => inFocus.some((t) => t.status === s));
  const laneAgents = useMemo(() => {
    const ordered: string[] = []; const seen = new Set<string>();
    const roots = focus ? [focus] : (net?.roots || []);
    const walk = (n: string) => { if (seen.has(n)) return; seen.add(n); ordered.push(n); (nodes[n]?.children || []).forEach(walk); };
    roots.forEach(walk);
    const own: Record<string, number> = {};
    inFocus.forEach((t) => { own[t.assignee!] = (own[t.assignee!] || 0) + 1; });
    return ordered.filter((n) => own[n]);
  }, [net, focus, nodes, data]); // eslint-disable-line react-hooks/exhaustive-deps
  const subCount = (n: string) => { const s = subtree(n); return tasks.filter((t) => t.assignee && s.has(t.assignee)).length; };
  const cell = (a: string, s: string) => inFocus.filter((t) => t.assignee === a && t.status === s);

  // ---- 看板切换 / 新建 ----
  const switchBoard = async (slug: string) => {
    // 任务由 [curBoard] effect 统一加载;这里只补 meta(diagnostics 分板),避免双重 loadTasks 闪烁
    try { await apiPost(`/api/kanban/boards/switch/${encodeURIComponent(slug)}`, {}); setCurBoard(slug); setSel(null); setDetail(null); loadMeta(slug); }
    catch (e) { toast(`切换失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };
  const createBoard = async () => {
    const slug = nb.slug.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
    if (!slug) { toast("请填「标识」(小写字母/连字符)", "bad"); return; }
    try {
      await apiPost("/api/kanban/boards/create", { slug, name: nb.name.trim() || undefined, description: nb.desc.trim() || undefined, icon: nb.icon.trim() || undefined, switch: true });
      setShowNB(false); setNb({ slug: "", name: "", desc: "", icon: "" }); setCurBoard(slug); loadMeta(slug);
    } catch (e) { toast(`建看板失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };

  // ---- dispatch ----
  const dispatch = async (dry: boolean) => {
    if (!dry && !(await confirmAsync("现在把就绪任务交给对应 AI 执行(会真的调用工具)。确定?", { danger: true }))) return;
    setOut(dry ? "试运行中…" : "派发中…");
    try { const j = await apiPost<{ result: unknown }>(kb("/dispatch", `dry_run=${dry}&max=${Math.max(1, dispatchMax || 1)}`), {}); setOut((dry ? "试运行：" : "已派发：") + JSON.stringify(j.result).slice(0, 240)); if (!dry) loadTasks(); }
    catch (e) { setOut(`失败：${e instanceof Error ? e.message : e}`); }
  };

  const create = async () => {
    if (!title.trim()) { toast("请填写任务标题", "bad"); return; }
    if (!assignee) { toast("请选择负责人", "bad"); return; }
    const workspace_path = workspaceKind === "scratch" ? "" : workspacePath.trim();
    if (workspaceKind !== "scratch" && !workspace_path) { toast("dir/worktree 任务需要填写绝对路径", "bad"); return; }
    try {
      await apiPost(kb("/create"), {
        title: title.trim(),
        assignee,
        priority: pri ? parseInt(pri, 10) : undefined,
        tenant: tenant.trim() || undefined,
        workspace_kind: workspaceKind,
        workspace_path: workspace_path || undefined,
        idempotency_key: idempotencyKey.trim() || undefined,
        max_runtime: maxRuntime.trim() || undefined,
        goal_mode: goalMode,
        triage,
      });
      setTitle(""); setPri(""); setIdempotencyKey(""); loadTasks();
    }
    catch (e) { toast(`派活失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };

  // ---- 详情 ----
  const openTask = (id: string) => {
    setSel(id); setDetail(null); setDetailAuxErr(null); setRuns([]); setLog(""); setReTo(""); setDepChild(""); setComment("");
    const reportAux = (label: string, e: unknown) => {
      const msg = `${label}加载失败：${e instanceof Error ? e.message : String(e)}`;
      setDetailAuxErr((current) => (current ? `${current}；${msg}` : msg));
    };
    apiGet<{ task: Detail }>(kb(`/show/${encodeURIComponent(id)}`)).then((r) => setDetail(r.task)).catch((e) => toast(`读取失败：${e?.message ?? e}`, "bad"));
    apiGet<{ runs: unknown[] }>(kb(`/runs/${encodeURIComponent(id)}`)).then((r) => setRuns(r.runs || [])).catch((e) => reportAux("运行历史", e));
    apiGet<{ log: string }>(kb(`/log/${encodeURIComponent(id)}`)).then((r) => setLog(r.log || "")).catch((e) => reportAux("工作日志", e));
  };
  const refresh = () => { if (sel) openTask(sel); loadTasks(); loadMeta(); };

  const requireTask = () => {
    if (sel) return sel;
    toast("请先选择一个任务", "bad");
    return null;
  };
  const transition = async (verb: string) => { const id = requireTask(); if (!id) return; if (verb === "complete" && !(await confirmAsync("标记完成?"))) return; try { await apiPost(kb(`/transition/${encodeURIComponent(id)}`), { verb }); refresh(); } catch (e) { toast(`${VERB_CN[verb] || verb}失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const claim = async () => { const id = requireTask(); if (!id) return; try { await apiPost(kb(`/claim/${encodeURIComponent(id)}`), {}); refresh(); } catch (e) { toast(`认领失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const specify = async () => { const id = requireTask(); if (!id) return; if (!(await confirmAsync("放行该任务?specifier 会细化规格并推进到 todo,随后进入调度。"))) return; try { await apiPost(kb(`/specify/${encodeURIComponent(id)}`), {}); refresh(); } catch (e) { toast(`放行失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const decompose = async () => { const id = requireTask(); if (!id) return; if (!(await confirmAsync("用 Hermes decomposer 把该 triage 任务拆成多 agent 子任务图?"))) return; try { const j = await apiPost<{ result: unknown }>(kb(`/decompose/${encodeURIComponent(id)}`, "author=dashboard"), {}); setOut(`拆解：${JSON.stringify(j.result).slice(0, 240)}`); refresh(); } catch (e) { toast(`拆解失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const reclaim = async () => { const id = requireTask(); if (!id) return; if (!(await confirmAsync(`收回任务 ${id}? 这会撤回当前执行归属。`))) return; try { await apiPost(kb(`/reclaim/${encodeURIComponent(id)}`), {}); refresh(); } catch (e) { toast(`收回失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const reassign = async () => { const id = requireTask(); if (!id) return; if (!reTo) { toast("请选择要改派给谁", "bad"); return; } if (!(await confirmAsync(`把任务 ${id} 改派给 ${reTo}?`))) return; try { await apiPost(kb(`/assign/${encodeURIComponent(id)}`), { assignee: reTo, reclaim: true }); setReTo(""); refresh(); } catch (e) { toast(`改派失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const sendComment = async () => { const id = requireTask(); if (!id) return; if (!comment.trim()) { toast("评论不能为空", "bad"); return; } try { await apiPost(kb(`/comment/${encodeURIComponent(id)}`), { text: comment.trim() }); setComment(""); refresh(); } catch (e) { toast(`评论失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const addDep = async () => { const id = requireTask(); if (!id) return; if (!depChild) { toast("请选择要添加的子任务", "bad"); return; } try { await apiPost(kb("/link"), { parent: id, child: depChild }); setDepChild(""); refresh(); } catch (e) { toast(`加依赖失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const rmDep = async (child: string) => { const id = requireTask(); if (!id) return; if (!(await confirmAsync(`移除任务依赖 ${id} → ${child}?`))) return; try { await apiPost(kb("/unlink"), { parent: id, child }); refresh(); } catch (e) { toast(`删依赖失败：${e instanceof Error ? e.message : e}`, "bad"); } };
  const archive = async (id: string) => { if (!(await confirmAsync(`归档任务 ${id}?`))) return; try { await apiPost(kb(`/archive/${encodeURIComponent(id)}`), {}); if (sel === id) { setSel(null); setDetail(null); } loadTasks(); } catch (e) { toast(`归档失败：${e instanceof Error ? e.message : e}`, "bad"); } };

  // ---- 拖拽换列/换泳道 ----
  const moveTask = async (id: string, lane: string, status: string) => {
    const t = tasks.find((x) => x.id === id); if (!t) return;
    try {
      if (lane && lane !== t.assignee) await apiPost(kb(`/assign/${encodeURIComponent(id)}`), { assignee: lane, reclaim: true });
      if (status && status !== t.status) {
        if (t.status === "triage") toast("triage 任务请打开后点「放行」推进(specify)", "bad");
        else {
          const verb = STATUS2VERB[status];
          if (verb) {
            if (verb === "complete" && !(await confirmAsync(`把任务 ${id} 标记完成?`))) return;
            await apiPost(kb(`/transition/${encodeURIComponent(id)}`), { verb });
          }
          else toast(`「${COL_CN[status]}」列不能手动设置(由认领/调度驱动)`, "bad");
        }
      }
      refresh();
    } catch (e) { toast(`移动失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };

  // ---- 批量 ----
  const toggleSel = (id: string) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const bulkRun = async (kind: "done" | "archive") => {
    const ids = [...selected];
    if (!ids.length) { toast("请先选择任务", "bad"); return; }
    if (kind === "done" && !(await confirmAsync(`标记完成 ${ids.length} 个任务?`))) return;
    if (kind === "archive" && !(await confirmAsync(`归档 ${ids.length} 个任务?`))) return;
    try {
      for (const id of ids) {
        if (kind === "done") await apiPost(kb(`/transition/${encodeURIComponent(id)}`), { verb: "complete" });
        else await apiPost(kb(`/archive/${encodeURIComponent(id)}`), {});
      }
      setSelected(new Set()); loadTasks();
    } catch (e) { toast(`批量失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };
  const tidyDone = async () => {
    const doneTasks = tasks.filter((t) => t.status === "done");
    if (!doneTasks.length) { toast("当前看板没有已完成任务", "ok"); return; }
    if (!(await confirmAsync(`整理:把当前看板 ${doneTasks.length} 个「已完成」任务全部归档?归档后可在「已归档」筛选里找回。`, { okLabel: `归档 ${doneTasks.length} 个` }))) return;
    setOut(`整理中…0/${doneTasks.length}`);
    let okCount = 0;
    for (const t of doneTasks) {
      try { await apiPost(kb(`/archive/${encodeURIComponent(t.id)}`), {}); okCount++; }
      catch { /* 单条失败不中断,最后汇总 */ }
      if (okCount % 20 === 0) setOut(`整理中…${okCount}/${doneTasks.length}`);
    }
    setOut("");
    toast(`整理完成:已归档 ${okCount}/${doneTasks.length} 个已完成任务`, okCount === doneTasks.length ? "ok" : "bad");
    if (sel && doneTasks.some((t) => t.id === sel)) { setSel(null); setDetail(null); }
    loadTasks();
  };

  const bulkAssign = async () => {
    const ids = [...selected];
    const who = reTo;
    if (!ids.length) { toast("请先选择任务", "bad"); return; }
    if (!who) { toast("请选择批量改派目标", "bad"); return; }
    if (!(await confirmAsync(`把 ${ids.length} 个任务批量改派给 ${who}?`))) return;
    try {
      for (const id of ids) {
        await apiPost(kb(`/assign/${encodeURIComponent(id)}`), { assignee: who, reclaim: true });
      }
      toast(`已改派 ${ids.length} 个任务`, "ok");
      setReTo("");
      setSelected(new Set());
      loadTasks();
    } catch (e) {
      toast(`批量改派失败：${e instanceof Error ? e.message : e}`, "bad");
    }
  };

  const Card = ({ t, showWho }: { t: Task; showWho?: boolean }) => (
    <div
      draggable
      onDragStart={(e) => e.dataTransfer.setData("text/plain", t.id)}
      onClick={() => { if (bulk) toggleSel(t.id); else openTask(t.id); }}
      className="group relative w-full cursor-pointer rounded-[4px] p-2 text-left text-xs transition-colors"
      style={{ background: sel === t.id || selected.has(t.id) ? "var(--gold-soft)" : "var(--surface-raised)", border: `1px solid ${selected.has(t.id) ? "var(--gold-deep)" : cream(13)}`, borderLeft: `3px solid ${staleColor(t)}`, boxShadow: selected.has(t.id) ? "inset 0 0 0 1px var(--gold-deep)" : undefined }}
    >
      {t.status !== "archived" && <button
        type="button"
        title="归档该任务(任何状态可清)"
        onClick={(e) => { e.stopPropagation(); void archive(t.id); }}
        className="absolute right-1 top-1 grid size-5 place-items-center rounded opacity-0 transition-opacity hover:bg-black/[0.07] group-hover:opacity-100"
        style={{ color: cream(45) }}
      >
        <Archive className="size-3" />
      </button>}
      <div className="mb-1 line-clamp-2" style={{ color: cream(80), fontWeight: 500 }}>{t.title}</div>
      <div className="flex items-center gap-1.5 text-[0.6rem]" style={{ color: cream(42) }}>
        {bulk && <input type="checkbox" checked={selected.has(t.id)} readOnly />}
        {t.priority != null && <span style={{ color: "var(--accent-text)" }}>P{t.priority}</span>}
        {showWho && <span>@{t.assignee || "—"}</span>}
        <span className="ml-auto font-mono">{t.id.slice(0, 8)}</span>
      </div>
    </div>
  );

  // 「建任务」那一行：正常态在工具栏下面，空态里点了「新建任务」才出现。
  const createRow = (
    <div className="flex flex-wrap items-center gap-2">
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="任务标题…" style={{ ...inputStyle(), width: 220 }} />
      <select value={assignee} onChange={(e) => setAssignee(e.target.value)} style={{ ...inputStyle(), width: 160, padding: "6px 8px" }}>
        {(focus ? [...focusSet] : allNames).map((n) => <option key={n} value={n}>{dname(nodes[n])}</option>)}
      </select>
      <input value={pri} onChange={(e) => setPri(e.target.value)} type="number" placeholder="优先级" style={{ ...inputStyle(), width: 88 }} />
      <input value={tenant} onChange={(e) => setTenant(e.target.value)} placeholder="tenant/cluster" style={{ ...inputStyle(), width: 130 }} />
      <select value={workspaceKind} onChange={(e) => setWorkspaceKind(e.target.value)} style={{ ...inputStyle(), width: 110, padding: "6px 8px" }}>
        <option value="scratch">scratch</option>
        <option value="dir">dir:</option>
        <option value="worktree">worktree:</option>
      </select>
      {workspaceKind !== "scratch" && <input value={workspacePath} onChange={(e) => setWorkspacePath(e.target.value)} placeholder="/absolute/path" style={{ ...inputStyle(), width: 210 }} />}
      <input value={maxRuntime} onChange={(e) => setMaxRuntime(e.target.value)} placeholder="max runtime 如 2h" style={{ ...inputStyle(), width: 130 }} />
      <input value={idempotencyKey} onChange={(e) => setIdempotencyKey(e.target.value)} placeholder="幂等 key" style={{ ...inputStyle(), width: 150 }} />
      <label className="flex items-center gap-1.5 text-[0.66rem]" style={{ color: cream(55) }}><input type="checkbox" checked={triage} onChange={(e) => setTriage(e.target.checked)} />先进 triage</label>
      <label className="flex items-center gap-1.5 text-[0.66rem]" style={{ color: cream(55) }}><input type="checkbox" checked={goalMode} onChange={(e) => setGoalMode(e.target.checked)} />goal-loop</label>
      <Button variant="primary" onClick={create}>派活</Button>
    </div>
  );

  return (
    <Overlay title="任务板 · Kanban" onClose={onClose} onReload={() => load()}>
      <AsyncState data={data} err={err}>{(data) => {
        const note = data.note;
        const curName = boards.find((b) => b.slug === curBoard)?.name || curBoard;
        /* 空态（批次十一第 7 件）。一个任务都没有时，原来这一屏摆着约 19 个控件
           （看板切换 / 视图 / 焦点 / 调度器 / 筛选 / 批量 / 建任务的一整排），
           走查者不知道从哪下手。空态只留：一句标题、一句说明、一枚「新建任务」；
           其余工具栏等有数据了再出现。**筛出来的空**不算空态——那时要把筛选留着，
           否则用户没法把条件改回去。 */
        const filtered = archived || Boolean(statusF) || Boolean(assigneeF) || Boolean(focus);
        if ((data.tasks?.length ?? 0) === 0 && !filtered) {
          return (
            <div className="flex flex-col items-start gap-3 py-8" data-testid="kanban-empty">
              <h3 className="text-sm font-semibold" style={{ color: cream(80) }}>还没有任务</h3>
              <p className="max-w-[46ch] text-xs leading-relaxed" style={{ color: cream(50) }}>
                任务板把要做的事排成队，交给对应的 AI 去执行。新建的任务先进「待分类」，
                你放行之后才会进调度流。
              </p>
              {showCreate ? createRow : <Button variant="primary" onClick={() => setShowCreate(true)}>新建任务</Button>}
            </div>
          );
        }
        return (
          <div className="flex flex-col gap-3">
            {/* 看板切换 + 视图 + 焦点 */}
            <div className="flex flex-wrap items-center gap-2.5">
              <span className="flex items-center gap-2 rounded-[4px] px-2.5 py-1.5" style={{ border: HAIR, background: cream(2) }}>
                <span className="text-[0.62rem]" style={{ color: cream(42) }}>看板</span>
                <select value={curBoard} onChange={(e) => switchBoard(e.target.value)} style={{ ...inputStyle(), height: 26, padding: "2px 6px" }}>
                  {boards.map((b) => <option key={b.slug} value={b.slug}>{b.name}</option>)}
                </select>
                <Button onClick={() => setShowNB(true)}>+ 新建看板</Button>
              </span>
              <span className="flex items-center overflow-hidden rounded-[4px]" style={{ border: HAIR }}>
                <button onClick={() => setView("tree")} className="px-2.5 py-1 text-[0.62rem] uppercase tracking-[0.1em]" style={{ background: view === "tree" ? "var(--gold-soft)" : "transparent", color: view === "tree" ? "var(--accent-text)" : cream(55) }}>层级</button>
                <button onClick={() => setView("flat")} className="px-2.5 py-1 text-[0.62rem] uppercase tracking-[0.1em]" style={{ background: view === "flat" ? "var(--gold-soft)" : "transparent", color: view === "flat" ? "var(--accent-text)" : cream(55) }}>状态</button>
              </span>
              {view === "tree" && (
                <select value={focus} onChange={(e) => setFocus(e.target.value)} title="只看某个子树" style={{ ...inputStyle(), width: 160, padding: "5px 8px" }}>
                  <option value="">全组织</option>
                  {allNames.map((n) => <option key={n} value={n}>{"· ".repeat(depthOf(n))}{dname(nodes[n])}</option>)}
                </select>
              )}
            </div>
            {metaErr && (
              <div className="rounded-[4px] px-3 py-2 text-[0.68rem]" style={{ border: "1px solid #9a7d3f66", background: "#9a7d3f17", color: "#7a5f24" }}>
                {metaErr}
              </div>
            )}

            {/* 派发 + 筛选 */}
            <div className="flex flex-wrap items-center gap-2.5">
              <Pill tone={gw?.running ? "success" : "muted"} title={(gw?.summary || []).join(" · ") || "gateway 调度器状态"}>调度器：{gw?.running ? "运行中(gateway)→就绪任务会自动执行" : "未运行"}</Pill>
              <span className="flex items-center gap-1.5 text-[0.66rem]" style={{ color: cream(45) }}>
                每次最多
                <input value={dispatchMax} onChange={(e) => setDispatchMax(parseInt(e.target.value || "1", 10))} type="number" min={1} max={20} style={{ ...inputStyle(), width: 54, padding: "4px 6px" }} />
              </span>
              <Button onClick={() => dispatch(true)}>试运行</Button>
              <Button variant="primary" onClick={() => dispatch(false)}>触发调度器</Button>
              {out && <span className="text-[0.66rem]" style={{ color: cream(50) }}>{out}</span>}
              <span className="ml-auto flex items-center gap-2.5">
                <select value={statusF} onChange={(e) => setStatusF(e.target.value)} style={{ ...inputStyle(), width: 110, padding: "5px 8px" }}>
                  <option value="">全部状态</option>
                  {ORDER.map((s) => <option key={s} value={s}>{COL_CN[s]}</option>)}
                </select>
                <select value={assigneeF} onChange={(e) => setAssigneeF(e.target.value)} style={{ ...inputStyle(), width: 130, padding: "5px 8px" }}>
                  <option value="">全部负责人</option>
                  {allNames.map((n) => <option key={n} value={n}>{dname(nodes[n])}</option>)}
                </select>
                <Button onClick={tidyDone} title="把当前看板所有「已完成」任务一键归档">整理 · 归档已完成</Button>
                <label className="flex items-center gap-1.5 text-[0.7rem]" style={{ color: cream(55) }}><input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} />已归档</label>
                <label className="flex items-center gap-1.5 text-[0.7rem]" style={{ color: cream(55) }}><input type="checkbox" checked={bulk} onChange={(e) => { setBulk(e.target.checked); if (!e.target.checked) setSelected(new Set()); }} />批量</label>
              </span>
            </div>

            {/* 建任务 */}
            {createRow}

            {/* 诊断「注意」栏 */}
            {diags.length > 0 && (
              <div className="rounded-[4px] p-2.5" style={{ border: "1px solid #9a7d3f66", background: "#9a7d3f17" }}>
                <div className="mb-1 text-[0.72rem] font-semibold" style={{ color: "#7a5f24" }}>{diags.length} 个任务需要关注</div>
                {diags.slice(0, 8).map((d, i) => (
                  <div key={i} className="text-[0.66rem]" style={{ color: cream(58) }}>
                    [{f(d, "severity") || "warn"}] {f(d, "task_id", "task", "id") || ""} {f(d, "message", "detail", "title", "summary") || JSON.stringify(d).slice(0, 80)}
                  </div>
                ))}
              </div>
            )}

            {/* 批量条 */}
            {bulk && selected.size > 0 && (
              <div className="flex flex-wrap items-center gap-2 rounded-[4px] px-3 py-2" style={{ border: "1px solid var(--gold-deep)", background: "var(--gold-soft)" }}>
                <span className="text-[0.72rem]" style={{ color: "var(--accent-text)" }}><b>{selected.size}</b> 已选</span>
                <Button variant="primary" onClick={() => bulkRun("done")}>标记完成</Button>
                <Button onClick={() => bulkRun("archive")}>归档</Button>
                <select value={reTo} onChange={(e) => setReTo(e.target.value)} style={{ ...inputStyle(), width: 140, padding: "4px 8px" }}>
                  <option value="">改派给…</option>
                  {allNames.map((n) => <option key={n} value={n}>{dname(nodes[n])}</option>)}
                </select>
                <Button onClick={bulkAssign}>改派</Button>
                <span className="ml-auto cursor-pointer text-[0.68rem] underline" style={{ color: cream(50) }} onClick={() => setSelected(new Set())}>清除</span>
              </div>
            )}
            <p className="text-[0.64rem]" style={{ color: cream(42) }}>当前看板「{curName}」· 新任务默认进「待分类」(不会自动执行),打开点「放行」才进调度流 · 拖卡片换列=状态流转、换泳道=改派。{note ? ` · ⚠ ${note}` : ""}</p>

            {/* 看板主体 */}
            {view === "flat" ? (
              <div className="flex gap-3 overflow-x-auto pb-2">
                {colStats.length === 0 ? <div className="text-sm" style={{ color: cream(45) }}>暂无任务</div>
                  : colStats.map((s) => (
                    <div key={s} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { const id = e.dataTransfer.getData("text/plain"); const t = tasks.find((x) => x.id === id); moveTask(id, t?.assignee || "", s); }} className="w-60 shrink-0 rounded-[4px] p-2" style={{ background: cream(3), border: HAIR }}>
                      <div className="mb-1 flex items-center gap-1.5"><h4 className="text-[0.68rem] font-semibold uppercase tracking-[0.12em]" style={{ color: "var(--accent-text)" }}>{COL_CN[s]}</h4><span className="text-[0.62rem]" style={{ color: cream(42) }}>{inFocus.filter((t) => t.status === s).length}</span></div>
                      <div className="mb-1.5 text-[0.6rem]" style={{ color: cream(40) }}>{COL_SUB[s]}</div>
                      <div className="flex flex-col gap-1.5">{inFocus.filter((t) => t.status === s).map((t) => <Card key={t.id} t={t} showWho />)}</div>
                    </div>
                  ))}
              </div>
            ) : laneAgents.length === 0 ? (
              <div className="text-sm" style={{ color: cream(45) }}>该范围暂无任务</div>
            ) : (
              <div className="overflow-x-auto pb-2">
                <div style={{ display: "grid", gridTemplateColumns: `180px repeat(${colStats.length}, minmax(150px, 1fr))`, gap: 8, minWidth: "fit-content" }}>
                  <div />
                  {colStats.map((s) => (
                    <div key={s} className="px-1 pb-1">
                      <div className="flex items-center gap-1.5"><span className="text-[0.64rem] font-semibold uppercase tracking-[0.1em]" style={{ color: "var(--accent-text)" }}>{COL_CN[s]}</span><span className="text-[0.6rem]" style={{ color: cream(42) }}>{inFocus.filter((t) => t.status === s).length}</span></div>
                      <div className="text-[0.58rem]" style={{ color: cream(40) }}>{COL_SUB[s]}</div>
                    </div>
                  ))}
                  {laneAgents.map((n) => (
                    <div key={n} style={{ display: "contents" }}>
                      <div className="flex flex-col justify-center rounded-[4px] px-2.5 py-2" style={{ background: cream(3), border: HAIR, marginLeft: depthOf(n) * 10 }}>
                        <button onClick={() => onGoProfile(n)} className="truncate text-left text-xs font-medium hover:underline" style={{ color: cream(78) }}>{dname(nodes[n])}</button>
                        <span className="text-[0.58rem]" style={{ color: cream(42) }}>直属 {inFocus.filter((t) => t.assignee === n).length} · 子树 {subCount(n)}</span>
                      </div>
                      {colStats.map((s) => {
                        const list = cell(n, s);
                        return (
                          <div key={s} onDragOver={(e) => e.preventDefault()} onDrop={(e) => moveTask(e.dataTransfer.getData("text/plain"), n, s)} className="flex min-h-[44px] flex-col gap-1.5 rounded-[4px] p-1.5" style={{ background: list.length ? cream(2) : "transparent", border: list.length ? HAIR : `1px dashed ${cream(8)}` }}>
                            {list.map((t) => <Card key={t.id} t={t} />)}
                          </div>
                        );
                      })}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      }}</AsyncState>

      {/* 新建看板弹窗 */}
      {showNB && (
        <Modal onClose={() => setShowNB(false)} width={440}>
          <ModalTitle>新建看板</ModalTitle>
          <ModalSub>看板把不相关的工作流分开——每个项目 / 代码库 / 域一个看板。一个看板上的工作者不会看到另一个看板的任务。</ModalSub>
          <div className="flex flex-col gap-2.5">
            <label className="text-[0.7rem]" style={{ color: cream(55) }}>标识 <span style={{ color: cream(40) }}>— 小写字母、连字符,例如 atm10-server</span>
              <input value={nb.slug} onChange={(e) => setNb({ ...nb, slug: e.target.value })} placeholder="my-board" style={{ ...inputStyle(), width: "100%", marginTop: 3 }} /></label>
            <label className="text-[0.7rem]" style={{ color: cream(55) }}>显示名称 <span style={{ color: cream(40) }}>(可选)</span>
              <input value={nb.name} onChange={(e) => setNb({ ...nb, name: e.target.value })} placeholder="My Board" style={{ ...inputStyle(), width: "100%", marginTop: 3 }} /></label>
            <label className="text-[0.7rem]" style={{ color: cream(55) }}>描述 <span style={{ color: cream(40) }}>(可选)</span>
              <input value={nb.desc} onChange={(e) => setNb({ ...nb, desc: e.target.value })} style={{ ...inputStyle(), width: "100%", marginTop: 3 }} /></label>
            <label className="text-[0.7rem]" style={{ color: cream(55) }}>图标 <span style={{ color: cream(40) }}>(单个字符或表情)</span>
              <input value={nb.icon} onChange={(e) => setNb({ ...nb, icon: e.target.value })} maxLength={2} placeholder="📋" style={{ ...inputStyle(), width: 90, marginTop: 3 }} /></label>
            <div className="mt-2 flex justify-end gap-2">
              <Button onClick={() => setShowNB(false)}>取消</Button>
              <Button variant="primary" onClick={createBoard}>创建看板</Button>
            </div>
          </div>
        </Modal>
      )}

      {/* 任务详情 slide-over */}
      {sel && (
        <>
          <div className="fixed inset-0 z-[60]" style={{ background: "var(--overlay-scrim)" }} onClick={() => { setSel(null); setDetail(null); }} />
          <div className="fixed inset-y-0 right-0 z-[61] flex w-[400px] max-w-[92vw] flex-col" style={{ background: "var(--panel)", borderLeft: `1px solid ${cream(15)}`, boxShadow: "var(--shadow-drawer)" }}>
            <div className="flex items-center gap-2 px-4 py-3" style={{ borderBottom: `1px solid ${cream(10)}`, background: cream(2) }}>
              <span className="font-mono text-xs" style={{ color: cream(45) }}>{sel.slice(0, 12)}</span>
              {detail?.status && <Pill tone="muted">{COL_CN[detail.status] || detail.status}</Pill>}
              <button className="ml-auto grid size-6 place-items-center rounded-[4px] hover:bg-black/[0.05]" onClick={() => { setSel(null); setDetail(null); }}><X className="size-3.5" style={{ color: cream(50) }} /></button>
            </div>
            {!detail ? <div className="p-4 text-sm" style={{ color: cream(45) }}>加载中…</div> : (
              <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
                <div><div className="text-sm font-semibold" style={{ color: cream(82) }}>{detail.title || "（无标题）"}</div>{detail.body && <p className="mt-1 whitespace-pre-wrap text-xs" style={{ color: cream(58) }}>{detail.body}</p>}</div>

                <div className="rounded-[4px] p-2.5" style={{ background: cream(2), border: HAIR }}>
                  <div className="mb-1.5 text-[0.6rem] uppercase tracking-wider" style={{ color: cream(42) }}>负责人</div>
                  <div className="mb-2 text-xs" style={{ color: cream(70) }}>@{detail.assignee || "—"}</div>
                  <div className="flex items-center gap-1.5">
                    <select value={reTo} onChange={(e) => setReTo(e.target.value)} style={{ ...inputStyle(), flex: 1, padding: "5px 8px" }}>
                      <option value="">改派给…</option><option value="none">取消指派</option>
                      {allNames.map((n) => <option key={n} value={n}>{"· ".repeat(depthOf(n))}{dname(nodes[n])}</option>)}
                    </select>
                    <Button onClick={reassign}>改派</Button>
                    <Button onClick={reclaim}>收回</Button>
                  </div>
                </div>

                <div>
                  <div className="mb-1.5 text-[0.6rem] uppercase tracking-wider" style={{ color: cream(42) }}>操作</div>
                  <div className="flex flex-wrap gap-1.5">
                    {detail.status === "triage" && <Button variant="primary" onClick={specify}>Specify 单卡放行</Button>}
                    {detail.status === "triage" && <Button onClick={decompose}>Decompose 多 agent 拆解</Button>}
                    {detail.status === "ready" && <Button onClick={claim}>认领</Button>}
                    {(LEGAL[detail.status || ""] || []).map((v) => <Button key={v} variant={v === "complete" ? "primary" : "ghost"} onClick={() => transition(v)}>{VERB_CN[v] || v}</Button>)}
                    <Button onClick={() => archive(sel)}>归档</Button>
                  </div>
                </div>

                {/* 依赖 */}
                <div>
                  <div className="mb-1.5 text-[0.6rem] uppercase tracking-wider" style={{ color: cream(42) }}>依赖</div>
                  {(detail.parents || []).map((p) => <div key={p} className="mb-1 flex items-center gap-2 rounded-[4px] px-2 py-1 text-xs" style={{ border: HAIR }}><span style={{ color: cream(40) }}>父</span><span className="font-mono">{p}</span></div>)}
                  {(detail.children || []).map((c) => <div key={c} className="mb-1 flex items-center gap-2 rounded-[4px] px-2 py-1 text-xs" style={{ border: HAIR }}><span style={{ color: cream(40) }}>子</span><span className="font-mono">{c}</span><button className="ml-auto text-[0.62rem] underline" style={{ color: cream(50) }} onClick={() => rmDep(c)}>移除</button></div>)}
                  {!(detail.parents?.length || detail.children?.length) && <span className="text-[0.7rem]" style={{ color: cream(40) }}>无</span>}
                  <div className="mt-1.5 flex items-center gap-1.5">
                    <select value={depChild} onChange={(e) => setDepChild(e.target.value)} style={{ ...inputStyle(), flex: 1, padding: "5px 8px" }}>
                      <option value="">添加子任务…</option>
                      {tasks.filter((x) => x.id !== sel).map((x) => <option key={x.id} value={x.id}>{x.title.slice(0, 20)}</option>)}
                    </select>
                    <Button onClick={addDep}>加</Button>
                  </div>
                </div>

                {/* 运行历史 + 工作日志 */}
                <div>
                  <div className="mb-1.5 text-[0.6rem] uppercase tracking-wider" style={{ color: cream(42) }}>运行历史</div>
                  {detailAuxErr && (
                    <div className="mb-1 rounded-[4px] px-2 py-1 text-[0.64rem]" style={{ border: "1px solid #9a7d3f66", background: "#9a7d3f17", color: "#7a5f24" }}>
                      {detailAuxErr}
                    </div>
                  )}
                  {runs.length === 0 ? <span className="text-[0.7rem]" style={{ color: cream(40) }}>尚未运行</span> : runs.slice(0, 8).map((r, i) => (
                    <div key={i} className="mb-1 flex items-center gap-2 rounded-[4px] px-2 py-1 text-[0.66rem]" style={{ border: HAIR, color: cream(58) }}>
                      <span className="font-mono">{f(r, "run_id", "id") || "run"}</span><span style={{ color: cream(40) }}>{f(r, "profile", "assignee") || ""}</span><span className="ml-auto">{f(r, "status", "outcome", "state") || ""}</span>
                    </div>
                  ))}
                  {log && <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-[4px] p-2 text-[0.64rem]" style={{ background: cream(3), border: HAIR, color: cream(60) }}>{log.slice(-3000)}</pre>}
                </div>

                {/* 评论 */}
                <div>
                  <div className="mb-1.5 text-[0.6rem] uppercase tracking-wider" style={{ color: cream(42) }}>评论</div>
                  <div className="flex items-center gap-1.5">
                    <input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="写条评论…" onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) sendComment(); }} style={{ ...inputStyle(), flex: 1 }} />
                    <Button onClick={sendComment}>发送</Button>
                  </div>
                  <div className="mt-2 flex flex-col gap-1.5">
                    {(detail.comments as unknown[] | undefined)?.map((c, i) => (
                      <div key={i} className="rounded-[4px] p-2 text-xs" style={{ background: cream(2), border: HAIR }}>
                        <span className="mr-1.5 font-medium" style={{ color: "var(--accent-text)" }}>{f(c, "author", "by") || "—"}</span>
                        <span style={{ color: cream(65) }}>{f(c, "body", "text", "message")}</span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* 事件 */}
                {Array.isArray(detail.events) && detail.events.length > 0 && (
                  <div>
                    <div className="mb-1.5 text-[0.6rem] uppercase tracking-wider" style={{ color: cream(42) }}>事件</div>
                    <div className="flex flex-col gap-1">
                      {(detail.events as unknown[]).slice(-12).map((ev, i) => (
                        <div key={i} className="flex items-center gap-2 text-[0.64rem]" style={{ color: cream(48) }}>
                          <span style={{ color: cream(62) }}>{f(ev, "kind", "type", "event") || "事件"}</span>
                          <span className="ml-auto font-mono">{(f(ev, "created_at", "at", "ts") || "").slice(0, 19)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </Overlay>
  );
}
