/* 输入区工具栏（DESIGN ★ 定稿 I + H-4）。
 *
 * 一行，左起：工作目录 · 引擎 · 模型 ▾ · 推理强度 ▾ · 审批模式 ▾ ｜ 📎 · 发送。
 * 会话页与草稿页共用这一条（草稿页把项目下拉塞进 `leading`）。
 *
 * 三条硬规则：
 *  ① **全中性**（H-4）：这一条上没有任何强调色，可点的只多一个 `▾`。
 *  ② **不用原生 `<select>`**：Pill + 浮层菜单，形态与站内其它菜单一致。
 *  ③ **缺的不渲染**（AD-71）：`source === "none"`（⇒ `value === null`）的项整枚不出现，
 *     `levels` 为空时没有推理强度，附件能力缺失时没有 📎——都不留占位、不写备注。
 *
 * 取值一律来自 `GET /api/bindings/{id}/effective-settings`；`source !== "binding"`
 * 时悬停提示「来自引擎配置」（`catalog` 用「目录默认」）——提示是 `title`，
 * 不是另一枚 chip，工具栏不因此变长。
 */

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, Folder, Paperclip } from "lucide-react";
import "../styles/conversation-polish.css";

import { useLocale, type DictKey } from "../i18n";
import type { EffectiveSettingWire, SettingSource } from "../lib/sessionApi";

/** 来源提示：本绑定自己的值不提示；其余按来源给一句话。 */
export function sourceHint(source: SettingSource, t: (key: DictKey) => string): string | undefined {
  if (source === "binding" || source === "none") return undefined;
  return source === "catalog" ? t("bar.source.catalog") : t("bar.source.engine");
}

/** 审批模式取值 → AD-106 的文案。引擎给了别的值就原样显示，不猜语义。 */
export function approvalLabel(value: string, t: (key: DictKey) => string): string {
  if (value === "bypass") return t("bar.approval.bypass");
  if (value === "read_only") return t("bar.approval.readOnly");
  if (value === "plan") return t("bar.approval.plan");
  if (value === "ask" || value === "auto" || value === "deny") {
    return t(`bar.approval.${value}` as DictKey);
  }
  return value;
}

/** 工作目录只显示末两级（DESIGN ★ I）：`/a/b/c/d` → `c/d`。 */
export function tailSegments(path: string, count = 2): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= count) return path;
  return parts.slice(-count).join("/");
}

/* ------------------------------------------------------------------ *
 * 基本件：中性 Pill / Pill + 菜单 / 工作目录
 * ------------------------------------------------------------------ */

/** 只读的一枚（引擎名、以及不可改时的取值）。 */
export function BarPill({ label, title, children }: { label: string; title?: string; children: ReactNode }) {
  return (
    <span className="kaus-bar-pill" title={title ? `${label} · ${title}` : label} data-bar-item={label}>
      {children}
    </span>
  );
}

export interface BarMenuOption {
  value: string;
  label: string;
  /** 层级缩进（草稿页的项目菜单，批次十六第 5 件）：只影响左内边距，不换形态。 */
  depth?: number;
  /** 分组头的文字（batch29 / DESIGN ★I-3）。**只有两个以上不同的组才渲染组头**：
   *  一个组的分组是噪音。缺这个键的项归到「没有组名」那一档，不出头。 */
  group?: string;
  /** batch30 / ★I-3：这一条属于「当前那家 provider」。它的组默认展开，别的组
   *  折成一行组头 + 条数。 */
  groupCurrent?: boolean;
}

/** 默认展开哪个组（batch30 / ★I-3）。
 *
 *  真机 P2：47 个模型即使分了组，平铺出来仍旧是一条要滚很久的长清单。所以默认
 *  只把**引擎此刻在用的那家**摊开，其余每组折成一行「组名 · N 个模型」。
 *
 *  判据按优先级：① 后端说的 `groupCurrent`；② 当前选中项所在的组（后端没给那个
 *  键时的兜底——用户至少能看见自己现在用的那一档）；③ 第一个有名字的组。
 *  一个组都分不出来时返回 null（那时压根没有组头，也就无所谓折叠）。 */
export function defaultExpandedGroup(
  options: BarMenuOption[],
  value: string | null,
): string | null {
  const named = options.filter((option) => option.group);
  if (named.length === 0) return null;
  const current = named.find((option) => option.groupCurrent);
  if (current) return current.group ?? null;
  const selected = named.find((option) => option.value === value);
  if (selected) return selected.group ?? null;
  return named[0].group ?? null;
}

