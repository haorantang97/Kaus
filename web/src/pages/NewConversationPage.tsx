import { AttachmentFilename } from "../components/AttachmentFilename";
import { useComposerAutosize, useComposerInputGuard } from "../hooks/useComposerInput";
import { engineAcceptsNewWork } from "../lib/engineAvailability";
import { EngineIcon } from "../components/EngineIcon";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FileText, LoaderCircle, Paperclip, ArrowUp, X } from "lucide-react";
import { useLocale } from "../i18n";
import { cream } from "../components/ui";
import { BarMenu, ComposerBar, type BarMenuOption } from "../components/ComposerBar";
import { AttentionBlock } from "../components/AttentionBlock";
import { backendDisplay, engineBrand, engineFamily } from "../lib/backend-display";
import { engineSelectionChoices } from "../lib/engineSelection";
import type { SidebarGroup } from "../lib/conversationIndex";
import {
  createConversation,
  ensureProjectBinding,
  emptyEffectiveSettings,
  fetchConnectedBackends,
  fetchEffectiveSettings,
  fetchModelCatalog,
  fetchProjectBindings,
  fetchProjects,
  fetchBackendUiCapabilities,
  patchConversation,
  patchProject,
  sendConversationMessage,
  type BindingWire,
  type BackendWire,
  type EffectiveSettingsWire,
  type ModelCatalogWire,
  type ProjectWire,
  describeFailure,
  SessionApiError,
} from "../lib/sessionApi";
import { hasCapability, type UiCapabilities } from "../components/cards/capabilities";
import { ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_COUNT, fileSizeLabel, uploadConversationAttachment, type ConversationAttachment } from "../lib/attachmentsApi";
import { modelEffort, modelOptionsFor, modelSelection, modelValue } from "../lib/modelCatalogView";
import { findProject, matchProjectId } from "../lib/projectRef";
import { isConfirmedMessageRejection } from "../lib/messageDelivery";
import { newClientRef } from "../lib/pendingMessages";
import {
  readDefaultDestination,
  readDraftText,
  writeDefaultDestination,
  writeDraftText,
} from "../lib/shellPrefs";

/* 新会话草稿页（★定稿 C / AD-80）。
 *
 * 整屏居中的空对话窗。**发出第一条消息之前不建会话**——侧栏因此不会 +1，
 * 离开草稿页也不留垃圾会话。三个下拉的依赖是单向的：
 *
 *     项目 → 已接入引擎（首次选用建立 binding）→ 模型/推理（该 binding 的 Model Catalog）
 *
 * 模型下拉在拿不到 Catalog（501/空目录）时**整块不渲染**，不写「本引擎不支持」——AD-71。
 */

/** 项目清单 → 菜单项：按项目树深度优先排开，`depth` 交给 `BarMenu` 做缩进。
 *  父不在清单里的当顶层处理，环（不该出现，但真出现时）靠 `seen` 兜住。 */
export function projectMenuOptions(projects: ProjectWire[], profiles: BindingWire[] = []): BarMenuOption[] {
  const ids = new Set(projects.map((project) => project.id));
  const children = new Map<string | null, ProjectWire[]>();
  for (const project of projects) {
    const parent =
      project.parentProjectId && ids.has(project.parentProjectId) ? project.parentProjectId : null;
    const bucket = children.get(parent);
    if (bucket) bucket.push(project);
    else children.set(parent, [project]);
  }
  const options: BarMenuOption[] = [];
  const seen = new Set<string>();
  const append = (project: ProjectWire, depth: number) => {
    options.push({ value: project.id, label: project.displayName || project.slug, depth });
    for (const profile of profiles.filter((item) => item.projectId === project.id && item.enabled && item.discriminator)) {
      options.push({ value: profile.id, label: profile.displayName || profile.discriminator!, depth: depth + 1 });
    }
  };
  const walk = (parent: string | null, depth: number) => {
    for (const project of children.get(parent) ?? []) {
      if (seen.has(project.id)) continue;
      seen.add(project.id);
      append(project, depth);
      walk(project.id, depth + 1);
    }
  };
  walk(null, 0);
  // 环里的项目一个都不能丢：兜底补在末尾（顶层缩进）。
  for (const project of projects) {
    if (seen.has(project.id)) continue;
    append(project, 0);
  }
  return options;
}

