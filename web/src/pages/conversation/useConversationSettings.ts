import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from "react";

import { useLocale } from "../../i18n";
import { hasCapability, type UiCapabilities } from "../../components/cards/capabilities";
import type { BarMenuOption } from "../../components/ComposerBar";
import { composerReasoning, observeComposerSettings, readComposerSettings } from "../../lib/composerSettings";
import { catalogForBinding, hasModelCatalog, modelOptionsFor, modelSelection } from "../../lib/modelCatalogView";
import {
  emptyEffectiveSettings,
  patchConversation,
  describeFailure,
  modelRejection,
  type BindingWire,
  type ConversationDetailWire,
  type EffectiveSettingsWire,
  type ModelCatalogWire,
} from "../../lib/sessionApi";

/* 会话的有效设置（模型 / 推理 / 审批 / 执行模式 / 引擎配置项）与能力矩阵。
 *
 * Defaults, live choices and capability flags come from the same settings snapshot
 * （`lib/composerSettings.ts`）。这里管三件事：读（订阅 + 运行态转换时重读）、改
 * （`PATCH /conversations/{id}`，改完重读），以及把快照折成工具栏要的那几项取值。 */

export interface ConversationSettingsOptions {
  conversationId: string;
  binding: BindingWire | null;
  detail: ConversationDetailWire | null;
  setDetail: Dispatch<SetStateAction<ConversationDetailWire | null>>;
  setError: (error: string | null) => void;
  /** 运行态：本轮开始 / 结束都可能改会话的选项，所以转换时重读一次。 */
  running: boolean;
  runningRef: RefObject<boolean>;
  sendLockRef: RefObject<boolean>;
}

