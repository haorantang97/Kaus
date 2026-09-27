/* 项目详情页 ② 区的取数壳：`GET /api/projects/{id}/bindings` + `GET /api/backends`
 * + 每条 Binding 的 `GET /api/backends/{id}/models?binding=`，拼成 EngineRow[] 交给
 * <EnginePanel>（面板本身不请求数据，component-boundaries §6）。
 *
 * 这是全站**唯一**要 detail 层能力的地方（AD-71）——能力清单上的 note 与
 * verification 只在这张面板里出现，对话页那棵子树连类型都拿不到。
 *
 * 写端点（批次八第 2 件）从这里接上：改模型 / 改推理强度 → `PATCH /bindings/{id}`；
 * 设默认 → `POST /bindings/{id}/make-default`；接入 → `POST /projects/{id}/bindings`；
 * 解除 → `DELETE /bindings/{id}`。
 *
 * batch19：登录态（AD-82/93）与原生会话数并入了 bindings 的每一行，这里直接透传给
 * 面板——不为此多发请求（会话页页头那一处走 `GET /api/bindings/{id}/status`）。
 * 拉起登录流程的写端点仍然没有，`onSignIn` 不传 ⇒ 按钮不渲染，只显示状态。
 */

import { useCallback, useEffect, useState } from "react";

import { useLocale } from "../i18n";
import { ConnectEngineModal } from "./ConnectEngineModal";
import { EnginePanel, type AttachableBackend, type EngineRow } from "./EnginePanel";
import { DriftModal, MaterializeModal } from "./MaterializeModal";
import { confirmAsync, cream, toast } from "./ui";
import {
  createBinding,
  deleteBinding,
  fetchBackends,
  fetchBackendWithCapabilityDetail,
  fetchBindingDrift,
  fetchEffectiveCapabilities,
  fetchEffectiveSettings,
  fetchModelCatalog,
  fetchProjectBindings,
  fetchProjectionMeta,
  makeBindingDefault,
  patchBinding,
  type BackendWire,
  type BindingDriftWire,
  type EffectiveCapabilitiesWire,
  type EffectiveSettingsWire,
} from "../lib/sessionApi";

