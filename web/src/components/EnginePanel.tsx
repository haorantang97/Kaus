/* 「已接引擎」面板（项目详情页 ② 区，IA §3.2）。
 *
 * 一个项目的每条 Binding = 一张卡：字母标签 · 显示名 · 状态 chip（读 `probeState`）·
 * 模型与推理强度 · 引擎侧身份 · 原生会话数 · 能力清单 · 登录状态与「去登录」。
 *
 * 三条边界：
 *  1. **不按 backendId 分支**（component-boundaries 边界①）。字母标签由显示名机械推导，
 *     引擎差异一律通过能力值表达；表里查不到的新引擎不改代码也能显示。
 *  2. **这里是全站唯一显示能力 note 的地方**（AD-71）：能力清单读 `capabilities.detail`，
 *     带 `note` 与 `verification`。对话页那棵子树只读 `ui`，类型上就没有这两个字段。
 *  3. **不自己请求数据**（component-boundaries §6）：全部靠 props 传进来。数据来源：
 *     `GET /api/projects/{id}/bindings`、`GET /api/backends/{id}`、
 *     `GET /api/backends/{id}/models?binding=`。
 *
 * 写端点（批次八第 2 件起有了四条：`PATCH /bindings/{id}`、`make-default`、
 * `POST /projects/{id}/bindings`、`DELETE /bindings/{id}`）由**容器**接上并以回调传进来：
 * 回调没给（或该引擎没有模型目录）时那一处渲染成只读并挂一个「需后端补」标记——
 * 这是**工程状态**，不是能力备注，且只出现在本面板。
 *
 * batch19（AD-82/93）：登录态与原生会话数有 wire 了——`GET /api/bindings/{id}/status`，
 * 并且并入了 `GET /api/projects/{id}/bindings` 的每一行，所以面板照旧不自己请求数据。
 */

import { useEffect, useMemo, useState } from "react";
import { LogIn, Plug } from "lucide-react";

import { useLocale, type DictKey } from "../i18n";
import { approvalLabel, BarMenu, sourceHint } from "./ComposerBar";
import { hasCapability, type UiCapabilities } from "./cards/capabilities";
import type { BindingDriftWire, EffectiveSettingsWire } from "../lib/sessionApi";
import { Button, Card, FieldRow, Pill, cream } from "./ui";

/* ------------------------------------------------------------------ *
 * wire 形状（kernel/app/api/views.py 的 binding_to_wire / backend_to_wire）
 * ------------------------------------------------------------------ */

/** detail 层的一项能力：只有这一层带 note / verification（AD-73）。 */
export interface DetailCapabilityState {
  value: string;
  status: "supported" | "unsupported" | "unknown";
  /** batch15-backend 新增 `cached`：读的是缓存下来的探测结果，**算验证过**，不是未知。 */
  verification: "declared" | "bench" | "live" | "cached" | "unknown";
  note?: string;
  supportedBool: boolean;
}

/** detail 层是与 ui 层同构的树，叶子换成 DetailCapabilityState。 */
export type DetailCapabilityTree = {
  [key: string]: DetailCapabilityState | DetailCapabilityTree | string | Record<string, string>;
};

/** batch25/26：`acp` 行可能带的只读预设。**缺键 = 整块不渲染**（不是 null）。
 *  `quirks` 那七位布尔**不在这里**：它们是 Driver 的分支开关，不是给人看的
 *  能力清单（AD-71），界面上只显示 `label` 一句。 */
export interface BackendPresetWire {
  id: string;
  label: string;
  authModel?: string;
  supportsExternalCli?: boolean;
  envKeysHint?: string[];
  notes?: string[];
}

export interface BackendWire {
  id: string;
  key: string;
  displayName: string;
  driverKind: string;
  installed: boolean;
  version?: string | null;
  driverVersion?: string | null;
  probeState: "unknown" | "available" | "unavailable" | "degraded";
  /** 探测失败的人话；没有就不解释。 */
  probeMessage?: string | null;
  lastProbeAt?: string | null;
  capabilities: { ui: UiCapabilities; detail: DetailCapabilityTree; unknownCount: number };
  unknownCount: number;
  /** batch26：这条 acp 行是按哪个预设装配的。缺席 ⇒ 卡上没有这枚小标。 */
  preset?: BackendPresetWire;
}

export interface BindingWire {
  id: string;
  projectId: string;
  backendId: string;
  displayName: string;
  nativeScopeRef?: string | null;
  enabled: boolean;
  isDefault: boolean;
  defaultModelId?: string | null;
  defaultProviderId?: string | null;
  /** opaque dict；后端老版本没这个键时按空处理。 */
  runtimeConfig?: Record<string, unknown>;
  /** ready | partial | blocked | unknown（wire 上是字符串，新值不该让前端崩）。 */
  compatibilityState: string;
  discriminator?: string | null;
}

/** `GET /api/projects/{id}/effective-capabilities?backend=` 的一行（views.py
 *  `effective_capabilities_to_wire`）。这里只声明引擎配置区要用的字段。 */
export interface EffectiveCapabilityRow {
  capabilityType: string;
  capabilityId: string;
  config: Record<string, unknown>;
  sourceProjectId: string;
  inherited: boolean;
}

export interface BlockedCapabilityRow {
  capabilityType: string;
  capabilityId: string;
  blockedByProjectId: string;
}

export interface EffectiveCapabilitiesView {
  entries: EffectiveCapabilityRow[];
  blocked: BlockedCapabilityRow[];
}

/** 登录状态（AD-82/93，batch19 起有 wire）。登录态不跨引擎共享，面板只显示状态
 *  并拉起各自的登录流程。`account` 是**脱敏摘要**，明文凭据不上 wire。 */
export interface EngineAuthState {
  state: "signed_in" | "signed_out" | "unknown";
  /** 这个引擎怎么认账号（AD-93 的 `auth_model`）：显示成一句人话跟在状态后面。 */
  model?: "managed-credential" | "own-auth" | "session-scoped" | string;
  provider?: string | null;
  account?: string | null;
  checkedAt?: string;
  /** 未登录时的下一步（后端给的人话）。这是**状态**不是能力备注，不违反 AD-71。 */
  hint?: string | null;
}

