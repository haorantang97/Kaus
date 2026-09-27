import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { apiDelete, apiGet, apiPost, type OrgNode } from "../lib/api";
import { Button, confirmAsync, cream, dname, ErrLine, FieldRow, HAIR, inputStyle, Loading, Modal, ModalSub, ModalTitle, Pill, Section, toast } from "./ui";
import { ConstitutionEditor } from "./ConstitutionEditor";

/* ------------------------- response types ------------------------- */
interface ProfileDetail {
  name: string; role: string; killed: boolean; effective_killed: boolean; main_twin: boolean;
  parent: string | null; model: string | null; provider: string | null; soul: string | null;
  skill_count: number; is_default: boolean;
}
interface MemoryResp { name: string; is_twin: boolean; memory: string; memory_size: number; user: string; user_size: number; }
interface KnownModel { default: string; provider?: string | null; base_url?: string | null; }
interface LeversResp {
  name: string; is_twin: boolean; model: string | { default?: string } | null; model_own: boolean;
  memory_enabled: boolean; memory_own: boolean; terminal_backend: string; terminal_own: boolean;
  approvals_mode: string; approvals_own: boolean; reasoning_effort: string; reasoning_own: boolean;
  known_models: KnownModel[];
}
interface SkillItem { name: string; desc?: string; category?: string; inherited: boolean; source?: string | null; via?: string; }
interface SkillsResp { name: string; skills: SkillItem[]; converted: boolean; }
interface ImpactResp {
  name: string; descendants: string[]; external_dirs: string[];
  symlink_consumers: { name: string; skills: string[] }[];
  summary: { descendants_count: number; config_affected: number; external_dirs_count: number; symlink_consumer_count: number };
}
interface BackupItem { kind: string; file: string; summary: string; when: string; ago: string; restore_target: string; }
interface SubtasksResp { name: string; tasks: { id: string; title: string; status: string; priority?: number }[]; total: number; }

const ROLE_LABEL: Record<string, string> = { none: "独立", source: "技能源", shared: "共享成员" };
const kb = (n: number) => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);
const Note = ({ children }: { children: React.ReactNode }) => (
  <p className="text-[0.7rem] leading-relaxed" style={{ color: cream(45) }}>{children}</p>
);

