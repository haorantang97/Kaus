/* Session Host（Phase 3B）与 Phase 1 领域库的前端接入层。
 *
 * 三条口径（IA §7.1 / AD-66 / D-17）：
 * 1. token 只放**内存**——不进 localStorage / sessionStorage / cookie。刷新时重新
 *    bootstrap，代价只有一次同源 GET。
 * 2. 普通请求带 `Authorization: Bearer`；**SSE 是唯一**走 `?token=` 的端点
 *    （EventSource 带不了自定义头），URL 由本层拼，组件不碰 token。
 * 3. 401 → 重新 bootstrap 一次再重试原请求；再 401 就抛错，由界面显示
 *    「会话功能未开启」。不做无限重试。
 *
 * 「会话功能开没开」的判断也在这里：前端不读后端 flag，只看
 * `GET /api/session-auth/bootstrap` 成不成功（flag 关闭时这条路由根本不存在 → 404）。
 */

import type { UiCapabilities } from "../components/cards/capabilities";
import type { BackendWire } from "../components/EnginePanel";
import { authorizedFetch, currentSessionToken } from "./authorizedFetch";

export type { BackendWire };

export class SessionApiError extends Error {
  readonly code: string;
  readonly status: number;
  /** 统一错误信封 `{error:{code,message,…}}` 里 code/message 之外的键，原样带上。
   *  第一个消费者是 `POST /messages` 失败时的 `emptyConversation`（第 4 件）。 */
  readonly detailFields: Record<string, unknown>;
  constructor(status: number, code: string, message: string, detailFields: Record<string, unknown> = {}) {
    super(message);
    this.name = "SessionApiError";
    this.status = status;
    this.code = code;
    this.detailFields = detailFields;
  }
}

/** 给人看的错误文案：`message` 之后接后端给的 `detail.hint`（batch11-backend 的
 *  `driver_not_registered` 会带一句"怎么修"）。非 SessionApiError 就原样字符串化。 */
export function describeFailure(failure: unknown): string {
  if (failure instanceof SessionApiError) {
    const detail = failure.detailFields.detail;
    const hint =
      detail && typeof detail === "object" && typeof (detail as { hint?: unknown }).hint === "string"
        ? (detail as { hint: string }).hint
        : null;
    return hint ? `${failure.message} ${hint}` : failure.message;
  }
  return String((failure as Error)?.message ?? failure);
}

/** batch38 第 2 件（批次三十七 R6 的 wire）：`POST /messages` 在**用户消息已经落库
 *  之后**才失败时（`409 turn_already_running` / `501` / `502 message_rejected` /
 *  `409 runtime_not_active`），错误体的 `detail` 里带上那句话的 `clientRef`。
 *  页面据它把本地那条置灰，而不必去猜是哪一句。没给编号时这个键**不出现**（不是
 *  null），所以拿不到就退回调用方自己手上的那个编号。 */
export function failureClientRef(failure: unknown): string | null {
  if (!(failure instanceof SessionApiError)) return null;
  const detail = failure.detailFields.detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  const ref = (detail as { clientRef?: unknown }).clientRef;
  return typeof ref === "string" && ref ? ref : null;
}

/* batch38 第 1 件：token 缓存、bootstrap 去重、401 重取这三件事搬到
   `lib/authorizedFetch.ts`——旧写接口（`lib/api.ts` 的 `apiPost` / `apiDelete`）
   在批次三十七 R5 之后也要带同一份 token，两条路抄成两份迟早会漂。这里原样再
   导出，既有调用方（含测试）一个都不用改。 */
export {
  SessionUnavailableError,
  bootstrapSessionAuth,
  currentSessionToken,
  resetSessionAuth,
} from "./authorizedFetch";

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

async function parseError(response: Response): Promise<SessionApiError> {
  const payload = (await response.json().catch(() => ({}))) as {
    error?: Record<string, unknown> & { code?: string; message?: string };
    detail?: string;
  };
  // 新域统一错误体 {"error":{code,message}}；旧域是 {"detail": "..."}。两种都收敛成一条。
  const code = payload.error?.code ?? (response.status === 404 ? "not_found" : "http_error");
  const message = payload.error?.message ?? payload.detail ?? `HTTP ${response.status}`;
  const { code: _code, message: _message, ...rest } = payload.error ?? {};
  return new SessionApiError(response.status, code, message, rest);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  /* batch38 第 1 件：Bearer + 401 重取那一段在 `authorizedFetch`（与旧写接口同一份）。
     这里只剩这一层自己的东西：JSON 信封、204、以及 `{"error":{code,message}}` 的解析。 */
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await authorizedFetch(path, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? null : JSON.stringify(options.body),
    signal: options.signal,
    // 会话接口没有 token 就是「会话功能未开启」，界面据此保持旧样子。
    tokenMode: "required",
  });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/* batch23：Group 端点（`/api/groups`）与会话端点同源——同一套 Bearer 鉴权、同一个
   `{"error":{code,message,detail}}` 信封、同一条 401 重取规则。所以
   `lib/groupsApi.ts` 复用这一层，而不是再抄一份重试逻辑。 */