/** 模型目录里的一项（`GET /api/backends/{id}/models?binding=`，只取显示要用的字段）。 */
export interface EngineModelOption {
  modelId: string;
  displayName?: string | null;
  reasoningLevels: string[];
}

export interface EngineModelCatalog {
  models: EngineModelOption[];
  defaultModelId?: string | null;
  supportsReasoning?: boolean;
  /** 目录是降级取来的（探测失败 / 兜底表）。Pill 上一个中性小点。 */
  degraded?: boolean;
  /** 降级或"就这一条"的原因，人话。菜单底部只显示第一条（DESIGN ★ I）。 */
  diagnostics?: string[];
}

export interface EngineRow {
  binding: BindingWire;
  backend: BackendWire;
  /** 该 Binding 的模型目录；拿不到（501 / 空目录）就是 null → 模型一行只读。 */
  catalog?: EngineModelCatalog | null;
  /** 该 Binding 下的原生会话数（batch19：来自 bindings 行 / `/status`）；
   *  拿不到就是 null / 缺席 → 那一行不渲染。 */
  nativeSessionCount?: number | null;
  /** 该 Binding 的**有效设置**（批次十三 `GET /bindings/{id}/effective-settings`）：
   *  模型 / 推理强度 / 审批模式的取值与来源。`null` = 读失败。 */
  effectiveSettings?: EffectiveSettingsWire | null;
  auth?: EngineAuthState;
  /** 该 Binding 所属引擎在本项目上的**有效**能力（AD-97 的引擎配置区）。
   *  `undefined` = 还在读，`null` = 读失败。容器取数，面板只渲染。 */
  capabilities?: EffectiveCapabilitiesView | null;
  /** batch25：`GET /api/bindings/{id}/drift` 的结果。**它同时是入口门控**
   *  （AD-126：能力入口只看入口）——读到了 = 这台引擎有投射面，「应用到引擎」
   *  与「配置漂移」那一行才渲染；`undefined`（还在读）/ `null`（
   *  `projection_unsupported`、或这次没读着）一律两处都不显示（AD-71）。 */
  drift?: BindingDriftWire | null;
  /** batch26：`GET /api/bindings/{id}/projection/_meta` 的 `supported`。**它才是
   *  入口门控**（batch25 拿 drift 成败当门控，那是"这次读着没"，不是"支不支持"）：
   *  `true` 才渲染「应用到引擎」；`false`（`projection_unsupported` /
   *  `driver_not_registered`）与 `undefined`（还没问到 / 问失败）一律两处都不显示。 */
  projectionSupported?: boolean;
}

/** 一个可接入的引擎（`GET /api/backends`），只要显示用的两项。 */
export interface AttachableBackend {
  id: string;
  displayName: string;
}

export interface EngineWriteActions {
  /** `PATCH /bindings/{id}` 的 defaultModelId。返回的 Promise **必须**在失败时 reject：
   *  字段旁的错误提示与"保留用户输入"靠它（批次十一第 2 件）。 */
  onChangeModel?: (row: EngineRow, modelId: string) => void | Promise<void>;
  /** `PATCH /bindings/{id}` 的 runtimeConfig.reasoning_effort。同上。 */
  onChangeReasoning?: (row: EngineRow, level: string) => void | Promise<void>;
  /** `PATCH /bindings/{id}` 的 runtimeConfig.approval_mode（批次十三）。同上。 */
  onChangeApproval?: (row: EngineRow, mode: string) => void | Promise<void>;
  /** `POST /bindings/{id}/make-default`。 */
  onMakeDefault?: (row: EngineRow) => void;
  /** `DELETE /bindings/{id}`（默认挂载与有活跃 Runtime 的会被后端挡成 409）。 */
  onDetach?: (row: EngineRow) => void;
  /** AD-82 占位：拉起该引擎自己的登录流程（写端点缺时不传，按钮不渲染）。 */
  onSignIn?: (row: EngineRow) => void;
  /** batch25：打开「应用到引擎」弹窗（dry-run 由弹窗自己发，面板不请求数据）。 */
  onMaterialize?: (row: EngineRow) => void;
  /** batch25：打开同一个弹窗的只读模式（配置漂移）。 */
  onViewDrift?: (row: EngineRow) => void;
  onNewConversation?: (row: EngineRow) => void;
}

/* ------------------------------------------------------------------ *
 * 展示助手
 * ------------------------------------------------------------------ */

/** 字母标签（DESIGN §2.4）：从显示名机械推导，不查任何引擎名表。 */
export function engineInitials(displayName: string): string {
  const words = displayName.trim().split(/[\s\-_]+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 1).toUpperCase();
  return words
    .slice(0, 2)
    .map((word) => word.slice(0, 1).toUpperCase())
    .join("");
}

const PROBE_TONE: Record<BackendWire["probeState"], "success" | "gold" | "danger" | "muted"> = {
  available: "success",
  degraded: "gold",
  unavailable: "danger",
  unknown: "muted",
};

/** runtime_config 是 opaque dict（公共层不解释内容），只做**通用**键名查找。 */
function pick(config: Record<string, unknown>, candidates: string[]): string | null {
  for (const [key, value] of Object.entries(config)) {
    const normalized = key.toLowerCase().replace(/[^a-z]/g, "");
    if (candidates.includes(normalized) && value !== null && value !== undefined && value !== "") {
      return typeof value === "string" ? value : JSON.stringify(value);
    }
  }
  return null;
}

/** 值不是本 Binding 自己设的时候，字段旁那句小字（「来自引擎配置」/「目录默认」）。
 *  它是**来源**不是能力备注，所以不违反 AD-71 的"不显示备注"。 */
function SourceNote({ text }: { text: string }) {
  return (
    <span className="ml-2 text-[0.65rem]" style={{ color: cream(45) }} data-testid="source-note">
      {text}
    </span>
  );
}

