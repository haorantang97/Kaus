/* `/dev/cards` —— 卡片与「已接引擎」面板的状态一览（仅开发环境，见 main.tsx 的分流）。
 *
 * 每种卡片的每种状态各一份；深浅色可切。数据来自两处，都不是手写的：
 *   - 事件序列：`__fixtures__/events.*.json`（内核 Mock 剧本导出）；
 *   - 能力预设：`__fixtures__/capabilities.json`（内核 drivers/mock/capabilities.py 的五档）。
 * 也就是说这一页展示的是"引擎真会发的东西"，不是为了好看捏的数据。
 */

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";

import { Button, Pill, cream } from "../ui";
import { CardRenderer } from "./CardRenderer";
import { ErrorCard } from "./ErrorCard";
import { GenericCard, LifecycleMarker } from "./GenericCard";
import { PermissionCard } from "./PermissionCard";
import { QuestionCard } from "./QuestionCard";
import { ReasoningCard } from "./ReasoningCard";
import { TextCard } from "./TextCard";
import { ToolCard } from "./ToolCard";
import { UsageBar, UsagePill } from "./UsageBar";
import { ALL_SUPPORTED_UI_CAPABILITIES, hasCapability, type UiCapabilities } from "./capabilities";
import {
  EnginePanel,
  type BackendWire,
  type BindingWire,
  type DetailCapabilityTree,
  type EngineRow,
} from "../EnginePanel";
import {
  initialTimelineState,
  reduceEvents,
  type AgentEventEnvelope,
  type AnyTimelineItem,
  type InteractionItem,
  type MessageItem,
  type ToolItem,
} from "./timelineReducer";

import capabilityPresets from "./__fixtures__/capabilities.json";
import fullLifecycle from "./__fixtures__/events.full-lifecycle.json";
import permissionEvents from "./__fixtures__/events.permission-roundtrip.json";
import questionEvents from "./__fixtures__/events.question-roundtrip.json";
import authEvents from "./__fixtures__/events.authentication-roundtrip.json";
import streamingReasoning from "./__fixtures__/events.streaming-reasoning.json";
import textStream from "./__fixtures__/events.text-stream.json";
import streamingToolOutput from "./__fixtures__/events.streaming-tool-output.json";
import extensionEvents from "./__fixtures__/events.extension-event.json";

const ENGINE = { label: "MK", name: "Mock Engine" };

type Preset = { ui: UiCapabilities; detail: DetailCapabilityTree; unknownCount: number };
const PRESETS = capabilityPresets as unknown as Record<string, Preset>;

function reduce(events: unknown[], count?: number) {
  const slice = count === undefined ? events : events.slice(0, count);
  return reduceEvents(initialTimelineState("preview"), slice as AgentEventEnvelope[]);
}

function itemOf<T extends AnyTimelineItem>(events: unknown[], kind: T["kind"], count?: number): T {
  return reduce(events, count).items.find((item) => item.kind === kind) as T;
}

/* --- 页面骨架 ------------------------------------------------------ */

function Row({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <div className="mb-2 flex items-baseline gap-2">
        <h3 className="text-[0.7rem] font-bold uppercase tracking-[0.16em]" style={{ color: "var(--midground-base)" }}>
          {title}
        </h3>
        {note && (
          <span className="text-[0.65rem]" style={{ color: cream(45) }}>
            {note}
          </span>
        )}
      </div>
      <div className="grid gap-3">{children}</div>
    </section>
  );
}