/** 把一列选项切成 `[组名, 该组的项]`，**顺序按选项自己的先后**（调用方已排好）。
 *
 * 导出是为了直接受测：分组顺序在真机上就是用户第二次打开下拉时"东西还在不在
 * 原地"这件事，值得有一条独立用例守着。同一个组名在列表里断开出现时**合并**回
 * 第一次出现的位置——组头出现两次会让人以为那是两个 provider。 */
export function groupBarMenuOptions(options: BarMenuOption[]): [string, BarMenuOption[]][] {
  const groups: [string, BarMenuOption[]][] = [];
  const index = new Map<string, BarMenuOption[]>();
  for (const option of options) {
    const key = option.group ?? "";
    let bucket = index.get(key);
    if (!bucket) {
      bucket = [];
      index.set(key, bucket);
      groups.push([key, bucket]);
    }
    bucket.push(option);
  }
  return groups;
}

/** 项数超过这个数就在菜单顶部加一个过滤框（批次十六第 5 件 / F11）。 */
export const BAR_MENU_FILTER_THRESHOLD = 8;

/** Pill + 浮层菜单（规则②：不用原生 `<select>`）。
 *
 * `footnote` 是菜单底部那行小字（目录 degraded / 只有一条时的 `diagnostics[0]`）。
 *
 * 批次十五第 5 件：**有目录就有 `▾`，一条也点得开**。原来"只有一条就退化成只读
 * Pill"看着像坏了——用户不知道是"没得选"还是"切不动了"。现在一条也列出来（选中态），
 * 一条都没有时菜单里给 `emptyNote`（「引擎未报告可用模型」）。`degraded` 时 Pill
 * 右侧一个**中性**小点（H：工作区不上强调色），悬停由 `title` 解释。 */