export { request as sessionRequest };

/** SSE 的 URL（唯一允许把 token 放查询串的地方）。 */
export function conversationEventsUrl(conversationId: string, after: number | null): string {
  const params = new URLSearchParams();
  if (after !== null && after >= 0) params.set("after", String(after));
  const token = currentSessionToken();
  if (token) params.set("token", token);
  const query = params.toString();
  return `/api/conversations/${encodeURIComponent(conversationId)}/events${query ? `?${query}` : ""}`;
}

/* ------------------------------------------------------------------ *
 * wire 类型（驼峰，与后端 by_alias 序列化一致）
 * ------------------------------------------------------------------ */

export interface ProjectWire {
  id: string;
  slug: string;
  displayName: string;
  parentProjectId: string | null;
  workspaceRoot: string | null;
  status: string;
  metadata?: Record<string, unknown>;
  createdAt?: string;
  updatedAt?: string;
}

/* batch19（AD-82/93）：登录状态。**Driver 自己报告的三态**，不跨引擎共享。
 * 明文凭据不上 wire：`account` 是脱敏摘要（`a***@example.com`）或 null。 */
export interface EngineAuthStateWire {
  state: "signed_in" | "signed_out" | "unknown";
  /** 这个引擎怎么认账号：托管凭据 / 自带登录 / 跟着会话走。 */
  model: "managed-credential" | "own-auth" | "session-scoped";
  provider?: string | null;
  account?: string | null;
  checkedAt?: string;
  /** 未登录时的下一步人话（后端 `failure_hints`）。 */
  hint?: string | null;
}

export interface BindingWire {
  id: string;
  projectId: string;
  backendId: string;
  displayName: string;
  nativeScopeRef: string | null;
  enabled: boolean;
  isDefault: boolean;
  defaultModelId: string | null;
  defaultProviderId: string | null;
  /** backend 私有配置（opaque dict，公共层不解释内容）。 */
  runtimeConfig?: Record<string, unknown>;
  compatibilityState: string;
  discriminator: string | null;
  /* batch19：`GET /api/projects/{id}/bindings` 的每行也带这两项，于是引擎卡
     不必为每条 Binding 再各拉一次 `/status`。宿主没登记状态取数面时这两个键
     缺席 ⇒ 那两行不渲染（AD-71）。 */
  auth?: EngineAuthStateWire;
  nativeSessionCount?: number | null;
}

/** `GET /api/bindings/{id}/status`（batch19）：登录态 + 原生会话数 + 探测态，
 *  一条请求里给全——会话页页头的「引擎离线」于是不必再单独拉一次 backend。 */
export interface BindingStatusWire {
  bindingId: string;
  auth?: EngineAuthStateWire;
  nativeSessionCount?: number | null;
  /** 领域库里没有这台 Backend 的行时缺席。 */
  probeState?: BackendWire["probeState"];
  probeMessage?: string | null;
}