export function useConversationSettings({ conversationId, binding, detail, setDetail, setError, running, runningRef, sendLockRef }: ConversationSettingsOptions) {
  const { t } = useLocale();
  const [catalog, setCatalog] = useState<ModelCatalogWire | null>(null);
  /* Defaults, live choices and capability flags come from the same settings snapshot. */
  const [settings, setSettings] = useState<EffectiveSettingsWire | null>(null);
  const [caps, setCaps] = useState<UiCapabilities | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);

  const reloadSettings = useCallback(async (bindingId: string) => {
    if (!binding || binding.id !== bindingId) return;
    const next = await readComposerSettings(binding, conversationId);
    setSettings(next.settings);
    setCatalog(next.catalog);
    setCaps(next.caps);
    return next.settings;
  }, [binding, conversationId]);

  useEffect(() => {
    setSettings(null);
    setCatalog(null);
    setCaps(null);
    if (!binding) return;
    return observeComposerSettings(binding, next => {
      setSettings(next.settings);
      setCatalog(next.catalog);
      setCaps(next.caps);
    }, { conversationId });
  }, [binding, conversationId]);

  const settingsRunRef = useRef(false);
  useEffect(() => {
    const wasRunning = settingsRunRef.current;
    settingsRunRef.current = running;
    if (!binding || (!running && !wasRunning)) return;
    let cancelled = false;
    // Initialization and a completed turn can both change the session's options.
    // Keep session discoveries together instead of replacing them with the
    // installation's older capability snapshot.
    void readComposerSettings(binding, conversationId).then(next => {
      if (cancelled) return;
      setSettings(next.settings);
      setCatalog(next.catalog);
      setCaps(next.caps);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [binding, conversationId, running]);

  /* Model choice belongs to this conversation. A catalog alone does not imply a writable runtime. */
  const changeModel = useCallback(
    async (next: string) => {
      if (!binding || !hasCapability(caps?.models.conversationScoped) || runningRef.current || settingsBusy) return;
      const selection = modelSelection(next);
      if (selection.modelId === detail?.conversation?.modelId && (!selection.providerId || selection.providerId === detail?.conversation?.providerId)) return;
      const selected = catalog?.models.find(item => item.modelId === selection.modelId && (!selection.providerId || item.providerId === selection.providerId));
      const label = selected?.displayName || selection.modelId;
      setSettingsBusy(true);
      try {
        const result = await patchConversation(conversationId, { ...selection, providerId: selection.providerId ?? selected?.providerId ?? undefined });
        setDetail(current => current ? { ...current, conversation: result.conversation } : current);
        await reloadSettings(binding.id);
      } catch (failure) {
        /* batch26：引擎自己把这个模型顶回来了（400 `model_rejected`）。**快照没写**，
           所以这里一个字都不改本地状态——下拉显示的一直是服务端那份有效设置，
           于是它自己就拨回去了；用户看到的是引擎给的那句理由，不是一句"失败了"。 */
        const rejected = modelRejection(failure);
        setError(
          rejected
            ? t("conversation.model.rejected", { model: rejected.modelId || label, reason: rejected.agentReason })
            : t("conversation.model.failed", { error: describeFailure(failure) }),
        );
      } finally {
        setSettingsBusy(false);
      }
    },
    [binding, caps?.models.conversationScoped, catalog, conversationId, detail?.conversation?.modelId, detail?.conversation?.providerId, reloadSettings, settingsBusy, t],
  );

  const changeSessionSetting = useCallback(async (patch: { reasoningMode?: string; approvalMode?: string; executionMode?: string; configOptions?: Record<string, string | boolean> }) => {
    if (!binding || runningRef.current || sendLockRef.current || settingsBusy) return;
    setSettingsBusy(true);
    setError(null);
    try {
      const result = await patchConversation(conversationId, patch);
      setDetail(current => current ? { ...current, conversation: result.conversation } : current);
      await reloadSettings(binding.id);
    } catch (failure) {
      setError(describeFailure(failure));
    } finally {
      setSettingsBusy(false);
    }
  }, [binding, conversationId, settingsBusy, reloadSettings]);

  /* 工具栏的四项取值：端点还没来（或返回 404）时按"四项都没有"渲染，也就是一枚都不出。 */
  const baseBar = settings ?? emptyEffectiveSettings(binding?.id ?? "");
  const bar = {
    ...baseBar,
    ...(detail?.conversation?.modelId ? { model: { value: detail.conversation.modelId, source: "engine" as const } } : {}),
    ...(detail?.runtime?.workspaceRoot ? { workspaceRoot: { value: detail.runtime.workspaceRoot, source: "engine" as const } } : {}),
  };
  const selectedModel = catalogForBinding(catalog, binding)?.models.find(model => model.modelId === bar.model.value && (!detail?.conversation?.providerId || model.providerId === detail.conversation.providerId));
  const conversationControls = settings?.conversationControls;
  const reasoning = composerReasoning(bar, selectedModel, detail?.conversation?.reasoningMode);
  const approval = {
    ...bar.approvalMode,
    ...(conversationControls?.approvalDefault ? { value: conversationControls.approvalDefault, source: "catalog" as const } : {}),
    ...(detail?.conversation?.approvalMode ? { value: detail.conversation.approvalMode, source: "engine" as const } : {}),
    options: conversationControls?.approvalModes ?? [],
    choices: conversationControls?.approvalOptions,
  };
  /* batch27 第 5 件（真机 D4 / DESIGN ★I）：下拉**只列本引擎目录里的模型**。
     认领与过滤都在 `lib/modelCatalogView.ts` 里（纯函数，直接受测）：目录的
     `bindingId` 对不上就当没有目录，模型自带 `backendId` 时按它过滤。 */
  const modelOptions: BarMenuOption[] = useMemo(
    () => modelOptionsFor(catalog, binding, bar.model.value),
    [binding, catalog, bar.model.value],
  );
  /* 没有目录（端点不在 / 取不到 / 那份目录不是这条 Binding 的）时不给写回调，
     工具栏那枚模型 Pill 于是是**只读的当前模型一项**。目录在但空着是另一回事
     （批次十五第 5 件）：菜单照旧点得开，里面写「引擎未报告可用模型」。 */
  const modelCatalogPresent = hasModelCatalog(catalog, binding);
  /* `degraded` 时下拉底部垫一行小字（DESIGN ★ I）。没有 diagnostics 就不垫——
     "降级了但说不出原因"这句话对用户没有用（AD-71 的同一条精神）。 */
  const ownCatalog = catalogForBinding(catalog, binding);
  const modelDiagnostic = ownCatalog?.diagnostics?.[0] ?? null;
  const modelDegraded = Boolean(ownCatalog?.degraded);

  return {
    settings,
    catalog,
    caps,
    settingsBusy,
    reloadSettings,
    changeModel,
    changeSessionSetting,
    bar,
    conversationControls,
    reasoning,
    approval,
    modelOptions,
    modelCatalogPresent,
    modelDiagnostic,
    modelDegraded,
  };
}
