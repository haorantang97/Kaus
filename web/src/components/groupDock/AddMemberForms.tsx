import { useEffect, useState } from "react";

import { useLocale } from "../../i18n";
import { engineAcceptsNewWork } from "../../lib/engineAvailability";
import { EngineIcon } from "../EngineIcon";
import { ProjectAgentPicker } from "../ProjectAgentPicker";
import { BarMenu, type BarMenuOption } from "../ComposerBar";
import { Button } from "../ui";
import { modelOptionsFor, modelSelection, modelValue } from "../../lib/modelCatalogView";
import { backendDisplay } from "../../lib/backend-display";
import {
  fetchRecentConversations,
  fetchConnectedBackends,
  fetchModelCatalog,
  type BindingWire,
  type ProjectWire,
} from "../../lib/sessionApi";

/* ------------------------------------------------------------------ *
 * 添加成员：两条路
 * ------------------------------------------------------------------ */

/** A new member chooses a global engine before its conversation starts. */
export function SpawnForm({
  projects,
  defaultProjectId,
  onSubmit,
  onCancel,
}: {
  projects: ProjectWire[];
  defaultProjectId: string | null;
  onSubmit: (body: { projectId: string; bindingId: string | null; modelId: string | null; providerId?: string; initialMessage: string | null }) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const [projectId, setProjectId] = useState(defaultProjectId ?? projects[0]?.id ?? "");
  const [binding, setBinding] = useState<BindingWire | null>(null);
  const [models, setModels] = useState<BarMenuOption[]>([]);
  const [modelId, setModelId] = useState<string | null>(null);
  const [first, setFirst] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { setBinding(null); setModels([]); setModelId(null); }, [projectId]);

  useEffect(() => {
    setModels([]); setModelId(null);
    if (!binding) return;
    const controller = new AbortController();
    fetchModelCatalog(binding.backendId, binding.id, controller.signal)
      .then((catalog) => {
        if (controller.signal.aborted) return;
        const options = modelOptionsFor(catalog, binding);
        setModels(options);
        setModelId(modelValue(catalog.defaultModelId, catalog.defaultProviderId, catalog) ?? options.find(option => !option.uncertain)?.value ?? null);
      })
      // 目录取不到就不给模型下拉：后端会用 Binding 的默认模型（AD-71）。
      .catch(() => setModels([]));
    return () => controller.abort();
  }, [binding]);

  const submit = async () => {
    if (!projectId || !binding || busy) return;
    setBusy(true);
    try {
      await onSubmit({
        projectId,
        bindingId: binding?.id ?? null,
        ...(modelId ? { ...modelSelection(modelId), providerId: modelSelection(modelId).providerId ?? models.find(model => model.value === modelId)?.providerId ?? undefined } : { modelId: null }),
        initialMessage: first.trim() || null,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="kaus-group-form" data-testid="group-spawn-form">
      <BarMenu
        label={t("group.spawn.project")}
        value={projectId}
        display={projects.find((project) => project.id === projectId)?.displayName ?? projectId}
        options={projects.map((project) => ({ value: project.id, label: project.displayName || project.slug }))}
        onSelect={setProjectId}
      />
      <ProjectAgentPicker projectId={projectId} value={binding?.id} onChange={setBinding} disabled={busy} />
      {models.length > 0 && (
        <BarMenu
          label={t("group.spawn.model")}
          value={modelId}
          display={models.find((model) => model.value === modelId)?.label ?? modelId ?? t("group.spawn.model")}
          options={models}
          onSelect={setModelId}
        />
      )}
      <input
        className="kaus-group-input"
        data-testid="group-spawn-first"
        aria-label={t("group.spawn.first")}
        placeholder={t("group.spawn.firstPlaceholder")}
        value={first}
        onChange={(event) => setFirst(event.target.value)}
      />
      <span className="kaus-group-form-actions">
        <Button onClick={onCancel}>{t("ui.cancel")}</Button>
        <Button variant="primary" disabled={busy || !projectId || !binding} onClick={() => void submit()}>
          {t("group.spawn.submit")}
        </Button>
      </span>
    </div>
  );
}

/** 「选择已有会话」：当前项目下的会话，走 `BarMenu`（项数多了它自己带过滤框）。 */
export function ExistingPicker({
  projectId,
  projectName,
  onPick,
  onCancel,
}: {
  projectId: string | null;
  /** batch52：`projectId → 项目名`（取不到就是 null，那一段不出现）。 */
  // batch52
  projectName: (projectId: string | null | undefined) => string | null;
  onPick: (conversationId: string) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const [options, setOptions] = useState<BarMenuOption[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    /* 缺省的 `GET /api/conversations` **不含** `group_only` 的那些——它们本来就已经
       在某个组里，列出来只会让人点到一个必然 409 的选项。 */
    Promise.all([fetchRecentConversations(controller.signal, projectId ? { project: projectId } : {}), fetchConnectedBackends(controller.signal)])
      .then(([payload, connections]) =>
        setOptions(
          payload.conversations.filter(row => engineAcceptsNewWork(connections.backends.find(engine => engine.id === row.backendId))).map((row) => ({
            value: row.id,
            icon: row.backendId ? <EngineIcon engine={row.backendId} /> : undefined,
            /* batch52 第 5 件（真机「藏过头了」第 1 条）：一行由「只有标题」改成
               「标题 · 引擎 · 项目」。长列表里几条同名的旧会话此前长得一模一样，
               靠标题分不出该点哪一条。缺的那一段**整个不出现**（AD-71：不写
               「未知引擎」），所以只有标题时这一行与从前一个字不差。 */
            label: [
              row.title,
              row.backendId ? backendDisplay(row.backendId).displayName : null,
              projectName(row.projectId),
            ]
              .filter(Boolean)
              .join(" · "),
          })),
        ),
      )
      .catch(() => setOptions([]));
    return () => controller.abort();
  }, [projectId, projectName]);

  return (
    <div className="kaus-group-form" data-testid="group-existing-picker">
      <BarMenu
        label={t("group.add.existing")}
        value={null}
        display={t("group.add.pick")}
        options={options ?? []}
        emptyNote={t("group.add.noConversations")}
        onSelect={(conversationId) => void onPick(conversationId)}
      />
      <span className="kaus-group-form-actions">
        <Button onClick={onCancel}>{t("ui.cancel")}</Button>
      </span>
    </div>
  );
}