export interface NewConversationPageProps {
  /** 从项目入口预填，发送前仍可切换项目。 */
  initialProjectId?: string | null;
  initialBindingId?: string | null;
  /** 第二个参数是首句的占位（AD-94）：跳转时把它带到会话页，那边接着等回声。 */
  onCreated: (conversationId: string, pending?: { clientRef: string; text: string; attachments?: ConversationAttachment[] }) => void;
  /* batch42（2026-09-13 用户裁决：概览页取消）：铭牌与「需要处理」搬到了这一页
     顶上。两样都从**已有的**会话索引算，所以数据由外壳传进来（`App.tsx` 里那份
     `useConversationIndex`），这一页不另开一路取数。缺省 = 两样都不渲染，
     `/new` 之外的调用方（测试、项目页）不受影响。 */
  indexGroups?: SidebarGroup[];
  indexProjectCount?: number | null;
  onOpenConversation?: (conversationId: string) => void;
}

const DRAFT_OPTIONS_TTL_MS = 30_000;
interface DraftEngineOptions {
  bindingId: string;
  catalog: ModelCatalogWire | null;
  settings: EffectiveSettingsWire;
  caps: UiCapabilities | null;
}

async function readDraftEngineOptions(binding: BindingWire): Promise<DraftEngineOptions> {
  const settings = await fetchEffectiveSettings(binding.id, undefined, { includeCatalog: true })
    .catch(() => emptyEffectiveSettings(binding.id));
  // Older hosts do not include the bundle; explicit null means unavailable, not missing.
  const [catalog, caps] = await Promise.all([
    settings.modelCatalog !== undefined ? settings.modelCatalog : fetchModelCatalog(binding.backendId, binding.id).catch(() => null),
    settings.uiCapabilities !== undefined ? settings.uiCapabilities : fetchBackendUiCapabilities(binding.backendId).catch(() => null),
  ]);
  return { bindingId: binding.id, catalog, settings, caps };
}

function engineLabel(backendId: string, backend?: BackendWire): string {
  return engineBrand(backend?.productId ?? backendId)?.name ?? backend?.productName
    ?? backend?.displayName ?? backendDisplay(backendId).displayName;
}

