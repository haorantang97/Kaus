/* 时间线的**唯一**类型分发点（component-boundaries 边界②）。
 *
 * 一个 switch (item.kind)，default 分支返回 <GenericCard>——不许抛错、不许 return null、
 * 不许渲染空白：协议会演进，前端比后端旧的时候用户该看到"这里有个东西我暂时不认识"。
 *
 * AD-126（批次十五第 1 件）：**能力门控只管"入口"，不管"已到达的事件"。**
 * 事件已经到了时间线上，它本身就是该能力存在的证明——网关掉线导致能力矩阵全
 * `unknown` 时，如果还按 AD-71 静默隐藏，用户会看到一条"少了几行"的会话，
 * 那是比"多显示一张卡"严重得多的失真。所以这棵子树里**没有一处能力门控**：
 * 到了就渲染，`showOutput` 也只看"有没有 output"。
 * 能力矩阵继续决定的是入口：工具栏项、按钮、页头入口（那些仍按 AD-71）。
 * 这棵子树仍只读能力的 **ui 层**（边界③），因此不可能出现 note / verification。
 * 全文没有任何引擎名分支：引擎信息只有调用方传进来的 `engine`（字母标签 + 显示名）。
 *
 * 合并说明：`sidebar-and-draft` 的 ConversationPage 有注入点
 * `renderTimelineItem: (item: TimelineItem) => ReactNode`，接法是
 *   renderTimelineItem={(item) => <CardRenderer item={item} caps={uiCaps} engine={engine} />}
 */

import type { ReactNode } from "react";

import { useLocale } from "../../i18n";
import { hasExtensionCard } from "../../lib/timeline/extensionCards";
import type { UiCapabilities } from "./capabilities";
import type { EngineLabel } from "./CardShell";
import { MonoBlock, stringify } from "./CardShell";
import { cleanLine, RowCard } from "./RowCard";
import { ErrorCard, toneForLevel } from "./ErrorCard";
import { GenericCard, LifecycleMarker } from "./GenericCard";
import { PermissionCard } from "./PermissionCard";
import { QuestionCard } from "./QuestionCard";
import { ReasoningCard } from "./ReasoningCard";
import { TextCard, type MessageActions } from "./TextCard";
import { ToolCard } from "./ToolCard";
import { FileReference } from "./ArtifactPreview";
import { isConfirmedMessageRejection } from "../../lib/messageDelivery";
import { useContentText } from "./contentStrings";
import {
  MODEL_ADOPTED_NAME,
  MODEL_ADOPTED_NAMESPACE,
  USER_MESSAGE_FAILED_NAME,
  USER_MESSAGE_NAMESPACE,
  messageHasReasoning,
  messageReasoningText,
  messageText,
  type AnyTimelineItem,
  type InteractionItem,
} from "./timelineReducer";

/** `kaus/model.adopted` 的载荷。`to` 缺席 = 这条通知说不出换成了什么，整条不渲染。 */
function modelAdoption(data: unknown): { from: string | null; to: string } | null {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const row = data as { from?: unknown; to?: unknown };
  if (typeof row.to !== "string" || row.to === "") return null;
  return { from: typeof row.from === "string" && row.from ? row.from : null, to: row.to };
}

/** `kaus/user.message.failed` 的载荷里那句人话。缺席就不编一个出来。 */
function failureReason(data: unknown): string | null {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const message = (data as { message?: unknown }).message;
  return typeof message === "string" && message ? message : null;
}

export interface CardCallbacks {
  /** 审批 / 认证的作答。缺省不传 = 只读回放（历史重放时按钮不接线）。 */
  onPermissionRespond?: (item: InteractionItem, optionId: string) => void;
  onQuestionRespond?: (item: InteractionItem, answer: { optionId?: string; optionIds?: string[]; text?: string; cancelled?: boolean }) => void;
  onRetry?: (item: AnyTimelineItem) => void;
  /* 批次十六第 4 件：消息悬停动作。调用方按"这一条能做什么"逐条给（助手能重发
     上一条、用户能编辑重发）；运行中与外部态下返回 null，整块不渲染。 */
  messageActions?: (item: AnyTimelineItem) => MessageActions | null;
}

