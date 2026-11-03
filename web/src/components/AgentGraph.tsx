import "./project-polish.css";
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { CornerDownLeft, MessageSquare, MoreHorizontal, Plus, X } from "lucide-react";
import { type NetworkResp } from "../lib/api";
import { useLocale } from "../i18n";
import { Button, cream, dname, Overlay } from "./ui";
import { ReparentModal } from "./ReparentModal";
import { ProjectSkillTree } from "./ProjectSkillTree";
import { wheelAnchor, wheelFling, wheelNodeTransform, wheelSnapDuration, wheelSnapEase, wheelTransformCss } from "../lib/wheelGeometry";
import { GROUP_DRAG_LABEL_MIME, PROJECT_DRAG_MIME } from "../lib/groupDrop";

/** 每列末尾那个「+ 添加项目」虚节点的 id（不是真 profile，`nodes[]` 里查不到）。 */
export const ADD_NODE_ID = "__add-child__";
const PROJECT_ENTER_SHAPE = "M .57 .01 H .97 Q .997 .01 .997 .096 V .904 Q .997 .99 .97 .99 H .03 Q .003 .99 .003 .904 V .649 Q .003 .563 .03 .563 H .51 Q .54 .563 .54 .477 V .096 Q .54 .01 .57 .01 Z";
type AgentGraphMode = "chat" | "governance";
/** `snap`：吸附 / 点击定位时的定时缓动段；滚动中为空，节点只跟一阶弹簧走。 */
type WheelSnap = { from: number; to: number; start: number; duration: number };
type WheelColumnState = { current: number; target: number; selected: number; key: string; snap?: WheelSnap };
type ColumnGeometry = { left: number; centerY: number; items: { id: string; width: number }[] };
type WheelGeometry = { columns: ColumnGeometry[]; statics: Record<string, { l: { x: number; y: number }; r: { x: number; y: number } }> };

/* Agents · 下钻图。
   保留旧版全部能力:拖拽改父级(+ReparentModal 影响预览)/ 顶层拖放区 / 草稿暂存 /
   ⋯菜单(置顶·改名·fork·删除,经 onOpenMenu→CtxMenu)/ 停用标记 /
   默认是对话浏览;治理模式才启用组织维护动作。 */