export function NewConversationPage({
  initialProjectId = null,
  initialBindingId = null,
  onCreated,
  indexGroups,
  onOpenConversation,
}: NewConversationPageProps) {
  const { t } = useLocale();
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [projectId, setProjectId] = useState<string>(() => matchProjectId([], initialProjectId || readDefaultDestination()));
  const [bindings, setBindings] = useState<BindingWire[] | null>(null);
  const [connectedBackends, setConnectedBackends] = useState<BackendWire[]>([]);
  const [bindingId, setBindingId] = useState<string>("");
  const selectionEpoch = useRef(0);
  // Remember each product's selected profile only for this draft, never in project defaults.
  const selectedBindings = useRef(new Map<string, string>());
  const binding = useMemo(
    () => bindings?.find((item) => item.id === bindingId && item.projectId === projectId && item.enabled && engineAcceptsNewWork(item) && engineAcceptsNewWork(connectedBackends.find(c => c.id === item.backendId))) ?? null,
    [bindings, bindingId, projectId, connectedBackends],
  );
  const [engineOptions, setEngineOptions] = useState<DraftEngineOptions | null>(null);
  const currentOptions = engineOptions?.bindingId === binding?.id ? engineOptions : null;
  const catalog = currentOptions?.catalog ?? null;
  const settings = currentOptions?.settings ?? null;
  const caps = currentOptions?.caps ?? null;
  // Draft-local caches: returning to a selection is immediate; reopening the page reads afresh.
  const bindingsCache = useRef(new Map<string, { value: BindingWire[]; expires: number }>());
  const optionsCache = useRef(new Map<string, { value: DraftEngineOptions; expires: number }>());
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
  const [selectedReasoning, setSelectedReasoning] = useState<string | null>(null);
  const [selectedApproval, setSelectedApproval] = useState<string | null>(null);
  const [selectedExecution, setSelectedExecution] = useState<string | null>(null);
  const [selectedConfig, setSelectedConfig] = useState<Record<string, string | boolean>>({});
  const [text, setText] = useState<string>(() => readDraftText());
  const [files, setFiles] = useState<File[]>([]);
  const filesRef = useRef<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [uploadingName, setUploadingName] = useState<string | null>(null);
  const sendLockRef = useRef(false);
  const createdDraftsRef = useRef(new Map<string, { id: string; modelId: string | null; uploads: Map<File, ConversationAttachment> }>());
  const [unconfirmed, setUnconfirmed] = useState<{ conversationId: string; clientRef: string; text: string; attachments: ConversationAttachment[] } | null>(null);
  const unconfirmedRef = useRef(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const canAttach = caps?.card.attachments === "images" || caps?.card.attachments === "files";
  const canChooseModel = hasCapability(caps?.models.conversationScoped);

  useEffect(() => {
    let cancelled = false;
    void fetchConnectedBackends().then(({ backends }) => {
      if (!cancelled) setConnectedBackends(backends ?? []);
    }).catch((failure) => { if (!cancelled) setError(describeFailure(failure)); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchProjects()
      .then(({ projects: list }) => {
        if (cancelled) return;
        setProjects(list);
        setProjectId((current) => findProject(list, current)?.id ?? list[0]?.id ?? current);
      })
      .catch((failure) => {
        if (!cancelled) setError(describeFailure(failure));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    selectionEpoch.current += 1;
    const applyBindings = (list: BindingWire[]) => {
      setBindings(list);
      const usable = list.filter((item) => item.enabled && engineAcceptsNewWork(item));
      const preferred = usable.find((item) => item.id === initialBindingId) ?? usable.find((item) => item.isDefault) ?? usable[0];
      setBindingId(preferred?.id ?? "");
    };
    setError(null);
    const cached = bindingsCache.current.get(projectId);
    if (cached && cached.expires > Date.now()) {
      applyBindings(cached.value);
      return () => { selectionEpoch.current += 1; };
    }
    setBindings(null);
    setBindingId("");
    // 每次换项目都先清错：上一个项目的失败（例如默认去向已被删掉、
    // 或首帧那个还没被项目列表纠正过来的 id）不该粘在新项目上。
    fetchProjectBindings(projectId, undefined, { includeStatus: false })
      .then(({ bindings: list }) => {
        if (cancelled) return;
        bindingsCache.current.set(projectId, { value: list, expires: Date.now() + DRAFT_OPTIONS_TTL_MS });
        applyBindings(list);
      })
      .catch((failure) => {
        if (cancelled) return;
        setBindings([]);
        setError(describeFailure(failure));
      });
    return () => {
      cancelled = true;
      selectionEpoch.current += 1;
    };
  }, [projectId, initialBindingId]);

  const selectEngine = async (value: string) => {
    if (sendLockRef.current || bindings === null) return;
    const epoch = ++selectionEpoch.current;
    setError(null);
    setBindingId(value);
    const backend = connectedBackends.find((item) => item.id === value);
    if (!backend) return;
    try {
      const next = await ensureProjectBinding(projectId, backend.id);
      if (selectionEpoch.current !== epoch) return;
      const list = [...bindings.filter((item) => item.id !== next.id), next];
      bindingsCache.current.set(projectId, { value: list, expires: Date.now() + DRAFT_OPTIONS_TTL_MS });
      setBindings(list);
      setBindingId(next.id);
    } catch (failure) {
      if (selectionEpoch.current === epoch) {
        setBindingId(binding?.id ?? "");
        setError(describeFailure(failure));
      }
    }
  };

  const engineGroups = useMemo(() => {
    const groups = new Map<string, { primary: BindingWire; choice: BindingWire }>();
    const baseRank = (item: BindingWire) => Number(!item.discriminator) * 4 + Number(item.isDefault) * 2
      + Number(connectedBackends.find((backend) => backend.id === item.backendId)?.preferred ?? false);
    for (const item of bindings ?? []) {
      if (!item.enabled || !engineAcceptsNewWork(item) || !engineAcceptsNewWork(connectedBackends.find(c => c.id === item.backendId)) || item.projectId !== projectId) continue;
      const product = connectedBackends.find((backend) => backend.id === item.backendId)?.productId ?? engineFamily(item.backendId);
      const remembered = selectedBindings.current.get(`${projectId}:${product}`);
      const choiceRank = (candidate: BindingWire) => Number(candidate.id === bindingId) * 32
        + Number(candidate.id === remembered) * 16 + baseRank(candidate);
      const group = groups.get(product);
      if (!group) groups.set(product, { primary: item, choice: item });
      else {
        if (baseRank(item) > baseRank(group.primary)) group.primary = item;
        if (choiceRank(item) > choiceRank(group.choice)) group.choice = item;
      }
    }
    return groups;
  }, [bindings, connectedBackends, bindingId, projectId]);

  const engineChoices: BarMenuOption[] = useMemo(() => {
    if (bindings === null) return [];
    return engineSelectionChoices(bindings, connectedBackends, bindingId, [...engineGroups.values()].map(g => g.choice.id))
      .map(item => ({ value: item.value, icon: <EngineIcon engine={item.productId} />,
        label: engineLabel(item.backendId, connectedBackends.find(connection => connection.id === item.backendId)) }));
  }, [bindings, connectedBackends, engineGroups, bindingId]);
  const selectedBackendId = binding?.backendId ?? connectedBackends.find((item) => item.id === bindingId)?.id;
  const selectedEngine = connectedBackends.find(item => item.id === selectedBackendId);
  const selectedProduct = selectedEngine?.productId ?? engineFamily(selectedBackendId ?? "");

  useEffect(() => {
    if (binding) selectedBindings.current.set(`${projectId}:${selectedProduct}`, binding.id);
  }, [binding, projectId, selectedProduct]);

  const selectDestination = (value: string) => {
    if (sendLockRef.current) return;
    const profile = bindings?.find((item) => item.id === value && item.projectId === projectId && item.enabled && item.discriminator);
    if (profile) {
      void selectEngine(profile.id);
    } else if (value === projectId) {
      const primary = engineGroups.get(selectedProduct)?.primary;
      if (primary) void selectEngine(primary.id);
    } else if (projects.some((item) => item.id === value)) {
      selectionEpoch.current += 1;
      setProjectId(value);
    }
  };

  const reloadSettings = useCallback(async (target: BindingWire) => {
    optionsCache.current.clear();
    const next = await readDraftEngineOptions(target);
    optionsCache.current.set(target.id, { value: next, expires: Date.now() + DRAFT_OPTIONS_TTL_MS });
    setEngineOptions((current) => current?.bindingId === target.id ? next : current);
  }, []);

  useEffect(() => {
    setSelectedReasoning(null);
    setSelectedApproval(null);
    setSelectedExecution(null);
    setSelectedConfig({});
    setSelectedModelId(null);
    if (!binding) {
      setEngineOptions(null);
      return;
    }
    const cached = optionsCache.current.get(binding.id);
    if (cached && cached.expires > Date.now()) {
      setEngineOptions(cached.value);
      return;
    }
    let cancelled = false;
    setEngineOptions(null);
    void readDraftEngineOptions(binding).then((next) => {
      if (cancelled) return;
      if (next.settings.model.source !== "none" || next.catalog !== null) {
        optionsCache.current.set(binding.id, { value: next, expires: Date.now() + DRAFT_OPTIONS_TTL_MS });
      }
      setEngineOptions(next);
    });
    return () => {
      cancelled = true;
    };
  }, [binding]);

  useEffect(() => {
    writeDraftText(text);
  }, [text]);

  useComposerAutosize(textareaRef, text);
  const composerInput = useComposerInputGuard();

  const bar = settings ?? emptyEffectiveSettings(bindingId);
  const modelOptions: BarMenuOption[] = useMemo(
    () =>
      modelOptionsFor(catalog, binding, selectedModelId || bar.model.value),
    [catalog, binding, selectedModelId, bar.model.value],
  );
  const modelDiagnostic = catalog?.diagnostics?.[0] ?? null;
  const modelDegraded = Boolean(catalog?.degraded);
  const controls = settings?.conversationControls;
  const selectedValue = selectedModelId || modelValue(bar.model.value ?? catalog?.defaultModelId, catalog?.defaultProviderId ?? binding?.defaultProviderId, catalog);
  const selection = selectedValue ? modelSelection(selectedValue) : null;
  const selectedDescriptor = catalog?.models.find(model => model.modelId === selection?.modelId && (!selection?.providerId || model.providerId === selection.providerId));
  const reasoning = {
    ...bar.reasoningEffort,
    ...(modelEffort(selectedDescriptor) ? { value: modelEffort(selectedDescriptor), source: "catalog" as const } : {}),
    ...(selectedReasoning ? { value: selectedReasoning } : {}),
    levels: selectedDescriptor?.reasoningLevels?.length ? selectedDescriptor.reasoningLevels : bar.reasoningEffort.levels,
  };
  const approval = {
    ...bar.approvalMode,
    ...(controls?.approvalDefault ? { value: controls.approvalDefault, source: "catalog" as const } : {}),
    ...(selectedApproval ? { value: selectedApproval } : {}),
    options: controls?.approvalModes ?? [],
    choices: controls?.approvalOptions,
  };

  const updateFiles = (next: File[]) => { filesRef.current = next; setFiles(next); };
  const addFiles = (incoming: File[]) => {
    if (!incoming.length || sendLockRef.current) return;
    if (!canAttach) { setError(t("conversation.attach.unsupported")); return; }
    if (filesRef.current.length + incoming.length > ATTACHMENT_MAX_COUNT) { setError(t("conversation.attach.tooMany")); return; }
    if (incoming.some((file) => file.size > ATTACHMENT_MAX_BYTES)) { setError(t("conversation.attach.tooLarge")); return; }
    if (caps?.card.attachments === "images" && incoming.some((file) => !/^image\/(?:png|jpeg|webp|gif|avif)$/.test(file.type))) {
      setError(t("draft.attachments.imagesOnly"));
      return;
    }
    setError(null);
    updateFiles([...filesRef.current, ...incoming]);
  };

  const changeWorkspaceRoot = useCallback(
    async (path: string) => {
      if (!projectId) return;
      try {
        await patchProject(projectId, { workspaceRoot: path });
      } catch (failure) {
        throw new Error(describeFailure(failure));
      }
      if (binding) await reloadSettings(binding);
    },
    [binding, projectId, reloadSettings],
  );

  const send = useCallback(async () => {
    const body = text.trim();
    const stagedFiles = filesRef.current;
    if ((!body && !stagedFiles.length) || !projectId || !binding || !currentOptions || sendLockRef.current || unconfirmedRef.current) return;
    if (stagedFiles.length && !canAttach) { setError(t("conversation.attach.unsupported")); return; }
    if (caps?.card.attachments === "images" && stagedFiles.some((file) => !/^image\/(?:png|jpeg|webp|gif|avif)$/.test(file.type))) {
      setError(t("draft.attachments.imagesOnly"));
      return;
    }
    sendLockRef.current = true;
    setSending(true);
    setError(null);
    const draftKey = `${projectId}\u0000${bindingId}`;
    let conversationId: string | null = null;
    let submitted = false;
    let completed = false;
    const attachments: ConversationAttachment[] = [];
    const clientRef = newClientRef();
    try {
      let draft = createdDraftsRef.current.get(draftKey);
      if (!draft) {
        const conversation = await createConversation(projectId, { bindingId, title: (body || stagedFiles[0]?.name || "").slice(0, 40) });
        draft = { id: conversation.id, modelId: modelValue(conversation.modelId, conversation.providerId, catalog), uploads: new Map() };
        createdDraftsRef.current.set(draftKey, draft);
      }
      conversationId = draft.id;
      const requestedModel = selectedValue;
      if (requestedModel && canChooseModel && requestedModel !== draft.modelId) {
        const requested = modelSelection(requestedModel);
        const updated = await patchConversation(draft.id, { ...requested, ...(requested.providerId ? {} : { providerId: modelOptions.find(option => option.value === requestedModel)?.providerId ?? undefined }) });
        const adopted = updated.conversation.modelId;
        if (adopted && (adopted !== requested.modelId || (requested.providerId && updated.conversation.providerId !== requested.providerId))) throw new Error(t("draft.model.notApplied"));
        draft.modelId = modelValue(adopted, updated.conversation.providerId, catalog) || requestedModel;
      }
      if (selectedReasoning) await patchConversation(draft.id, { reasoningMode: selectedReasoning });
      if (selectedApproval) await patchConversation(draft.id, { approvalMode: selectedApproval });
      if (selectedExecution) await patchConversation(draft.id, { executionMode: selectedExecution });
      for (const [key, value] of Object.entries(selectedConfig)) await patchConversation(draft.id, { configOptions: { [key]: value } });
      for (const file of stagedFiles) {
        let uploaded = draft.uploads.get(file);
        if (!uploaded) {
          setUploadingName(file.name);
          uploaded = await uploadConversationAttachment(draft.id, file);
          draft.uploads.set(file, uploaded);
        }
        attachments.push(uploaded);
      }
      setUploadingName(null);
      submitted = true;
      if (attachments.length) await sendConversationMessage(draft.id, body, clientRef, attachments);
      else await sendConversationMessage(draft.id, body, clientRef);
      writeDraftText("");
      writeDefaultDestination(projectId);
      onCreated(draft.id, { clientRef, text: body, ...(attachments.length ? { attachments } : {}) });
      completed = true;
    } catch (failure) {
      // Without a receipt, the engine may already be working. Never delete or blindly resend.
      const knownRejection = failure instanceof SessionApiError && isConfirmedMessageRejection({ failureCode: failure.code });
      if (submitted && conversationId && !knownRejection) {
        unconfirmedRef.current = true;
        setUnconfirmed({ conversationId, clientRef, text: body, attachments });
      }
      setError(describeFailure(failure));
    } finally {
      if (!completed) { sendLockRef.current = false; setSending(false); }
      setUploadingName(null);
    }
  }, [binding, bindingId, currentOptions, bar.model.value, canAttach, canChooseModel, caps?.card.attachments, onCreated, projectId, selectedModelId, selectedReasoning, selectedApproval, selectedExecution, selectedConfig, text, t]);

  /* 第 4 件①：`Enter` 发送、`Shift+Enter` 换行，⌘/Ctrl+Enter 继续可用。
     中文输入法组字期间的 Enter 是"选词"，绝不能当发送（`isComposing`）。 */
  const onComposerKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (composerInput.guardKeyDown(event)) return;
      if (event.key !== "Enter" || event.shiftKey || event.altKey) return;
      event.preventDefault();
      void send();
    },
    [send],
  );

  const projectName = projects.find((item) => item.id === projectId)?.displayName ?? projectId;
  /* 第 5 件：项目菜单按项目树排（父在前、子跟着，`depth` 只用来缩进）。
     父不在这份清单里的（被过滤 / 权限外）当作顶层，不让它整枝消失。 */
  const projectOptions: BarMenuOption[] = useMemo(() => projectMenuOptions(projects, bindings ?? []), [projects, bindings]);

  return (
    <div className="kaus-draft-page" data-testid="draft-page">
      <div className="kaus-draft-window">
        {indexGroups && onOpenConversation && (
          <AttentionBlock groups={indexGroups} onOpenConversation={onOpenConversation} />
        )}

        <div className="kaus-draft-empty">
          <h1>{t("draft.heading")}</h1>
        </div>

        {error && <div className="kaus-draft-error" role="alert">{error}</div>}
        {unconfirmed && <div className="kaus-draft-error" role="status">
          {t("draft.send.unconfirmed")}
          <button type="button" className="kaus-inline-action" onClick={() => onCreated(unconfirmed.conversationId, { clientRef: unconfirmed.clientRef, text: unconfirmed.text, ...(unconfirmed.attachments.length ? { attachments: unconfirmed.attachments } : {}) })}>{t("draft.send.openExisting")}</button>
        </div>}

        {/* 批次十三：草稿页接会话页那条工具栏（DESIGN ★ I），项目下拉保留在最左。 */}
        <div
          className={`kaus-composer is-stacked${dragging ? " is-dragging" : ""}`}
          onDragOver={(event) => { if (!event.dataTransfer.types.includes("Files") || sending) return; event.preventDefault(); setDragging(true); }}
          onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); }}
          onDrop={(event) => { if (!event.dataTransfer.files.length) return; event.preventDefault(); setDragging(false); addFiles(Array.from(event.dataTransfer.files)); }}
        >
          <ComposerBar
            contextKey={projectId ?? "draft"}
            leading={
              <>
                <BarMenu
                  label={t("draft.field.project")}
                  value={binding?.discriminator ? binding.id : projectId}
                  display={binding?.discriminator ? `${projectName} · ${binding.displayName || binding.discriminator}` : projectName}
                  options={projectOptions}
                  onSelect={selectDestination}
                />
                {/* 引擎在草稿页是可选的（会话建出来之后就定死了，会话页那边只读）。 */}
                <BarMenu
                  label={t("draft.field.engine")}
                  icon={selectedBackendId && <EngineIcon engine={selectedEngine?.productId ?? selectedBackendId} />}
                  value={bindingId}
                  display={
                    binding
                      ? engineLabel(binding.backendId, selectedEngine)
                      : selectedBackendId ? engineLabel(selectedBackendId, selectedEngine)
                      : bindings === null
                        ? t("draft.engine.loading")
                        : t("draft.engine.none")
                  }
                  options={engineChoices}
                  onSelect={(next) => { void selectEngine(next); }}
                />
              </>
            }
            workspaceRoot={bar.workspaceRoot}
            onChangeWorkspaceRoot={changeWorkspaceRoot}
            model={{ ...bar.model, value: selectedValue }}
            modelOptions={modelOptions}
            modelDiagnostic={modelDiagnostic}
            modelDegraded={modelDegraded}
            onChangeModel={canChooseModel && !sending ? setSelectedModelId : undefined}
            reasoning={reasoning}
            onChangeReasoning={controls?.reasoning && !sending ? setSelectedReasoning : undefined}
            approval={approval}
            onChangeApproval={approval.options.length && !sending ? setSelectedApproval : undefined}
            executionMode={selectedExecution ?? controls?.executionDefault}
            executionOptions={controls?.executionModes?.map(option => ({ value: option.id, label: option.name }))}
            onChangeExecutionMode={!sending ? setSelectedExecution : undefined}
            configOptions={controls?.configOptions}
            configValues={selectedConfig}
            onChangeConfig={!sending ? (key, value) => setSelectedConfig(current => ({ ...current, [key]: value })) : undefined}
            attachments={caps?.card.attachments}
            onAttach={canAttach && !sending ? () => fileInputRef.current?.click() : undefined}
            actions={
              <button
                type="button"
                className="kaus-send"
                aria-label={sending ? t("draft.sending") : t("draft.send")}
                title={sending ? t("draft.sending") : t("draft.send")}
                disabled={(!text.trim() && !files.length) || !binding || !currentOptions || sending || !!unconfirmed || (files.length > 0 && !canAttach)}
                onClick={() => void send()}
              >
                {sending ? <LoaderCircle size={18} aria-hidden /> : <ArrowUp size={18} aria-hidden />}
              </button>
            }
          >
          <input ref={fileInputRef} type="file" multiple hidden aria-label={t("bar.attach")} accept={caps?.card.attachments === "images" ? "image/png,image/jpeg,image/webp,image/gif,image/avif" : undefined} disabled={sending} onChange={(event) => { const selected = Array.from(event.target.files || []); event.target.value = ""; addFiles(selected); }} />
          {!!files.length && <div className="kaus-attachment-tray" aria-label={t("conversation.attach.list")}>
            {files.map((file, index) => <div className="kaus-attachment-chip" key={`${file.name}:${file.lastModified}:${index}`} title={file.name}>
              {uploadingName === file.name ? <LoaderCircle size={17} /> : <FileText size={17} />}
              <span><strong><AttachmentFilename name={file.name} /></strong><small>{uploadingName === file.name ? t("conversation.attach.uploading") : fileSizeLabel(file.size)}</small></span>
              <button type="button" disabled={sending} onClick={() => updateFiles(files.filter((_, itemIndex) => itemIndex !== index))} aria-label={t("conversation.attach.remove", { name: file.name })}><X size={14} /></button>
            </div>)}
          </div>}
          {dragging && <div className="kaus-drop-hint"><Paperclip size={18} />{t("conversation.attach.drop")}</div>}
          <div className="kaus-composer-row">
            <textarea
              ref={textareaRef}
              aria-label={t("conversation.message.label")}
              placeholder={t("draft.composer.placeholder")}
              value={text}
              disabled={sending}
              rows={3}
              onChange={(event) => setText(event.target.value)}
              onCompositionStart={composerInput.onCompositionStart}
              onCompositionEnd={composerInput.onCompositionEnd}
              onKeyDown={onComposerKeyDown}
              onPaste={(event) => { if (!event.clipboardData.files.length) return; event.preventDefault(); addFiles(Array.from(event.clipboardData.files)); }}
            />
          </div>
          </ComposerBar>
        </div>
      </div>
    </div>
  );
}