/** 漂移检查时间：能解析就按本地格式显示，解析不了原样回显（不编造时间）。
 *
 *  batch27：后端的 ISO 带**微秒**六位（`…:14.266661Z`），`Date` 只认到毫秒——多出来
 *  的位先削掉再解析，否则这一格在真机上直接把那串 ISO 摆给用户看。时区后缀原样留着。 */
export function formatCheckedAt(iso: string): string {
  if (!iso) return "";
  const at = new Date(iso.replace(/(\.\d{3})\d+/, "$1"));
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString();
}

/** 「这一项后端还没有写端点」。工程状态，不是能力备注，只出现在本面板。 */
function Pending() {
  const { t } = useLocale();
  return (
    <Pill tone="muted" title={t("common.pending.title")}>
      {t("common.pending")}
    </Pill>
  );
}

/* ------------------------------------------------------------------ *
 * 可改字段（批次十一第 2 件）
 * ------------------------------------------------------------------ */

/** 一个下拉 + 「保存 / 取消」。
 *
 * 走查里三条卡点，这里一次修完：
 *  ① 改完没回执 —— 保存成功由容器给一句 toast「已保存」；
 *  ② 「取消」只是关掉编辑态，值留在界面上骗人 —— 这里取消 = 回滚到 `serverValue`
 *     （最后一次**保存成功**的服务器值，容器重取后由 props 带下来）；
 *  ③ 保存失败一声不吭 —— 错误显示在字段旁，用户输入**不回滚**（重试比重填便宜）。
 *
 * 没改动时不显示按钮：静止状态与改造前逐像素一致。 */
function EditableSelect({
  ariaLabel,
  serverValue,
  options,
  footnote,
  emptyNote,
  degraded = false,
  onSave,
}: {
  ariaLabel: string;
  /** 上一次保存成功的服务器值。它一变（= 容器重取到了新值）就重置草稿。 */
  serverValue: string;
  options: { value: string; label: string }[];
  /** 菜单底部那行小字（目录降级 / 只有一条模型时的 `diagnostics[0]`）。 */
  footnote?: string | null;
  /** 一条选项都没有时菜单里那句话。 */
  emptyNote?: string | null;
  degraded?: boolean;
  onSave: (value: string) => void | Promise<void>;
}) {
  const { t } = useLocale();
  const [draft, setDraft] = useState(serverValue);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(serverValue);
    setError(null);
  }, [serverValue]);

  const dirty = draft !== serverValue;
  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft);
      setError(null);
    } catch (failure) {
      setError(String(failure instanceof Error ? failure.message : failure));
    } finally {
      setSaving(false);
    }
  };

  const display = options.find((option) => option.value === draft)?.label ?? draft;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {/* 批次十五第 4 件：原生 `<select>` 换成与输入区工具栏同一枚 Pill + 菜单
          （AD-120 说的"下拉为 Pill + 菜单，不用原生 select"，这里同样适用）。
          「保存 / 取消」的语义不动（批次十一第 2 件）：选了只改草稿，按保存才写。 */}
      <BarMenu
        label={ariaLabel}
        value={draft}
        display={display}
        options={options}
        footnote={footnote}
        emptyNote={emptyNote}
        degraded={degraded}
        onSelect={setDraft}
      />
      {dirty && (
        <>
          <Button variant="primary" disabled={saving} onClick={() => void save()}>
            {t("engine.field.save")}
          </Button>
          <Button disabled={saving} onClick={() => { setDraft(serverValue); setError(null); }}>
            {t("engine.field.cancel")}
          </Button>
        </>
      )}
      {error && (
        <span className="text-[0.65rem]" style={{ color: "var(--danger)" }}>
          {t("engine.field.saveFailed", { error })}
        </span>
      )}
    </span>
  );
}

/* ------------------------------------------------------------------ *
 * 引擎配置（AD-97）
 * ------------------------------------------------------------------ */

/** backend-scoped 的能力类型长成 `<引擎 key>:<名字>`；通用能力（mcp / memory /
 *  hooks / delegation）没有冒号。判据只看**形状**，不看是哪个引擎——这正是
 *  组件边界①要的：新引擎接进来不改这一行。 */
export function isBackendScoped(capabilityType: string): boolean {
  return capabilityType.includes(":");
}

/** 分组名的查表键：先按 `capability_id`，再退到能力类型的后缀，最后用原键名。
 *  （`<引擎 key>:delegation-extras` 的 id 是 `delegation`，靠后缀才分得出来。） */
export function groupNameKeys(capabilityType: string, capabilityId: string): [string, string] {
  const suffix = isBackendScoped(capabilityType)
    ? capabilityType.slice(capabilityType.indexOf(":") + 1)
    : capabilityType;
  return [capabilityId, suffix];
}

/** 值的**形状**（不解释内容：各引擎的 config 长什么样公共层不知道）。 */
export type ConfigShape =
  | { kind: "empty" }
  | { kind: "scalar"; text: string }
  | { kind: "list"; count: number }
  | { kind: "object"; keys: string[] };

/** 导入侧把每个配置键包成 `{"value": 原值}`（capability_import.capability_rows_for_key）；
 *  老行或别的形状就把整个 config 当值看。凭据在库里已经是 `credential-ref://…`
 *  占位串（R-06 脱敏），所以标量分支原样显示即可，**不展开对象**。 */
export function describeConfig(config: Record<string, unknown>): ConfigShape {
  const raw = config && "value" in config ? config.value : config;
  if (raw === null || raw === undefined || raw === "") return { kind: "empty" };
  if (Array.isArray(raw)) return { kind: "list", count: raw.length };
  if (typeof raw === "object") {
    const keys = Object.keys(raw as Record<string, unknown>);
    return keys.length === 0 ? { kind: "empty" } : { kind: "object", keys };
  }
  return { kind: "scalar", text: String(raw) };
}

/** 键名列表默认只露这么多，其余折在「展开」后面。 */
const KEYS_PREVIEW = 6;

