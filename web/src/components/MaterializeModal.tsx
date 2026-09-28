/* 「应用到引擎」弹窗（批次二十五，DESIGN ★K）与它的只读孪生「配置漂移」。
 *
 * 后端两条端点（AD-149，`docs/ops/projection.md`）：
 *   `POST /api/bindings/{id}/materialize?confirm=0|1&adopt=0|1`
 *   `GET  /api/bindings/{id}/drift`
 *
 * 四条口径：
 *  1. **先预演再写。** 打开弹窗只做 dry-run（`confirm=0`）：算得出变更集，磁盘一个
 *     字节都不动。真写只发生在按下「备份后写入」那一下。
 *  2. **值只在这里过一眼。** `before` / `after` 是后端脱敏过的、供当场比对的东西，
 *     不进任何缓存、不落 storage；关掉弹窗就没了。要再看就再预演一次。
 *  3. **`adopt` 是用户的动作，不是默认值。** 「当前有值、又不是本仪表盘写的」那些
 *     键默认不覆盖（`factory_protected`）；勾了「接管」才带 `adopt=1` 重新预演。
 *  4. **这个弹窗自己请求数据。** 它是一段有状态的交互流（预演 → 接管 → 写入 →
 *     成功态），不是展示组件；`EnginePanel` 那条"面板不请求数据"的边界因此没被破——
 *     卡片只发出回调，弹窗由容器 `ProjectEngines` 渲染。
 */

import { useCallback, useEffect, useState } from "react";

import { useLocale, type DictKey } from "../i18n";
/* 检查时间的格式化与引擎卡上那一行同源（batch27：两处都要削掉后端 ISO 的微秒位）。 */
import { formatCheckedAt, groupNameKeys } from "./EnginePanel";
import { Button, Modal, ModalSub, ModalTitle, Pill, cream } from "./ui";
import {
  busyConversations,
  describeFailure,
  materializeBinding,
  type BindingDriftWire,
  type DriftItemWire,
  type MaterializeResultWire,
  type ProjectionEntryWire,
  type ProjectionUnsupportedWire,
} from "../lib/sessionApi";

/* ------------------------------------------------------------------ *
 * 值的显示
 * ------------------------------------------------------------------ */

/** 一行装得下多少个字符；超了就截断，「展开」后完整显示（DESIGN ★K）。 */
export const VALUE_PREVIEW = 72;

/** 对象 / 数组折成**一行** JSON；字符串原样；空值交给调用方显示「（空）」。 */
export function formatProjectionValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

/* batch54（真机 PJ-04）：备份文件名里编码着原文件名。
 *
 * 写前备份一律叫 `<原文件名>.kaus-backup-<UTC 时间戳>`（`drivers/projection_store.py`
 * 的 `BACKUP_INFIX`），所以「回滚拷回哪里」不用后端多给一个字段，从这个名字里就
 * 读得出来。此前这句提示把目标写死成 `config.yaml`——物化 MCP 那会儿它确实是唯一
 * 的目标，批次四十四让项目指令投影（写 `AGENTS.md`）复用了同一个弹窗之后，照抄那
 * 条命令的人会把 `AGENTS.md` 的备份盖到当前目录的 `config.yaml` 上。
 *
 * 读不出来（名字不合这个约定）就回 `null`，由调用方换一句不带命令的话——不编一个
 * 路径，那正是这次要修的毛病。 */
const BACKUP_INFIX = ".kaus-backup-";

export function rollbackTarget(backupPath: string | null | undefined): string | null {
  if (!backupPath) return null;
  const cut = backupPath.lastIndexOf(BACKUP_INFIX);
  if (cut <= 0) return null;
  const target = backupPath.slice(0, cut);
  return target.length > 0 ? target : null;
}

const MONO: React.CSSProperties = {
  font: "0.7rem var(--theme-font-mono, monospace)",
  color: cream(62),
  wordBreak: "break-all",
};

