import { ErrorNotice } from "./ui";
import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { Button, cream, dname, HAIR, inputStyle, Pill, Spinner, toast } from "./ui";

interface Layer { node: string; label: string; text: string; is_self: boolean }
interface ResolvedRule { id: string; op: string; node: string; label: string; title: string; disabled: boolean; is_self: boolean }
interface NodeConst {
  name: string;
  label: string;
  own: string;
  inherited: Layer[];
  effective: string;
  governed: boolean;
  rules?: ResolvedRule[];
}

/* 某项目的守则编辑器：继承自上级的各层(只读、灰显) + 本级(可编辑)。内容只存领域库，
   送达各 Agent 由驱动的指令投射负责。 */
export function ConstitutionEditor({ name, onSaved }: { name: string; onSaved?: () => void }) {
  const [d, setD] = useState<NodeConst | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const load = () => apiGet<NodeConst>(`/api/constitution/${encodeURIComponent(name)}`)
    .then((r) => { setErr(null); setD(r); setText(r.own); })
    .catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { setD(null); setErr(null); load(); }, [name]);

  if (err) return <ErrorNotice error={err} className="py-3 text-xs" />;
  if (!d) return <div className="flex items-center gap-2 py-3 text-xs" style={{ color: cream(50) }}><Spinner /> 加载中…</div>;
  const dirty = text !== d.own;
  const save = async () => {
    setSaving(true);
    try {
      const r = await apiPost<{ resynced: number }>(`/api/constitution/${encodeURIComponent(name)}`, { text });
      toast(`已保存，影响 ${r.resynced} 个项目`, "ok");
      await load();
      onSaved?.();
    } catch (e) { toast(`保存失败：${e instanceof Error ? e.message : e}`, "bad"); }
    setSaving(false);
  };

  return (
    <div className="flex flex-col gap-2">
      {d.inherited.length > 0 && (
        <div>
          <div className="mb-1 text-[0.62rem] uppercase tracking-[0.14em]" style={{ color: cream(45) }}>继承自上级（只读 · rule_id 可被本级 override / disable）</div>
          <div className="flex flex-col gap-1.5">
            {d.inherited.map((l) => (
              <div key={l.node} className="rounded-md p-2" style={{ border: HAIR, background: cream(3) }}>
                <div className="mb-0.5 text-[0.66rem] font-semibold" style={{ color: "var(--accent-text)" }}>⟦{l.label}⟧</div>
                <pre className="m-0 whitespace-pre-wrap text-[0.68rem] leading-snug" style={{ color: cream(55), fontFamily: "var(--theme-font-mono, monospace)" }}>{l.text}</pre>
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="mt-1 flex items-center gap-2">
        <span className="text-[0.62rem] uppercase tracking-[0.14em]" style={{ color: cream(45) }}>本级守则 · {dname({ name })} 自己这层</span>
        {d.governed ? <Pill tone="gold">已生效</Pill> : <Pill tone="muted">无内容</Pill>}
      </div>
      <div className="rounded-md p-2 text-[0.66rem] leading-relaxed" style={{ border: HAIR, background: cream(3), color: cream(50) }}>
        规则语法：<code>### [rule:family-love] 标题</code> 定义；<code>### [override:family-love] 标题</code> 覆盖父级同 ID；<code>### [disable:family-love]</code> 屏蔽父级同 ID。判断靠 ID 和操作痕迹，不猜语义。
      </div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} spellCheck={false}
        placeholder={"写本级红线 / 目标 / 风格。普通文本会叠加；需要替换父级某条时使用 rule_id。\n例：\n### [override:family-love] 家庭偏好\n我爱爸爸"}
        style={{ ...inputStyle(), resize: "vertical", lineHeight: 1.6 }} />
      {(d.rules || []).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {(d.rules || []).map((r) => (
            <Pill key={`${r.id}:${r.node}`} tone={r.disabled ? "muted" : r.is_self ? "gold" : "success"}>
              {r.disabled ? "屏蔽" : r.op === "override" ? "覆盖" : "继承"} · {r.id} · {r.label}
            </Pill>
          ))}
        </div>
      )}
      {d.effective && (
        <details className="rounded-md p-2" style={{ border: HAIR, background: cream(2) }}>
          <summary className="cursor-pointer text-[0.62rem] uppercase tracking-[0.14em]" style={{ color: cream(45) }}>有效守则预览</summary>
          <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap text-[0.66rem] leading-snug" style={{ color: cream(55), fontFamily: "var(--theme-font-mono, monospace)" }}>{d.effective}</pre>
        </details>
      )}
      <div className="flex items-center gap-2">
        <Button variant="primary" disabled={saving || !dirty} onClick={save}>{saving ? "保存中…" : "保存"}</Button>
        {dirty && <span className="text-[0.62rem]" style={{ color: cream(45) }}>有未保存改动</span>}
      </div>
    </div>
  );
}
