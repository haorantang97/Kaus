/* 能力矩阵的 **ui 层**（AD-73）：每项能力只剩取值字符串。
 *
 * 对话页整棵子树只准读这一层——`note` / `verification` 只存在于 `detail` 层，
 * 而这里的类型根本没有那两个字段，写错了是编译错误而不是 review 才发现的问题
 * （component-boundaries 边界③）。`detail` 层的类型住在 `EnginePanel.tsx` 旁边，
 * 只给项目详情页的能力清单用。
 *
 * 取值真源：`kernel/runtime/capability_matrix.py`（FEATURE_PATHS 23 项、四根枚举轴）。
 */

/** 一项能力的取值。枚举轴上是 `warm` / `own_process` / `tool_boundary` 这类字面量。 */
export type CapabilityValue = string;

export interface UiSessionCapabilities {
  /** none | own_process | all | unknown */
  list: CapabilityValue;
  create: CapabilityValue;
  /** none | warm | cold | unknown */
  resume: CapabilityValue;
  history: CapabilityValue;
  branch: CapabilityValue;
}

export interface UiToolCardCapabilities {
  calls: CapabilityValue;
  output: CapabilityValue;
}

export interface UiCardCapabilities {
  streaming: CapabilityValue;
  tools: UiToolCardCapabilities;
  terminal: CapabilityValue;
  fileChanges: CapabilityValue;
  artifacts: CapabilityValue;
  plan: CapabilityValue;
  reasoning: CapabilityValue;
  /** none | protocol | mirror | unverified | unknown */
  permissions: CapabilityValue;
  questions: CapabilityValue;
  authentication: CapabilityValue;
  usage: CapabilityValue;
  /** none | tool_boundary | immediate | unknown */
  interrupt: CapabilityValue;
  /** 附件上传：`images` | `files` 时输入区才出 📎；其余不渲染（AD-71）。 */
  attachments: CapabilityValue;
}

export interface UiExternalCliCapabilities {
  supported: CapabilityValue;
  resume: CapabilityValue;
}

export interface UiModelCapabilities {
  /** fixed | constrained | open —— 这是形态而不是能力轴，没有 unknown。 */
  mode: string;
  reasoning: CapabilityValue;
  providers: CapabilityValue;
  conversationScoped?: CapabilityValue;
}

export interface UiCapabilities {
  structuredEvents: CapabilityValue;
  sessions: UiSessionCapabilities;
  card: UiCardCapabilities;
  externalCli: UiExternalCliCapabilities;
  models: UiModelCapabilities;
  capabilityProjection: Record<string, string>;
}

/** 本轴的否定值：声明为没有。`unknown` 不在其中，但按 AD-71 同样不渲染。 */
const NEGATIVE: ReadonlySet<string> = new Set(["unsupported", "none"]);

/**
 * 「这项能力确认可用吗」。
 *
 * AD-71 只有两条分支：能用就渲染控件，`unsupported` 与 `unknown` 一律**静默不渲染**
 * ——不留占位、不写备注。所以未声明（undefined）也返回 false。
 */
export function hasCapability(value: CapabilityValue | undefined | null): boolean {
  if (!value) return false;
  return value !== "unknown" && !NEGATIVE.has(value);
}

/** 全部能力都声明为最强档：预览页与「能力还没探测出来之前不该假定有」之外的默认值。 */
export const ALL_SUPPORTED_UI_CAPABILITIES: UiCapabilities = {
  structuredEvents: "supported",
  sessions: {
    list: "all",
    create: "supported",
    resume: "warm",
    history: "supported",
    branch: "supported",
  },
  card: {
    streaming: "supported",
    tools: { calls: "supported", output: "supported" },
    terminal: "supported",
    fileChanges: "supported",
    artifacts: "supported",
    plan: "supported",
    reasoning: "supported",
    permissions: "protocol",
    questions: "supported",
    authentication: "supported",
    usage: "supported",
    interrupt: "immediate",
    attachments: "unknown",
  },
  externalCli: { supported: "supported", resume: "supported" },
  models: { mode: "open", reasoning: "supported", providers: "supported", conversationScoped: "supported" },
  capabilityProjection: {},
};
