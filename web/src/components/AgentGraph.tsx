import "./project-polish.css";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { MessageSquare, MoreHorizontal, Plus, Settings2 } from "lucide-react";
import { type NetworkResp } from "../lib/api";
import { t, useLocale, type DictKey } from "../i18n";
import { Button, cream, dname, Overlay } from "./ui";
import { ReparentModal } from "./ReparentModal";
import { wheelAnchor, wheelNodeTransform, wheelTransformCss } from "../lib/wheelGeometry";
import { PROJECT_DRAG_MIME } from "../lib/groupDrop";

/* 角色名走词典（批次十一第 4 件）：以前这里写死英文，进项目页语言就突变。 */
const roleFull = (role: string | null | undefined): string =>
  role === "source" || role === "shared" || role === "none"
    ? t(`graph.role.${role}` as DictKey)
    : t("graph.value.standalone");
/** 每列末尾那个「+ 添加项目」虚节点的 id（不是真 profile，`nodes[]` 里查不到）。 */
export const ADD_NODE_ID = "__add-child__";
type AgentGraphMode = "chat" | "governance";
type WheelColumnState = { current: number; target: number; selected: number; key: string };
type ColumnGeometry = { left: number; centerY: number; items: { id: string; width: number }[] };
type WheelGeometry = { columns: ColumnGeometry[]; statics: Record<string, { l: { x: number; y: number }; r: { x: number; y: number } }> };

/* Agents · 下钻图。
   保留旧版全部能力:拖拽改父级(+ReparentModal 影响预览)/ 顶层拖放区 / 草稿暂存 /
   ⋯菜单(置顶·改名·fork·删除,经 onOpenMenu→CtxMenu)/ 分身挂 X(非下属)/ 停用·gateway·角色元信息 /
   默认是对话浏览;治理模式才启用组织维护动作。 */
