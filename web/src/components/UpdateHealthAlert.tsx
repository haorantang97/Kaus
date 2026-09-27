import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { apiGet, apiPost, fetchNetwork } from "../lib/api";
import { Button, Card, confirmAsync, cream, Pill, toast } from "./ui";

interface Health {
  head?: string | null;
  version?: string | null;
  checked_at?: number;
  changed_since_last?: boolean;
  ok?: boolean;
  failures?: string[];
}

/* 更新后体检 · 被动告警:后端在 Hermes 版本变化时自动跑 selfcheck。
   绿 → 返回 null(零占位)。红 → 弹一张红卡:列出红项 + 操作(收敛 / 派 dev 修复)。
   检测全自动;修复人工把关(改 dashboard 代码必须有人点头)。 */
export function UpdateHealthAlert({ onGoProfile }: { onGoProfile: (name: string) => void }) {
  const [h, setH] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => apiGet<Health>("/api/update-health").then(setH).catch(() => setH(null));
  useEffect(() => {
    load();
    const t = setInterval(load, 30000); // 每 30s 轮询(后端比对廉价)
    return () => clearInterval(t);
  }, []);

  if (!h || h.ok !== false) return null; // 关键:绿 / 未知 → 零占位

  const onlyConverge = (h.failures || []).length === 1 && /收敛|converge/i.test(h.failures![0]);

  const runConverge = async () => {
    if (!(await confirmAsync("一键收敛会重写重复技能继承关系并清理冗余副本。确定现在执行？", {
      danger: true,
      okLabel: "执行收敛",
    }))) return;
    setBusy(true);
    try {
      await apiPost("/api/skills/converge-all", { dry_run: false });
      toast("已重新收敛技能,正在复检…", "ok");
      await apiPost<Health>("/api/update-health/run", {}).then(setH);
    } catch (e) {
      toast(`收敛失败:${e instanceof Error ? e.message : e}`, "bad");
    } finally { setBusy(false); }
  };
  const recheck = async () => {
    setBusy(true);
    try { await apiPost<Health>("/api/update-health/run", {}).then(setH); toast("已重新体检", "ok"); }
    finally { setBusy(false); }
  };
  const openFixAgent = async () => {
    setBusy(true);
    try {
      const net = await fetchNetwork();
      const target = ["dashboard-project", "dashboard-ops", "coding", "default"].find((name) => net.nodes[name]);
      if (!target) {
        toast("没有找到可承接修复的 agent", "bad");
        return;
      }
      onGoProfile(target);
      toast(`已打开 ${net.nodes[target].label || target} 对话`, "ok");
    } catch (e) {
      toast(`打开修复 agent 失败:${e instanceof Error ? e.message : e}`, "bad");
    } finally { setBusy(false); }
  };

  return (
    <Card
      title="更新后体检 · 待处理"
      icon={ShieldAlert}
      right={<Pill tone="gold">{(h.failures || []).length} 红</Pill>}
      className="mb-4"
    >
      <p className="mb-2 text-[0.68rem]" style={{ color: cream(48) }}>
        检测到 Hermes 内核更新(<span className="font-mono">{h.version || h.head?.slice(0, 8) || "?"}</span>),自动体检发现 dashboard 对接有漂移。
        修复只动 <span className="font-mono">~/.hermes/dashboard/</span>,绝不碰 Hermes 核心。
      </p>
      <div className="mb-3 flex flex-col gap-1">
        {(h.failures || []).map((f, i) => (
          <div key={i} className="flex items-center gap-2 text-[0.74rem]">
            <span className="size-1.5 rounded-full" style={{ background: "var(--danger)" }} />
            <span style={{ color: "var(--midground-base)" }}>{f}</span>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {onlyConverge ? (
          <Button variant="primary" onClick={runConverge} disabled={busy}>一键收敛技能</Button>
        ) : (
          <Button variant="primary" onClick={openFixAgent} disabled={busy}>
            打开修复 agent
          </Button>
        )}
        <Button variant="ghost" onClick={recheck} disabled={busy}>重新体检</Button>
      </div>
    </Card>
  );
}