/** 等宽的一个值。长值默认截断成一行，「展开」后整段显示（仍是等宽）。 */
export function MonoValue({ value }: { value: unknown }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const text = formatProjectionValue(value);
  if (text === "") {
    return <span style={{ color: cream(38) }}>{t("materialize.value.none")}</span>;
  }
  const long = text.length > VALUE_PREVIEW;
  return (
    <span className="inline-flex min-w-0 flex-wrap items-baseline gap-1">
      <span style={MONO} data-testid="materialize-value">
        {long && !open ? `${text.slice(0, VALUE_PREVIEW)}…` : text}
      </span>
      {long && (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="text-[0.6rem] underline-offset-2 hover:underline"
          style={{ color: cream(45) }}
        >
          {open ? t("materialize.value.collapse") : t("materialize.value.expand")}
        </button>
      )}
    </span>
  );
}

/** 分段头：小号大写标题 + 计数，与卡片里的折叠头同一档。 */
function SectionHead({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1 mt-3 text-[0.62rem] uppercase tracking-[0.16em]" style={{ color: cream(45) }}>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 一行差异
 * ------------------------------------------------------------------ */

/** 能力名：与引擎卡的「引擎配置」区同一套查表（`capGroup.*`），查不到用 id。 */
function useCapabilityName(): (capabilityType: string, capabilityId: string) => string {
  const { tDynamic } = useLocale();
  return useCallback(
    (capabilityType: string, capabilityId: string) => {
      const [byId, bySuffix] = groupNameKeys(capabilityType, capabilityId);
      return tDynamic(`capGroup.${byId}`, tDynamic(`capGroup.${bySuffix}`, byId));
    },
    [tDynamic],
  );
}

function DiffRow({ entry }: { entry: ProjectionEntryWire }) {
  const { t } = useLocale();
  const name = useCapabilityName();
  const action = entry.action === "unset" ? t("materialize.act.unset") : t("materialize.act.set");
  return (
    <div
      className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1.5 text-xs"
      style={{ borderTop: `1px solid ${cream(10)}` }}
      data-testid="materialize-row"
    >
      <span className="w-28 shrink-0" style={{ color: "var(--midground-base)" }}>
        {name(entry.capabilityType, entry.capabilityId)}
      </span>
      <span className="shrink-0" style={MONO}>
        {entry.keyPath}
      </span>
      <Pill tone="muted">{action}</Pill>
      <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-1.5">
        <MonoValue value={entry.before} />
        <span style={{ color: cream(38) }}>→</span>
        <MonoValue value={entry.after} />
      </span>
    </div>
  );
}

/** `unsupported[]` 的一行：能力名 · 键路径（有就显示）· 后端给的 detail。 */
function UnsupportedRow({ entry }: { entry: ProjectionUnsupportedWire }) {
  const name = useCapabilityName();
  return (
    <div
      className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1 text-xs"
      style={{ borderTop: `1px solid ${cream(10)}` }}
      data-testid="materialize-unsupported-row"
    >
      <span className="w-28 shrink-0" style={{ color: "var(--midground-base)" }}>
        {name(entry.capabilityType, entry.capabilityId)}
      </span>
      {entry.keyPath && (
        <span className="shrink-0" style={MONO}>
          {entry.keyPath}
        </span>
      )}
      {entry.detail && (
        <span className="min-w-0 flex-1 text-[0.65rem]" style={{ color: cream(50) }}>
          {entry.detail}
        </span>
      )}
    </div>
  );
}

/** 原因分组的显示顺序：先说"永远不会写"的，再说"你可以决定"的。
 *
 * batch44 新增的三档排在中段：它们都有一个用户自己能做的下一步（去项目设置里填
 * 工作目录、给其中一个项目换个目录），所以比"这台引擎压根没有落点"更该先看到。 */
const REASON_ORDER = [
  "credential_bearing",
  "factory_protected",
  "no_workspace_root",
  "workspace_shared",
  "workspace_denied",
  "no_convention",
  "invalid_config",
  "not_mapped",
  "not_blockable",
] as const;

/** 这几档除了标题还有一句"下一步怎么办"。其余只有标题。 */
const REASON_NOTES = ["credential_bearing", "no_workspace_root", "workspace_shared"] as const;

function reasonLabel(reason: string, t: (key: DictKey) => string): string {
  return REASON_ORDER.includes(reason as (typeof REASON_ORDER)[number])
    ? t(`materialize.reason.${reason}` as DictKey)
    : t("materialize.reason.other");
}

/* ------------------------------------------------------------------ *
 * 应用到引擎
 * ------------------------------------------------------------------ */

type Phase =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; result: MaterializeResultWire }
  | { kind: "writing"; result: MaterializeResultWire }
  | { kind: "busy"; result: MaterializeResultWire; busy: { conversationId: string; title: string }[] }
  | { kind: "writeFailed"; result: MaterializeResultWire; message: string }
  | { kind: "done"; result: MaterializeResultWire };

const isWrite = (entry: ProjectionEntryWire) => entry.action === "set" || entry.action === "unset";

export function MaterializeModal({
  bindingId,
  displayName,
  onClose,
  onWritten,
}: {
  bindingId: string;
  /** 卡片上的挂载名，只用来做标题。 */
  displayName: string;
  onClose: () => void;
  /** 真写成功后通知容器（重取一次 drift，见任务书第 3 件）。 */
  onWritten?: () => void;
}) {
  const { t } = useLocale();
  const [adopt, setAdopt] = useState(false);
  /** 「重试」只是再演一遍：加一个自增的令牌，而不是把 `adopt` 拨来拨去。 */
  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  /* 预演。`adopt` 一变就重演一遍——「接管」改变的正是变更集本身
     （factory_protected 的那些键会从"不会写入"挪到"将写入"）。 */
  useEffect(() => {
    let cancelled = false;
    setPhase({ kind: "loading" });
    materializeBinding(bindingId, { confirm: false, adopt })
      .then((result) => {
        if (!cancelled) setPhase({ kind: "ready", result });
      })
      .catch((failure) => {
        if (!cancelled) setPhase({ kind: "error", message: describeFailure(failure) });
      });
    return () => {
      cancelled = true;
    };
  }, [bindingId, adopt, attempt]);

  const result = "result" in phase ? phase.result : null;
  const applied = result?.result.applied ?? [];
  const writes = applied.filter(isWrite);
  const unchanged = applied.filter((entry) => !isWrite(entry));
  const unsupported = result?.result.unsupported ?? [];
  const warnings = result?.result.warnings ?? [];
  const hasProtected = unsupported.some((entry) => entry.reason === "factory_protected");

  const write = useCallback(async () => {
    if (!result) return;
    setPhase({ kind: "writing", result });
    try {
      const written = await materializeBinding(bindingId, { confirm: true, adopt });
      setPhase({ kind: "done", result: written });
      onWritten?.();
    } catch (failure) {
      const busy = busyConversations(failure);
      if (busy) {
        setPhase({ kind: "busy", result, busy });
        return;
      }
      setPhase({ kind: "writeFailed", result, message: describeFailure(failure) });
    }
  }, [adopt, bindingId, onWritten, result]);

  return (
    <Modal onClose={onClose} width={720}>
      <div data-testid="materialize-modal">
        <ModalTitle>{t("materialize.title", { name: displayName })}</ModalTitle>
        {phase.kind !== "done" && <ModalSub>{t("materialize.sub")}</ModalSub>}

        {phase.kind === "loading" && (
          <div className="py-3 text-xs" style={{ color: cream(45) }}>
            {t("materialize.loading")}
          </div>
        )}

        {phase.kind === "error" && (
          <div className="py-3 text-xs" style={{ color: "var(--danger)" }}>
            {t("materialize.failed", { error: phase.message })}
          </div>
        )}

        {phase.kind === "done" && (
          <div className="py-2" data-testid="materialize-done">
            <p className="text-sm" style={{ color: "var(--midground-base)" }}>
              {t("materialize.done")}
            </p>
            {phase.result.backupPath ? (
              <>
                <p className="mt-1 text-xs" style={{ color: cream(55) }}>
                  {t("materialize.done.backup", { path: phase.result.backupPath })}
                </p>
                {rollbackTarget(phase.result.backupPath) ? (
                  <p className="mt-1 text-xs" style={MONO} data-testid="materialize-rollback">
                    {t("materialize.done.rollback", {
                      path: phase.result.backupPath,
                      target: rollbackTarget(phase.result.backupPath) as string,
                    })}
                  </p>
                ) : (
                  <p className="mt-1 text-xs" style={{ color: cream(55) }} data-testid="materialize-rollback">
                    {t("materialize.done.rollback.unknown")}
                  </p>
                )}
              </>
            ) : (
              <p className="mt-1 text-xs" style={{ color: cream(50) }}>
                {t("materialize.done.noBackup")}
              </p>
            )}
          </div>
        )}

        {result && phase.kind !== "done" && (
          <>
            {/* batch27 第 3 件（真机 A1）：**三段标题始终可见**，各带计数。
                一段是空的也留着标题——"这次没有要写的""这次没有不写的"本身就是
                这次预演的结论，折没了用户只会以为弹窗少了一块（DESIGN ★K）。 */}
            {/* ① 将写入 */}
            <SectionHead>{t("materialize.willWrite.count", { count: writes.length })}</SectionHead>
            {writes.length === 0 ? (
              <div className="py-1 text-xs" style={{ color: cream(45) }} data-testid="materialize-nothing">
                {t("materialize.willWrite.empty")}
              </div>
            ) : (
              <div>
                {writes.map((entry) => (
                  <DiffRow key={`${entry.capabilityType}/${entry.capabilityId}/${entry.keyPath}`} entry={entry} />
                ))}
              </div>
            )}

            {/* ② 无变化：标题与计数常显，行**默认折叠**（它是证据，不是要读的内容）。 */}
            <UnchangedSection entries={unchanged} />

            {/* ③ 不会写入（按 reason 分组），标题与计数同样常显。 */}
            <SectionHead>{t("materialize.unsupported.count", { count: unsupported.length })}</SectionHead>
            {unsupported.length === 0 && (
              <div className="py-1 text-xs" style={{ color: cream(45) }} data-testid="materialize-unsupported-empty">
                {t("materialize.unsupported.empty")}
              </div>
            )}
            {unsupported.length > 0 && (
              <>
                {groupByReason(unsupported).map(([reason, entries]) => (
                  <div key={reason} className="mb-2" data-testid="materialize-unsupported-group">
                    <div className="flex flex-wrap items-baseline gap-2 pt-1">
                      <span className="text-xs" style={{ color: "var(--midground-base)" }}>
                        {reasonLabel(reason, t)}
                      </span>
                      <span className="text-[0.65rem]" style={{ color: cream(45) }}>
                        {entries.length}
                      </span>
                    </div>
                    {REASON_NOTES.includes(reason as (typeof REASON_NOTES)[number]) && (
                      <p className="pt-0.5 text-[0.65rem]" style={{ color: cream(50) }}>
                        {t(`materialize.reason.${reason}.note` as DictKey)}
                      </p>
                    )}
                    {entries.map((entry) => (
                      <UnsupportedRow
                        key={`${entry.capabilityType}/${entry.capabilityId}/${entry.keyPath ?? ""}`}
                        entry={entry}
                      />
                    ))}
                    {reason === "factory_protected" && (
                      <label className="mt-1.5 flex cursor-pointer items-center gap-2 text-xs" style={{ color: "var(--midground-base)" }}>
                        <input
                          type="checkbox"
                          checked={adopt}
                          onChange={(event) => setAdopt(event.target.checked)}
                          data-testid="materialize-adopt"
                        />
                        <span>{t("materialize.adopt")}</span>
                      </label>
                    )}
                  </div>
                ))}
              </>
            )}
            {/* 勾上之后那一组就空了（键挪进了「将写入」），说明得留着，否则用户看不出
                自己刚才勾了什么。 */}
            {adopt && !hasProtected && (
              <label className="mt-1 flex cursor-pointer items-center gap-2 text-xs" style={{ color: "var(--midground-base)" }}>
                <input
                  type="checkbox"
                  checked={adopt}
                  onChange={(event) => setAdopt(event.target.checked)}
                  data-testid="materialize-adopt"
                />
                <span>{t("materialize.adopt")}</span>
              </label>
            )}
            {(hasProtected || adopt) && (
              <p className="pt-0.5 text-[0.65rem]" style={{ color: cream(50) }}>
                {t("materialize.adopt.note")}
              </p>
            )}

            {/* 提醒（后端的 warnings，含 dry-run 那一句） */}
            {warnings.length > 0 && (
              <>
                <SectionHead>{t("materialize.warnings")}</SectionHead>
                <ul className="flex flex-col gap-0.5">
                  {warnings.map((warning) => (
                    <li key={warning} className="text-[0.68rem] leading-relaxed" style={{ color: cream(55) }}>
                      {warning}
                    </li>
                  ))}
                </ul>
              </>
            )}

            {phase.kind === "busy" && (
              <div
                className="mt-3 rounded-md px-3 py-2 text-xs"
                style={{
                  color: "var(--danger)",
                  background: "rgba(179,87,63,.08)",
                  border: "1px solid rgba(179,87,63,.32)",
                }}
                data-testid="materialize-busy"
              >
                {t("materialize.busy", { count: phase.busy.length })}
                <ul className="mt-1" data-testid="materialize-busy-list">
                  {phase.busy.map((row) => (
                    <li key={row.conversationId}>{row.title}</li>
                  ))}
                </ul>
              </div>
            )}
            {phase.kind === "writeFailed" && (
              <div className="mt-3 text-xs" style={{ color: "var(--danger)" }} data-testid="materialize-write-failed">
                {t("materialize.writeFailed", { error: phase.message })}
              </div>
            )}
          </>
        )}

        <div className="mt-4 flex items-center justify-end gap-3">
          <Button onClick={onClose}>{t("common.close")}</Button>
          {phase.kind === "error" && (
            <Button variant="primary" onClick={() => setAttempt((value) => value + 1)}>
              {t("materialize.retry")}
            </Button>
          )}
          {result && phase.kind !== "done" && (
            <Button
              variant="primary"
              disabled={writes.length === 0 || phase.kind === "writing"}
              title={writes.length === 0 ? t("materialize.willWrite.empty") : undefined}
              onClick={() => void write()}
            >
              {phase.kind === "writing"
                ? t("materialize.writing")
                : phase.kind === "busy" || phase.kind === "writeFailed"
                  ? t("materialize.retry")
                  : t("materialize.confirm")}
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}

/** 「无变化」：**标题与计数常显**，行默认折叠——它是"这次什么都没动"的证据，
 *  不是要读的内容（DESIGN ★K / batch27 第 3 件）。 */
function UnchangedSection({ entries }: { entries: ProjectionEntryWire[] }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  return (
    <>
      <div className="mb-1 mt-3">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          disabled={entries.length === 0}
          className="text-[0.62rem] uppercase tracking-[0.16em]"
          style={{ color: cream(45) }}
          data-testid="materialize-unchanged-toggle"
        >
          {t("materialize.unchanged", { count: entries.length })} {entries.length === 0 ? "" : open ? "▾" : "▸"}
        </button>
      </div>
      {open && (
        <div data-testid="materialize-unchanged">
          {entries.map((entry) => (
            <DiffRow key={`${entry.capabilityType}/${entry.capabilityId}/${entry.keyPath}`} entry={entry} />
          ))}
        </div>
      )}
    </>
  );
}

function groupByReason(entries: ProjectionUnsupportedWire[]): [string, ProjectionUnsupportedWire[]][] {
  const groups = new Map<string, ProjectionUnsupportedWire[]>();
  for (const entry of entries) {
    const bucket = groups.get(entry.reason);
    if (bucket) bucket.push(entry);
    else groups.set(entry.reason, [entry]);
  }
  const rank = (reason: string) => {
    const index = REASON_ORDER.indexOf(reason as (typeof REASON_ORDER)[number]);
    return index === -1 ? REASON_ORDER.length : index;
  };
  return [...groups.entries()].sort((a, b) => rank(a[0]) - rank(b[0]));
}

/* ------------------------------------------------------------------ *
 * 配置漂移（同一个弹窗的只读模式）
 * ------------------------------------------------------------------ */

/** 只列 `drifted` 与 `missing`：`unmanaged` 不算漂移（ops 文档 §6），
 *  `in_sync` 没什么可看的。 */
export function driftRows(drift: BindingDriftWire): DriftItemWire[] {
  return drift.items.filter((item) => item.state === "drifted" || item.state === "missing");
}

export function DriftModal({
  drift,
  displayName,
  onClose,
  onMaterialize,
}: {
  drift: BindingDriftWire;
  displayName: string;
  onClose: () => void;
  /** batch27 第 6 件（真机 A4）：「用项目配置覆盖引擎侧」——切到同一枚弹窗的
   *  materialize 流程（dry-run → 备份后写入）。容器不给这个回调时按钮不渲染
   *  （AD-71：没有去处的按钮不摆）。 */
  onMaterialize?: () => void;
}) {
  const { t } = useLocale();
  const name = useCapabilityName();
  const rows = driftRows(drift);
  return (
    <Modal onClose={onClose} width={720}>
      <div data-testid="drift-modal">
        <ModalTitle>{t("drift.title", { name: displayName })}</ModalTitle>
        <ModalSub>{t("drift.sub")}</ModalSub>
        {rows.length === 0 ? (
          <div className="py-2 text-xs" style={{ color: cream(45) }}>
            {t("drift.empty")}
          </div>
        ) : (
          <div>
            {rows.map((item) => (
              <div
                key={`${item.capabilityType}/${item.capabilityId}/${item.keyPath}`}
                className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1.5 text-xs"
                style={{ borderTop: `1px solid ${cream(10)}` }}
                data-testid="drift-row"
              >
                <span className="w-28 shrink-0" style={{ color: "var(--midground-base)" }}>
                  {name(item.capabilityType, item.capabilityId)}
                </span>
                <span className="shrink-0" style={MONO}>
                  {item.keyPath}
                </span>
                <Pill tone="muted">
                  {item.state === "missing" ? t("drift.state.missing") : t("drift.state.drifted")}
                </Pill>
                <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-1.5">
                  <span className="text-[0.65rem]" style={{ color: cream(45) }}>
                    {t("drift.expected")}
                  </span>
                  <MonoValue value={item.expected} />
                  <span className="text-[0.65rem]" style={{ color: cream(45) }}>
                    {t("drift.actual")}
                  </span>
                  <MonoValue value={item.actual} />
                </span>
              </div>
            ))}
          </div>
        )}
        <div className="mt-4 flex items-center justify-end gap-3">
          <span className="mr-auto text-[0.65rem]" style={{ color: cream(45) }} data-testid="drift-checked-at">
            {t("drift.checkedAt", { time: formatCheckedAt(drift.checkedAt) })}
          </span>
          <Button onClick={onClose}>{t("common.close")}</Button>
          {/* 只读弹窗不再是死路：一枚主按钮把这几处漂移交给 materialize 流程去写回。
              没有对不上的键时不给（没什么可覆盖的）。 */}
          {onMaterialize && rows.length > 0 && (
            <Button variant="primary" onClick={onMaterialize}>
              {t("drift.materialize")}
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