export function ProjectEngines({
  projectId,
  projectLabel,
  onNewConversation,
  onChanged,
  refreshToken = 0,
}: {
  /** 已解析好的 Project id（`project:<slug>`）。解析在页面层做。 */
  projectId: string;
  /** Project id → 显示名（引擎配置区的「继承自 X」用；页面持有项目列表）。 */
  projectLabel?: (projectId: string) => string;
  onNewConversation?: (row: EngineRow) => void;
  /** 写成功后通知页面（会话列表、能力表都可能跟着变）。 */
  onChanged?: () => void;
  refreshToken?: number;
}) {
  const { t } = useLocale();
  const [rows, setRows] = useState<EngineRow[] | null>(null);
  const [attachable, setAttachable] = useState<AttachableBackend[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [localToken, setLocalToken] = useState(0);
  /* batch25：每条 Binding 一次 drift。`undefined` = 还没读到，`null` = 读不着
     （端点不在 / `projection_unsupported` / 请求失败）——两者在卡片上都是"什么都不显示"，
     所以这里**失败静默**，不进 `error`（AD-71）。 */
  const [drifts, setDrifts] = useState<Record<string, BindingDriftWire | null>>({});
  /* batch26：入口门控换成 `projection/_meta`。`true` 才有「应用到引擎」与漂移行，
     也**只有那时才去拉 drift**——对一台没有投射面的引擎发一条必然失败的请求，
     既费一个来回又把"支不支持"和"这次读着没"搅在一起（AD-126）。 */
  const [projection, setProjection] = useState<Record<string, boolean>>({});
  const [modal, setModal] = useState<{ kind: "materialize" | "drift"; row: EngineRow } | null>(null);
  const [connecting, setConnecting] = useState(false);

  /** 拉一条 Binding 的 drift。并发调用彼此独立，一条失败不影响别的卡。 */
  const loadDrift = useCallback((bindingId: string) => {
    fetchBindingDrift(bindingId)
      .then((drift) => setDrifts((current) => ({ ...current, [bindingId]: drift })))
      .catch(() => setDrifts((current) => ({ ...current, [bindingId]: null })));
  }, []);

  /** 先问 `_meta`，`supported=true` 才接着拉 drift。问不到（端点不在 / 出错）
      = 什么都不渲染，失败静默（AD-71）。 */
  const loadProjection = useCallback(
    (bindingId: string) => {
      fetchProjectionMeta(bindingId)
        .then((meta) => {
          setProjection((current) => ({ ...current, [bindingId]: meta.supported === true }));
          if (meta.supported === true) loadDrift(bindingId);
        })
        .catch(() => setProjection((current) => ({ ...current, [bindingId]: false })));
    },
    [loadDrift],
  );

  const reload = useCallback(() => {
    setLocalToken((value) => value + 1);
    onChanged?.();
  }, [onChanged]);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    (async () => {
      const { bindings } = await fetchProjectBindings(projectId);
      const backends = new Map<string, BackendWire>();
      // 同一个引擎被挂多次时只取一次声明。
      for (const backendId of new Set(bindings.map((binding) => binding.backendId))) {
        backends.set(backendId, await fetchBackendWithCapabilityDetail(backendId));
      }
      const catalogs = new Map<string, Awaited<ReturnType<typeof fetchModelCatalog>> | null>();
      /* 批次十三第 4 件：模型 / 推理强度 / 审批模式的取值与来源都从这里来。
         端点不在（404）时 `fetchEffectiveSettings` 自己回退成"四项都没有"，
         面板于是退回旧的 runtimeConfig 猜键，不报错。 */
      const settings = new Map<string, EffectiveSettingsWire | null>();
      for (const binding of bindings) {
        /* 拿不到目录（501 / 请求失败）= 没有目录这回事：那一行只读。
           **空目录不等于没有目录**（批次十五第 5 件）：菜单照旧点得开，
           里面写「引擎未报告可用模型」，用户才知道是引擎没报而不是界面坏了。 */
        catalogs.set(binding.id, await fetchModelCatalog(binding.backendId, binding.id).catch(() => null));
        settings.set(binding.id, await fetchEffectiveSettings(binding.id).catch(() => null));
      }
      /* AD-97：引擎配置区的数据。`?backend=<裸 key>` 把结果收窄成「通用 + 该引擎的
         scoped 能力」；面板再只留 scoped 的那半（通用能力在项目页的「能力」表里）。
         读不到（端点不在 / 出错）就传 null：那一区显示「没有专属配置」，不报错。 */
      const scoped = new Map<string, EffectiveCapabilitiesWire | null>();
      for (const [backendId, backend] of backends) {
        scoped.set(
          backendId,
          await fetchEffectiveCapabilities(projectId, { backend: backend.key }).catch(() => null),
        );
      }
      const all = await fetchBackends().catch(() => ({ backends: [] as BackendWire[], count: 0 }));
      if (cancelled) return;
      // 进项目页时每张卡先问一次 `_meta`（并发，失败静默），支持的才接着拉 drift。
      // 不 await：漂移那一行晚一点出现无所谓，引擎卡本身不该为它等着。
      for (const binding of bindings) loadProjection(binding.id);
      setAttachable(all.backends.map((backend) => ({ id: backend.id, displayName: backend.displayName })));
      setRows(
        bindings.flatMap((binding) => {
          const backend = backends.get(binding.backendId);
          const catalog = catalogs.get(binding.id) ?? null;
          return backend
            ? [
                {
                  binding,
                  backend,
                  catalog,
                  effectiveSettings: settings.get(binding.id) ?? null,
                  capabilities: scoped.get(binding.backendId) ?? null,
                  /* batch19 第 1、2 件（AD-82/93）：登录态与原生会话数**跟着
                     bindings 行一起来**，所以这里一条额外请求都不加；老后端没有
                     这两个键时它们就是 undefined，面板那两行不渲染（AD-71）。 */
                  auth: binding.auth,
                  nativeSessionCount: binding.nativeSessionCount ?? null,
                },
              ]
            : [];
        }),
      );
    })().catch((failure) => {
      if (cancelled) return;
      // 拿不到就说拿不到：这里不假装「这个项目没有引擎」。
      setError(String((failure as Error)?.message ?? failure));
      setRows([]);
    });
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshToken, localToken, loadProjection]);

  /** 写端点统一走这一条：成功就重取（服务端是真源，不在前端猜新值）。 */
  const write = useCallback(
    async (label: string, run: () => Promise<unknown>) => {
      try {
        await run();
        toast(t("engines.write.ok", { label }), "ok");
        reload();
      } catch (failure) {
        toast(
          t("engines.write.fail", {
            label,
            error: String(failure instanceof Error ? failure.message : failure),
          }),
          "bad",
        );
      }
    },
    [reload, t],
  );

  /** 字段类写入（模型 / 推理强度）：成功给一句「已保存」，失败**往上抛**——
   *  面板要把错误显示在字段旁并保留用户输入（批次十一第 2 件），不是弹个 toast 了事。 */
  const writeField = useCallback(
    async (run: () => Promise<unknown>) => {
      await run();
      toast(t("engines.write.saved"), "ok");
      reload();
    },
    [reload, t],
  );

  if (rows === null && !error) {
    return (
      <div className="px-1 py-3 text-xs" style={{ color: cream(45) }}>
        {t("engines.loading")}
      </div>
    );
  }
  return (
    <>
      {error && (
        <div className="px-1 pb-2 text-xs" style={{ color: cream(45) }}>
          {t("engines.error", { error })}
        </div>
      )}
      <EnginePanel
        rows={(rows ?? []).map((row) => ({
          ...row,
          ...(row.binding.id in projection ? { projectionSupported: projection[row.binding.id] } : {}),
          ...(row.binding.id in drifts ? { drift: drifts[row.binding.id] } : {}),
        }))}
        projectLabel={projectLabel}
        onNewConversation={onNewConversation}
        onChangeModel={(row, modelId) =>
          writeField(() => patchBinding(row.binding.id, { defaultModelId: modelId || null }))
        }
        onChangeReasoning={(row, level) =>
          writeField(() =>
            // 通用键白名单里的那个（binding_router.GENERIC_RUNTIME_CONFIG_KEYS）；
            // 传 null 表示清掉这个键，回到引擎自己的默认。
            patchBinding(row.binding.id, { runtimeConfig: { reasoning_effort: level || null } }),
          )
        }
        onChangeApproval={(row, mode) =>
          writeField(() => patchBinding(row.binding.id, { runtimeConfig: { approval_mode: mode } }))
        }
        onMakeDefault={(row) =>
          void write(t("engines.write.makeDefault"), () => makeBindingDefault(row.binding.id))
        }
        onDetach={(row) =>
          void (async () => {
            const ok = await confirmAsync(
              t("engines.detach.confirm", { name: row.binding.displayName }),
              { danger: true, okLabel: t("engines.detach.ok") },
            );
            if (!ok) return;
            await write(t("engines.write.detach"), () => deleteBinding(row.binding.id));
          })()
        }
        onConnectEngine={() => setConnecting(true)}
        onMaterialize={(row) => setModal({ kind: "materialize", row })}
        onViewDrift={(row) => setModal({ kind: "drift", row })}
      />
      {/* 弹窗由容器渲染（面板不请求数据）；「应用到引擎」的 dry-run / 写入都在弹窗里发。 */}
      {modal?.kind === "materialize" && (
        <MaterializeModal
          bindingId={modal.row.binding.id}
          displayName={modal.row.binding.displayName}
          onClose={() => setModal(null)}
          /* 写完重取一次这条 Binding 的 drift：刚写进去的键该立刻显示成"与项目一致"。 */
          onWritten={() => loadDrift(modal.row.binding.id)}
        />
      )}
      {/* batch40（★L 第 3 条）：「接入引擎…」是引擎区**唯一**的接入入口——原来那枚
          独立的下拉 + 「接入」按钮搬进了这个弹窗。 */}
      {connecting && (
        <ConnectEngineModal
          attachable={attachable}
          onAttach={(backendId) => {
            setConnecting(false);
            void write(t("engines.write.attach"), () => createBinding(projectId, { backendId }));
          }}
          onClose={() => setConnecting(false)}
        />
      )}
      {modal?.kind === "drift" && drifts[modal.row.binding.id] && (
        <DriftModal
          drift={drifts[modal.row.binding.id] as BindingDriftWire}
          displayName={modal.row.binding.displayName}
          onClose={() => setModal(null)}
          /* batch27 第 6 件（真机 A4）：漂移弹窗上的「用项目配置覆盖引擎侧」——
             不另开一块界面，就是切到同一枚弹窗的 materialize 流程（照旧先 dry-run，
             写入仍然要用户再按一次「备份后写入」）。 */
          onMaterialize={() => setModal({ kind: "materialize", row: modal.row })}
        />
      )}
    </>
  );
}