export function fetchBindingStatus(bindingId: string, signal?: AbortSignal): Promise<BindingStatusWire> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}/status`, { signal });
}

export type ConversationState =
  | "idle"
  | "running-card"
  | "running-external"
  | "paused"
  | "ended"
  | "error";

export interface ConversationWire {
  id: string;
  projectId: string;
  agentBindingId: string;
  title: string;
  state: ConversationState;
  modelId: string | null;
  providerId: string | null;
  reasoningMode: string | null;
  approvalMode?: string | null;
  executionMode?: string | null;
  archivedAt?: string | null;
  preferredSurface: string;
  visibility: string;
  origin: string;
  createdAt: string;
  updatedAt: string;
}

export interface ModelDescriptorWire {
  modelId: string;
  displayName: string | null;
  providerId: string | null;
  /** batch27：这条模型属于哪台引擎。绝大多数目录不给（一份目录本来就只有自己的
   *  模型）；给了就按它过滤，别的引擎的模型不进这条会话的下拉（DESIGN ★I）。 */
  backendId?: string | null;
  /** batch29：给人看的 provider 名（`Anthropic` / `OpenAI Codex`）。`providerId`
   *  是 slug（与引擎配置对得上），这一个只用来显示。下拉按它分组；**没有就不分组**
   *  ——前端不拿 slug 猜名字，猜出来的 `Openai Codex` 比没有名字更糟。 */
  providerLabel?: string | null;
  /** batch29：这个模型的 provider 是不是引擎此刻在用的那家。它的组排第一。 */
  isCurrentProvider?: boolean;
  contextWindow: number | null;
  reasoningLevels: string[];
}

export interface ModelCatalogWire {
  bindingId: string;
  mode: string;
  /** 已经由后端过滤过：这里列出的就是该 Binding 真能用的模型（batch13-backend）。 */
  models: ModelDescriptorWire[];
  defaultModelId: string | null;
  defaultProviderId: string | null;
  supportsReasoning: boolean;
  /** 目录是"降级"取来的（探测失败 / 用了兜底表）。批次十三后端新增。 */
  degraded?: boolean;
  engineDefaultAvailable?: boolean;
  /** 降级原因，人话一行一条。界面只显示第一条（DESIGN ★ I）。 */
  diagnostics?: string[];
}

/* ---- 有效设置（batch13-backend：`GET /api/bindings/{id}/effective-settings`） ---- */

/** 这一项的取值是从哪儿解析出来的。`none` = 压根没有值 → 前端不渲染（AD-71）。 */
export type SettingSource = "binding" | "engine" | "catalog" | "project" | "none";

export interface EffectiveSettingWire {
  value: string | null;
  source: SettingSource;
}

export interface EffectiveSettingsWire {
  bindingId: string;
  conversationControls?: { reasoning: boolean; reasoningDefault?: string | null; reasoningLevels?: string[]; approvalModes: string[]; approvalDefault?: string | null; executionModes?: { id: string; name: string }[]; executionDefault?: string | null };
  model: EffectiveSettingWire;
  /** `levels` 为空 = 当前模型没有推理档位 → 那枚下拉不渲染。 */
  reasoningEffort: EffectiveSettingWire & { levels: string[] };
  /** 后端给的是 `["ask","auto","deny"]`（AD-106 的三档）。 */
  approvalMode: EffectiveSettingWire & { options: string[] };
  workspaceRoot: EffectiveSettingWire;
}

/** 「一项都没有」：还没读到、或读取失败时工具栏按这一份渲染——四枚都不出，
 *  而不是弹一句"读取失败"。 */
export function emptyEffectiveSettings(bindingId: string): EffectiveSettingsWire {
  return {
    bindingId,
    model: { value: null, source: "none" },
    reasoningEffort: { value: null, source: "none", levels: [] },
    approvalMode: { value: null, source: "none", options: [] },
    workspaceRoot: { value: null, source: "none" },
  };
}

/** Binding 的有效设置：Binding → 引擎配置 → 目录默认，后端一次解析好。 */
export function fetchEffectiveSettings(bindingId: string, signal?: AbortSignal): Promise<EffectiveSettingsWire> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}/effective-settings`, { signal });
}

/** 改工作目录（DESIGN ★ I）。body 只有这一个键，后端 400 码：
 *  `workspace_root_not_absolute` / `_not_found` / `_not_a_directory`。 */
export function patchProject(projectId: string, body: { workspaceRoot: string }): Promise<ProjectWire> {
  return request(`/api/projects/${encodeURIComponent(projectId)}`, { method: "PATCH", body });
}

export interface TimelineSummaryWire {
  conversationId: string;
  runState: string;
  activeRunId: string | null;
  lastSequence: number | null;
  itemCount: number;
  itemCounts: Record<string, number>;
  pendingInteractions: { interactionId: string; interactionKind: string | null; runId: string | null }[];
  usage: Record<string, unknown> | null;
  error: { code?: string; message?: string } | null;
}

export interface RuntimeViewWire {
  workspaceRoot?: string | null;
  active: boolean;
  runtimeId: string | null;
  backendId: string | null;
  nativeSessionId: string | null;
  owner: { ownerType?: string; ownerId?: string } | null;
}

export interface ConversationDetailWire {
  conversation: ConversationWire;
  runtime: RuntimeViewWire;
  timeline: TimelineSummaryWire | null;
  advisory: { message?: string } | null;
  /** 这条会话此刻的运行态，**以后端有没有 runtime 为准**
   *  （`idle | running | stopping-unconfirmed`，见 `lib/runState.ts`）。**不要**读
   *  `timeline.runState` 顶这个位置：那一份和前端镜像同源，会卡在假运行态上。 */
  runState: string;
}

/* ------------------------------------------------------------------ *
 * 端点
 * ------------------------------------------------------------------ */

export function fetchProjects(signal?: AbortSignal): Promise<{ projects: ProjectWire[]; roots: string[]; count: number }> {
  return request("/api/projects", { signal });
}

