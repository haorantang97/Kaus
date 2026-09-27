# Fixtures

`model-options.json` 是目录解析的静态测试样例。认证标记表示测试场景，不包含账号、凭据或本机配置；覆盖未认证提供商过滤、模型能力和显示名。

`hermes_run_sse.txt` —— **逐字节抄自** `docs/probes/2026-09-02-run3-live.md`
的 `http.runs` 证据块（`json · SSE raw head`，15 条 `data:` 行 + 空行）。

规格附录 A 验收点 2 明确说这份 fixture「不再依赖新的 live 探针」：它就是
Hermes 0.21.0 上一个真实回合的原始 SSE 报文。**不要修改它**——一改，
translator 的断言就不再是在对实测事实作证。

它覆盖的三条实测特征：
- 每条消息只有一行 `data:` + 一个空行，**没有 `event:` 行**（事件名在载荷的 `event` 键里）；
- **没有 `id:` 行**（因此没有 `Last-Event-ID`，`nativeEventId` 必须合成）；
- 每条载荷都带 `run_id` 与 `timestamp`（float 秒）。

样本流在最后一条 `reasoning.available` 处结束——探针报告没有留下
`run.completed` 的报文字段（规格 §3.2 标了 `[未验证]`），所以 fixture 里也没有。