export function CardsPreview() {
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [presetKey, setPresetKey] = useState<string>("default");

  useEffect(() => {
    document.body.classList.toggle("dark", theme === "dark");
    document.documentElement.style.colorScheme = theme;
  }, [theme]);

  // 主应用是"一屏工作台"：index.css 把 html / body / #root 都锁成 100dvh + overflow:hidden。
  // 预览页是一长条清单，得解开这三层才滚得动（也才截得全）。离开时原样还回去。
  useEffect(() => {
    const targets = [document.documentElement, document.body, document.getElementById("root")];
    const saved = targets.map((node) => node?.getAttribute("style") ?? null);
    for (const node of targets) {
      if (!node) continue;
      node.style.height = "auto";
      node.style.maxHeight = "none";
      node.style.overflow = "visible";
    }
    return () => {
      targets.forEach((node, index) => {
        if (!node) return;
        if (saved[index] === null) node.removeAttribute("style");
        else node.setAttribute("style", saved[index] as string);
      });
    };
  }, []);

  const preset = PRESETS[presetKey];
  const caps = preset?.ui ?? ALL_SUPPORTED_UI_CAPABILITIES;

  // 各种状态的样本（都从 fixture 事件流里截出来）。
  // 流式中：text-stream 剧本停在第 5 条事件（两片 delta 已到、还没 completed）。
  const streamingMessage = itemOf<MessageItem>(textStream, "message", 5);
  const doneMessage = itemOf<MessageItem>(streamingReasoning, "message");
  const runningTool = itemOf<ToolItem>(streamingToolOutput, "tool", 4);
  const doneTool = itemOf<ToolItem>(streamingToolOutput, "tool");
  const failedTool: ToolItem = { ...doneTool, status: "failed", output: "exit 1: build failed" };
  /* batch17 第 2 件：真机上的那条（引擎的 SSE 只带 preview）与它回填之后的样子。 */
  const previewOnlyTool: ToolItem = {
    ...doneTool,
    itemId: "tool:preview-only",
    name: "terminal",
    input: { preview: "pwd + 2 commands" },
    output: null,
    progress: null,
  };
  const backfilledTool: ToolItem = {
    ...previewOnlyTool,
    itemId: "tool:backfilled",
    output: "/Users/example\nDesktop  Documents  Downloads",
    progress: {
      source: "native_history",
      nativeCallId: "call_01",
      name: "terminal",
      input: { command: "pwd", workdir: "/Users/example", timeoutMs: 120000 },
    },
  };
  const pendingPermission = itemOf<InteractionItem>(permissionEvents, "interaction", 3);
  const resolvedPermission = itemOf<InteractionItem>(permissionEvents, "interaction");
  const pendingQuestion = itemOf<InteractionItem>(questionEvents, "interaction", 2);
  const pendingAuth = itemOf<InteractionItem>(authEvents, "interaction", 2);
  const fullState = reduce(fullLifecycle);
  const extensionState = reduce(extensionEvents);

  const engineRows: EngineRow[] = Object.entries(PRESETS).map(([key, value], index) => {
    const backend: BackendWire = {
      id: `backend:mock-${key}`,
      key: `mock-${key}`,
      displayName: `Mock ${key}`,
      driverKind: "mock",
      installed: key !== "unprobed",
      version: key === "unprobed" ? null : "0.1.0",
      probeState:
        key === "unprobed"
          ? "unknown"
          : key === "lean"
            ? "degraded"
            : key === "unverified-approval"
              ? "unavailable"
              : "available",
      capabilities: { ui: value.ui, detail: value.detail, unknownCount: value.unknownCount },
      unknownCount: value.unknownCount,
    };
    const binding: BindingWire = {
      id: `binding:preview:mock-${key}:main`,
      projectId: "project:preview",
      backendId: backend.id,
      displayName: `Mock ${key}`,
      nativeScopeRef: `native-scope-${key}`,
      enabled: key !== "unverified-approval",
      isDefault: index === 0,
      defaultModelId: key === "unprobed" ? null : "mock-large",
      runtimeConfig: { reasoning_effort: "medium", twin_mode: index === 1 ? "架构师模式 · 高权限" : "" },
      compatibilityState: "unknown",
    };
    return {
      binding,
      backend,
      nativeSessionCount: key === "default" ? 3 : null,
      auth: { state: key === "default" ? "signed_in" : "signed_out", account: "mock@example.com" },
    };
  });

  return (
    <div className="min-h-dvh" style={{ background: "var(--background-base)", color: "var(--midground-base)" }}>
      <header className="flex flex-wrap items-center gap-3 px-6 py-4" style={{ borderBottom: `1px solid ${cream(13)}` }}>
        <span className="text-sm font-bold uppercase tracking-[0.16em]">卡片预览 · /dev/cards</span>
        <span className="flex items-center gap-1.5">
          {Object.keys(PRESETS).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => setPresetKey(key)}
              className="rounded px-2 py-1 text-[0.65rem]"
              style={{
                border: `1px solid ${cream(key === presetKey ? 38 : 13)}`,
                color: key === presetKey ? "var(--accent-text)" : cream(55),
              }}
            >
              {key}
            </button>
          ))}
        </span>
        <span className="ml-auto">
          <Button onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            <span className="inline-flex items-center gap-1">
              {theme === "dark" ? <Sun className="size-3" /> : <Moon className="size-3" />}
              {theme === "dark" ? "浅色" : "深色"}
            </span>
          </Button>
        </span>
      </header>

      <main className="mx-auto max-w-[1100px] px-6 py-6">
        <p className="mb-5 text-xs leading-relaxed" style={{ color: cream(55) }}>
          能力预设决定"渲不渲染"：切到 <code>lean</code> 看工具卡输出栏整块缺席、审批卡消失；
          切到 <code>unprobed</code> 看全部 unknown 的形态（AD-71：unknown 与 unsupported 一样不渲染）。
        </p>

        <Row title="文字卡" note="流式中 / 已完成">
          <TextCard item={streamingMessage} />
          <TextCard item={doneMessage} time="14:32" />
        </Row>

        <Row title="工具卡" note="运行中 / 完成 / 失败 / 输出栏缺席（card.tools.output ≠ supported）">
          <ToolCard item={runningTool} showOutput />
          <ToolCard item={doneTool} showOutput seconds={1.2} defaultOpen />
          <ToolCard item={failedTool} showOutput seconds={0.4} />
          <ToolCard item={doneTool} showOutput={false} />
        </Row>

        {/* batch17 第 2 件：展开态的两种形态。左边是"引擎只给了 preview"（回填前），
            右边是同一条在 `tool.updated` 回填之后（AD-133）。两边都不摆原始 JSON。 */}
        <Row title="工具行展开态（batch17）" note="回填前：只有 preview → 一行「命令：…」；回填后：键值对 + 行尾中性小点">
          <ToolCard item={previewOnlyTool} showOutput seconds={8.0} defaultOpen />
          <ToolCard item={backfilledTool} showOutput seconds={8.0} defaultOpen />
        </Row>

        <Row title="审批卡" note="待答（边框加重）/ 已答 / 认证请求">
          <PermissionCard item={pendingPermission} engine={ENGINE} onRespond={() => undefined} />
          <PermissionCard item={resolvedPermission} engine={ENGINE} time="14:34" />
          <PermissionCard item={pendingAuth} engine={ENGINE} onRespond={() => undefined} />
        </Row>

        <Row title="提问卡" note="选项 + 自由文本">
          <QuestionCard item={pendingQuestion} engine={ENGINE} onRespond={() => undefined} />
        </Row>

        <Row title="推理卡" note="默认折叠 / 展开 / 只有状态摘要">
          <ReasoningCard text="先看输入，再想一步，得出结论。" seconds={7} />
          <ReasoningCard text="先看输入，再想一步，得出结论。" seconds={7} defaultCollapsed={false} />
          <ReasoningCard status="thinking" summary="正在规划" defaultCollapsed={false} />
        </Row>

        <Row title="报错 / 诊断卡" note="错误（可重试、可展开原始错误）/ 警告 / 提示">
          <ErrorCard
            summary="Mock Driver 故意失败"
            code="mock.boom"
            detail={'{\n  "code": "mock.boom",\n  "retriable": false\n}'}
            engine={ENGINE}
            onRetry={() => undefined}
          />
          <ErrorCard summary="即将失败" tone="warn" />
          <ErrorCard summary="已切换到备用通道" tone="info" />
        </Row>

        <Row title="用量条" note="只渲染引擎报了的字段；一项都没有时整行不渲染">
          <UsageBar usage={fullState.usage} model="mock-large" durationMs={4200} />
          <UsagePill usage={fullState.usage} />
          <UsageBar usage={{ contextUsed: 12000, contextWindow: 200000 }} />
          <UsageBar usage={null} />
        </Row>

        <Row title="通用卡 / 生命周期" note="未知条目必须看得见（N §8.2）">
          {extensionState.items
            .filter((item) => item.kind === "extension")
            .map((item) => (
              <CardRenderer key={item.itemId} item={item} caps={caps} engine={ENGINE} />
            ))}
          <GenericCard title="未知条目 · future" subtitle="来自比前端更新的信封版本" body='{"shape":"unknown"}' />
          <LifecycleMarker text="run.completed" time="14:35" />
        </Row>

        <Row title="整条时间线" note="full-lifecycle 剧本经 reducer 归并后按 kind 分发">
          {fullState.items.map((item, index) => (
            <CardRenderer
              key={item.itemId}
              item={item}
              caps={caps}
              engine={index === 0 ? ENGINE : undefined}
              callbacks={{ onPermissionRespond: () => undefined, onQuestionRespond: () => undefined }}
            />
          ))}
          {/* 用量条由页面（而不是 CardRenderer）渲染，因此 card.usage 的门也在页面这一层。 */}
          {hasCapability(caps.card.usage) && <UsageBar usage={fullState.usage} model="mock-large" />}
        </Row>

        <Row title="已接引擎面板" note="五档能力预设各一张卡；note 只在这里出现">
          <EnginePanel rows={engineRows} onNewConversation={() => undefined} onSignIn={() => undefined} />
          <EnginePanel rows={[]} />
        </Row>

        <div className="py-6 text-[0.65rem]" style={{ color: cream(38) }}>
          <Pill tone="muted">dev</Pill> 这一页不进生产构建的路由，仅供开发与截图。
        </div>
      </main>
    </div>
  );
}