export function fetchProjectBindings(projectId: string, signal?: AbortSignal): Promise<{ projectId: string; bindings: BindingWire[]; count: number }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/bindings`, { signal });
}

export function fetchBinding(bindingId: string, signal?: AbortSignal): Promise<BindingWire> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}`, { signal });
}

/** `GET /api/conversations` 一行：跨项目会话索引，刻意是扁平的一小把字段。
 *  完整对象仍走 `GET /api/conversations/{id}`（`fetchConversation`）。 */
export interface ConversationIndexEntryWire {
  id: string;
  projectId: string;
  bindingId: string;
  /** Binding 上取；取不到就是 null（后端不编造）。 */
  backendId: string | null;
  title: string;
  /** 封闭集合：idle | running | paused | ended | error。 */
  status: string;
  /** batch23（批次二十二第 2 件）：这条会话现在在哪个**还开着的**组里；
   *  不在任何组里、或后端还没这个键，都是 null/缺席 ⇒ 侧栏不标组图标（AD-71）。 */
  groupId?: string | null;
  /** batch26：那个组的标题。索引行自己带了标题，侧栏于是不必再去 store 里查
   *  （store 只在浮窗挂载过之后才有数据）。缺席 ⇒ 退回 store，再拿不到就不标。 */
  groupTitle?: string | null;
  updatedAt: string;
  lastSequence: number | null;
}

/** 批次八第 1 件：跨项目最近会话，一次查完（此前是 1 + N 次请求）。
 *  `project` 把同一个端点收窄成单项目视图（项目详情页 ④ 区用它）。 */
export function fetchRecentConversations(
  signal?: AbortSignal,
  options: { project?: string; limit?: number } = {},
): Promise<{ conversations: ConversationIndexEntryWire[]; count: number; nextUpdatedAfter: string | null }> {
  const params = new URLSearchParams();
  if (options.project) params.set("project", options.project);
  if (options.limit) params.set("limit", String(options.limit));
  const query = params.toString();
  return request(`/api/conversations${query ? `?${query}` : ""}`, { signal });
}

/* ---- Binding 写端点（批次八第 2 件，kernel/app/api/binding_router.py） ---- *
 * 四条都只改领域库里的 Binding 行，**不**投影到引擎（那是 Phase 5 的事）。 */

/** 改默认模型 / 通用 runtimeConfig 键（推理强度走 `reasoning_effort`）。 */
export function patchBinding(
  bindingId: string,
  body: { defaultModelId?: string | null; runtimeConfig?: Record<string, unknown> },
): Promise<BindingWire & { projectedToBackend?: boolean }> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}`, { method: "PATCH", body });
}

/** batch26：会话级写端点（AD-134）。`appliedToRuntime` 说这次有没有真的落到
 *  正在跑的 runtime 上；引擎拒了这个模型时是 400 `model_rejected`——**那时快照
 *  没有被写**，界面必须把下拉拨回服务端的值，不能留着一个假的选中态。 */
// batch52：多收一个 `title`（会话自己的名字，批次五十二第 5 件）。侧栏 ⋯ 菜单里
// 那条「改名」走的是 `POST /api/agent/{name}/rename`，改的是**项目**的显示名，
// 不是这个；会话标题此前没有任何界面入口。
export function patchConversation(
  conversationId: string,
  body: { title?: string; modelId?: string | null; reasoningMode?: string; approvalMode?: string; executionMode?: string },
): Promise<{ conversation: ConversationWire; appliedToRuntime: boolean }> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "PATCH", body });
}

/** 400 `model_rejected` 的 `detail`：引擎自己给的一句拒绝理由 + 被拒的模型 id。
 *  不是这个码就返回 null。 */
export function modelRejection(failure: unknown): { modelId: string; agentReason: string } | null {
  if (!(failure instanceof SessionApiError) || failure.code !== "model_rejected") return null;
  const detail = failure.detailFields.detail as
    | { modelId?: unknown; agentReason?: unknown }
    | undefined;
  return {
    modelId: typeof detail?.modelId === "string" ? detail.modelId : "",
    agentReason: typeof detail?.agentReason === "string" ? detail.agentReason : failure.message,
  };
}

export function makeBindingDefault(bindingId: string): Promise<BindingWire & { changed: boolean }> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}/make-default`, { method: "POST" });
}

/** 接入一条**非默认**的挂载（换默认是另一个动作）。 */
export function createBinding(
  projectId: string,
  body: { backendId: string; discriminator?: string | null; displayName?: string | null },
): Promise<BindingWire> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/bindings`, { method: "POST", body });
}

/** 解除挂载。默认 Binding 与名下有活跃 Runtime 的会被后端挡成 409。 */
export function deleteBinding(bindingId: string): Promise<{ bindingId: string; deleted: boolean }> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}`, { method: "DELETE" });
}