export function CardRenderer({
  item,
  engine,
  time,
  seconds,
  live = false,
  showLifecycle = false,
  callbacks = {},
  conversationId,
}: {
  item: AnyTimelineItem;
  /** 该会话所属 Binding 的引擎能力，**ui 层**。AD-126 之后这棵子树不再读它
   *  （事件到了就渲染），保留在签名里是为了调用方不必改，也留着以后按能力
   *  调整"渲染成什么"（而不是"渲不渲染"）的余地。 */
  caps?: UiCapabilities;
  /** 纯展示的引擎标签；连续同一引擎时上层可以只给第一张卡传（DESIGN §6.1）。 */
  engine?: EngineLabel;
  time?: string;
  /** 页面按信封时间算出来的这条目耗时（内核 item 不存时间戳）。 */
  seconds?: number;
  /** 这一条就是时间线末尾、且这一轮还在跑：推理行显示「正在思考…」（G-4）。 */
  live?: boolean;
  showLifecycle?: boolean;
  callbacks?: CardCallbacks;
  conversationId?: string;
}): ReactNode {
  // 订阅界面语言：切语言时整棵卡片子树跟着重渲染（AD-96）。
  const { t, tDynamic } = useLocale();
  const c = useContentText();
  switch (item.kind) {
    case "message": {
      const reasoning = messageReasoningText(item);
      const showReasoning = messageHasReasoning(item);
      /* 正文还没开始吐、这一条又在末尾 = 引擎还在想（G-4 的运行中态）。 */
      const thinking = live && messageText(item) === "";
      return (
        <>
          {showReasoning && <ReasoningCard text={reasoning} running={thinking} seconds={seconds} />}
          <TextCard item={item} time={time} actions={callbacks.messageActions?.(item) ?? null} conversationId={conversationId} />
        </>
      );
    }

    case "reasoning":
      return <ReasoningCard status={item.status} summary={item.summary} seconds={seconds} running={live} />;

    case "plan": {
      /* 批次十三：一行 `▸ 计划 · 2/5 完成`，展开列步骤。 */
      const done = item.entries.filter((entry) => entry.status === "completed").length;
      return (
        <RowCard
          testId="plan-row"
          name={t("card.plan.row")}
          preview={t("card.plan.progress", { done, total: item.entries.length })}
          time={time}
        >
          <ul className="kaus-plan-list">
            {item.entries.map((entry, index) => (
              <li key={entry.entryId ?? `${index}`} data-status={entry.status ?? "pending"}>
                <span className="kaus-plan-status">
                  {tDynamic(`card.plan.status.${entry.status ?? "pending"}`, entry.status ?? "pending")}
                </span>
                {entry.content}
              </li>
            ))}
          </ul>
        </RowCard>
      );
    }

    case "tool":
      /* 有 output 就显示 output——不再问能力（AD-126）。 */
      return <ToolCard item={item} seconds={seconds} conversationId={conversationId} />;

    case "terminal":
      /* 批次十三：一行 `▸ 终端 · <命令>`，展开是输出。 */
      return (
        <RowCard
          testId="terminal-row"
          name={t("card.terminal.row")}
          preview={cleanLine(item.command)}
          time={time}
        >
          {item.output ? <MonoBlock max={320}>{item.output}</MonoBlock> : null}
        </RowCard>
      );

    case "file":
      /* 批次十三：一行 `▸ 修改了 a.txt`，展开是 diff。连续多条由页面合并成
         `▸ 修改了 3 个文件`（分组是版式，不进 reducer）。 */
      return (
        <RowCard
          testId="file-row"
          name={t("card.file.row", { path: cleanLine(item.path) ?? item.itemId })}
          time={time}
        >
          {item.diff ? <MonoBlock max={320}>{item.diff}</MonoBlock> : null}
          {item.path && <FileReference source={{ uri: item.path }} conversationId={conversationId} />}
        </RowCard>
      );

    case "artifact":
      // A delivered file is a direct preview action, not a URI hidden behind two clicks.
      return (
        <div className="kaus-artifact-row" data-testid="artifact-row">
          {item.artifact.uri
            ? <FileReference source={{ ...item.artifact, uri: item.artifact.uri }} conversationId={conversationId} />
            : <span>{item.artifact.title || item.artifact.artifactId}</span>}
          {time && <time>{time}</time>}
        </div>
      );

    case "interaction": {
      if (item.interactionKind === "question") {
        return (
          <QuestionCard
            item={item}
            engine={engine}
            time={time}
            onRespond={
              callbacks.onQuestionRespond
                ? (answer) => callbacks.onQuestionRespond?.(item, answer)
                : undefined
            }
          />
        );
      }
      return (
        <PermissionCard
          item={item}
          engine={engine}
          time={time}
          onRespond={
            callbacks.onPermissionRespond
              ? (optionId) => callbacks.onPermissionRespond?.(item, optionId)
              : undefined
          }
        />
      );
    }

    case "diagnostic":
      return (
        <ErrorCard
          summary={item.message}
          tone={toneForLevel(item.level)}
          engine={engine}
          time={time}
        />
      );

    case "extension":
      /* 批次二十八第 4 件（AD-71）：没有专属卡的扩展事件**整条不渲染**。
         真机 ACP 会话里 `available_commands_update` / `session_info_update`
         每一条都排出一行「Extension event」，那一行既没有标题也没有内容，
         只是把对话挤散。白名单在 `lib/timeline/extensionCards.ts`。
         页面那一层（ConversationPage）连包裹它的 <div> 也一并去掉。 */
      if (!hasExtensionCard(item.namespace, item.name)) return null;
      /* batch31 / AD-155：模型采纳是一行**中性系统提示**，不是一张可展开的卡。
         它不该看起来像错误（发送没有失败），也不该看起来像回答（引擎没说话）——
         所以用生命周期那一行的版式：一条细线夹一句小字。 */
      /* batch38 / R6：`kaus/user.message.failed` 走到这里，只有一种情况——reducer
         没能按 `clientRef` 配到那条用户消息（编号缺席且没有可标的消息、或历史被截断）。
         那就留一行中性系统提示：不指认是哪一句（凭空指认比不指认更糟），但也绝不
         静默丢掉——一次没送到的发送被藏起来，界面就等于骗人说它发出去了。 */
      if (item.namespace === USER_MESSAGE_NAMESPACE && item.name === USER_MESSAGE_FAILED_NAME) {
        const reason = failureReason(item.data);
        const code = item.data && typeof item.data === "object" ? (item.data as { code?: string }).code : undefined;
        const confirmed = isConfirmedMessageRejection({ failureCode: code });
        return (
          <LifecycleMarker
            text={
              !confirmed
                ? `${c("chat.content.deliveryUnconfirmed")} · ${c("chat.content.deliveryCheckFirst")}${reason ? ` · ${reason}` : ""}`
                : reason
                ? t("timeline.userMessageFailed", { reason })
                : t("timeline.userMessageFailed.noReason")
            }
            time={time}
          />
        );
      }
      if (item.namespace === MODEL_ADOPTED_NAMESPACE && item.name === MODEL_ADOPTED_NAME) {
        const adopted = modelAdoption(item.data);
        if (!adopted) return null;
        return (
          <LifecycleMarker
            text={
              adopted.from
                ? t("timeline.modelAdopted", { to: adopted.to, from: adopted.from })
                : t("timeline.modelAdopted.noFrom", { to: adopted.to })
            }
            time={time}
          />
        );
      }
      return (
        <GenericCard
          title={genericTitle(item, t)}
          subtitle={genericSubtitle(item)}
          body={stringify(item.data ?? item)}
          engine={engine}
          time={time}
          collapsed
        />
      );

    case "lifecycle":
      if (!showLifecycle) return null;
      return <LifecycleMarker text={`${item.eventType}${item.detail ? ` · ${item.detail}` : ""}`} time={time} />;

    default:
      // 真正**不认识**的 kind：兜底通用卡，永远看得见（协议演进时前端可能比
      // 后端旧，那时"这里有个我不认识的东西"比一片空白诚实）。扩展事件不走
      // 这里——它有自己的分支与白名单。
      return (
        <GenericCard
          title={genericTitle(item, t)}
          subtitle={genericSubtitle(item)}
          body={stringify((item as { data?: unknown }).data ?? item)}
          engine={engine}
          time={time}
          collapsed
        />
      );
  }
}

function genericTitle(item: AnyTimelineItem, t: (key: "card.type.extension" | "card.type.unknown", vars?: Record<string, string | number>) => string): string {
  return item.kind === "extension"
    ? t("card.type.extension")
    : t("card.type.unknown", { kind: item.kind });
}

function genericSubtitle(item: AnyTimelineItem): string | null {
  return item.kind === "extension" ? `${item.namespace}.${item.name}` : item.itemId;
}
