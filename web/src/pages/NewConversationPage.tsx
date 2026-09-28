import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FileText, LoaderCircle, Lock, Paperclip, ArrowUp, Unlock, X } from "lucide-react";
import { useLocale } from "../i18n";
import { cream } from "../components/ui";
import { BarMenu, ComposerBar, type BarMenuOption } from "../components/ComposerBar";
import { AttentionBlock } from "../components/AttentionBlock";
import { backendDisplay } from "../lib/backend-display";
import type { SidebarGroup } from "../lib/conversationIndex";
import {
  createConversation,
  emptyEffectiveSettings,
  fetchEffectiveSettings,
  fetchModelCatalog,
  fetchProjectBindings,
  fetchProjects,
  fetchBackendUiCapabilities,
  patchConversation,
  patchProject,
  sendConversationMessage,
  type BindingWire,
  type EffectiveSettingsWire,
  type ModelCatalogWire,
  type ProjectWire,
  describeFailure,
  SessionApiError,
} from "../lib/sessionApi";
import { hasCapability, type UiCapabilities } from "../components/cards/capabilities";
import { ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_COUNT, fileSizeLabel, uploadConversationAttachment, type ConversationAttachment } from "../lib/attachmentsApi";
import { modelEffort, modelOptionsFor } from "../lib/modelCatalogView";
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
 *     项目 → 引擎（该项目的 bindings）→ 模型/推理（该 binding 的 Model Catalog）
 *
 * 模型下拉在拿不到 Catalog（501/空目录）时**整块不渲染**，不写「本引擎不支持」——AD-71。
 */

/** 项目清单 → 菜单项：按项目树深度优先排开，`depth` 交给 `BarMenu` 做缩进。
 *  父不在清单里的当顶层处理，环（不该出现，但真出现时）靠 `seen` 兜住。 */
export function projectMenuOptions(projects: ProjectWire[]): BarMenuOption[] {
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
  const walk = (parent: string | null, depth: number) => {
    for (const project of children.get(parent) ?? []) {
      if (seen.has(project.id)) continue;
      seen.add(project.id);
      options.push({ value: project.id, label: project.displayName || project.slug, depth });
      walk(project.id, depth + 1);
    }
  };
  walk(null, 0);
  // 环里的项目一个都不能丢：兜底补在末尾（顶层缩进）。
  for (const project of projects) {
    if (seen.has(project.id)) continue;
    options.push({ value: project.id, label: project.displayName || project.slug, depth: 0 });
  }
  return options;
}