export function AgentGraph({ net, onClose, onChanged, onGoProfile, onDetail, onOpenMenu, onAddChild, embedded = false, focusName = null, mode = "governance", onModeChange }: {
  net: NetworkResp;
  onClose: () => void;
  onChanged: () => void;
  onGoProfile: (n: string) => void;
  onDetail: (n: string) => void;
  onOpenMenu: (name: string, rect: DOMRect) => void;
  /** 「+ 添加项目」：父级 = 该列的父节点（批次七 d 第 5 条）。不传则不显示虚节点。 */
  onAddChild?: (parentId: string) => void;
  embedded?: boolean;
  focusName?: string | null;
  mode?: AgentGraphMode;
  onModeChange?: (mode: AgentGraphMode) => void;
}) {
  const { t } = useLocale();
  const nodes = net.nodes;
  const canGovern = !embedded || mode === "governance";
  const displayName = (id: string) => dname(nodes[id] ?? { name: id });
  const domainKids = (id: string) =>
    (nodes[id]?.children || []).filter((c) => !(nodes[id]?.twins || []).includes(c)).slice().sort((a, b) => a.localeCompare(b));
  const lineage = (id: string): string[] => {
    const a: string[] = []; let c: string | undefined = id; const seen = new Set<string>();
    while (c && !seen.has(c)) { seen.add(c); a.unshift(c); c = nodes[c]?.parent || undefined; }
    return a;
  };
  const focusPath = (id: string) => {
    if (nodes[id]?.main_twin) return ["default"];
    const nextPath = lineage(id);
    return nextPath.length ? nextPath : ["default"];
  };
  const initialFocus = focusName && nodes[focusName] ? focusName : "default";
  const [path, setPath] = useState<string[]>(() => focusPath(initialFocus));
  const [sel, setSel] = useState<string>(initialFocus);
  const [panelVisible, setPanelVisible] = useState(true);
  const pathRef = useRef<string[]>(path);
  const selRef = useRef<string>(sel);
  const [drag, setDrag] = useState<string | null>(null);
  const [reparent, setReparent] = useState<{ node: string; target: string | null } | null>(null);
  const colsRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const wheelState = useRef<Record<number, WheelColumnState>>({});
  const geometry = useRef<WheelGeometry | null>(null);
  const wheelFrame = useRef<number | null>(null);
  const wheelSnapTimer = useRef<number | null>(null);
  const wheelLastFrame = useRef<number | null>(null);
  const pendingFocus = useRef<{ id: string; animate: boolean } | null>(null);
  const wheelHydrated = useRef(false);
  const suppressNodeClick = useRef(false);
  // 用户正在滚 / 拖的那一列。非 null 期间:(a) 不提交选中,只做预览高亮;
  // (b) useLayoutEffect 里的 alignWheelToPath 不许把 target 拽回去(AD-81 之一)。
  const wheelInteracting = useRef<number | null>(null);
  pathRef.current = path;
  selRef.current = sel;

  const isDesc = (anc: string, x: string) => { let c: string | undefined = x; const seen = new Set<string>(); while (c && !seen.has(c)) { if (c === anc) return true; seen.add(c); c = nodes[c]?.parent || undefined; } return false; };
  function focusNode(id: string, columnIndex?: number, animate = true) {
    if (!nodes[id]) return;
    // 点击/键盘胜过还没到期的吸附:否则 110ms 后那次提交会把点击结果顶掉。
    if (wheelSnapTimer.current !== null) { window.clearTimeout(wheelSnapTimer.current); wheelSnapTimer.current = null; }
    wheelInteracting.current = null;
    setPanelVisible(true);
    const nextPath = focusPath(id);
    selRef.current = id;
    pathRef.current = nextPath;
    pendingFocus.current = { id, animate };
    if (animate && typeof columnIndex === "number") pointWheelAt(columnIndex, id);
    setSel(id);
    setPath(nextPath);
  }
  const activeIdForColumn = (columnIndex: number) => columnIndex === 0 ? "default" : (pathRef.current[columnIndex] || selRef.current);
  const isSettledFront = (columnIndex: number, id: string) => {
    const columnsEl = colsRef.current;
    const column = columnsEl?.querySelectorAll<HTMLElement>(".graph-col")[columnIndex];
    const items = [...(column?.querySelectorAll<HTMLElement>(".ag-node") || [])];
    const index = items.findIndex((item) => item.dataset.id === id);
    const state = wheelState.current[columnIndex];
    return index >= 0 && !!state && Math.abs(state.current - index) < 0.02 && Math.abs(state.target - index) < 0.02;
  };
  const stateKeyForColumn = (columnIndex: number) => columnIndex === 0 ? "__root__" : (pathRef.current[columnIndex - 1] || "__missing__");
  const commitWheelSelection = (columnIndex: number, items: HTMLElement[], index: number) => {
    const id = items[index]?.dataset.id;
    if (!id || !nodes[id]) return;
    const state = wheelState.current[columnIndex];
    if (state) state.selected = index;
    const nextPath = focusPath(id);
    selRef.current = id;
    pathRef.current = nextPath;
    pendingFocus.current = { id, animate: true };
    setSel(id);
    setPath(nextPath);
  };
  const prefersReducedMotion = () =>
    typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  /* commit=false(滚动中):只挪 target,选中不动 —— 提交会走 setSel/setPath,
     React 重渲染后 alignWheelToPath 又把 target 拽回来,和手指打架。
     commit=true 只在吸附定时器到期 / 抬手 / 点击 / 键盘时发生。 */
  const setWheelTarget = (columnIndex: number, target: number, commit = false) => {
    const columnsEl = colsRef.current;
    const column = columnsEl?.querySelectorAll<HTMLElement>(".graph-col")[columnIndex];
    const items = [...(column?.querySelectorAll<HTMLElement>(".ag-node") || [])];
    if (!items.length) return;
    const selectedIndex = items.findIndex((item) => item.dataset.id === activeIdForColumn(columnIndex));
    const initialIndex = selectedIndex >= 0 ? selectedIndex : Math.floor(items.length / 2);
    const key = stateKeyForColumn(columnIndex);
    const currentState = wheelState.current[columnIndex];
    const state = wheelState.current[columnIndex] = currentState?.key === key ? currentState : {
      current: initialIndex,
      target: initialIndex,
      selected: initialIndex,
      key,
    };
    state.target = Math.min(items.length - 1, Math.max(0, target));
    if (commit) {
      const nextIndex = Math.min(items.length - 1, Math.max(0, Math.round(state.target)));
      if (nextIndex !== state.selected) commitWheelSelection(columnIndex, items, nextIndex);
      if (prefersReducedMotion()) {
        state.current = state.target;
        redraw();
        return;
      }
    }
    startWheelAnimation();
  };

  // 列:col0=[default];colK = 当前 path 上一层的"域子级"(分身已排除)
  const columns: string[][] = [["default"]];
  for (let k = 1; k <= path.length; k++) { const par = path[k - 1]; const kids = domainKids(par); if (kids.length) columns.push(kids); else break; }

  const askReparent = (node: string, target: string | null) => {
    if (!canGovern) return;
    if (!node || node === target) return;
    if (node === "default") return;                       // X 不可移
    if (nodes[node]?.main_twin) return;                   // 分身不可移(模式,非下属)
    if (target && isDesc(node, target)) return;           // 防环:不能移到自己的后代下
    setReparent({ node, target });
  };

  const dropProps = (target: string | null) => canGovern ? ({
    onDragOver: (e: React.DragEvent) => { if (drag && drag !== target) { e.preventDefault(); (e.currentTarget as HTMLElement).classList.add("ag-drop"); } },
    onDragLeave: (e: React.DragEvent) => (e.currentTarget as HTMLElement).classList.remove("ag-drop"),
    onDrop: (e: React.DragEvent) => { e.preventDefault(); (e.currentTarget as HTMLElement).classList.remove("ag-drop"); if (drag) askReparent(drag, target); },
  }) : {};

  /* ---- edges ----
     连线端点**不再每帧量 DOM**（批次七 d 第 2 条）：静止时量一次几何底稿
     （每列的左边缘/中心线、每个节点的布局宽度），动画帧里按 `state.current`
     用 `wheelAnchor` 解析地算端点。于是连线逐帧跟着名字走，且每帧零
     `getBoundingClientRect`（AD-81 要修的布局抖动依然不会回来）。 */
  const measureWheelGeometry = () => {
    const cols = colsRef.current, svg = svgRef.current;
    if (!cols || !svg) return;
    svg.setAttribute("width", String(cols.scrollWidth));
    svg.setAttribute("height", String(cols.scrollHeight));
    const base = cols.getBoundingClientRect();
    const statics: Record<string, { l: { x: number; y: number }; r: { x: number; y: number } }> = {};
    const columns: ColumnGeometry[] = [...cols.querySelectorAll<HTMLElement>(".graph-col")].map((column) => {
      const rect = column.getBoundingClientRect();
      const items = [...column.querySelectorAll<HTMLElement>(".ag-node")].map((item) => ({
        id: item.dataset.id ?? "",
        width: item.offsetWidth,
      }));
      return { left: rect.left - base.left, centerY: rect.top - base.top + rect.height / 2, items };
    });
    // 非轮盘元素（X 根节点、分身）不受 transform 影响，直接记它们量到的端点。
    cols.querySelectorAll<HTMLElement>(".ag-root[data-id], .ag-twin[data-id]").forEach((el) => {
      const rect = el.getBoundingClientRect();
      const y = rect.top - base.top + rect.height / 2;
      statics[el.dataset.id ?? ""] = {
        l: { x: rect.left - base.left - 2, y },
        r: { x: rect.right - base.left + 6, y },
      };
    });
    geometry.current = { columns, statics };
  };
  /** 某个 id 的端点：轮盘节点走解析式，其余走静止时量到的值。 */
  const anchorFor = (id: string, side: "l" | "r"): { x: number; y: number } | null => {
    const geom = geometry.current;
    if (!geom) return null;
    for (let columnIndex = 0; columnIndex < geom.columns.length; columnIndex++) {
      const column = geom.columns[columnIndex];
      const itemIndex = column.items.findIndex((item) => item.id === id);
      if (itemIndex < 0) continue;
      const state = wheelState.current[columnIndex];
      const current = state ? state.current : itemIndex;
      return wheelAnchor({
        columnLeft: column.left,
        columnCenterY: column.centerY,
        width: column.items[itemIndex].width,
        offset: itemIndex - current,
        side,
      });
    }
    return geom.statics[id] ? geom.statics[id][side] : null;
  };
  const drawEdges = () => {
    const svg = svgRef.current, geom = geometry.current;
    if (!svg || !geom) return;
    const activePath = pathRef.current;
    let html = "";
    for (let c = 1; c < geom.columns.length; c++) {
      const a = anchorFor(activePath[c - 1], "r");
      if (!a) continue;
      const activeId = activePath[c];
      for (const item of geom.columns[c].items) {
        if (item.id === ADD_NODE_ID) continue;   // 虚节点不是真子级,不牵线
        const b = anchorFor(item.id, "l");
        if (!b) continue;
        const active = item.id === activeId;
        const mx = (a.x + b.x) / 2;
        html += `<path d="M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}" fill="none" stroke="${active ? "var(--gold-deep)" : "var(--graph-link-idle)"}" stroke-width="${active ? 1.7 : 1}"/>`;
      }
    }
    svg.innerHTML = html;
  };
  const applyWheelTransforms = () => {
    const cols = colsRef.current;
    if (!cols) return;
    const activeSel = selRef.current;
    cols.querySelectorAll<HTMLElement>(".graph-col").forEach((column, columnIndex) => {
      const items = [...column.querySelectorAll<HTMLElement>(".ag-node")];
      if (!items.length) return;
      const selectedIndex = items.findIndex((item) => item.dataset.id === activeIdForColumn(columnIndex));
      const initialIndex = selectedIndex >= 0 ? selectedIndex : Math.floor(items.length / 2);
      const key = stateKeyForColumn(columnIndex);
      const currentState = wheelState.current[columnIndex];
      const state = wheelState.current[columnIndex] = currentState?.key === key ? currentState : {
        current: initialIndex,
        target: initialIndex,
        selected: initialIndex,
        key,
      };
      state.current = Math.min(items.length - 1, Math.max(0, state.current));
      state.target = Math.min(items.length - 1, Math.max(0, state.target));
      /* 高亮跟轮盘走,不跟 React 状态走:滚动中选中还没提交,前排节点就是
         round(current)。非交互列保持老行为(只有被选中的那个亮),视觉不变。 */
      const previewIndex = Math.round(state.current);
      const previewing =
        wheelInteracting.current === columnIndex || items[previewIndex]?.dataset.id === activeSel;
      items.forEach((item, index) => {
        const t = wheelNodeTransform(index - state.current);
        // 远端 blur 去掉(AD-81):合成层上每帧重绘,是掉帧的第二来源;深度靠 opacity 表达。
        item.style.transform = wheelTransformCss(t);
        item.style.opacity = t.opacity.toFixed(2);
        item.style.zIndex = String(50 - Math.round(t.depth * 8));
        item.classList.toggle("current-front", previewing && index === previewIndex);
      });
    });
  };
  /** `measure=false` 只在动画帧里用：复用静止时的几何底稿，一次 DOM 测量都不做。 */
  const redraw = (measure = true) => {
    if (!colsRef.current || !svgRef.current) return;
    if (measure || !geometry.current) measureWheelGeometry();
    applyWheelTransforms();
    drawEdges();
  };
  const animateWheel = (now = performance.now()) => {
    wheelFrame.current = null;
    const previous = wheelLastFrame.current ?? now - 16;
    const dt = Math.min(34, Math.max(8, now - previous));
    wheelLastFrame.current = now;
    const spring = 1 - Math.exp(-dt / 86);
    let moving = false;
    Object.values(wheelState.current).forEach((state) => {
      const distance = state.target - state.current;
      if (Math.abs(distance) < 0.001) {
        state.current = state.target;
      } else {
        state.current += distance * spring;
        moving = true;
      }
    });
    // 动画帧也画连线,但走解析式几何(measure=false),不量 DOM ——
    // 既没有布局抖动(AD-81),连线也不再滞后于名字(批次七 d 第 2 条)。
    redraw(false);
    if (moving) wheelFrame.current = requestAnimationFrame(animateWheel);
    else wheelLastFrame.current = null;
  };
  const startWheelAnimation = () => {
    if (wheelFrame.current === null) wheelFrame.current = requestAnimationFrame(animateWheel);
  };
  const pointWheelAt = (columnIndex: number, id: string) => {
    const columnsEl = colsRef.current;
    const column = columnsEl?.querySelectorAll<HTMLElement>(".graph-col")[columnIndex];
    const items = [...(column?.querySelectorAll<HTMLElement>(".ag-node") || [])];
    if (!items.length) return;
    const targetIndex = items.findIndex((item) => item.dataset.id === id);
    if (targetIndex < 0) return;
    const activeIndex = items.findIndex((item) => item.dataset.id === activeIdForColumn(columnIndex));
    const initialIndex = activeIndex >= 0 ? activeIndex : Math.floor(items.length / 2);
    const currentState = wheelState.current[columnIndex];
    const key = stateKeyForColumn(columnIndex);
    const state = wheelState.current[columnIndex] = currentState?.key === key ? currentState : {
      current: initialIndex,
      target: initialIndex,
      selected: initialIndex,
      key,
    };
    state.target = targetIndex;
    state.selected = targetIndex;
    startWheelAnimation();
  };
  const alignWheelToPath = (animated = true) => {
    const columnsEl = colsRef.current;
    if (!columnsEl) return;
    const columnEls = [...columnsEl.querySelectorAll<HTMLElement>(".graph-col")];
    columnEls.forEach((column, columnIndex) => {
      const desiredId = activeIdForColumn(columnIndex);
      const items = [...column.querySelectorAll<HTMLElement>(".ag-node")];
      if (!items.length || !desiredId) return;
      const targetIndex = items.findIndex((item) => item.dataset.id === desiredId);
      if (targetIndex < 0) return;
      const currentState = wheelState.current[columnIndex];
      const fallbackIndex = currentState?.key === stateKeyForColumn(columnIndex)
        ? Math.min(items.length - 1, Math.max(0, currentState.current))
        : targetIndex;
      const key = stateKeyForColumn(columnIndex);
      const state = wheelState.current[columnIndex] = currentState?.key === key ? currentState : { current: fallbackIndex, target: fallbackIndex, selected: Math.round(fallbackIndex), key };
      state.target = targetIndex;
      if (!animated) {
        state.current = targetIndex;
        state.selected = targetIndex;
      }
    });
    if (animated) startWheelAnimation();
    else redraw();
  };
  useLayoutEffect(redraw);
  useLayoutEffect(() => {
    if (colsRef.current) colsRef.current.style.transform = "";
    const pending = pendingFocus.current;
    const animated = wheelHydrated.current && pending?.animate === true;
    // 用户手还在轮盘上时不要对齐:那会把 target 拽回 path 说的位置。
    if (wheelInteracting.current === null) alignWheelToPath(animated);
    wheelHydrated.current = true;
    pendingFocus.current = null;
  }, [path, sel]); // eslint-disable-line
  const appliedFocus = useRef<string | null>(null);
  useEffect(() => {
    if (!focusName || !nodes[focusName]) return;
    // 只在 focusName 真变化时重聚焦。net 留在依赖里只为"节点后到"的首次应用;
    // 否则任何 SSE 刷新造出新 net 对象都会把用户滚轮浏览到的位置拽回旧焦点。
    if (appliedFocus.current === focusName) return;
    appliedFocus.current = focusName;
    focusNode(focusName, undefined, false);
    requestAnimationFrame(() => alignWheelToPath(false));
  }, [focusName, net]); // eslint-disable-line
  useEffect(() => {
    const f = () => requestAnimationFrame(() => redraw());
    window.addEventListener("resize", f);
    const c = canvasRef.current; c?.addEventListener("scroll", f);
    return () => { window.removeEventListener("resize", f); c?.removeEventListener("scroll", f); };
  }, [path, sel]); // eslint-disable-line

  useEffect(() => {
    if (!embedded) return;
    const canvas = canvasRef.current;
    const columns = colsRef.current;
    if (!canvas || !columns) return;
    const onWheel = (event: WheelEvent) => {
      const multiplier = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? canvas.clientHeight : 1;
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        event.preventDefault();
        canvas.scrollLeft += event.deltaX * multiplier;
        return;
      }
      if (event.shiftKey && Math.abs(event.deltaY) > 0) {
        event.preventDefault();
        canvas.scrollLeft += event.deltaY * multiplier;
        return;
      }
      const columnEls = [...columns.querySelectorAll<HTMLElement>(".graph-col")];
      const pointerColumn = columnEls.findIndex((column) => {
        const rect = column.getBoundingClientRect();
        return event.clientX >= rect.left && event.clientX <= rect.right;
      });
      if (pointerColumn < 1) return;
      const columnIndex = pointerColumn;
      const items = [...(columnEls[columnIndex]?.querySelectorAll<HTMLElement>(".ag-node") || [])];
      if (items.length < 2) return;
      event.preventDefault();
      const selectedIndex = items.findIndex((item) => item.dataset.id === activeIdForColumn(columnIndex));
      const initialIndex = selectedIndex >= 0 ? selectedIndex : Math.floor(items.length / 2);
      const key = stateKeyForColumn(columnIndex);
      const currentState = wheelState.current[columnIndex];
      const state = currentState?.key === key ? currentState : { current: initialIndex, target: initialIndex, selected: initialIndex, key };
      const delta = event.deltaY * multiplier;
      wheelInteracting.current = columnIndex;
      setWheelTarget(columnIndex, state.target + delta * 0.0018);
      if (wheelSnapTimer.current !== null) window.clearTimeout(wheelSnapTimer.current);
      wheelSnapTimer.current = window.setTimeout(() => {
        wheelSnapTimer.current = null;
        wheelInteracting.current = null;
        const live = wheelState.current[columnIndex];
        if (live) setWheelTarget(columnIndex, Math.round(live.target), true);
      }, 110);
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      canvas.removeEventListener("wheel", onWheel);
    };
  }, [embedded, net]); // eslint-disable-line
  useEffect(() => {
    if (!embedded || canGovern) return;
    const canvas = canvasRef.current;
    const columns = colsRef.current;
    if (!canvas || !columns) return;
    let gesture: { pointerId: number; columnIndex: number; startX: number; startY: number; startTarget: number; startScrollLeft: number; mode: "pending" | "wheel" | "pan"; moved: boolean; captured: boolean } | null = null;
    const columnAt = (clientX: number) => {
      const columnEls = [...columns.querySelectorAll<HTMLElement>(".graph-col")];
      const pointerColumn = columnEls.findIndex((column) => {
        const rect = column.getBoundingClientRect();
        return clientX >= rect.left && clientX <= rect.right;
      });
      return pointerColumn >= 1 ? pointerColumn : -1;
    };
    const onPointerDown = (event: PointerEvent) => {
      if ((event.target as HTMLElement).closest(".ag-dots,.ag-root,.ag-twincluster,.ag-panel")) return;
      const columnIndex = columnAt(event.clientX);
      if (columnIndex < 1) return;
      const column = columns.querySelectorAll<HTMLElement>(".graph-col")[columnIndex];
      const items = [...(column?.querySelectorAll<HTMLElement>(".ag-node") || [])];
      if (items.length < 2) return;
      const selectedIndex = items.findIndex((item) => item.dataset.id === activeIdForColumn(columnIndex));
      const initialIndex = selectedIndex >= 0 ? selectedIndex : Math.floor(items.length / 2);
      const key = stateKeyForColumn(columnIndex);
      const currentState = wheelState.current[columnIndex];
      const state = wheelState.current[columnIndex] = currentState?.key === key ? currentState : { current: initialIndex, target: initialIndex, selected: initialIndex, key };
      gesture = { pointerId: event.pointerId, columnIndex, startX: event.clientX, startY: event.clientY, startTarget: state.target, startScrollLeft: canvas.scrollLeft, mode: "pending", moved: false, captured: false };
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      const dx = event.clientX - gesture.startX;
      const dy = event.clientY - gesture.startY;
      if (gesture.mode === "pending" && Math.max(Math.abs(dx), Math.abs(dy)) > 6) {
        gesture.mode = Math.abs(dx) > Math.abs(dy) ? "pan" : "wheel";
        gesture.moved = true;
        gesture.captured = true;
        canvas.setPointerCapture?.(event.pointerId);
        canvas.classList.add(gesture.mode === "pan" ? "ag-panning" : "ag-wheel-dragging");
      }
      if (!gesture.moved) return;
      event.preventDefault();
      if (gesture.mode === "pan") {
        canvas.scrollLeft = gesture.startScrollLeft - dx;
        return;
      }
      const delta = -dy / 76;
      wheelInteracting.current = gesture.columnIndex;
      setWheelTarget(gesture.columnIndex, gesture.startTarget + delta);
    };
    const endGesture = (event: PointerEvent) => {
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      if (gesture.moved) {
        suppressNodeClick.current = true;
        if (gesture.mode === "wheel") {
          wheelInteracting.current = null;
          const live = wheelState.current[gesture.columnIndex];
          if (live) setWheelTarget(gesture.columnIndex, Math.round(live.target), true);
        }
        window.setTimeout(() => { suppressNodeClick.current = false; }, 120);
      }
      if (gesture.captured) canvas.releasePointerCapture?.(event.pointerId);
      canvas.classList.remove("ag-panning");
      canvas.classList.remove("ag-wheel-dragging");
      gesture = null;
    };
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", endGesture);
    canvas.addEventListener("pointercancel", endGesture);
    return () => {
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", endGesture);
      canvas.removeEventListener("pointercancel", endGesture);
      canvas.classList.remove("ag-panning");
      canvas.classList.remove("ag-wheel-dragging");
    };
  }, [embedded, canGovern, net]); // eslint-disable-line
  useEffect(() => () => {
    if (wheelFrame.current !== null) cancelAnimationFrame(wheelFrame.current);
    if (wheelSnapTimer.current !== null) window.clearTimeout(wheelSnapTimer.current);
  }, []);

  // ---- pan (drag empty canvas) ----
  useEffect(() => {
    const c = canvasRef.current; if (!c) return;
    let pan = false, sx = 0, sy = 0, sl = 0, st = 0;
    const down = (e: MouseEvent) => {
      if ((e.target as HTMLElement).closest(".ag-node,.ag-twin,.ag-root,.ag-dots,.ag-twincluster,.ag-panel,.ag-draft,.ag-droptop,button,a,input,textarea,select")) return;
      pan = true; sx = e.clientX; sy = e.clientY; sl = c.scrollLeft; st = c.scrollTop; c.classList.add("ag-panning");
      e.preventDefault();
    };
    const move = (e: MouseEvent) => { if (!pan) return; c.scrollLeft = sl - (e.clientX - sx); c.scrollTop = st - (e.clientY - sy); };
    const up = () => { if (pan) { pan = false; c.classList.remove("ag-panning"); } };
    c.addEventListener("mousedown", down); window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
    return () => { c.removeEventListener("mousedown", down); window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
  }, []);

  const node = nodes[sel];
  const drafts = net.drafts || [];
  const handleColumnClick = (event: React.MouseEvent<HTMLDivElement>, columnIndex: number) => {
    if (columnIndex < 1) return;
    if (suppressNodeClick.current) return;
    if ((event.target as HTMLElement).closest(".ag-node,.ag-dots")) return;
    const items = [...event.currentTarget.querySelectorAll<HTMLElement>(".ag-node")];
    const hit = items.find((item) => {
      const rect = item.getBoundingClientRect();
      return (
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom
      );
    });
    const id = hit?.dataset.id;
    if (!id || !nodes[id]) return;
    if (selRef.current === id && isSettledFront(columnIndex, id)) {
      onGoProfile(id);
      return;
    }
    focusNode(id, columnIndex, true);
  };
  const handleBlankPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest(".ag-panel,.ag-node,.ag-dots,.ag-root,.ag-twin,.ag-twincluster,.ag-draft,.ag-droptop,.ag-act,button,a,input,textarea,select")) return;
    setPanelVisible(false);
  };

  const Node = ({ id, ci }: { id: string; ci: number }): ReactNode => {
    const n = nodes[id]; if (!n) return null;
    const kids = domainKids(id);
    const dead = n.killed || n.effective_killed;
    const sl = (n.symlinks || []).filter((s) => s.target_profile);
    const roleLabel = id === "default" ? t("graph.value.mainAgent") : roleFull(n.role);
    return (
      <div
        className={`ag-node ${id === sel ? "sel" : ""} ${path.includes(id) && id !== sel ? "path" : ""}`}
        data-id={id}
        draggable={canGovern && id !== "default"}
        /* batch43 第 2 件（★J-4 追加）：同一次拖拽现在有两个去处，互不打架——
           拖到**别的项目节点**上仍是改父级（那条路只看下面的 `drag` state，
           一个字没动）；拖到**组浮窗的胶囊/成员栏**上则是「从这个项目启动一名
           新成员」，靠 `dataTransfer` 上这个新 MIME 认出来。
           不设 `effectAllowed`：设了会连带改掉改父级那头的落点光标。 */
        onDragStart={(e) => {
          if (!canGovern) { e.preventDefault(); return; }
          e.dataTransfer.setData(PROJECT_DRAG_MIME, id);
          // 顺手带一份显示名：组那头的回执要说「已从 <项目名> 启动一名成员」。
          e.dataTransfer.setData("text/plain", dname(n));
          setDrag(id);
        }}
        onDragEnd={() => setDrag(null)}
        {...dropProps(id)}
        onClick={(e) => {
          if (suppressNodeClick.current) return;
          if ((e.target as HTMLElement).closest(".ag-dots")) return;
          if (sel === id && isSettledFront(ci, id)) { onGoProfile(id); return; }
          focusNode(id, ci, true);
        }}
        onDoubleClick={() => onGoProfile(id)}
        style={{ opacity: dead ? 0.5 : 1 }}
      >
        {/* 命中区：至少 32×32 的透明面，点它冒泡到节点自己的 onClick（第 6 件）。 */}
        <span className="ag-node-hit" aria-hidden />
        <span className={`ag-dot ${n.gateway === "running" ? "on" : "idle"}`} title={`gateway ${n.gateway || "?"}`} />
        <span className="ag-nm">{dname(n)}</span>
        <span className="ag-rl">{roleLabel} · {n.skill_count}{sl.length ? ` · 🔗${sl.length}` : ""}</span>
        {dead && <span className="ag-bad">{t("graph.value.disabled")}</span>}
        {kids.length > 0 && <span className="ag-cn">▸{kids.length}</span>}
        {canGovern && <button className="ag-dots" title={t("common.more")} onClick={(e) => { e.stopPropagation(); onOpenMenu(id, (e.currentTarget as HTMLElement).getBoundingClientRect()); }}><MoreHorizontal className="size-3.5" /></button>}
      </div>
    );
  };

  const graph = (
    <>
      {canGovern && drafts.length > 0 && (
        <div className={`flex flex-wrap items-center gap-2 ${embedded ? "ag-embedded-drafts" : "mb-2 pb-2"}`} style={{ borderBottom: `1px dashed ${cream(20)}` }}>
          <span className="text-[0.7rem]" style={{ color: cream(50) }}>{t("graph.drafts", { count: drafts.length })}</span>
          {drafts.map((dn) => (
            <div key={dn} className="ag-draft" draggable onDragStart={() => setDrag(dn)} onDragEnd={() => setDrag(null)}>
              {dname({ name: dn })}<span className="ag-bad" style={{ marginLeft: 6 }}>{t("graph.draftBadge")}</span>
            </div>
          ))}
        </div>
      )}
      {canGovern && <div className="ag-droptop" {...dropProps(null)}>{t("graph.dropTop")}</div>}

        <div
          className={`${embedded ? "agent-browser-body" : "flex"} ${panelVisible ? "" : "is-panel-hidden"}`}
          style={embedded ? undefined : { height: "70vh" }}
          onPointerDownCapture={handleBlankPointerDown}
        >
        <div className="ag-canvas" ref={canvasRef}>
          <svg className="ag-edges" ref={svgRef} />
          <div className="ag-cols" ref={colsRef}>
            {columns.map((ids, ci) => (
              <div
                className={`graph-col ${ci > 0 ? "ag-wheel-col" : ""}`}
                data-column={ci}
                key={`${ci}:${ci === 0 ? "__root__" : path[ci - 1]}`}
                onClick={(event) => handleColumnClick(event, ci)}
              >
                {ci === 0 ? (
                  <>
                    <div className="ag-root" data-id="default" onClick={() => focusNode("default")} {...dropProps("default")}>
                      <div className="ag-rootline" />
                      <div className="ag-rootnm">{displayName("default")}</div>
                      <div className="ag-rootsub">{nodes["default"]?.twin_mode || t("graph.rootSub")}</div>
                    </div>
                    {(nodes["default"]?.twins || []).length > 0 && (
                      <div className="ag-twincluster">
                        <div className="ag-twinh">{t("tree.twinModes")}</div>
                        {(nodes["default"]?.twins || []).map((tn) => {
                          const twin = nodes[tn]; if (!twin) return null;
                          return (
                            <div key={tn} className={`ag-twin ${tn === sel ? "sel" : ""}`} onClick={() => focusNode(tn)} onDoubleClick={() => onGoProfile(tn)}>
                              <span className="ag-tdot" /><span className="ag-nm">{dname(twin)}</span>
                              {twin.twin_mode && <span className="ag-tmode">{twin.twin_mode}</span>}
                              {canGovern && <button className="ag-dots" title={t("common.more")} onClick={(e) => { e.stopPropagation(); onOpenMenu(tn, (e.currentTarget as HTMLElement).getBoundingClientRect()); }}><MoreHorizontal className="size-3.5" /></button>}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    {ids.map((id) => <Node key={id} id={id} ci={ci} />)}
                    {onAddChild && path[ci - 1] && (
                      /* 每列子级末尾的虚节点：样式沿用 .ag-node（所以它跟着轮盘一起滚），
                         虚线框 + 淡色区分。根列（X 那一列）不加。 */
                      <div
                        className="ag-node ag-node-add"
                        data-id={ADD_NODE_ID}
                        onClick={(event) => {
                          event.stopPropagation();
                          if (suppressNodeClick.current) return;
                          onAddChild(path[ci - 1]);
                        }}
                      >
                        <Plus className="size-3.5" />
                        <span className="ag-nm">{t("graph.addProject")}</span>
                        <span className="ag-rl">{t("graph.addProject.under", { name: displayName(path[ci - 1]) })}</span>
                      </div>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* 详情面板:摘要 + 进入对话 / 详情(→ProfileDrawer 全字段) */}
        {panelVisible && <aside className="ag-panel">
          {node && (
            <>
              <div className="ag-panel-head">
                <div className="ag-ptag">
                  <span>{node.main_twin ? t("graph.tag.twin") : t("graph.head.title")}</span>
                </div>
                <h2 className="ag-ph2">{dname(node)}</h2>
                <div className="ag-prole"><span className={`ag-status ${node.gateway === "running" ? "" : "idle"}`} />{(sel === "default" ? t("graph.value.mainAgent") : roleFull(node.role))}{node.twin_mode ? ` · ${node.twin_mode}` : ""}{(node.killed || node.effective_killed) ? ` · ${t("graph.value.disabled")}` : ""}</div>
                <div className="ag-pactions">
                  <button type="button" className="ag-act p" onClick={() => onGoProfile(sel)} title={t("graph.openChat")} aria-label={t("graph.openChat")}><MessageSquare size={17} /></button>
                  <button type="button" className="ag-act g" onClick={() => onDetail(sel)} title={t("graph.details")} aria-label={t("graph.details")}><Settings2 size={17} /></button>
                </div>
              </div>
              <div className="ag-panel-section">
                <h3>{t("graph.section.identity")}</h3>
                <div className="ag-prow"><span>{t("graph.row.type")}</span><span>{node.main_twin ? t("graph.value.twinShared") : sel === "default" ? t("graph.value.apexRoot") : roleFull(node.role)}</span></div>
                <div className="ag-prow"><span>{t("graph.row.parent")}</span><span>{node.parent ? displayName(node.parent) : t("graph.value.topLevel")}</span></div>
                <div className="ag-prow"><span>{t("graph.row.model")}</span><span>{node.model || t("common.dash")}</span></div>
              </div>
              <div className="ag-panel-section">
                <h3>{t("graph.section.capabilities")}</h3>
                <div className="ag-stat-row">
                  <div><strong>{node.skill_count}</strong><span>{t("graph.stat.skills")}</span></div>
                  <div><strong>{(node.symlinks || []).length}</strong><span>{t("graph.stat.links")}</span></div>
                  <div><strong>{domainKids(sel).length}</strong><span>{t("graph.stat.children")}</span></div>
                </div>
                <div className="ag-prow"><span>{t("graph.row.gateway")}</span><span>{node.gateway || t("common.dash")}</span></div>
              </div>
              <div className="ag-panel-section">
                <h3>{t("graph.section.inheritance")}</h3>
                <div className="ag-prow"><span>{t("graph.row.parentConfig")}</span><span>{node.parent ? t("graph.value.fromParent", { name: displayName(node.parent) }) : t("graph.value.localBaseline")}</span></div>
                {node.main_twin && <div className="ag-note">{t("graph.note.twin")}</div>}
              </div>
            </>
          )}
        </aside>}
      </div>

      {canGovern && reparent && <ReparentModal node={reparent.node} target={reparent.target} onClose={() => setReparent(null)} onDone={() => { setReparent(null); onChanged(); }} />}
    </>
  );

  if (embedded) {
    return (
      <section className={`agent-browser ${canGovern ? "is-governance" : "is-chat"}`}>
        <header className="agent-browser-head">
          <h1>{t("graph.head.title")}</h1>
          <div>{canGovern ? t("graph.head.governance") : t("graph.head.browse")} · <b>{lineage(sel).map(displayName).join(" / ")}</b></div>
          <div className="agent-browser-modes" role="tablist" aria-label={t("graph.mode.aria")}>
            <button type="button" className={mode === "chat" ? "is-active" : ""} onClick={() => onModeChange?.("chat")}>{t("graph.mode.chat")}</button>
            <button type="button" className={mode === "governance" ? "is-active" : ""} onClick={() => onModeChange?.("governance")}>{t("graph.mode.governance")}</button>
          </div>
          {/* 第 6 件：虚节点保留，但它跟着轮盘滚、离屏时找不着。这里再放一个固定入口，
              父级 = 当前选中的节点，与虚节点同一语义。样式用现有 <Button>。 */}
          {onAddChild && (
            <Button onClick={() => onAddChild(sel)}>
              <span className="inline-flex items-center gap-1">
                <Plus className="size-3" /> {t("graph.addProject")}
              </span>
            </Button>
          )}
        </header>
        {graph}
      </section>
    );
  }

  return (
    <Overlay title={t("graph.overlay.title")} onClose={onClose} onReload={onChanged}>
      {graph}
    </Overlay>
  );
}
