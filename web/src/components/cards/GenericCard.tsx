/* 通用卡（N §8.2 + component-boundaries 边界②的 default 分支）。
 *
 * 两个用途：
 *   1. **兜底**：信封版本比前端新时，不认识的条目类型也要看得见——不许 return null、
 *      不许抛错、不许渲染一段空白。用户应该看到"这里有个东西我暂时不认识"。
 *   2. 已知但还没有专属卡片的条目（计划 / 终端 / 文件 / 产物 / 生命周期）：
 *      标题 + 一段等宽正文，够读就行。合并时若那几种卡片被单独实现，
 *      把 CardRenderer 里对应的 case 改掉即可，本组件仍留给兜底用。
 */

import { cream } from "../ui";
import { CardShell, MonoBlock, type EngineLabel } from "./CardShell";

export function GenericCard({
  title,
  subtitle,
  body,
  engine,
  time,
  collapsed = false,
}: {
  title: string;
  subtitle?: string | null;
  /** 等宽正文：终端输出 / diff / 原始 JSON。 */
  body?: string | null;
  engine?: EngineLabel;
  time?: string;
  collapsed?: boolean;
}) {
  return (
    <CardShell
      engine={engine}
      type={title}
      time={time}
      collapsible={Boolean(body)}
      defaultCollapsed={collapsed}
    >
      {subtitle && (
        <p className="mb-1 text-xs leading-relaxed" style={{ color: cream(62) }}>
          {subtitle}
        </p>
      )}
      {body && <MonoBlock>{body}</MonoBlock>}
    </CardShell>
  );
}

/** 生命周期标记：一行不带边框的小字（`session.created` / `run.completed` 之类）。 */
export function LifecycleMarker({ text, time }: { text: string; time?: string }) {
  return (
    <div className="flex items-center gap-2 px-1 py-0.5 text-[0.65rem]" style={{ color: cream(40) }}>
      <span className="h-px flex-1" style={{ background: cream(11) }} />
      <span>{text}</span>
      {time && <span className="tabular-nums">{time}</span>}
      <span className="h-px flex-1" style={{ background: cream(11) }} />
    </div>
  );
}