/* ---- 物化 / 漂移（批次二十四后端，AD-149/150，`docs/ops/projection.md`） ---- *
 *
 * 两条端点，一条写一条读：
 *   `POST /api/bindings/{id}/materialize?confirm=0|1&adopt=0|1`
 *   `GET  /api/bindings/{id}/drift`
 *
 * 口径三条（前端这一侧要守住的）：
 *  1. **默认 dry-run**：`confirm` 不传就是预演，磁盘一个字节都不动。真写要显式给
 *     `confirm: true`，界面上对应「备份后写入」那一下。
 *  2. **`adopt` 只为 `factory_protected`**：当前有值、又不是 Kaus 写的键，不带
 *     `adopt=1` 就不覆盖。所以它在界面上是一枚复选框，不是默认值。
 *  3. **值不留在前端**：`before` / `after` 是后端脱敏过的、只为当场比对，不进
 *     localStorage、不进任何缓存。
 */

/** 变更集里一行的动作。字符串而非联合类型的原因同 `compatibilityState`：
 *  后端将来多一个动作值时，界面该照样渲染，而不是崩在一个 switch 上。 */
export type ProjectionAction = "set" | "unset" | "unchanged";

/** `unsupported[]` 的四个原因（`docs/ops/projection.md` §7）。 */
export type ProjectionUnsupportedReason =
  | "credential_bearing"
  | "not_mapped"
  | "not_blockable"
  | "factory_protected";

/** `applied[]` 的一行 = 变更集的一行。`before` / `after` 已过 Driver 脱敏器。 */
export interface ProjectionEntryWire {
  capabilityType: string;
  capabilityId: string;
  level: string;
  keyPath: string;
  targetRef?: string | null;
  before?: unknown;
  after?: unknown;
  /** `set` | `unset` | `unchanged`（新值按"未知动作"处理，不崩）。 */
  action: string;
  reason?: string | null;
  detail?: string | null;
}

/** `unsupported[]` 的一行 = 一条**不会**被写下去的能力，带原因，不静默丢失。 */
export interface ProjectionUnsupportedWire {
  capabilityType: string;
  capabilityId: string;
  level: string;
  keyPath?: string | null;
  /** `credential_bearing` | `not_mapped` | `not_blockable` | `factory_protected`。 */
  reason: string;
  detail?: string | null;
}

export interface ProjectionReportWire {
  bindingId?: string;
  applied: ProjectionEntryWire[];
  unsupported: ProjectionUnsupportedWire[];
  warnings: string[];
  dryRun?: boolean;
  backupPath?: string | null;
}

export interface MaterializeResultWire {
  bindingId: string;
  dryRun: boolean;
  /** 真写成功才有；dry-run 恒为 null。 */
  backupPath: string | null;
  result: ProjectionReportWire;
}

/** 预演 / 真写。**默认预演**——`confirm` 不给就是 dry-run（后端也是这个默认，
 *  这里显式传是为了让调用点自己读得出来在做哪件事）。 */
export function materializeBinding(
  bindingId: string,
  options: { confirm?: boolean; adopt?: boolean } = {},
): Promise<MaterializeResultWire> {
  const params = new URLSearchParams();
  params.set("confirm", options.confirm ? "1" : "0");
  if (options.adopt) params.set("adopt", "1");
  return request(`/api/bindings/${encodeURIComponent(bindingId)}/materialize?${params.toString()}`, {
    method: "POST",
  });
}

/** Drift 四态（`docs/ops/projection.md` §6）。`unmanaged` **不算**漂移。 */
export type DriftState = "in_sync" | "drifted" | "unmanaged" | "missing";

export interface DriftItemWire {
  capabilityType: string;
  capabilityId: string;
  keyPath: string;
  expected?: unknown;
  actual?: unknown;
  /** 四态之一（同上，按字符串收）。 */
  state: string;
  detail?: string | null;
}

export interface BindingDriftWire {
  bindingId: string;
  checkedAt: string;
  items: DriftItemWire[];
  /** 只数 `drifted` + `missing`。 */
  driftedCount: number;
}

/** 引擎侧现在长什么样。**失败往上抛**：调用方要按错误码分辨"这台引擎没有投射面"
 *  （→ 整行与入口都不渲染，AD-71/126）与"这次没读着"，吞掉就分不出来了。 */