export function BarMenu({
  label,
  value,
  display,
  options,
  title,
  footnote,
  emptyNote,
  degraded = false,
  floating = false,
  onSelect,
}: {
  label: string;
  value: string | null;
  display: string;
  options: BarMenuOption[];
  title?: string;
  footnote?: string | null;
  /** 一条选项都没有时菜单里的那句话。不给就渲染一个空菜单（不会发生：调用方都给）。 */
  emptyNote?: string | null;
  degraded?: boolean;
  floating?: boolean;
  onSelect: (value: string) => void;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  /* 第 5 件：项数多时顶部一个过滤框。查询只活在"这次打开"里——关掉就清空，
     下次打开是整份清单，不会出现"上次筛过、这次看着像少了几条"。 */
  const [query, setQuery] = useState("");
  /* batch30 / ★I-3：手动展开过哪些组。null = 还没动过，用默认那一档。
     **什么都不持久化**：关掉菜单就忘，下次打开又是"当前那家展开、别家折着"。 */
  const [expandedGroups, setExpandedGroups] = useState<string[] | null>(null);
  const ref = useRef<HTMLSpanElement | null>(null);
  const menuRef = useRef<HTMLSpanElement | null>(null);
  const [floatingStyle, setFloatingStyle] = useState<CSSProperties>({ visibility: "hidden" });
  useLayoutEffect(() => {
    if (!open || !floating) return;
    const place = () => {
      const rect = ref.current?.getBoundingClientRect();
      if (!rect) return;
      const below = window.innerHeight - rect.bottom - 14;
      const above = rect.top - 14;
      const downward = below >= Math.min(260, above);
      const width = Math.min(Math.max(rect.width, 200), window.innerWidth - 16);
      setFloatingStyle({
        position: "fixed", zIndex: 1000, bottom: downward ? "auto" : window.innerHeight - rect.top + 6,
        top: downward ? rect.bottom + 6 : "auto", left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
        width, minWidth: Math.min(160, width), maxHeight: Math.max(60, Math.min(260, downward ? below : above)),
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, floating]);
  const showFilter = options.length > BAR_MENU_FILTER_THRESHOLD;
  /* batch29（真机 D4 / ★I-3）：**两个以上不同的组才出组头**。判据看的是整份
     `options` 而不是筛过的 `shown`——筛剩一个组时不该把组头也一起去掉，那样
     用户会以为自己筛掉的是"另一个东西"。一个 provider 的引擎照旧一列到底。 */
  const showGroups = new Set(options.map((option) => option.group ?? "")).size > 1;
  const needle = query.trim().toLowerCase();
  const shown = needle
    ? options.filter(
        (option) =>
          option.label.toLowerCase().includes(needle) || option.value.toLowerCase().includes(needle),
      )
    : options;
  /* batch30（真机 P2 / ★I-3）：47 个模型即使分了组，摊开仍旧长得要滚很久。默认
     只展开**引擎此刻在用的那家**，其余每组折成一行「组名 · N 个模型」，点组头
     展开。**正在过滤时全部展开**：这时用户要的是"哪儿有命中"，折叠只会把答案
     藏起来（筛空的组连组头一起不渲染，那一条没变）。 */
  const fallbackExpanded = defaultExpandedGroup(options, value);
  const expandedSet = new Set(expandedGroups ?? (fallbackExpanded ? [fallbackExpanded] : []));
  const filtering = needle !== "";
  const toggleGroup = (name: string) =>
    setExpandedGroups((current) => {
      const base = current ?? (fallbackExpanded ? [fallbackExpanded] : []);
      return base.includes(name) ? base.filter((item) => item !== name) : [...base, name];
    });

  /* 点外面 / Esc 关掉。菜单是浮层，不该拦住页面上别的点击。 */
  useEffect(() => {
    if (!open) {
      setQuery("");
      // batch30：折叠状态也不跨"这次打开"（★I-3：不持久化任何东西）。
      setExpandedGroups(null);
      return;
    }
    const onDocDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node) && !menuRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      // Esc 先清过滤词（还在筛的时候不该一下子把菜单关掉），再按一次才关菜单。
      if (event.key !== "Escape") return;
      if (query !== "") setQuery("");
      else setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, query]);

  const menuLayer = (content: ReactNode) => floating ? createPortal(content, document.body) : content;
  return (
    <span className="kaus-bar-menu" ref={ref} data-bar-item={label}>
      <button
        type="button"
        className="kaus-bar-pill is-button"
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="listbox"
        title={title ? `${label} · ${title}` : label}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="kaus-bar-value" title={display}>{display}</span>
        {/* 降级的小点：中性色，解释挂在整枚 Pill 的 title 上（工具栏不因此变长）。 */}
        {degraded && <span className="kaus-bar-dot" data-testid="bar-degraded" aria-hidden />}
        <ChevronDown className="kaus-bar-caret" size={12} aria-hidden />
      </button>
      {open && menuLayer(
        <span className="kaus-bar-pop" ref={menuRef} style={floating ? floatingStyle : undefined} role="listbox" aria-label={label}>
          {/* 第 5 件：清单长了才给过滤框（短清单加一个输入框只会碍事）。 */}
          {showFilter && (
            <input
              autoFocus
              className="kaus-bar-filter"
              data-testid="bar-filter"
              aria-label={`${label} · ${t("bar.filter.placeholder")}`}
              placeholder={t("bar.filter.placeholder")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          )}
          {options.length === 0 && emptyNote && (
            <span className="kaus-bar-pop-note" data-testid="menu-empty">
              {emptyNote}
            </span>
          )}
          {options.length > 0 && shown.length === 0 && (
            <span className="kaus-bar-pop-note" data-testid="menu-filter-empty">
              {t("bar.filter.empty")}
            </span>
          )}
          {groupBarMenuOptions(shown).map(([name, items]) => {
            const collapsible = showGroups && Boolean(name);
            const groupOpen = !collapsible || filtering || expandedSet.has(name);
            return (
            <span key={name || " "} className="kaus-bar-pop-group">
              {/* 组头：小号大写中性小字（★H 不上强调色）。只有真的分了组、且这个组
                  有名字时才出——筛空的组连头一起不渲染（AD-71：空组的标题是一句
                  没有内容的话）。batch30 起它是个按钮：点一下展开 / 收起这一组。 */}
              {collapsible && (
                <button
                  type="button"
                  className="kaus-bar-pop-head is-button"
                  data-testid="menu-group"
                  aria-expanded={groupOpen}
                  aria-label={t(groupOpen ? "bar.group.collapse" : "bar.group.expand", { name })}
                  onClick={() => toggleGroup(name)}
                >
                  {name}
                  {/* 折着的时候把条数说出来：不然用户不知道这一行后面藏了多少东西。 */}
                  {!groupOpen && (
                    <span className="kaus-bar-pop-count">
                      {items.length}
                    </span>
                  )}
                </button>
              )}
              {groupOpen &&
                items.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={option.value === value}
                  className={`kaus-bar-pop-item ${option.value === value ? "is-current" : ""}`}
                  /* 层级缩进：项目树在菜单里也看得出父子关系（第 5 件）。 */
                  style={option.depth ? { paddingLeft: 8 + option.depth * 12 } : undefined}
                  onClick={() => {
                    setOpen(false);
                    if (option.value !== value) onSelect(option.value);
                  }}
                >
                  {option.label}
                </button>
              ))}
            </span>
            );
          })}
          {/* 目录降级时的一行小字：说明这份列表是怎么来的，不阻断选择。 */}
          {footnote && (
            <span className="kaus-bar-pop-note" data-testid="model-diagnostic">
              {footnote}
            </span>
          )}
        </span>
      )}
    </span>
  );
}

/** 工作目录：平时一枚 Pill（末两级），点开换成一个小输入框，回车保存。 */
export function WorkspacePill({
  setting,
  onSave,
}: {
  setting: EffectiveSettingWire;
  onSave?: (path: string) => Promise<void>;
}) {
  const { t } = useLocale();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(setting.value ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const label = t("bar.workspace");

  useEffect(() => {
    setDraft(setting.value ?? "");
  }, [setting.value]);

  if ((setting.source === "none" || setting.value === null) && !onSave) return null;

  const commit = async () => {
    const next = draft.trim();
    if (!onSave || next === "" || next === setting.value) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSave(next);
      setEditing(false);
    } catch (failure) {
      // 保留用户输入：路径打错时不能把他刚敲的那串抹掉。
      setError(String((failure as Error)?.message ?? failure));
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <span className="kaus-bar-menu" data-bar-item={label}>
        <input
          autoFocus
          aria-label={label}
          className="kaus-bar-input"
          value={draft}
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Enter") {
              event.preventDefault();
              void commit();
            }
            if (event.key === "Escape") {
              setEditing(false);
              setDraft(setting.value ?? "");
              setError(null);
            }
          }}
        />
        {error && (
          <span className="kaus-bar-pop-note is-error" role="alert">
            {error}
          </span>
        )}
      </span>
    );
  }

  const hint = sourceHint(setting.source, t);
  const title = [setting.value, hint].filter(Boolean).join(" · ");
  if (!onSave) {
    return (
      <BarPill label={label} title={title}>
        <Folder size={13} aria-hidden />
        {setting.value ? tailSegments(setting.value) : t("bar.workspace.choose")}
      </BarPill>
    );
  }
  return (
    <button
      type="button"
      className="kaus-bar-pill is-button"
      aria-label={label}
      title={`${label} · ${title}`}
      data-bar-item={label}
      onClick={() => setEditing(true)}
    >
      <Folder size={13} aria-hidden />
      {setting.value ? tailSegments(setting.value) : t("bar.workspace.choose")}
    </button>
  );
}