export interface NewConversationPageProps {
  /** 从项目页进入时预填并锁定；用户可点解锁自行改。 */
  lockedProjectId?: string | null;
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

export function NewConversationPage({
  lockedProjectId = null,
  onCreated,
  indexGroups,
  onOpenConversation,
}: NewConversationPageProps) {
  const { t } = useLocale();
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [projectId, setProjectId] = useState<string>(() => lockedProjectId || readDefaultDestination());
  const [locked, setLocked] = useState<boolean>(Boolean(lockedProjectId));
  const [bindings, setBindings] = useState<BindingWire[] | null>(null);
  const [bindingId, setBindingId] = useState<string>("");
  const [catalog, setCatalog] = useState<ModelCatalogWire | null>(null);
  /* 批次十三：模型 / 推理强度 / 审批模式 / 工作目录都改从有效设置读（DESIGN ★ I），
     草稿页与会话页用**同一条**工具栏。端点不在时它自己回退成"四项都没有"。 */
  const [settings, setSettings] = useState<EffectiveSettingsWire | null>(null);
  const [caps, setCaps] = useState<UiCapabilities | null>(null);
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
  const [selectedReasoning, setSelectedReasoning] = useState<string | null>(null);
  const [selectedApproval, setSelectedApproval] = useState<string | null>(null);
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
    fetchProjects()
      .then(({ projects: list }) => {
        if (cancelled) return;
        setProjects(list);
        setProjectId((current) => (list.some((p) => p.id === current) ? current : list[0]?.id ?? current));
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
    setBindings(null);
    setBindingId("");
    setCatalog(null);
    // 每次换项目都先清错：上一个项目的失败（例如默认去向已被删掉、
    // 或首帧那个还没被项目列表纠正过来的 id）不该粘在新项目上。
    setError(null);
    fetchProjectBindings(projectId)
      .then(({ bindings: list }) => {
        if (cancelled) return;
        const usable = list.filter((binding) => binding.enabled);
        setBindings(usable);
        const preferred = usable.find((binding) => binding.isDefault) ?? usable[0];
        setBindingId(preferred?.id ?? "");
      })
      .catch((failure) => {
        if (cancelled) return;
        setBindings([]);
        setError(describeFailure(failure));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const binding = useMemo(
    () => bindings?.find((item) => item.id === bindingId) ?? null,
    [bindings, bindingId],
  );

  const reloadSettings = useCallback(async (bindingId: string) => {
    const next = await fetchEffectiveSettings(bindingId).catch(() => emptyEffectiveSettings(bindingId));
    setSettings(next);
  }, []);

  useEffect(() => {
    setSelectedReasoning(null);
    setSelectedApproval(null);
    if (!binding) {
      setCatalog(null);
      setSettings(null);
      setCaps(null);
      setSelectedModelId(null);
      return;
    }
    let cancelled = false;
    setCatalog(null);
    setSettings(null);
    setCaps(null);
    setSelectedModelId(null);
    fetchModelCatalog(binding.backendId, binding.id)
      .then((next) => {
        if (!cancelled) setCatalog(next);
      })
      .catch(() => {
        // 拿不到目录 = 这个引擎没有可选模型：下拉整块不渲染（AD-71），不解释。
        if (!cancelled) setCatalog(null);
      });
    void fetchEffectiveSettings(binding.id)
      .catch(() => emptyEffectiveSettings(binding.id))
      .then((next) => {
        if (!cancelled) setSettings(next);
      });
    void fetchBackendUiCapabilities(binding.backendId).then((next) => {
      if (!cancelled) setCaps(next);
    }).catch(() => { if (!cancelled) setCaps(null); });
    return () => {
      cancelled = true;
    };
  }, [binding]);

  useEffect(() => {
    writeDraftText(text);
  }, [text]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(240, Math.max(72, textarea.scrollHeight))}px`;
    textarea.style.overflowY = textarea.scrollHeight > 240 ? "auto" : "hidden";
  }, [text]);

  const bar = settings ?? emptyEffectiveSettings(bindingId);
  const modelOptions: BarMenuOption[] = useMemo(
    () =>
      modelOptionsFor(catalog, binding, selectedModelId || bar.model.value),
    [catalog, binding, selectedModelId, bar.model.value],
  );
  const modelDiagnostic = catalog?.diagnostics?.[0] ?? null;
  const modelDegraded = Boolean(catalog?.degraded);
  const controls = settings?.conversationControls;
  const selectedDescriptor = catalog?.models.find(model => model.modelId === (selectedModelId || bar.model.value || catalog.defaultModelId));
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
      if (bindingId) await reloadSettings(bindingId);
    },
    [bindingId, projectId, reloadSettings],
  );

  const send = useCallback(async () => {
    const body = text.trim();
    const stagedFiles = filesRef.current;
    if ((!body && !stagedFiles.length) || !projectId || !bindingId || sendLockRef.current || unconfirmedRef.current) return;
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
        draft = { id: conversation.id, modelId: conversation.modelId ?? null, uploads: new Map() };
        createdDraftsRef.current.set(draftKey, draft);
      }
      conversationId = draft.id;
      const requestedModel = selectedModelId || bar.model.value;
      if (requestedModel && canChooseModel && requestedModel !== draft.modelId) {
        const updated = await patchConversation(draft.id, { modelId: requestedModel });
        const adopted = updated.conversation.modelId;
        if (adopted && adopted !== requestedModel) throw new Error(t("draft.model.notApplied"));
        draft.modelId = adopted || requestedModel;
      }
      if (selectedReasoning) await patchConversation(draft.id, { reasoningMode: selectedReasoning });
      if (selectedApproval) await patchConversation(draft.id, { approvalMode: selectedApproval });
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
  }, [bindingId, bar.model.value, canAttach, canChooseModel, caps?.card.attachments, onCreated, projectId, selectedModelId, selectedReasoning, selectedApproval, text, t]);

  /* 第 4 件①：`Enter` 发送、`Shift+Enter` 换行，⌘/Ctrl+Enter 继续可用。
     中文输入法组字期间的 Enter 是"选词"，绝不能当发送（`isComposing`）。 */
  const onComposerKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key !== "Enter") return;
      if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
      if (event.shiftKey) return;
      event.preventDefault();
      void send();
    },
    [send],
  );

  const projectName = projects.find((item) => item.id === projectId)?.displayName ?? projectId;
  /* 第 5 件：项目菜单按项目树排（父在前、子跟着，`depth` 只用来缩进）。
     父不在这份清单里的（被过滤 / 权限外）当作顶层，不让它整枝消失。 */
  const projectOptions: BarMenuOption[] = useMemo(() => projectMenuOptions(projects), [projects]);

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
          <input ref={fileInputRef} type="file" multiple hidden aria-label={t("bar.attach")} accept={caps?.card.attachments === "images" ? "image/png,image/jpeg,image/webp,image/gif,image/avif" : undefined} disabled={sending} onChange={(event) => { const selected = Array.from(event.target.files || []); event.target.value = ""; addFiles(selected); }} />
          {!!files.length && <div className="kaus-attachment-tray" aria-label={t("conversation.attach.list")}>
            {files.map((file, index) => <div className="kaus-attachment-chip" key={`${file.name}:${file.lastModified}:${index}`} title={file.name}>
              {uploadingName === file.name ? <LoaderCircle size={17} /> : <FileText size={17} />}
              <span><strong>{file.name}</strong><small>{uploadingName === file.name ? t("conversation.attach.uploading") : fileSizeLabel(file.size)}</small></span>
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
              onKeyDown={onComposerKeyDown}
              onPaste={(event) => { if (!event.clipboardData.files.length) return; event.preventDefault(); addFiles(Array.from(event.clipboardData.files)); }}
            />
          </div>
          <ComposerBar
            settingsAreDefaults
            leading={
              <>
                {locked ? (
                  <span className="kaus-bar-pill" data-bar-item={t("draft.field.project")}>
                    {projectName}
                    <button type="button" disabled={sending} onClick={() => setLocked(false)} title={t("draft.unlock")}>
                      <Lock className="size-3" />
                    </button>
                  </span>
                ) : (
                  <>
                    <BarMenu
                      label={t("draft.field.project")}
                      value={projectId}
                      display={projectName}
                      options={projectOptions}
                      onSelect={(next) => { if (!sendLockRef.current) setProjectId(next); }}
                    />
                    {lockedProjectId && (
                      <button
                        type="button"
                        className="kaus-bar-pill is-button"
                        disabled={sending}
                        onClick={() => { setLocked(true); setProjectId(lockedProjectId); }}
                        title={t("draft.relock")}
                      >
                        <Unlock className="size-3" />
                      </button>
                    )}
                  </>
                )}
                {/* 引擎在草稿页是可选的（会话建出来之后就定死了，会话页那边只读）。 */}
                <BarMenu
                  label={t("draft.field.engine")}
                  value={bindingId}
                  display={
                    binding
                      ? `${backendDisplay(binding.backendId).displayName} · ${binding.displayName}`
                      : bindings === null
                        ? t("draft.engine.loading")
                        : t("draft.engine.none")
                  }
                  options={(bindings ?? []).map((item) => ({
                    value: item.id,
                    label: `${backendDisplay(item.backendId).displayName} · ${item.displayName}`,
                  }))}
                  onSelect={(next) => { if (!sendLockRef.current) setBindingId(next); }}
                />
              </>
            }
            workspaceRoot={bar.workspaceRoot}
            onChangeWorkspaceRoot={changeWorkspaceRoot}
            model={selectedModelId ? { value: selectedModelId, source: "catalog" } : bar.model}
            modelOptions={modelOptions}
            modelDiagnostic={modelDiagnostic}
            modelDegraded={modelDegraded}
            onChangeModel={canChooseModel && !sending ? setSelectedModelId : undefined}
            reasoning={reasoning}
            onChangeReasoning={controls?.reasoning && !sending ? setSelectedReasoning : undefined}
            approval={approval}
            onChangeApproval={approval.options.length && !sending ? setSelectedApproval : undefined}
            attachments={caps?.card.attachments}
            onAttach={canAttach && !sending ? () => fileInputRef.current?.click() : undefined}
            actions={
              <button
                type="button"
                className="kaus-send"
                aria-label={sending ? t("draft.sending") : t("draft.send")}
                title={sending ? t("draft.sending") : t("draft.send")}
                disabled={(!text.trim() && !files.length) || !bindingId || sending || !!unconfirmed || (files.length > 0 && !canAttach)}
                onClick={() => void send()}
              >
                {sending ? <LoaderCircle size={18} aria-hidden /> : <ArrowUp size={18} aria-hidden />}
              </button>
            }
          />
        </div>
      </div>
    </div>
  );
}