export function fetchBindingDrift(bindingId: string, signal?: AbortSignal): Promise<BindingDriftWire> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}/drift`, { signal });
}

/** 409 `binding_busy` 点名的那几条正在跑的会话（信封上的 `activeConversations`）。
 *  不是这个码就返回 null。 */
export function busyConversations(
  failure: unknown,
): { conversationId: string; title: string }[] | null {
  if (!(failure instanceof SessionApiError) || failure.code !== "binding_busy") return null;
  return failure.detailFields.activeConversations as { conversationId: string; title: string }[];
}

/** batch26：`GET /api/bindings/{id}/projection/_meta` —— **投射面的入口门控**
 *  （AD-126：能力入口只看入口，不看某次读数成没成）。`supported:false` 时带
 *  `reason`（`projection_unsupported` | `driver_not_registered`），界面上一律
 *  「什么都不显示」，不摆一个按下去必然报错的按钮。 */
export interface ProjectionMetaWire {
  bindingId: string;
  supported: boolean;
  /** 缺哪个 Driver 方法（后端给才有，且可能是 null）。 */
  missingMethod?: string | null;
  reason?: string;
}

export function fetchProjectionMeta(
  bindingId: string,
  signal?: AbortSignal,
): Promise<ProjectionMetaWire> {
  return request(`/api/bindings/${encodeURIComponent(bindingId)}/projection/_meta`, { signal });
}

export function fetchBackends(signal?: AbortSignal): Promise<{ backends: BackendWire[]; count: number }> {
  return request("/api/backends", { signal });
}

/* ---- 能力（项目详情页 ③ 区） ---- */

export interface EffectiveCapabilityEntryWire {
  capabilityType: string;
  capabilityId: string;
  config: Record<string, unknown>;
  version: string | null;
  /** 这一行的验证等级（AD-73 的第三根轴）。后端还没有普遍下发，缺省当作"验证过"
   *  处理；显式为 `unknown` 的行按 AD-71 静默隐藏（见 `lib/capabilityRows.ts`）。 */
  verification?: "declared" | "bench" | "live" | "unknown" | null;
  sourceProjectId: string;
  inherited: boolean;
  overridden: boolean;
  contributingProjectIds: string[];
  blocked: false;
}

export interface BlockedCapabilityWire {
  capabilityType: string;
  capabilityId: string;
  blockedByProjectId: string;
  blocked: true;
}

export interface EffectiveCapabilitiesWire {
  projectId: string;
  backendKey: string | null;
  ancestry: string[];
  entries: EffectiveCapabilityEntryWire[];
  blocked: BlockedCapabilityWire[];
  counts: { entries: number; blocked: number };
}

/** Resolver 算出的**有效**能力：本地 + 继承 + 被禁止的（views.py `effective_capabilities_to_wire`）。 */
export function fetchEffectiveCapabilities(
  projectId: string,
  options: { backend?: string | null } = {},
  signal?: AbortSignal,
): Promise<EffectiveCapabilitiesWire> {
  const query = options.backend ? `?backend=${encodeURIComponent(options.backend)}` : "";
  return request(`/api/projects/${encodeURIComponent(projectId)}/effective-capabilities${query}`, { signal });
}

/* ---- 能力的写端点（AD-144） ----------------------------------------- *
 *
 * `PUT /api/projects/{id}/capabilities/{type}/{capId}`  body `{value?, blocked?}`
 * `DELETE` 同路径
 * 两条都返回**更新后的那一条有效能力条目**（被禁止时是 `blocked:true` 的形状）。
 */

function capabilityPath(projectId: string, capabilityType: string, capabilityId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/capabilities/${encodeURIComponent(
    capabilityType,
  )}/${encodeURIComponent(capabilityId)}`;
}

/** 写端点回来的那一条：正常条目，或"这一层把它禁了"的形状。 */
export type CapabilityWriteResultWire = EffectiveCapabilityEntryWire | BlockedCapabilityWire;

/** 赋值 / 禁止。`blocked:true` = 在本项目禁止（位置式传播到子树，AD-45）。 */
export function putProjectCapability(
  projectId: string,
  capabilityType: string,
  capabilityId: string,
  body: { value?: unknown; blocked?: boolean },
): Promise<CapabilityWriteResultWire> {
  return request(capabilityPath(projectId, capabilityType, capabilityId), { method: "PUT", body });
}

/** 删除本层赋值 / 解除本层的禁止（都是"把这一层的记录去掉"，于是回到继承）。 */
export function deleteProjectCapability(
  projectId: string,
  capabilityType: string,
  capabilityId: string,
): Promise<CapabilityWriteResultWire> {
  return request(capabilityPath(projectId, capabilityType, capabilityId), { method: "DELETE" });
}