function ConfigValue({ shape }: { shape: ConfigShape }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  if (shape.kind === "empty") return <span style={{ color: cream(38) }}>{t("common.dash")}</span>;
  if (shape.kind === "scalar") {
    return (
      <span style={{ font: "0.7rem var(--theme-font-mono, monospace)", color: cream(62) }}>
        {shape.text}
      </span>
    );
  }
  if (shape.kind === "list") {
    return <span style={{ color: cream(62) }}>{t("engine.config.items", { count: shape.count })}</span>;
  }
  const shown = open ? shape.keys : shape.keys.slice(0, KEYS_PREVIEW);
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
      <span style={{ color: cream(45) }}>{t("engine.config.keys", { count: shape.keys.length })}</span>
      <span style={{ font: "0.7rem var(--theme-font-mono, monospace)", color: cream(62) }}>
        {shown.join(" · ")}
      </span>
      {shape.keys.length > KEYS_PREVIEW && (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="text-[0.6rem] underline-offset-2 hover:underline"
          style={{ color: cream(45) }}
        >
          {open ? t("common.collapse") : t("common.expand")}
        </button>
      )}
    </span>
  );
}

/** 一行 = 一组能力：组名 · 来源 · 值摘要。 */
function ConfigRow({
  name,
  source,
  danger = false,
  shape,
}: {
  name: string;
  source: string;
  danger?: boolean;
  shape: ConfigShape;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1 text-xs">
      <span className="w-28 shrink-0" style={{ color: "var(--midground-base)" }}>
        {name}
      </span>
      <span className="shrink-0 text-[0.65rem]" style={{ color: danger ? "var(--danger)" : "var(--accent-text)" }}>
        {source}
      </span>
      <span className="min-w-0 flex-1">
        <ConfigValue shape={shape} />
      </span>
    </div>
  );
}

/** 引擎卡里的「引擎配置」折叠区。
 *
 * 只列 **backend-scoped** 的行；通用能力（mcp / memory / hooks / delegation）留在
 * 项目页的「能力」表，不在这里重复一遍。**只读**——写端点还没有，标记挂在区头上
 * （工程状态，不是能力备注）。 */