/* ------------------------------------------------------------------ *
 * 整条工具栏
 * ------------------------------------------------------------------ */

export interface ComposerBarProps {
  /** These values are project defaults, not proof of the current runtime policy. */
  settingsAreDefaults?: boolean;
  /** 草稿页把项目下拉塞在最左（会话页不传）。 */
  leading?: ReactNode;
  engineName?: string | null;
  workspaceRoot: EffectiveSettingWire;
  onChangeWorkspaceRoot?: (path: string) => Promise<void>;
  model: EffectiveSettingWire;
  /** 后端已过滤过的可用模型（`GET /api/backends/{id}/models`）。 */
  modelOptions: BarMenuOption[];
  /** 目录的第一条 `diagnostics`：降级时、或只有一条模型时垫在菜单底部（第 5 件）。 */
  modelDiagnostic?: string | null;
  /** 目录是降级取来的：Pill 右侧一个中性小点 + 悬停解释。 */
  modelDegraded?: boolean;
  onChangeModel?: (modelId: string) => void;
  reasoning: EffectiveSettingWire & { levels: string[] };
  onChangeReasoning?: (level: string) => void;
  approval: EffectiveSettingWire & { options: string[] };
  onChangeApproval?: (mode: string) => void;
  executionMode?: string | null;
  executionOptions?: BarMenuOption[];
  onChangeExecutionMode?: (mode: string) => void;
  /** 引擎声明的 `card.attachments`。缺 / `none` / `unknown` ⇒ 不渲染 📎（AD-71）。 */
  attachments?: string | null;
  onAttach?: () => void;
  /** 右侧的发送 / 停止按钮，由页面给。 */
  actions?: ReactNode;
}