export function fetchModelCatalog(backendId: string, bindingId: string, signal?: AbortSignal): Promise<ModelCatalogWire> {
  return request(
    `/api/backends/${encodeURIComponent(backendId)}/models?binding=${encodeURIComponent(bindingId)}`,
    { signal },
  );
}

export function createConversation(projectId: string, body: { bindingId: string; title?: string }): Promise<ConversationWire> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/conversations`, {
    method: "POST",
    body,
  });
}

/** 归档一条会话（batch11-backend 新增）：归档后 `GET /api/conversations` 不再返回它。
 *  草稿页首条消息发送失败时用来收拾刚建出来的空会话（第 4 件）——**失败也无妨**，
 *  调用方一律吞掉异常：留一条空会话比把用户的文字弄丢轻得多。 */
export function deleteConversation(conversationId: string): Promise<{ conversationId: string; deleted: boolean }> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
}

/** 会话页只准拿能力的 **ui 层**（边界③）：返回类型里根本没有 detail，写错是编译错误。 */
export function fetchBackendUiCapabilities(backendId: string, signal?: AbortSignal): Promise<UiCapabilities> {
  return request<{ capabilities: { ui: UiCapabilities } }>(
    `/api/backends/${encodeURIComponent(backendId)}`,
    { signal },
  ).then((wire) => wire.capabilities.ui);
}

/** 项目详情页的能力清单要 detail 层（AD-71：全站只有那一处显示 note）。 */
export function fetchBackendWithCapabilityDetail(backendId: string, signal?: AbortSignal): Promise<BackendWire> {
  return request(`/api/backends/${encodeURIComponent(backendId)}`, { signal });
}

export function fetchConversation(conversationId: string, signal?: AbortSignal): Promise<ConversationDetailWire> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}`, { signal });
}

/* batch30 第 1 件：SSE 的非流式替身。同一批信封，普通 Bearer 请求拿——隧道下
   长连接一个字节都不到时（真机 P1），页面靠它照旧看得见历史与新事件。 */
export interface EventSnapshotWire {
  conversationId: string;
  events: AgentEventEnvelopeWire[];
  lastSequence: number;
  /** 后端一次给不完（上限 2000）：客户端立刻带新游标再取一次。 */
  truncated?: boolean;
  runState?: string | null;
}

/** 信封在这一层不做结构约束：它原样进 reducer，形状由 `lib/timeline` 认。 */
type AgentEventEnvelopeWire = { sequence: number } & Record<string, unknown>;

export function fetchEventsSnapshot(
  conversationId: string,
  since: number | null,
  signal?: AbortSignal,
): Promise<EventSnapshotWire> {
  const query = since !== null && since >= 0 ? `?since=${encodeURIComponent(String(since))}` : "";
  return request(`/api/conversations/${encodeURIComponent(conversationId)}/events/snapshot${query}`, {
    signal,
  });
}

/** 中断当前一轮（★ 定稿 G-6 的「停止」）。
 *  能力 `card.interrupt` 不是 supported 时页面根本不显示停止按钮（AD-71），
 *  所以这条只会在引擎自己说得动的时候被调到。 */