/* ============================ drawer ============================ */
export function ProfileDrawer({ node, onClose, onChanged, refreshKey = 0, topSlot }: { node: OrgNode; onClose: () => void; onChanged: () => void; refreshKey?: number;
  /** 抽屉正文最上面的一段。项目详情页（`/projects/:ref`）用它挂「已接引擎」面板。 */
  topSlot?: React.ReactNode }) {
  const name = node.name;
  const [detail, setDetail] = useState<ProfileDetail | null>(null);
  const [detailErr, setDetailErr] = useState<string | null>(null);
  const [killed, setKilled] = useState(node.effective_killed);

  useEffect(() => {
    setDetailErr(null);
    apiGet<ProfileDetail>(`/api/profile/${encodeURIComponent(name)}`)
      .then((d) => { setDetail(d); setKilled(d.killed); })
      .catch((e) => setDetailErr(String(e?.message ?? e)));
  }, [name, refreshKey]);

  const toggleKill = async () => {
    const next = !killed;
    if (!(await confirmAsync(next ? `停用「${dname(node)}」？会级联冻结它和子树经仪表盘开终端/派任务，随时可恢复。` : `恢复「${dname(node)}」？`, { danger: next }))) return;
    try {
      await apiPost("/api/kill", { name, killed: next });
      setKilled(next);
      onChanged();
    } catch (e) { toast(`操作失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };

  return createPortal(
    <>
      <div className="fixed inset-0 z-40" style={{ background: "var(--overlay-scrim)" }} onClick={onClose} />
      <aside
        className="fixed right-0 top-0 z-40 flex h-dvh flex-col"
        style={{ width: 470, maxWidth: "95vw", background: "var(--panel)", borderLeft: HAIR, boxShadow: "var(--shadow-drawer)", color: "var(--midground-base)" }}
      >
        <div className="flex items-center gap-2.5 px-6 py-4" style={{ borderBottom: HAIR }}>
          <span className="text-base font-semibold">{dname(node)}</span>
          {node.name !== dname(node) && <Pill tone="muted">{node.name}</Pill>}
          {node.main_twin && <Pill tone="gold">化身</Pill>}
          <button className="ml-auto grid size-7 place-items-center rounded hover:bg-black/[0.05]" style={{ color: cream(55) }} onClick={onClose} title="关闭">
            <X className="size-4.5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-12">
          {topSlot && <div className="pt-4">{topSlot}</div>}
          {/* 标识 */}
          <Section title="标识 · Identity">
            {detailErr && <ErrLine e={detailErr} />}
            <FieldRow label="名字">{dname(node)}</FieldRow>
            <FieldRow label="类型">{node.main_twin ? "化身（与主 AI 同脑）" : ROLE_LABEL[detail?.role ?? node.role] ?? "独立"}</FieldRow>
            <FieldRow label="上级">{node.parent ? dname({ name: node.parent }) : <Pill tone="gold">最高层</Pill>}</FieldRow>
            <FieldRow label="模型">{node.model || "—"}{detail?.provider ? <Pill tone="muted">{detail.provider}</Pill> : null}</FieldRow>
            <FieldRow label="状态">
              <span className="flex items-center gap-2">
                {killed ? <Pill tone="danger">已停用</Pill> : <Pill tone="success">运行中</Pill>}
                {!node.main_twin && node.name !== "default" && (
                  <button className="text-[0.7rem] underline" style={{ color: cream(55) }} onClick={toggleKill}>{killed ? "恢复" : "停用"}</button>
                )}
              </span>
            </FieldRow>
          </Section>

          {/* 人格（就地可编辑） */}
          <PersonaSection name={name} onSaved={onChanged} refreshKey={refreshKey} />

          {/* 宪法（本级层 + 继承层）—— 沿组织树分层继承，注入 SOUL 顶部 */}
          <Section title="宪法 · Constitution" meta="沿组织树分层继承 · 注入 SOUL 顶部">
            <ConstitutionEditor name={name} onSaved={onChanged} />
          </Section>

          <MemorySection name={name} refreshKey={refreshKey} />
          <LeversSection name={name} onSaved={onChanged} refreshKey={refreshKey} />
          <SkillsSection name={name} onChanged={onChanged} refreshKey={refreshKey} />
          <ImpactSection name={name} refreshKey={refreshKey} />
          <BackupsSection name={name} refreshKey={refreshKey} />
          <SubtasksSection name={name} refreshKey={refreshKey} />
        </div>
      </aside>
    </>,
    document.body,
  );
}

/* ============================ 人格（就地编辑） ============================ */
export function PersonaSection({ name, onSaved, refreshKey = 0 }: { name: string; onSaved: () => void; refreshKey?: number }) {
  const [d, setD] = useState<{ persona: string; is_twin: boolean } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const load = () => apiGet<{ persona: string; is_twin: boolean }>(`/api/agent/${encodeURIComponent(name)}/soul`).then((r) => {
    setErr(null);
    setD((prev) => {
      setText((current) => prev && current !== prev.persona ? current : r.persona);
      return r;
    });
  }).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { setD(null); setErr(null); load(); }, [name]);
  useEffect(() => { if (refreshKey) load(); }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = d !== null && text !== d.persona;
  const save = async () => {
    if (d?.is_twin && !(await confirmAsync(`「${name}」是化身。保存人格会实际修改主 agent X 的 SOUL。确定继续？`, { danger: true }))) return;
    setSaving(true);
    try { await apiPost(`/api/agent/${encodeURIComponent(name)}/soul`, { persona: text }); toast("人格已保存", "ok"); await load(); onSaved(); }
    catch (e) { toast(`保存失败：${e instanceof Error ? e.message : e}`, "bad"); }
    setSaving(false);
  };

  return (
    <Section title="人格 · Persona" meta="agent 本体设定(SOUL) · 就地可改">
      {err ? <ErrLine e={err} /> : d === null ? <Loading /> : (
        <div className="flex flex-col gap-2">
          {d.is_twin && <Note>⚠ 化身与主 AI（X）共享人格，改这里 = 改 X。</Note>}
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={8} spellCheck={false}
            placeholder="写这个 agent 的人格 / 职责 / 风格…（宪法块由系统自动叠在最上方，不在这里编辑）"
            style={{ ...inputStyle(), resize: "vertical", lineHeight: 1.6 }} />
          <div className="flex items-center gap-2">
            <Button variant="primary" disabled={saving || !dirty} onClick={save}>{saving ? "保存中…" : "保存人格"}</Button>
            {dirty && <span className="text-[0.62rem]" style={{ color: cream(45) }}>有未保存改动</span>}
          </div>
        </div>
      )}
    </Section>
  );
}

/* ============================ 记忆 ============================ */
export function MemorySection({ name, refreshKey = 0 }: { name: string; refreshKey?: number }) {
  const [d, setD] = useState<MemoryResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => apiGet<MemoryResp>(`/api/agent/${encodeURIComponent(name)}/memory`).then((r) => { setErr(null); setD(r); }).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { setD(null); setErr(null); load(); }, [name]);
  useEffect(() => { if (refreshKey) load(); }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Section title="引擎记忆">
      {err ? <ErrLine e={err} /> : d === null ? <Loading /> : (
        <div className="flex flex-col gap-3">
          {d.is_twin && <Note>与 X 共用记忆文件。</Note>}
          <MemField name={name} label="MEMORY.md" kind="memory" initial={d.memory} size={d.memory_size} isTwin={d.is_twin} onSaved={load} />
          <MemField name={name} label="USER.md" kind="user" initial={d.user} size={d.user_size} isTwin={d.is_twin} onSaved={load} />
        </div>
      )}
    </Section>
  );
}

function MemField({ name, label, kind, initial, size, isTwin, onSaved }: { name: string; label: string; kind: "memory" | "user"; initial: string; size: number; isTwin: boolean; onSaved: () => void }) {
  const [text, setText] = useState(initial);
  const [saving, setSaving] = useState(false);
  const initialRef = useRef(initial);
  useEffect(() => {
    setText((current) => current === initialRef.current ? initial : current);
    initialRef.current = initial;
  }, [initial]);
  const dirty = text !== initial;
  const save = async () => {
    if (isTwin && !(await confirmAsync(`「${name}」是化身。保存 ${label} 会修改 X 的同名记忆文件。确定继续？`, { danger: true }))) return;
    setSaving(true);
    try { await apiPost(`/api/agent/${encodeURIComponent(name)}/memory`, kind === "memory" ? { memory: text } : { user: text }); toast(`${label} 已保存`, "ok"); onSaved(); }
    catch (e) { toast(`保存失败：${e instanceof Error ? e.message : e}`, "bad"); }
    setSaving(false);
  };
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[0.62rem] uppercase tracking-[0.14em]" style={{ color: cream(45) }}>{label}</span>
        {size > 0 && <span className="text-[0.6rem]" style={{ color: cream(40) }}>{kb(size)}</span>}
      </div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} spellCheck={false}
        placeholder={kind === "memory" ? "这个 agent 的长期记忆（自动沉淀，也可手改）…" : "关于用户的画像 / 偏好…"}
        style={{ ...inputStyle(), resize: "vertical", lineHeight: 1.6 }} />
      <div className="mt-1 flex items-center gap-2">
        <Button variant="primary" disabled={saving || !dirty} onClick={save}>{saving ? "保存中…" : "保存"}</Button>
        {dirty && <span className="text-[0.62rem]" style={{ color: cream(45) }}>未保存</span>}
      </div>
    </div>
  );
}

/* ============================ 运行设置 (levers) ============================ */
const REASONING_CAP = /\bo[1-9]\b|r1|reasoner|thinking|qwq|deepseek-r|claude-(sonnet|opus)-4|claude-4|gpt-5|gemini-[0-9.]+-(flash|pro)-thinking/i;
const selStyle = { ...inputStyle(), padding: "6px 8px" };

export function LeversSection({ name, onSaved, refreshKey = 0 }: { name: string; onSaved: () => void; refreshKey?: number }) {
  const [d, setD] = useState<LeversResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => apiGet<LeversResp>(`/api/agent/${encodeURIComponent(name)}/levers`).then((r) => { setErr(null); setD(r); }).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { setFormTouched(false); setD(null); setErr(null); load(); }, [name]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (refreshKey) load(); }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // form state。formTouched:用户动过任一字段后,后台 refreshKey 重拉不再覆盖表单
  //(否则 SSE 一来正在选的模型/审批就被悄悄还原);切 agent 或保存成功后复位。
  const [formTouched, setFormTouched] = useState(false);
  const [modelSel, setModelSel] = useState("");
  const [modelDirty, setModelDirty] = useState(false);
  const [adv, setAdv] = useState(false);
  const [reasoning, setReasoning] = useState("__inherit__");
  const [mem, setMem] = useState("__inherit__");
  const [term, setTerm] = useState("__inherit__");
  const [appr, setAppr] = useState("manual");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (!d || d.is_twin || formTouched) return;
    const curModel = typeof d.model === "object" ? d.model?.default : d.model;
    const known = d.known_models || [];
    setModelSel(curModel && known.some((m) => m.default === curModel) ? `known:${curModel}` : "");
    setModelDirty(false);
    setReasoning(!d.reasoning_own ? "__inherit__" : d.reasoning_effort);
    setMem(!d.memory_own ? "__inherit__" : d.memory_enabled ? "on" : "off");
    setTerm(!d.terminal_own ? "__inherit__" : d.terminal_backend);
    setAppr(d.approvals_mode || "manual");
  }, [d, formTouched]);

  if (err) return <Section title="运行设置 · Runtime"><ErrLine e={err} /></Section>;
  if (d === null) return <Section title="运行设置 · Runtime"><Loading /></Section>;
  if (d.is_twin) return <Section title="运行设置 · Runtime"><Note>化身的运行设置与主 AI（X）完全一致、不可单独改。要改请去 X 的运行设置。</Note></Section>;

  const curModel = (typeof d.model === "object" ? d.model?.default : d.model) || "";
  const known = d.known_models || [];
  const isKnownSel = modelSel.startsWith("known:") && known.some((m) => `known:${m.default}` === modelSel);
  const mname = modelSel.startsWith("known:") ? modelSel.slice(6) : curModel;
  const cap = REASONING_CAP.test(mname);

  const save = async () => {
    setMsg("");
    if (appr === "off" && d.approvals_mode !== "off") {
      const ok = await confirmAsync(`把「${name}」的危险命令审批切到「全部放行」？之后这个 agent 执行命令不会再逐条询问，请确认这是你想要的权限级别。`, {
        danger: true,
        okLabel: "切到 off",
      });
      if (!ok) return;
    }
    const payload: Record<string, unknown> = {};
    if (modelDirty && modelSel.startsWith("known:")) {
      const km = known.find((m) => m.default === modelSel.slice(6));
      if (km) payload.model = km;
    }
    payload.memory_enabled = mem === "__inherit__" ? "__inherit__" : mem === "on";
    payload.terminal_backend = term;
    payload.approvals_mode = appr;
    payload.reasoning_effort = reasoning;
    try {
      await apiPost(`/api/agent/${encodeURIComponent(name)}/levers`, payload);
      toast("运行设置已保存", "ok"); setFormTouched(false); load(); onSaved();
    } catch (e) { setMsg(`保存失败：${e instanceof Error ? e.message : e}`); }
  };

  return (
    <Section title="运行设置 · Runtime">
      <FieldRow label="模型">
        <select value={isKnownSel ? modelSel : ""} onChange={(e) => { setModelSel(e.target.value); setModelDirty(true); setFormTouched(true); }} style={selStyle}>
          {!isKnownSel && <option value="" disabled>{curModel ? `${curModel}（未列入候选）` : "未配置模型"}</option>}
          {known.map((m) => <option key={m.default} value={`known:${m.default}`}>{m.default}{m.provider ? `（${m.provider}）` : ""}</option>)}
        </select>
        <div className="mt-1.5">
          <button className="text-[0.7rem]" style={{ color: cream(55) }} onClick={() => setAdv(!adv)}>{adv ? "高级 ▴" : "高级 ▾"}</button>
          {adv && (
            <div className="mt-1.5">
              <div className="flex items-center gap-2">
                <span className="shrink-0 text-[0.7rem]" style={{ color: cream(50) }}>推理强度</span>
                <select value={reasoning} onChange={(e) => { setReasoning(e.target.value); setFormTouched(true); }} style={selStyle}>
                  {["__inherit__", "none", "minimal", "low", "medium", "high", "xhigh"].map((v) => <option key={v} value={v}>{v === "__inherit__" ? "继承（默认 medium）" : v}</option>)}
                </select>
              </div>
              <Note>{cap ? `✓ 当前模型「${mname || "?"}」看起来支持推理档位` : `⚠ 当前模型「${mname || "当前默认"}」可能不吃这个参数（仅 o系/R1/Claude-4 thinking 等有效）`}</Note>
            </div>
          )}
        </div>
      </FieldRow>
      <FieldRow label="引擎记忆">
        <select value={mem} onChange={(e) => { setMem(e.target.value); setFormTouched(true); }} style={selStyle}>
          <option value="__inherit__">继承</option>
          <option value="on">开（长期记忆 + 用户画像）</option>
          <option value="off">关（一次性,不记）</option>
        </select>
      </FieldRow>
      <FieldRow label="终端隔离">
        <select value={term} onChange={(e) => { setTerm(e.target.value); setFormTouched(true); }} style={selStyle}>
          <option value="__inherit__">默认（local 本机跑）</option>
          <option value="docker">docker（沙箱隔离）</option>
          <option value="ssh">ssh（远程机器）</option>
          <option value="modal">modal（云沙箱）</option>
          <option value="daytona">daytona（云沙箱）</option>
        </select>
        <Note>会跑代码的 agent 建议 docker;只聊天用默认即可。</Note>
      </FieldRow>
      <FieldRow label="危险命令审批">
        <select value={appr} onChange={(e) => { setAppr(e.target.value); setFormTouched(true); }} style={selStyle}>
          {/* 批次十一第 3 件：与会话卡片、引擎卡同一组叫法；数据值不变。 */}
          <option value="manual">每次询问</option>
          <option value="smart">自动放行（智能判断，只拦真危险的）</option>
          <option value="off">全部放行（不拦，快但险）</option>
        </select>
        <Note>这是当前 agent 的 Hermes 运行权限，不是模型参数；已运行 PTY 会热读 config，后续危险命令按新策略执行。</Note>
      </FieldRow>
      <div className="mt-2 flex items-center gap-3">
        <Button variant="primary" onClick={save}>保存运行设置</Button>
        {msg && <span className="text-[0.7rem]" style={{ color: "var(--danger)" }}>{msg}</span>}
      </div>
    </Section>
  );
}

/* ============================ 能力 (skills) ============================ */
export function SkillsSection({ name, onChanged, refreshKey = 0 }: { name: string; onChanged: () => void; refreshKey?: number }) {
  const [d, setD] = useState<SkillsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const load = () => apiGet<SkillsResp>(`/api/agent-skills/${encodeURIComponent(name)}`).then((r) => { setErr(null); setD(r); }).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { setD(null); setErr(null); load(); }, [name, refreshKey]);

  const convert = async () => {
    try {
      const p = await apiGet<{ error?: string; redundant: string[]; kept_customized: string[]; kept_unique: string[]; ancestors_inherited_from?: string[] }>(`/api/skills/convert-inherit/${encodeURIComponent(name)}`);
      if (p.error) { toast(p.error, "bad"); return; }
      const from = (p.ancestors_inherited_from || []).join(", ") || "（无祖先）";
      if (!(await confirmAsync(`精简「${name}」的重复技能?\n· 把 ${p.redundant.length} 个和上级完全一样的改为共享(原文件进备份,可还原)\n· 保留定制 ${p.kept_customized.length} 个、独有 ${p.kept_unique.length} 个\n· 之后从上级(${from})自动获得\n随时可还原。`))) return;
      await apiPost(`/api/skills/convert-inherit/${encodeURIComponent(name)}`, {});
      load(); onChanged();
    } catch (e) { toast(`精简失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };
  const revert = async () => {
    if (!(await confirmAsync(`还原「${name}」为独立技能库?\n· 把备份里的重复副本搬回来\n· 并停止从上级自动继承(opt-out)——之后不再跟随上级增删\n可随时再点「精简重复技能」恢复默认继承。`))) return;
    try { await apiPost(`/api/skills/revert-inherit/${encodeURIComponent(name)}`, {}); load(); onChanged(); }
    catch (e) { toast(`还原失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };
  const doUnshare = async (cat: string) => {
    if (!(await confirmAsync(`解除借用「${cat}」?只收回借用,不影响原技能。`))) return;
    try {
      // batch38 第 1 件：这条裸 fetch 是全仓最后一处不带 token 的写请求，收进 apiDelete。
      await apiDelete("/api/link", { target: name, skill: cat });
      load();
    } catch (e) { toast(`解除失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };

  return (
    <Section title="能力 · Capabilities" meta={d ? `${d.skills.length} 技能` : undefined}>
      {err ? <ErrLine e={err} /> : d === null ? <Loading /> : (
        <>
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Button onClick={() => setAdding(true)}>装 / 卸技能</Button>
            {d.converted
              ? <><Pill tone="gold">已精简</Pill><button className="text-[0.7rem] underline" style={{ color: cream(55) }} onClick={revert}>还原</button></>
              : <button className="text-[0.7rem] underline" style={{ color: cream(55) }} onClick={convert}>精简重复技能</button>}
          </div>
          <Note>技能按需自动触发(不做开关)。「继承自 X」= 从上级自动获得,X 增删跟着变;借来的可解除。</Note>
          <div className="mt-2 max-h-52 overflow-y-auto">
            {d.skills.map((s) => (
              <div key={s.name} className="flex items-center gap-2 py-1 text-xs" style={{ borderBottom: HAIR }}>
                <span className="flex-1 truncate" title={s.desc || ""}>{s.name}</span>
                {s.inherited ? <Pill tone="muted">继承←{s.source || "?"}</Pill> : <Pill tone="gold">自带</Pill>}
                {s.via === "symlink" && <button className="text-[0.65rem] underline" style={{ color: cream(50) }} onClick={() => doUnshare(s.category || s.name)}>解除</button>}
              </div>
            ))}
          </div>
        </>
      )}
      {adding && <SkillAddModal name={name} onClose={() => setAdding(false)} onDone={() => { setAdding(false); load(); }} />}
    </Section>
  );
}

function SkillAddModal({ name, onClose, onDone }: { name: string; onClose: () => void; onDone: () => void }) {
  const [install, setInstall] = useState("");
  const [uninstall, setUninstall] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const doInstall = async () => {
    if (!install.trim()) { setErr("请填写要安装的技能地址或标识"); return; }
    if (!(await confirmAsync(`给 ${name} 安装「${install.trim()}」?会联网拉取并安全扫描。`))) return;
    setBusy(true); setErr(null);
    try { await apiPost(`/api/agent-skills/${encodeURIComponent(name)}/install`, { identifier: install.trim() }); toast("已安装", "ok"); onDone(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  const doUninstall = async () => {
    if (!uninstall.trim()) { setErr("请填写要卸载的技能名"); return; }
    if (!(await confirmAsync(`从 ${name} 卸载「${uninstall.trim()}」?不可恢复(内置卸不掉)。`, { danger: true }))) return;
    setBusy(true); setErr(null);
    try { await apiPost(`/api/agent-skills/${encodeURIComponent(name)}/uninstall`, { skill: uninstall.trim() }); toast("已卸载", "ok"); onDone(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  return (
    <Modal onClose={onClose} width={520}>
      <ModalTitle>装 / 卸技能 · {name}</ModalTitle>
      <ModalSub>成员制:装=获得能力,卸=移除。技能按需自动触发,不需要开关。</ModalSub>
      <div className="mb-1 text-[0.7rem]" style={{ color: cream(55) }}>安装（owner/skills/name 或 SKILL.md URL）</div>
      <div className="flex gap-2"><input value={install} onChange={(e) => setInstall(e.target.value)} placeholder="如 anthropic/skills/pdf" style={inputStyle()} /><Button variant="primary" disabled={busy} onClick={doInstall}>安装</Button></div>
      <div className="mb-1 mt-3 text-[0.7rem]" style={{ color: cream(55) }}>卸载（技能名）</div>
      <div className="flex gap-2"><input value={uninstall} onChange={(e) => setUninstall(e.target.value)} placeholder="技能名" style={inputStyle()} /><Button variant="danger" disabled={busy} onClick={doUninstall}>卸载</Button></div>
      {err && <div className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="mt-4 flex justify-end"><Button onClick={onClose}>关闭</Button></div>
    </Modal>
  );
}

/* ============================ 协作 (impact) ============================ */
export function ImpactSection({ name, refreshKey = 0 }: { name: string; refreshKey?: number }) {
  const [d, setD] = useState<ImpactResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setD(null);
    setErr(null);
    apiGet<ImpactResp>(`/api/profile/${encodeURIComponent(name)}/impact`)
      .then((r) => { setErr(null); setD(r); })
      .catch((e) => setErr(String(e?.message ?? e)));
  }, [name, refreshKey]);
  if (err) return <Section title="协作 · Collaboration"><ErrLine e={err} /></Section>;
  if (!d) return null;
  const s = d.summary;
  if (s.descendants_count + s.symlink_consumer_count === 0) return null; // 叶子:无协作,省屏
  const names = (arr: string[]) => (arr.length < 10 ? arr.join("、") : `${arr.slice(0, 5).join("、")}…其余 ${arr.length - 5} 个`);
  return (
    <Section title="协作 · Collaboration">
      <FieldRow label="手下的 AI"><span className="font-medium" style={{ color: "var(--accent-text)" }}>{s.descendants_count}</span> 个 {names(d.descendants)}</FieldRow>
      <FieldRow label="继承我设置"><span style={{ color: "var(--accent-text)" }}>{s.config_affected}</span> 个 · 我改设置它们几秒内跟随</FieldRow>
      <FieldRow label="继承我技能"><span style={{ color: "var(--accent-text)" }}>{s.external_dirs_count}</span> 个 · 我增删技能它们跟着变</FieldRow>
      <FieldRow label="借我技能的">{s.symlink_consumer_count} 个：{d.symlink_consumers.map((c) => `${c.name}(${c.skills.length})`).join("、") || "无"}</FieldRow>
    </Section>
  );
}

/* ============================ 资源 (backups) ============================ */
export function BackupsSection({ name, refreshKey = 0 }: { name: string; refreshKey?: number }) {
  const [d, setD] = useState<{ backups: BackupItem[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const load = () => apiGet<{ backups: BackupItem[] }>(`/api/profile/${encodeURIComponent(name)}/backups`).then((r) => { setErr(null); setD(r); }).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { setD(null); setErr(null); load(); }, [name, refreshKey]);
  const restore = async (file: string) => {
    if (!(await confirmAsync(`从「${file}」恢复?当前内容会再备份一份。`))) return;
    try { await apiPost(`/api/profile/${encodeURIComponent(name)}/restore`, { file }); toast("已恢复", "ok"); load(); }
    catch (e) { toast(`恢复失败：${e instanceof Error ? e.message : e}`, "bad"); }
  };
  return (
    <Section title="资源 · Backups">
      {err ? <ErrLine e={err} /> : d === null ? <Loading /> : d.backups.length === 0 ? <Pill tone="muted">无备份记录</Pill> : (
        <div>
          <button className="text-xs underline" style={{ color: cream(55) }} onClick={() => setOpen(!open)}>{d.backups.length} 条备份（最近 {d.backups[0].ago}）{open ? " ▴" : " ▾"}</button>
          {open && d.backups.map((b) => (
            <div key={b.file} className="flex items-center gap-2 py-1 text-[0.7rem]" style={{ borderBottom: HAIR }}>
              <span style={{ color: cream(45) }}>{b.ago}</span>
              <span className="flex-1 truncate" title={b.when}>{b.file}</span>
              <button className="underline" style={{ color: cream(55) }} onClick={() => restore(b.file)}>恢复</button>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

/* ============================ 子任务 (subtasks) ============================ */
export function SubtasksSection({ name, refreshKey = 0 }: { name: string; refreshKey?: number }) {
  const [d, setD] = useState<SubtasksResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const load = () => apiGet<SubtasksResp>(`/api/profile/${encodeURIComponent(name)}/subtasks`).then((r) => { setErr(null); setD(r); }).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { setD(null); setErr(null); load(); }, [name, refreshKey]);
  return (
    <Section title="子任务 · Subtasks" meta={d ? `${d.total}` : undefined}>
      {err ? <ErrLine e={err} /> : d === null ? <Loading /> : (
        <div className="flex flex-col gap-1.5">
          {d.tasks.map((t) => (
            <div key={t.id} className="rounded-lg px-3 py-2 text-xs" style={{ background: cream(4), border: HAIR }}>
              <div className="truncate">{t.title || "(无标题)"}</div>
              <div className="mt-1 flex gap-1.5"><Pill tone="muted">{t.status || "?"}</Pill>{t.priority ? <Pill tone="gold">P{t.priority}</Pill> : null}</div>
            </div>
          ))}
          <button className="rounded-lg border border-dashed px-3 py-2 text-xs" style={{ borderColor: cream(20), color: cream(55) }} onClick={() => setCreating(true)}>+ 新建子任务（分配给 {name}）</button>
        </div>
      )}
      {creating && <SubtaskCreateModal assignee={name} onClose={() => setCreating(false)} onDone={() => { setCreating(false); load(); }} />}
    </Section>
  );
}

function SubtaskCreateModal({ assignee, onClose, onDone }: { assignee: string; onClose: () => void; onDone: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [pri, setPri] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const create = async () => {
    if (!title.trim()) { setErr("标题不能为空"); return; }
    try {
      await apiPost("/api/kanban/create", { title: title.trim(), assignee, body: body || null, priority: pri ? parseInt(pri, 10) : null });
      toast("子任务已创建", "ok"); onDone();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <Modal onClose={onClose} width={520}>
      <ModalTitle>新建子任务 · {assignee}</ModalTitle>
      <ModalSub>这条任务会进任务板,assignee 预填为 {assignee}。</ModalSub>
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="任务标题（必填）" style={inputStyle()} autoFocus />
      <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={5} placeholder="详情（可选）" style={{ ...inputStyle(), marginTop: 8, resize: "vertical" }} />
      <select value={pri} onChange={(e) => setPri(e.target.value)} style={{ ...selStyle, marginTop: 8 }}>
        <option value="">默认优先级</option><option value="1">P1 紧急</option><option value="2">P2 高</option><option value="3">P3 中</option><option value="4">P4 低</option>
      </select>
      {err && <div className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="mt-3 flex justify-end gap-3"><Button onClick={onClose}>取消</Button><Button variant="primary" onClick={create}>创建</Button></div>
    </Modal>
  );
}
