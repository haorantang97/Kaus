import { useEffect, useState } from "react";

import { sessionRequest } from "./sessionApi";
import { WORKBENCH_CHANGED } from "./workbenchApi";

/** 模块任务停在审批、提问或登录上（模块会话不进普通聊天列表，由宿主统一列出）。 */
export interface ModuleAttentionItem {
  moduleId: string;
  moduleName: string | null;
  jobId: string;
  stepTitle: string | null;
  conversationId: string;
  updatedAt: string | null;
}

export const MODULE_ATTENTION_INTERVAL_MS = 30_000;

export const fetchModuleAttention = (signal?: AbortSignal) =>
  sessionRequest<{ items: ModuleAttentionItem[] }>("/api/workbench/attention", { signal });

/** 与侧栏索引同一节奏轮询；工作台有变化时立即刷新。拿不到就是空（不报错，不猜）。 */
export function useModuleAttention(): ModuleAttentionItem[] {
  const [items, setItems] = useState<ModuleAttentionItem[]>([]);
  useEffect(() => {
    let controller: AbortController | null = null;
    const load = () => {
      controller?.abort();
      controller = new AbortController();
      fetchModuleAttention(controller.signal)
        .then(value => setItems(Array.isArray(value?.items) ? value.items : [])).catch(() => {});
    };
    load();
    const timer = window.setInterval(load, MODULE_ATTENTION_INTERVAL_MS);
    window.addEventListener(WORKBENCH_CHANGED, load);
    return () => { window.clearInterval(timer); window.removeEventListener(WORKBENCH_CHANGED, load); controller?.abort(); };
  }, []);
  return items;
}