export function interruptConversation(
  conversationId: string,
  expectedRunId?: string | null,
): Promise<{
  conversationId: string;
  interrupted: boolean;
  /** batch17-backend：中断时后端还有没有活跃 runtime。 */
  active?: boolean;
  /** 后端已经把"其实没在跑"这件事对账掉了（无 runtime 时 200 而不是报错）。
   *  为 true 时页面直接收敛成空闲，不用等那 8 秒确认窗口。 */
  reconciled?: boolean;
}> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}/interrupt`, {
    method: "POST",
    ...(expectedRunId ? { body: { expectedRunId } } : {}),
  });
}

/** 答审批 / 答提问 / 答认证（`interactionId` 就是事件里的 `requestId`）。 */
export function resolveInteraction(
  conversationId: string,
  interactionId: string,
  body: { kind: "permission" | "question" | "authentication"; optionId?: string | null; optionIds?: string[]; text?: string | null; cancelled?: boolean },
): Promise<{ conversationId: string; interactionId: string; resolved: boolean }> {
  return request(
    `/api/conversations/${encodeURIComponent(conversationId)}/interactions/${encodeURIComponent(interactionId)}`,
    { method: "POST", body },
  );
}

/* ------------------------------------------------------------------ *
 * 双表面交接（Phase 4：Card ⇄ External CLI）
 * ------------------------------------------------------------------ *
 * 四条端点对应 `kernel/app/api/session_router.py` 的 surface 段。错误码前端**按 code
 * 分支**（不按文案）：409 `card_running` / `lease_held` / `external_active` /
 * `surface_external_active`，501 `external_cli_unsupported` /
 * `native_session_precreate_unsupported`。
 */

/** 一次终端启动（`launch_to_wire`）。不含任何 Secret。 */
export interface TerminalLaunchWire {
  id: string;
  launcher: string;
  commandSummary: string;
  correlationId?: string | null;
  externalProcessRef: string | null;
  status: string;
  /** 退出码；还没退出就是 null。 */
  exitStatus: number | null;
  /** false = 启动器降级了（这台机器上没有可用的 `open`/终端）——命令要用户自己粘。 */
  launched: boolean;
  envPassthrough?: string[];
  launchedAt: string;
  exitedAt: string | null;
}

/** 写权租约。`owner` 是 `card` | `external-cli`。 */
export interface SurfaceLeaseWire {
  owner: string;
  ownerId: string | null;
  acquiredAt: string;
  heartbeatAt: string;
  expiresAt: string | null;
  stale: boolean;
  launchId: string | null;
}

export interface ExternalSurfaceWire {
  conversationId: string;
  surface: "external-cli" | "card";
  launch: TerminalLaunchWire | null;
  lease: SurfaceLeaseWire | null;
  /** 降级时唯一能用的东西：让用户自己粘到终端里。 */
  commandSummary: string | null;
  launched: boolean;
  reason: string | null;
}

export interface CardSurfaceWire {
  conversationId: string;
  surface: "card";
  reconciled: boolean;
  /** 折进来的条目（与 `kaus/history.reconciled` 的 data.entries 同形，batch15-backend）；
   *  条数在 `entryCount`。 */
  entries: unknown[];
  entryCount?: number;
  lastEntryId?: string | null;
  complete: boolean;
  gaps: string[];
}

export interface SurfaceStateWire {
  conversationId: string;
  surface: "card" | "external-cli";
  lease: SurfaceLeaseWire | null;
  launch: TerminalLaunchWire | null;
}

/** 交给外部终端。`force` = 强制接管（v1.0 §8.6：二次确认在前端，风险在这条 query 上）。 */
export function openExternalSurface(
  conversationId: string,
  options: { force?: boolean } = {},
): Promise<ExternalSurfaceWire> {
  const query = options.force ? "?force=1" : "";
  return request(`/api/conversations/${encodeURIComponent(conversationId)}/surface/external${query}`, {
    method: "POST",
  });
}

/** 收回站内写权并校准原生历史。`force` = 终端还开着也抢回来。 */
export function returnToCardSurface(
  conversationId: string,
  options: { force?: boolean } = {},
): Promise<CardSurfaceWire> {
  const query = options.force ? "?force=1" : "";
  return request(`/api/conversations/${encodeURIComponent(conversationId)}/surface/card${query}`, {
    method: "POST",
  });
}

/** 现在谁在写。刷新后靠它恢复外部态；端点不在（未装 Launcher → 501/404）时
 *  调用方按"站内"处理，不显示错误——AD-71 的同一条精神。 */
export function fetchSurface(conversationId: string, signal?: AbortSignal): Promise<SurfaceStateWire> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}/surface`, { signal });
}

/** 终端启动历史（页头「历史 ▾」）。`count === 0` 时那枚菜单不渲染。 */
export function fetchLaunches(
  conversationId: string,
  signal?: AbortSignal,
): Promise<{ conversationId: string; launches: TerminalLaunchWire[]; count: number }> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}/launches`, { signal });
}

/** `clientRef` 是可选的对账编号（AD-94）：给了，内核就把它原样放进
 *  `kaus` / `user.message` 事件的 `data.clientRef`，页面据此撤掉本地占位。 */
export function sendConversationMessage(
  conversationId: string,
  text: string,
  clientRef?: string,
  attachments?: import("./attachmentsApi").ConversationAttachment[],
): Promise<{ conversationId: string; runId: string | null; runIdPending: boolean; acceptedAfterSequence: number | null }> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: "POST",
    body: { text, ...(clientRef ? { clientRef } : {}), ...(attachments?.length ? { attachments } : {}) },
  });
}

export interface ArchivedConversationWire {
  id: string;
  title: string;
  projectId: string;
  backendId: string | null;
  updatedAt: string;
  archivedAt: string;
}

export function fetchArchivedConversations(): Promise<{ conversations: ArchivedConversationWire[] }> {
  return request("/api/conversations?archived=true&limit=500");
}

export async function archiveConversation(conversationId: string, archived = true): Promise<{ conversation: ConversationWire }> {
  const result = await request<{ conversation: ConversationWire }>(`/api/conversations/${encodeURIComponent(conversationId)}/archive`, {
    method: "PATCH", body: { archived },
  });
  window.dispatchEvent(new Event("kaus:conversations-changed"));
  return result;
}