export function ComposerBar({
  settingsAreDefaults = false,
  leading,
  engineName,
  workspaceRoot,
  onChangeWorkspaceRoot,
  model,
  modelOptions,
  modelDiagnostic = null,
  modelDegraded = false,
  onChangeModel,
  reasoning,
  onChangeReasoning,
  approval,
  onChangeApproval,
  executionMode,
  executionOptions = [],
  onChangeExecutionMode,
  attachments = null,
  onAttach,
  actions,
}: ComposerBarProps) {
  const { t } = useLocale();
  const settingDisplay = (value: string) => value;
  const settingHint = (source: EffectiveSettingWire["source"]) => settingsAreDefaults ? t("bar.defaults.hint") : sourceHint(source, t);
  const modelLabel =
    modelOptions.find((option) => option.value === model.value)?.label ?? model.value ?? "";
  const showAttachments = attachments !== null && attachments !== undefined && attachments !== "none" && attachments !== "unknown";

  return (
    <div className="kaus-composer-bar" data-testid="composer-bar">
      {leading}
      <WorkspacePill setting={workspaceRoot} onSave={onChangeWorkspaceRoot} />
      {engineName && <BarPill label={t("bar.engine")}>{engineName}</BarPill>}

      {/* 第 5 件：**能写就有 `▾`**。目录只有一条、甚至一条都没有，菜单照旧点得开——
          "为什么切不了模型"这句话由菜单里的 diagnostics 回答，不是让 Pill 变哑。
          压根没有取值（`source === "none"`）时仍旧整枚不渲染（AD-71）。 */}
      {model.source !== "none" && model.value !== null && (
        onChangeModel ? (
          <BarMenu
            label={t("bar.model")}
            value={model.value}
            display={modelLabel || t("bar.model.none")}
            options={modelOptions}
            title={[sourceHint(model.source, t), modelDegraded ? modelDiagnostic : null]
              .filter(Boolean)
              .join(" · ") || undefined}
            /* 一条时也要说清楚"就这一条"的原因（有 diagnostics 才说）。 */
            footnote={modelDegraded || modelOptions.length <= 1 ? modelDiagnostic : null}
            emptyNote={t("bar.model.empty")}
            degraded={modelDegraded}
            onSelect={onChangeModel}
          />
        ) : (
          <BarPill label={t("bar.model")} title={sourceHint(model.source, t)}>
            {modelLabel}
          </BarPill>
        )
      )}

      {/* 推理强度只在当前模型真有档位时出现（DESIGN ★ I）。 */}
      {reasoning.levels.length > 0 && reasoning.source !== "none" && (
        onChangeReasoning ? (
          <BarMenu
            label={t("bar.reasoning")}
            value={reasoning.value}
            display={settingDisplay(reasoning.value ?? reasoning.levels[0])}
            options={reasoning.levels.map((level) => ({ value: level, label: level }))}
            title={settingHint(reasoning.source)}
            onSelect={onChangeReasoning}
          />
        ) : (
          <BarPill label={t("bar.reasoning")} title={settingHint(reasoning.source)}>
            {settingDisplay(reasoning.value || "")}
          </BarPill>
        )
      )}

      {approval.source !== "none" && approval.value !== null && (
        approval.options.length > 0 && onChangeApproval ? (
          <BarMenu
            label={t("bar.approval")}
            value={approval.value}
            display={settingDisplay(approvalLabel(approval.value, t))}
            options={approval.options.map((option) => ({ value: option, label: approvalLabel(option, t) }))}
            title={settingHint(approval.source)}
            onSelect={onChangeApproval}
          />
        ) : (
          <BarPill label={t("bar.approval")} title={settingHint(approval.source)}>
            {settingDisplay(approvalLabel(approval.value, t))}
          </BarPill>
        )
      )}

      {executionOptions.length > 0 && (onChangeExecutionMode ? <BarMenu
        label={t("bar.execution")}
        value={executionMode ?? null}
        display={executionOptions.find(option => option.value === executionMode)?.label ?? executionMode ?? t("bar.execution")}
        options={executionOptions}
        onSelect={onChangeExecutionMode}
      /> : <BarPill label={t("bar.execution")}>
        {executionOptions.find(option => option.value === executionMode)?.label ?? executionMode}
      </BarPill>)}
      <span className="kaus-bar-spacer" />
      {/* Attachment controls follow the engine's reported capability. */}
      {showAttachments && onAttach && (
        <button type="button" className="kaus-bar-pill is-button kaus-composer-attach" aria-label={t("bar.attach")} title={t("bar.attach")} onClick={onAttach}>
          <Paperclip className="size-3" />
        </button>
      )}
      {actions}
    </div>
  );
}