export function AgentGraph({ net, onClose, onChanged, onGoProfile, onDetail, onNewConversation, onOpenMenu, onAddChild, embedded = false, focusName = null, mode = "governance", onModeChange }: {
  net: NetworkResp;
  onClose: () => void;
  onChanged: () => void;
  onGoProfile: (n: string) => void;
  onDetail: (n: string) => void;
  onNewConversation?: (projectRef: string) => void;
  onOpenMenu: (name: string, rect: DOMRect) => void;
  /** 「+ 添加项目」：父级 = 该列的父节点（批次七 d 第 5 条）。不传则不显示虚节点。 */
  onAddChild?: (parentId: string) => void;
  embedded?: boolean;
  focusName?: string | null;
  mode?: AgentGraphMode;
  onModeChange?: (mode: AgentGraphMode) => void;
}) {
  const { t } = useLocale();
  const enterShapeId = useId();
  const nodes = net.nodes;
  const canGovern = !embedded || mode === "governance";
  const displayName = (id: string) => dname(nodes[id] ?? { name: id });
  const domainKids = (id: string) =>
    (nodes[id]?.children || []).slice().sort((a, b) => a.localeCompare(b));
  const lineage = (id: string): string[] => {
    const a: string[] = []; let c: string | undefined = id; const seen = new Set<string>();
    while (c && !seen.has(c)) { seen.add(c); a.unshift(c); c = nodes[c]?.parent || undefined; }
    return a;
  };
  const focusPath = (id: string) => {
    const nextPath = lineage(id);
    return nextPath.length ? nextPath : ["default"];
  };
  const initialFocus = focusName && nodes[focusName] ? focusName : "default";
  const [path, setPath] = useState<string[]>(() => focusPath(initialFocus));
  const [sel, setSel] = useState<string>(initialFocus);
  /* 右侧详情面板的开合（初版就有，9 月 28 日快照时被删掉）：选中任何节点都打开；
     再点一次已选中的节点、点画布空白、Esc、面板上的关闭钮都收起。面板留在 DOM 里
     只做 transform 滑动，收起时 inert，焦点回到选中的节点。 */
  const [panelOpen, setPanelOpen] = useState(true);
  const panelOpenRef = useRef(panelOpen);
  const panelRef = useRef<HTMLElement>(null);
  const pathRef = useRef<string[]>(path);
  const selRef = useRef<string>(sel);
  const pendingDomFocus = useRef<string | null>(null);
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
  panelOpenRef.current = panelOpen;

  const isDesc = (anc: string, x: string) => { let c: string | undefined = x; const seen = new Set<string>(); while (c && !seen.has(c)) { if (c === anc) return true; seen.add(c); c = nodes[c]?.parent || undefined; } return false; };
  function focusNode(id: string, columnIndex?: number, animate = true) {
    if (!nodes[id]) return;
    // 点击/键盘胜过还没到期的吸附:否则 110ms 后那次提交会把点击结果顶掉。
    if (wheelSnapTimer.current !== null) { window.clearTimeout(wheelSnapTimer.current); wheelSnapTimer.current = null; }
    wheelInteracting.current = null;
    const nextPath = focusPath(id);
    selRef.current = id;
    pathRef.current = nextPath;
    pendingFocus.current = { id, animate };
    if (animate && typeof columnIndex === "number") pointWheelAt(columnIndex, id);
    setPanelOpen(true);
    setSel(id);
    setPath(nextPath);
  }
  const nodeElement = (id: string): HTMLElement | null =>
    colsRef.current?.querySelector<HTMLElement>(id === "default" ? ".ag-root" : `.ag-node[data-id="${CSS.escape(id)}"]`) ?? null;
  /** 收起面板。焦点在面板里（关闭钮）时必须挪走（面板随即 inert）；键盘关的（Esc）
      也把焦点交给选中的节点，方向键接着能用。鼠标点空白 / 点节点关的不碰焦点，免得凭空冒出焦点环。 */
  const closePanel = (restoreFocus = false) => {
    if (!panelOpenRef.current) return;
    panelOpenRef.current = false;
    setPanelOpen(false);
    const active = document.activeElement;
    const inPanel = !!active && !!panelRef.current?.contains(active);
    if (inPanel || (restoreFocus && (!active || active === document.body))) nodeElement(selRef.current)?.focus();
  };
  /** 点击 / 回车一个节点：没选中就选中并打开面板，已选中就开合面板。 */
  const activateNode = (id: string, columnIndex?: number) => {
    if (!nodes[id]) return;
    if (id === selRef.current && wheelInteracting.current === null) {
      if (panelOpenRef.current) closePanel();
      else { panelOpenRef.current = true; setPanelOpen(true); }
      return;
    }
    focusNode(id, columnIndex, true);
  };
  /** 方向键：上下在本列换节点，左右在上下级之间走；焦点跟着选中走。 */
  const handleNodeArrow = (key: string, id: string, columnIndex: number): boolean => {
    let next: string | null;
    let nextColumn = columnIndex;
    if (key === "ArrowUp" || key === "ArrowDown") {
      const siblings = columnIndex === 0 ? ["default"] : domainKids(pathRef.current[columnIndex - 1] ?? "");
      const index = siblings.indexOf(id);
      next = siblings[index + (key === "ArrowDown" ? 1 : -1)] ?? null;
    } else if (key === "ArrowRight") {
      if (id !== selRef.current && !pathRef.current.includes(id)) { next = id; }
      else { next = pathRef.current[columnIndex + 1] ?? domainKids(id)[0] ?? null; nextColumn = columnIndex + 1; }
    } else if (key === "ArrowLeft") {
      next = nodes[id]?.parent || null;
      nextColumn = columnIndex - 1;
    } else return false;
    if (!next || !nodes[next]) return false;
    pendingDomFocus.current = next;
    focusNode(next, nextColumn >= 1 ? nextColumn : undefined, true);
    return true;
  };
  const activeIdForColumn = (columnIndex: number) => columnIndex === 0 ? "default" : (pathRef.current[columnIndex] || selRef.current);
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
      state.target = nextIndex;
      if (nextIndex !== state.selected) commitWheelSelection(columnIndex, items, nextIndex);
      if (prefersReducedMotion()) {
        state.current = state.target;
        state.snap = undefined;
        redraw();
        return;
      }
      beginSnap(state);
    } else {
      // 手还在动：回到一阶弹簧跟手，吸附段作废。
      state.snap = undefined;
    }
    startWheelAnimation();
  };
  /** 从当前位置到 `state.target` 走一段定时缓动（见 wheelSnapEase）。 */
  const beginSnap = (state: WheelColumnState) => {
    const distance = state.target - state.current;
    if (Math.abs(distance) < 0.001) { state.current = state.target; state.snap = undefined; return; }
    state.snap = { from: state.current, to: state.target, start: performance.now(), duration: wheelSnapDuration(distance) };
  };

  // 列:col0=[default];colK = 当前 path 上一层的"域子级"(分身已排除)
  const columns: string[][] = [["default"]];
  for (let k = 1; k <= path.length; k++) { const par = path[k - 1]; const kids = domainKids(par); if (kids.length) columns.push(kids); else break; }

  const askReparent = (node: string, target: string | null) => {
    if (!canGovern) return;
    if (!node || node === target) return;
    if (node === "default") return;                       // X 不可移
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
    /* 右端点贴着**可见内容**（名字 / 停用标 / 子级数）的右缘，不是布局盒的右缘：
       节点有 330px 的 min-width，按盒子量会让连线从名字后面一大截空白里长出来，
       到下一列只剩 20px 的小钩。命中面、⋯ 与进入钮不算内容（后两者只在悬停 /
       选中时出现，算进去端点会在提交那一刻跳 40px）。 */
    const contentWidth = (item: HTMLElement) => {
      let right = 0;
      for (const child of Array.from(item.children)) {
        if (!(child instanceof HTMLElement)) continue;
        if (child.classList.contains("ag-node-hit") || child.classList.contains("ag-dots") || child.classList.contains("ag-node-enter")) continue;
        right = Math.max(right, child.offsetLeft + child.offsetWidth);
      }
      return right > 0 ? right : item.offsetWidth;
    };
    const columns: ColumnGeometry[] = [...cols.querySelectorAll<HTMLElement>(".graph-col")].map((column) => {
      const rect = column.getBoundingClientRect();
      const items = [...column.querySelectorAll<HTMLElement>(".ag-node")].map((item) => ({
        id: item.dataset.id ?? "",
        width: contentWidth(item),
      }));
      return { left: rect.left - base.left, centerY: rect.top - base.top + rect.height / 2, items };
    });
    // 非轮盘元素（X 根节点）不受 transform 影响，直接记它量到的端点。
    cols.querySelectorAll<HTMLElement>(".ag-root[data-id]").forEach((el) => {
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
        html += `<path d="M${a.x.toFixed(1)},${a.y.toFixed(1)} C${mx.toFixed(1)},${a.y.toFixed(1)} ${mx.toFixed(1)},${b.y.toFixed(1)} ${b.x.toFixed(1)},${b.y.toFixed(1)}" fill="none" stroke="${active ? "var(--gold-deep)" : "var(--graph-link-idle)"}" stroke-width="${active ? 1.7 : 1.1}" stroke-linecap="round"/>`;
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
      const snap = state.snap;
      if (snap) {
        // 吸附段：定时缓动，到时精确落位；期间 target 不会变（变了 setWheelTarget 会清掉 snap）。
        const p = Math.min(1, (now - snap.start) / snap.duration);
        state.current = snap.from + (snap.to - snap.from) * wheelSnapEase(p);
        if (p >= 1) { state.current = snap.to; state.snap = undefined; }
        else moving = true;
        return;
      }
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
    if (prefersReducedMotion()) { state.current = targetIndex; state.snap = undefined; }
    else beginSnap(state);
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
      // 正在往别处吸附的那段作废；往同一处的继续走完。
      if (state.snap && state.snap.to !== targetIndex) state.snap = undefined;
      if (!animated) {
        state.current = targetIndex;
        state.selected = targetIndex;
        state.snap = undefined;
      }
    });
    if (animated) startWheelAnimation();
    else redraw();
  };
  /** 选中后把最深那一列（选中项的子级）挪进画布可见区：三层以上时它会落到面板底下。只滚最少的距离。 */
  const revealDeepestColumn = () => {
    const canvas = canvasRef.current, cols = colsRef.current;
    if (!canvas || !cols) return;
    const columnEls = cols.querySelectorAll<HTMLElement>(".graph-col");
    const last = columnEls[columnEls.length - 1];
    if (!last) return;
    const view = canvas.getBoundingClientRect();
    const rect = last.getBoundingClientRect();
    if (view.width === 0 || rect.width === 0) return;
    const pad = 24;
    let delta = 0;
    if (rect.right > view.right - pad) delta = rect.right - (view.right - pad);
    if (rect.left - delta < view.left + pad) delta = rect.left - (view.left + pad);
    if (Math.abs(delta) < 1) return;
    if (typeof canvas.scrollBy === "function") canvas.scrollBy({ left: delta, behavior: prefersReducedMotion() ? "auto" : "smooth" });
    else canvas.scrollLeft += delta;
  };
  useLayoutEffect(redraw);
  useLayoutEffect(() => {
    const id = pendingDomFocus.current;
    if (!id) return;
    pendingDomFocus.current = null;
    nodeElement(id)?.focus();
  });
  useLayoutEffect(() => {
    if (colsRef.current) colsRef.current.style.transform = "";
    const pending = pendingFocus.current;
    const animated = wheelHydrated.current && pending?.animate === true;
    // 用户手还在轮盘上时不要对齐:那会把 target 拽回 path 说的位置。
    if (wheelInteracting.current === null) alignWheelToPath(animated);
    if (wheelHydrated.current && pending) revealDeepestColumn();
    wheelHydrated.current = true;
    pendingFocus.current = null;
  }, [path, sel, canGovern]); // eslint-disable-line
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
  }, [path, sel, canGovern]); // eslint-disable-line

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
  }, [embedded, net, canGovern]); // eslint-disable-line
  useEffect(() => {
    if (!embedded || canGovern) return;
    const canvas = canvasRef.current;
    const columns = colsRef.current;
    if (!canvas || !columns) return;
    let gesture: { pointerId: number; columnIndex: number; startX: number; startY: number; startTarget: number; startScrollLeft: number; mode: "pending" | "wheel" | "pan"; moved: boolean; captured: boolean; samples: { t: number; y: number }[] } | null = null;
    const columnAt = (clientX: number) => {
      const columnEls = [...columns.querySelectorAll<HTMLElement>(".graph-col")];
      const pointerColumn = columnEls.findIndex((column) => {
        const rect = column.getBoundingClientRect();
        return clientX >= rect.left && clientX <= rect.right;
      });
      return pointerColumn >= 1 ? pointerColumn : -1;
    };
    const onPointerDown = (event: PointerEvent) => {
      if ((event.target as HTMLElement).closest(".ag-dots,.ag-root,.ag-panel")) return;
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
      gesture = { pointerId: event.pointerId, columnIndex, startX: event.clientX, startY: event.clientY, startTarget: state.target, startScrollLeft: canvas.scrollLeft, mode: "pending", moved: false, captured: false, samples: [{ t: event.timeStamp, y: event.clientY }] };
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
      // 只留最近 100ms 的采样，松手时据此算速度（惯性）。
      gesture.samples.push({ t: event.timeStamp, y: event.clientY });
      while (gesture.samples.length > 1 && event.timeStamp - gesture.samples[0].t > 100) gesture.samples.shift();
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
          if (live) {
            const first = gesture.samples[0];
            const last = gesture.samples[gesture.samples.length - 1];
            const elapsed = last.t - first.t;
            // 像素 / ms → 节点 / ms（76px 一格，方向与拖动相反），再按弹簧常数外推。
            const velocity = elapsed > 8 ? -((last.y - first.y) / elapsed) / 76 : 0;
            setWheelTarget(gesture.columnIndex, Math.round(live.target + wheelFling(velocity)), true);
          }
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
    let pan = false, moved = false, sx = 0, sy = 0, sl = 0, st = 0;
    const down = (e: MouseEvent) => {
      if ((e.target as HTMLElement).closest(".ag-node,.ag-root,.ag-dots,.ag-panel,.ag-draft,.ag-droptop,button,a,input,textarea,select")) return;
      pan = true; moved = false; sx = e.clientX; sy = e.clientY; sl = c.scrollLeft; st = c.scrollTop; c.classList.add("ag-panning");
      e.preventDefault();
    };
    const move = (e: MouseEvent) => {
      if (!pan) return;
      if (!moved && Math.max(Math.abs(e.clientX - sx), Math.abs(e.clientY - sy)) > 4) { moved = true; suppressNodeClick.current = true; }
      c.scrollLeft = sl - (e.clientX - sx); c.scrollTop = st - (e.clientY - sy);
    };
    const up = () => {
      if (!pan) return;
      pan = false; c.classList.remove("ag-panning");
      // 拖过画布的那次松手不算「点空白」：click 紧跟在 mouseup 后面，等它过去再放开。
      if (moved) window.setTimeout(() => { suppressNodeClick.current = false; }, 120);
    };
    c.addEventListener("mousedown", down); window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
    return () => { c.removeEventListener("mousedown", down); window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
  }, [canGovern]);

  const node = nodes[sel];
  const drafts = net.drafts || [];
  /* 画布上的一次点击（没拖动过）：落在某个节点的矩形里就当点了那个节点——
     节点本体只有 20px 高，列空白处按矩形再判一次；哪个节点都没碰到就是点空白，收起面板。 */
  const handleCanvasClick = (event: React.MouseEvent<HTMLDivElement>) => {
    if (suppressNodeClick.current) return;
    if ((event.target as HTMLElement).closest(".ag-node,.ag-dots,.ag-root,.ag-panel,.ag-draft,.ag-droptop,button,a,input,textarea,select")) return;
    const columnEls = [...(colsRef.current?.querySelectorAll<HTMLElement>(".graph-col") ?? [])];
    for (let columnIndex = 1; columnIndex < columnEls.length; columnIndex++) {
      const items = [...columnEls[columnIndex].querySelectorAll<HTMLElement>(".ag-node")];
      const hit = items.find((item) => {
        const rect = item.getBoundingClientRect();
        return (
          rect.width > 0 && rect.height > 0 &&
          event.clientX >= rect.left &&
          event.clientX <= rect.right &&
          event.clientY >= rect.top &&
          event.clientY <= rect.bottom
        );
      });
      const id = hit?.dataset.id;
      if (id && nodes[id]) { activateNode(id, columnIndex); return; }
    }
    closePanel();
  };
  // Esc 收起面板。弹窗 / 菜单开着时 Esc 归它们（App 层也在听 Esc 关菜单），输入框里不管。
  useEffect(() => {
    if (!embedded || canGovern) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if (document.querySelector("[role='dialog'], [role='menu']")) return;
      closePanel(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [embedded, canGovern]); // eslint-disable-line

  const renderNode = (id: string, ci: number): ReactNode => {
    const n = nodes[id]; if (!n) return null;
    const kids = domainKids(id);
    const dead = n.killed || n.effective_killed;
    return (
      <div
        key={id}
        className={`ag-node ${id === sel ? "sel" : ""} ${path.includes(id) && id !== sel ? "path" : ""}`}
        data-id={id}
        role="button"
        tabIndex={0}
        aria-label={dname(n)}
        aria-pressed={id === sel}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activateNode(id, ci); return; }
          if (handleNodeArrow(event.key, id, ci)) event.preventDefault();
        }}
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
          e.dataTransfer.setData(GROUP_DRAG_LABEL_MIME, dname(n));
          setDrag(id);
        }}
        onDragEnd={() => setDrag(null)}
        {...dropProps(id)}
        onClick={(e) => {
          if (suppressNodeClick.current) return;
          if ((e.target as HTMLElement).closest(".ag-dots")) return;
          activateNode(id, ci);
        }}
        onDoubleClick={() => onGoProfile(id)}
        style={{ opacity: dead ? 0.5 : 1 }}
      >
        {/* 命中区：至少 32×32 的透明面，点它冒泡到节点自己的 onClick（第 6 件）。 */}
        <span className="ag-node-hit" aria-hidden />
        <span className="ag-nm">{dname(n)}</span>
        {dead && <span className="ag-bad">{t("graph.value.disabled")}</span>}
        {kids.length > 0 && <span className="ag-cn">▸{kids.length}</span>}
        {id === sel && <button type="button" className="ag-node-enter" title={t("graph.openProject")} aria-label={t("graph.openProject")}
          onClick={(event) => { event.stopPropagation(); onGoProfile(id); }}><CornerDownLeft size={16} aria-hidden="true" /></button>}
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
          className={embedded ? "agent-browser-body" : "flex"}
          style={embedded ? undefined : { height: "70vh" }}
        >
        <div className="ag-canvas" ref={canvasRef} onClick={handleCanvasClick}>
          <svg className="ag-edges" ref={svgRef} aria-hidden="true" />
          <div className="ag-cols" ref={colsRef}>
            {columns.map((ids, ci) => (
              <div
                className={`graph-col ${ci > 0 ? "ag-wheel-col" : ""}`}
                data-column={ci}
                key={`${ci}:${ci === 0 ? "__root__" : path[ci - 1]}`}
              >
                {ci === 0 ? (
                  <>
                    <div
                      className="ag-root"
                      data-id="default"
                      role="button"
                      tabIndex={0}
                      aria-label={displayName("default")}
                      aria-pressed={sel === "default"}
                      onClick={() => activateNode("default")}
                      onKeyDown={(event) => {
                        if (event.target !== event.currentTarget) return;
                        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activateNode("default"); return; }
                        if (handleNodeArrow(event.key, "default", 0)) event.preventDefault();
                      }}
                      {...dropProps("default")}
                    >
                      <div className="ag-rootline" />
                      <div className="ag-rootnm">{displayName("default")}</div>
                      <div className="ag-rootsub">{t("graph.rootSub")}</div>
                    </div>
                  </>
                ) : (
                  <>
                    {ids.map((id) => renderNode(id, ci))}
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

        {/* 项目摘要和稳定可见的主操作。收起时留在 DOM 里只做位移，inert 挡住焦点。 */}
        <aside
          className={`ag-panel ${panelOpen ? "" : "is-hidden"}`}
          ref={panelRef}
          data-open={panelOpen ? "true" : "false"}
          inert={!panelOpen}
          onTransitionEnd={(event) => { if (event.target === event.currentTarget) redraw(); }}
        >
          {node && (
            <>
              <div className="ag-panel-head">
                <div className="ag-ptag">
                  <span>{t("graph.head.title")}</span>
                  <button type="button" className="ag-panel-close" title={t("common.close")} aria-label={t("common.close")} onClick={() => closePanel()}>
                    <X size={14} aria-hidden="true" />
                  </button>
                </div>
                <h2 className="ag-ph2">{dname(node)}</h2>
                <div className="ag-prole">{sel === "default" ? t("graph.value.mainAgent") : t("graph.value.project")}{(node.killed || node.effective_killed) ? ` · ${t("graph.value.disabled")}` : ""}</div>
                <div className="ag-pactions">
                  <Button variant="primary" className="kaus-project-primary" onClick={() => (onNewConversation ?? onGoProfile)(sel)}>
                    <MessageSquare size={17} /><span>{t(onNewConversation ? "graph.newConversation" : "graph.openChat")}</span>
                  </Button>
                  <button type="button" className="kaus-button kaus-project-secondary kaus-project-enter" style={{ clipPath: `url("#${enterShapeId}")` }} title={t(onNewConversation ? "graph.openProject" : "graph.details")} onClick={() => onDetail(sel)}>
                    <svg className="kaus-project-enter-shape" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true" focusable="false">
                      <defs><clipPath id={enterShapeId} clipPathUnits="objectBoundingBox"><path d={PROJECT_ENTER_SHAPE} /></clipPath></defs>
                      <path d={PROJECT_ENTER_SHAPE} vectorEffect="non-scaling-stroke" />
                    </svg>
                    <CornerDownLeft className="kaus-project-enter-icon" size={22} aria-hidden="true" />
                    <span className="kaus-project-enter-label">{t(onNewConversation ? "graph.openProject" : "graph.details")}</span>
                  </button>
                </div>
              </div>
              <div className="ag-panel-section">
                <h3>{t("graph.section.identity")}</h3>
                <div className="ag-prow"><span>{t("graph.row.type")}</span><span>{sel === "default" ? t("graph.value.apexRoot") : t("graph.value.project")}</span></div>
                <div className="ag-prow"><span>{t("graph.row.parent")}</span><span>{node.parent ? displayName(node.parent) : t("graph.value.topLevel")}</span></div>
              </div>
              <div className="ag-panel-section">
                <h3>{t("graph.section.structure")}</h3>
                <div className="ag-stat-row">
                  <div><strong>{domainKids(sel).length}</strong><span>{t("graph.stat.children")}</span></div>
                </div>
              </div>
              <div className="ag-panel-section">
                <h3>{t("graph.section.inheritance")}</h3>
                <div className="ag-prow"><span>{t("graph.row.parentConfig")}</span><span>{node.parent ? t("graph.value.fromParent", { name: displayName(node.parent) }) : t("graph.value.localBaseline")}</span></div>
              </div>
            </>
          )}
        </aside>
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
        {canGovern ? <>
          <ProjectSkillTree focusProject={sel} refreshKey={net} onFocusProject={slug => focusNode(slug, undefined, false)} onOpenProject={onDetail}
            onProjectMenu={onOpenMenu} onAddProject={onAddChild} onReparentProject={askReparent} />
          {reparent && <ReparentModal node={reparent.node} target={reparent.target} onClose={() => setReparent(null)} onDone={() => { setReparent(null); onChanged(); }} />}
        </> : graph}
      </section>
    );
  }

  return (
    <Overlay title={t("graph.overlay.title")} onClose={onClose}>
      {graph}
    </Overlay>
  );
}