export function EngineConfigSection({
  capabilities,
  projectLabel,
}: {
  capabilities?: EffectiveCapabilitiesView | null;
  /** Project id → 显示名。默认原样回显 id。 */
  projectLabel?: (projectId: string) => string;
}) {
  const { t, tDynamic } = useLocale();
  const [open, setOpen] = useState(false);
  const label = projectLabel ?? ((projectId: string) => projectId);

  const rows = useMemo(() => {
    const name = (type: string, id: string) => {
      const [byId, bySuffix] = groupNameKeys(type, id);
      return tDynamic(`capGroup.${byId}`, tDynamic(`capGroup.${bySuffix}`, byId));
    };
    const entries = (capabilities?.entries ?? [])
      .filter((entry) => isBackendScoped(entry.capabilityType))
      .map((entry) => ({
        key: `${entry.capabilityType}/${entry.capabilityId}`,
        name: name(entry.capabilityType, entry.capabilityId),
        source: entry.inherited
          ? t("capabilities.source.inherited", { name: label(entry.sourceProjectId) })
          : t("capabilities.source.local"),
        danger: false,
        shape: describeConfig(entry.config),
      }));
    const blocked = (capabilities?.blocked ?? [])
      .filter((entry) => isBackendScoped(entry.capabilityType))
      .map((entry) => ({
        key: `blocked:${entry.capabilityType}/${entry.capabilityId}`,
        name: name(entry.capabilityType, entry.capabilityId),
        source: t("capabilities.source.blocked", { name: label(entry.blockedByProjectId) }),
        danger: true,
        shape: { kind: "empty" } as ConfigShape,
      }));
    return [...entries, ...blocked];
  }, [capabilities, label, t, tDynamic]);

  /* 批次十五第 4 件：一组配置都没有 = 整段不渲染（AD-111 同样适用于此处）。
     还在读的时候（`undefined`）留住加载态，否则会闪一下。 */
  if (capabilities !== undefined && rows.length === 0) return null;

  return (
    <div className="pt-1" data-testid="engine-config-section">
      <div className="mb-1 flex items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="text-[0.6rem] uppercase tracking-[0.14em]"
          style={{ color: cream(45) }}
        >
          {t("engine.config.toggle", { count: rows.length })} {open ? "▾" : "▸"}
        </button>
        {/* 「需后端补」chip 随第 4 件去掉：写端点缺不缺是我们的工程状态，
            不该在用户脸上挂一整年（AD-111 的同一条精神）。 */}
      </div>
      {open &&
        (capabilities === undefined ? (
          <div className="py-1 text-xs" style={{ color: cream(45) }}>
            {t("engine.config.loading")}
          </div>
        ) : (
          <div>
            {rows.map((row) => (
              <ConfigRow
                key={row.key}
                name={row.name}
                source={row.source}
                danger={row.danger}
                shape={row.shape}
              />
            ))}
          </div>
        ))}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 能力清单（唯一允许显示 note 的地方）
 * ------------------------------------------------------------------ */

/** 与内核 FEATURE_PATHS 一一对应（capability_matrix.py）。少一条就是少一行清单。 */
const FEATURE_ROWS: { path: string }[] = [
  { path: "structuredEvents" },
  { path: "sessions.list" },
  { path: "sessions.create" },
  { path: "sessions.resume" },
  { path: "sessions.history" },
  { path: "sessions.branch" },
  { path: "card.streaming" },
  { path: "card.tools.calls" },
  { path: "card.tools.output" },
  { path: "card.terminal" },
  { path: "card.fileChanges" },
  { path: "card.artifacts" },
  { path: "card.plan" },
  { path: "card.reasoning" },
  { path: "card.permissions" },
  { path: "card.questions" },
  { path: "card.authentication" },
  { path: "card.usage" },
  { path: "card.interrupt" },
  { path: "externalCli.supported" },
  { path: "externalCli.resume" },
  { path: "models.reasoning" },
  { path: "models.providers" },
];

function detailAt(tree: DetailCapabilityTree, path: string): DetailCapabilityState | null {
  let node: unknown = tree;
  for (const segment of path.split(".")) {
    if (typeof node !== "object" || node === null) return null;
    node = (node as Record<string, unknown>)[segment];
  }
  if (typeof node !== "object" || node === null) return null;
  const candidate = node as Partial<DetailCapabilityState>;
  return typeof candidate.value === "string" && typeof candidate.status === "string"
    ? (candidate as DetailCapabilityState)
    : null;
}

const STATUS_MARK: Record<string, string> = {
  supported: "✓",
  unsupported: "✗",
  unknown: "—",
};

/** 这一行该不该出现在能力清单里（批次十五第 4 件，AD-111 落到引擎卡）。
 *
 * 走查者管这一段叫"墙"：真机上一屏 23 行全是 `— unknown`，看完只知道"什么都不知道"。
 * 规则与项目页的能力表同口径（`lib/capabilityRows.ts`）：**说不出的就不说**。
 *   - `verification` 是 `unknown`（或缺）= 没人验证过 → 不渲染；
 *     `cached`（batch15-backend 新增）是**验证过**的一档，读的是上一次探测结果，照常渲染；
 *   - 取值本身就是 `unknown` → 不渲染（这一档正是那面墙；后端把 verification
 *     标成 declared 也不改变"这行说不出有没有"这个事实）；
 *   - 值是 `none` = 这项能力等于没有 → 不渲染。
 * 一行都没有时整段不渲染（由调用方判断 `visibleFeatureRows(...).length === 0`）。 */
export function visibleFeatureRows(
  detail: DetailCapabilityTree,
): { path: string; state: DetailCapabilityState }[] {
  const rows: { path: string; state: DetailCapabilityState }[] = [];
  for (const row of FEATURE_ROWS) {
    const state = detailAt(detail, row.path);
    if (!state) continue;
    if (!state.verification || state.verification === "unknown") continue;
    if (state.status === "unknown" || state.value === "unknown") continue;
    if (state.value === "none") continue;
    rows.push({ path: row.path, state });
  }
  return rows;
}

export function CapabilityList({ detail }: { detail: DetailCapabilityTree }) {
  const { t, tDynamic } = useLocale();
  const rows = visibleFeatureRows(detail);
  if (rows.length === 0) return null;
  return (
    <ul className="grid gap-1 sm:grid-cols-2">
      {rows.map(({ path, state }) => {
        const row = { path };
        const color =
          state.status === "supported"
            ? "var(--midground-base)"
            : state.status === "unsupported"
              ? cream(45)
              : cream(38);
        return (
          <li key={row.path} className="flex items-start gap-2 text-xs" style={{ color }}>
            <span className="w-3 shrink-0 tabular-nums">{STATUS_MARK[state.status]}</span>
            <span className="min-w-0">
              <span>{t(`feature.${row.path}` as DictKey)}</span>
              {/* 第 4 件：取值本地化。枚举轴上的值走词典，词典里没有的原样显示。 */}
              <span className="ml-1" style={{ color: cream(45) }}>
                {tDynamic(`capValue.${state.value}`, state.value)}
              </span>
              <span className="ml-1 text-[0.6rem]" style={{ color: cream(38) }}>
                · {tDynamic(`capValue.${state.verification}`, state.verification)}
              </span>
              {/* AD-71：note 只出现在这里（和 GET /api/backends/{id} 的输出里）。 */}
              {state.note && (
                <span className="mt-0.5 block text-[0.65rem] leading-relaxed" style={{ color: cream(50) }}>
                  {state.note}
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/* ------------------------------------------------------------------ *
 * 单条挂载 = 一张卡
 * ------------------------------------------------------------------ */

export function EngineCard({
  row,
  projectLabel,
  onSignIn,
  onNewConversation,
  onChangeModel,
  onChangeReasoning,
  onChangeApproval,
  onMakeDefault,
  onDetach,
  onMaterialize,
  onViewDrift,
}: { row: EngineRow; projectLabel?: (projectId: string) => string } & EngineWriteActions) {
  const { t, tDynamic } = useLocale();
  /* batch37 第 4 件（外部评审的产品判断 #6）：能力清单**默认折起来**。
     AD-111 已经把那面「23 行 unknown」的墙拆了，但剩下的那几行仍然是**取证**，
     不是日常动作：每次打开项目页都先读完一屏能力说明，把真正要做的事挤到窄处。
     折叠头上那个数（`engine.caps.toggle` 的 count）已经把"有几行可看"说清楚，
     想看的人点一下就有。除此之外这张卡的版式一律不动。 */
  const [showCapabilities, setShowCapabilities] = useState(false);
  /* batch40（★L 第 3 条）：卡上其余的一切（引擎侧身份 / 原生会话 / 与项目一致的
     漂移 / 已登录 / 审批模式 / 能力清单 / 引擎配置）收进这一枚「详情」，默认折着。 */
  const [showDetails, setShowDetails] = useState(false);
  const { binding, backend } = row;
  const ui = backend.capabilities.ui;
  const runtimeConfig = binding.runtimeConfig ?? {};
  /* 模型 / 推理强度 / 审批模式读**有效设置**（Binding → 引擎配置 → 目录默认，
     后端一次解析好）。这次没读着时只剩 Binding 自己记着的模型。 */
  const settings = row.effectiveSettings ?? null;
  const model = settings?.model.value ?? binding.defaultModelId;
  const reasoning = settings?.reasoningEffort.value ?? null;
  /* 「来自引擎配置」/「目录默认」：值不是本 Binding 自己设的就在字段旁写一句小字，
     这样"空着"与"引擎那边已经设了"就分得开了（不再一律显示「未设置」）。 */
  const modelHint = settings ? sourceHint(settings.model.source, t) : undefined;
  const reasoningHint = settings ? sourceHint(settings.reasoningEffort.source, t) : undefined;
  const twinNote = pick(runtimeConfig, ["twinmode", "mode"]);
  // AD-82：登录**入口**（按钮）只在引擎声明了站外 CLI 时才有意义；状态本身照显。
  const canSignIn = hasCapability(ui.externalCli.supported);
  const authState = row.auth ?? null;
  /* AD-93 的 `auth_model` 三档说成人话；词典里没有的新档位不显示（不回显枚举值）。 */
  const authModelLabel = authState?.model ? tDynamic(`engine.authModel.${authState.model}`, "") : "";
  /* batch33（AD-157）：own-auth 的引擎（ACP 那八家）我们**查不到**登录态，只能
     转述它自己让不让开会话——所以那句话必须写明是谁说的，否则用户会以为仪表盘
     真的知道他的账号状态。 */
  const selfReportedAuth = authState?.model === "own-auth";
  const signedOut = authState?.state === "signed_out";
  /* 没登录时模型目录必然是空的（引擎连会话都不给开）。那时写「未设置」是把
     「问不出来」说成了「你没设」——两句完全不同的话，用户会去改一个改不了的字段。 */
  const unsetLabel = signedOut ? t("engine.value.signInFirst") : t("engine.value.unset");
  const models = row.catalog?.models ?? [];
  /* 第 5 件：**有目录就可点开**（哪怕只有一条、甚至空目录）。目录整个拿不到
     （`catalog === null`：501 / 请求失败）才退回只读——那是"没有目录"，不是"没得选"。 */
  const hasCatalog = row.catalog !== null && row.catalog !== undefined;
  const canChangeModel = Boolean(onChangeModel) && hasCatalog;
  const modelDiagnostic = row.catalog?.diagnostics?.[0] ?? null;
  const modelDegraded = Boolean(row.catalog?.degraded);
  // 推理档位跟着**当前**模型走（不同模型的档位不一样）。
  const reasoningLevels =
    settings && settings.reasoningEffort.levels.length > 0
      ? settings.reasoningEffort.levels
      : (models.find((item) => item.modelId === (model ?? row.catalog?.defaultModelId))?.reasoningLevels ?? []);
  const canChangeReasoning = Boolean(onChangeReasoning) && reasoningLevels.length > 0;
  /* 第 4 件：推理强度**有档位就出现**——真机上 effective-settings 明明给了 `levels`，
     却因为能力矩阵是 `unknown` 整行不见了。档位本身就是这项能力存在的证据（同 AD-126），
     所以这里改成"有档位 或 引擎明说支持"才渲染，而不是只认能力矩阵。 */
  const showReasoning = reasoningLevels.length > 0 || hasCapability(ui.models.reasoning);
  /* 审批模式（AD-106 三档）。后端说 `none` = 这个引擎没有这项设置 → 整行不渲染（AD-71）。 */
  const approval = settings?.approvalMode ?? null;
  const showApproval = approval !== null && approval.source !== "none" && approval.value !== null;
  const capabilityRowCount = visibleFeatureRows(backend.capabilities.detail).length;

  /* batch40（★L 第 3 条）：模型与推理强度从「一行一个 FieldRow」搬到摘要那一行。
     两枚控件本身一个字不改（还是那套 EditableSelect / 只读 + 来源小字），改的只是
     它们摆在哪儿——标签去掉，值自己说话（同输入区工具栏那条 chip 规则）。 */
  const modelField = (
    <>
      {canChangeModel ? (
        <EditableSelect
          ariaLabel={t("engine.model.aria", { name: binding.displayName })}
          serverValue={model ?? ""}
          /* 只有一条时也把原因说清楚（批次十五第 5 件）。 */
          footnote={modelDegraded || models.length <= 1 ? modelDiagnostic : null}
          emptyNote={t("bar.model.empty")}
          degraded={modelDegraded}
          options={[
            /* 库里那个值不在目录里时也要看得见（不静默改掉用户已有的设置）。 */
            ...(model && !models.some((item) => item.modelId === model)
              ? [{ value: model, label: model }]
              : []),
            ...(model ? [] : [{ value: "", label: unsetLabel }]),
            ...models.map((item) => ({
              value: item.modelId,
              label: item.displayName || item.modelId,
            })),
          ]}
          onSave={(value) => onChangeModel?.(row, value)}
        />
      ) : (
        <span className="inline-flex items-center gap-2 text-xs">
          <span style={{ color: model ? undefined : cream(45) }} data-testid="engine-model-value">
            {model ?? unsetLabel}
          </span>
          {/* 没有模型目录就没得选：只读，不写「本引擎不支持」（AD-71）。 */}
          <Pending />
        </span>
      )}
      {modelHint && <SourceNote text={modelHint} />}
    </>
  );
  const reasoningField = (
    <>
      {canChangeReasoning ? (
        <EditableSelect
          ariaLabel={t("engine.reasoning.aria", { name: binding.displayName })}
          serverValue={reasoning ?? ""}
          options={[
            { value: "", label: t("engine.reasoning.default") },
            ...reasoningLevels.map((level) => ({ value: level, label: level })),
          ]}
          onSave={(value) => onChangeReasoning?.(row, value)}
        />
      ) : (
        <span className="inline-flex items-center gap-2 text-xs">
          <span style={{ color: reasoning ? undefined : cream(45) }}>
            {reasoning ?? t("engine.value.unset")}
          </span>
          <Pending />
        </span>
      )}
      {reasoningHint && <SourceNote text={reasoningHint} />}
    </>
  );

  return (
    <div style={{ background: cream(2), border: `1px solid ${cream(16)}`, borderRadius: 8 }}>
      <div className="flex flex-wrap items-center gap-2 px-4 py-3" style={{ borderBottom: `1px solid ${cream(16)}` }}>
        <span
          className="grid size-6 shrink-0 place-items-center rounded-[2px] text-[0.65rem] font-bold"
          style={{ background: cream(7), color: cream(70), border: `1px solid ${cream(14)}` }}
          title={backend.displayName}
        >
          {engineInitials(backend.displayName)}
        </span>
        <span className="text-sm font-semibold" style={{ color: "var(--midground-base)" }}>
          {binding.displayName}
        </span>
        {/* batch40（★L 第 3 条）：摘要第一行 = 引擎名 · 版本 · 状态 · 默认。
            引擎名与版本原来只在卡里那一行 FieldRow 上，现在提到卡头，那一行删掉。 */}
        <span className="inline-flex items-baseline gap-1.5 text-[0.7rem]" style={{ color: cream(45) }} data-testid="engine-name">
          <span>{backend.displayName}</span>
          {backend.version && <span>{backend.version}</span>}
        </span>
        {binding.isDefault && <Pill tone="gold">{t("engine.badge.default")}</Pill>}
        {!binding.enabled && <Pill tone="muted">{t("engine.badge.disabled")}</Pill>}
        {/* 第 4 件：「未就绪」不再是一句谜语——探测的人话挂在 chip 的 title 上
            （`GET /api/backends/{id}` 的 `probeMessage`）。后端没给就没有 title。 */}
        <Pill
          tone={PROBE_TONE[backend.probeState]}
          title={
            backend.probeState !== "available" && backend.probeMessage
              ? t("engine.probe.offline", { message: backend.probeMessage })
              : undefined
          }
        >
          {t(`engine.probe.${backend.probeState}` as DictKey)}
        </Pill>
        {/* batch26：按预设装配的行给一枚只读小标。`quirks` 不显示（AD-71）。 */}
        {backend.preset?.label && (
          <Pill tone="muted" title={t("engine.field.preset")}>
            <span data-testid="engine-preset-badge">{backend.preset.label}</span>
          </Pill>
        )}
        <span className="ml-auto flex items-center gap-2">
          {/* batch26：门控换成 `projection/_meta` 的 `supported`（AD-126 只看入口）。
              读 drift 成不成功是"这次读着没"，不是"这台引擎支不支持"。 */}
          {onMaterialize && row.projectionSupported === true && (
            <Button onClick={() => onMaterialize(row)} className="kaus-materialize">
              {t("materialize.action")}
            </Button>
          )}
          {onNewConversation && (
            <Button onClick={() => onNewConversation(row)}>{t("engine.action.newConversation")}</Button>
          )}
          {!binding.isDefault && onMakeDefault && (
            <Button onClick={() => onMakeDefault(row)}>{t("engine.action.makeDefault")}</Button>
          )}
          {onDetach && !binding.isDefault && (
            <Button onClick={() => onDetach(row)}>{t("engine.action.detach")}</Button>
          )}
        </span>
      </div>

      {/* batch40（★L 第 3 条）：卡面默认只有两行。
          第一行（引擎名 · 版本 · 状态 · 默认）已经在卡头上了，这里是第二行：
          模型 · 推理强度 · 登录态（只在没登录时出现）。**例外照旧常显**：
          `driftedCount > 0` 的漂移、`unavailable` 的探测原因、未登录的下一步——
          它们是"有事发生"，不是"可查的资料"。 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3" data-testid="engine-summary">
        <span className="inline-flex items-center gap-2">{modelField}</span>
        {showReasoning && <span className="inline-flex items-center gap-2">{reasoningField}</span>}
        {authState?.state === "signed_out" && (
          <span className="inline-flex flex-wrap items-center gap-2 text-xs" data-testid="engine-auth">
            <span style={{ color: cream(45) }}>
              {t("engine.summary.signedOut")}
              {authState.hint ? ` · ${authState.hint}` : ""}
            </span>
            {onSignIn && canSignIn && (
              <Button onClick={() => onSignIn(row)}>
                <span className="inline-flex items-center gap-1">
                  <LogIn className="size-3" /> {t("engine.action.signIn")}
                </span>
              </Button>
            )}
          </span>
        )}
        {/* 探测失败的人话：`unavailable` 时不只挂在 chip 的 title 上，摆到摘要里。 */}
        {backend.probeState === "unavailable" && backend.probeMessage && (
          <span className="text-xs" style={{ color: "var(--danger)" }} data-testid="engine-probe-message">
            {backend.probeMessage}
          </span>
        )}
        {/* 漂移只在**真有对不上的键**时常显；`in_sync` 那一行收进「详情」。 */}
        {row.projectionSupported === true && row.drift && row.drift.driftedCount > 0 && (
          <span className="inline-flex flex-wrap items-center gap-2 text-xs" data-testid="engine-drift">
            <span style={{ color: "var(--midground-base)" }}>
              {t("drift.count", { count: row.drift.driftedCount })}
            </span>
            {onViewDrift && <Button onClick={() => onViewDrift(row)}>{t("drift.view")}</Button>}
          </span>
        )}
        <button
          type="button"
          className="ml-auto text-[0.6rem] uppercase tracking-[0.14em]"
          style={{ color: cream(45) }}
          data-testid="engine-details-toggle"
          aria-expanded={showDetails}
          onClick={() => setShowDetails((value) => !value)}
        >
          {t("engine.details.toggle")} {showDetails ? "▾" : "▸"}
        </button>
      </div>

      {showDetails && (
      <div className="px-4 pb-3" data-testid="engine-details">
        {/* 批次十三：审批模式。取值与选项都来自有效设置；`none` ⇒ 整行不渲染（AD-71）。 */}
        {showApproval && approval && (
          <FieldRow label={t("bar.approval")}>
            {onChangeApproval && approval.options.length > 0 ? (
              <EditableSelect
                ariaLabel={t("engine.approval.aria", { name: binding.displayName })}
                serverValue={approval.value ?? ""}
                options={approval.options.map((option) => ({
                  value: option,
                  label: approvalLabel(option, t),
                }))}
                onSave={(value) => onChangeApproval(row, value)}
              />
            ) : (
              <span>{approvalLabel(approval.value ?? "", t)}</span>
            )}
            {sourceHint(approval.source, t) && <SourceNote text={sourceHint(approval.source, t) as string} />}
          </FieldRow>
        )}
        <FieldRow label={t("engine.field.nativeScope")}>
          <span style={{ font: "0.7rem var(--theme-font-mono, monospace)", color: cream(62) }}>
            {binding.nativeScopeRef ?? "—"}
          </span>
        </FieldRow>
        {twinNote && <FieldRow label={t("engine.field.mode")}>{twinNote}</FieldRow>}
        {/* 原生会话数（batch19 第 1 件）。端点落地后这一行自己回来了。
            判据只有一条：**wire 上有没有这个数**。数拿不到（null / 字段缺席）就整行
            不渲染，不摆「BACKEND TODO」也不写备注（AD-71）。不再另外查
            `sessions.list` 能力——数本身就是这项能力存在的证据（同 AD-126）。 */}
        {row.nativeSessionCount != null && (
          <FieldRow label={t("engine.field.nativeSessions")}>
            <span className="tabular-nums" data-testid="engine-native-sessions">
              {t("engine.nativeSessions.count", { count: row.nativeSessionCount })}
            </span>
          </FieldRow>
        )}
        {/* batch25：配置漂移。这一行的存在本身就是"这台引擎有投射面"的证据
            （同 AD-126）：读不到 drift（端点不在 / `projection_unsupported` /
            这次没读着）整行不渲染，不写「不支持」也不报错（AD-71）。
            `driftedCount` 只数 drifted + missing，`unmanaged` 不出现在这里。 */}
        {/* batch40（★L 第 3 条）：`driftedCount > 0` 那一档搬到摘要上常显（它是
            "有事发生"）；**「与项目一致」留在这里**——那是一句可查的资料。 */}
        {row.projectionSupported === true && row.drift && row.drift.driftedCount === 0 && (
          <FieldRow label={t("drift.field")}>
            <span className="inline-flex flex-wrap items-center gap-2" data-testid="engine-drift-in-sync">
              <span style={{ color: cream(45) }}>
                {t("drift.inSync", { time: formatCheckedAt(row.drift.checkedAt) })}
              </span>
            </span>
          </FieldRow>
        )}
        {/* 登录（batch19 第 1 件，AD-82/93）。batch40（★L 第 3 条）之后这里只剩
             **已登录**那一档——它是"一切正常"的存档，属于详情；`signed_out`（含后端
             给的下一步 hint）搬到了摘要上常显，`unknown` 照旧整行不渲染（AD-71）。 */}
        {authState && authState.state === "signed_in" && (
          <FieldRow label={t("engine.field.login")}>
            <span className="inline-flex flex-wrap items-center gap-2" data-testid="engine-auth-detail">
              <span>
                {selfReportedAuth
                  ? t("engine.auth.signedInSelfReported")
                  : t("engine.auth.signed_in")}
                {!selfReportedAuth && authModelLabel ? ` · ${authModelLabel}` : ""}
                {authState.account ? ` · ${authState.account}` : ""}
              </span>
            </span>
          </FieldRow>
        )}

        {/* 第 4 件：数的是**看得见的**行，不是"多少项未知"——后者正是那面墙。
            一行都看不见时连折叠头都不渲染（AD-111）。 */}
        {capabilityRowCount > 0 && (
          <button
            type="button"
            onClick={() => setShowCapabilities((value) => !value)}
            aria-expanded={showCapabilities}
            className="mb-2 text-[0.6rem] uppercase tracking-[0.14em]"
            style={{ color: cream(45) }}
          >
            {t("engine.caps.toggle", { count: capabilityRowCount })} {showCapabilities ? "▾" : "▸"}
          </button>
        )}
        {showCapabilities && <CapabilityList detail={backend.capabilities.detail} />}
        {/* 能力清单区的一行「预设：<label>」。怪癖表**不摊开**——那七位是 Driver 的
            分支开关，不是能力（AD-71）；缺 `preset` 这个键时整行不渲染。 */}
        {backend.preset?.label && (
          <div className="mt-2 text-[0.65rem]" style={{ color: cream(50) }} data-testid="engine-preset-line">
            {t("engine.field.preset")}：{backend.preset.label}
          </div>
        )}
        {/* AD-97：引擎配置（backend-scoped 能力按组列出，只读）。 */}
        <EngineConfigSection capabilities={row.capabilities} projectLabel={projectLabel} />
      </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 面板
 * ------------------------------------------------------------------ */

/* batch40（★L 第 3 条）：原来这里有一枚 `AttachEngine`（下拉 + 「接入」按钮），
   摆在卡片下面；再加上区头那枚「接入引擎…」，同一件事在一屏里有两个入口。现在合成
   **一个**：区头的「接入引擎…」，那枚下拉搬进它打开的弹窗（`ConnectEngineModal`）。 */

export function EnginePanel({
  rows,
  onConnectEngine,
  projectLabel,
  ...actions
}: {
  rows: EngineRow[];
  /** Project id → 显示名（引擎配置区的「继承自 X」用）。 */
  projectLabel?: (projectId: string) => string;
  /** batch26：打开「接入引擎」面板（batch40 起那枚「接入」下拉也在这个弹窗里）。 */
  onConnectEngine?: () => void;
} & EngineWriteActions) {
  const { t } = useLocale();
  return (
    <Card
      title={t("engine.panel.title")}
      icon={Plug}
      right={
        <span className="inline-flex items-center gap-2">
          {onConnectEngine && (
            <Button onClick={onConnectEngine} className="kaus-connect-engine">
              {t("engines.connect")}
            </Button>
          )}
          <Pill tone="muted">{rows.length}</Pill>
        </span>
      }
    >
      {rows.length === 0 ? (
        <div className="flex flex-col items-start gap-3 py-2">
          <p className="text-xs" style={{ color: cream(55) }}>
            {t("engine.panel.empty")}
          </p>
          {/* 一个项目还没接引擎时，唯一的下一步就是区头那枚「接入引擎…」。 */}
          {onConnectEngine && (
            <Button onClick={onConnectEngine} className="kaus-connect-engine">
              {t("engines.connect")}
            </Button>
          )}
        </div>
      ) : (
        <div className="grid gap-3">
          {rows.map((row) => (
            <EngineCard key={row.binding.id} row={row} projectLabel={projectLabel} {...actions} />
          ))}
        </div>
      )}
    </Card>
  );
}
