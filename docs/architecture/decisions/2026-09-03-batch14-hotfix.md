# 2026-09-03 批次十四后端热修：引擎设置按继承链读；模型目录兜底不再为空

**起因（真机）：** Media 绑定的 `GET /api/bindings/{id}/effective-settings` 返回
`model: none`、`reasoningEffort.levels: []`，而用户在 Mac 上
`hermes -p media config get model` 与 `hermes config get model` 都给出
`default: gpt-5.5 / provider: openai-codex / base_url: …`。两个根因：

1. **media profile 自己那份 `config.yaml` 没有 `model` 键**，Hermes 按继承回落到根配置
   `~/.hermes/config.yaml`；我们只读了 profile 那一层，读了个空。
2. `config.yaml` 的 `providers` 段**只列自定义 provider**（这台机器上是 `openclaw`），
   内置 provider（`openai-codex` / `anthropic` …）根本不在里面。于是 AD-117 的
   「静态目录 ∩ 声明的 providers」= 空集：模型下拉为空、推理强度无档位。

**验收：** kernel **1011 passed / 21 skipped**，仓库级 `tests` 222。

- **AD-123 引擎设置按继承链逐键读取：** Hermes Driver 的 `read_engine_settings`
  先读 profile 的 `config.yaml`，**逐键**回退到 `<hermes_root>/config.yaml`
  （`session_bootstrap.hermes_root_for(gateway)` 算出来的那个 root，Driver 手上就有）。
  对上游 `source` 仍是 `engine`——继承链是引擎内部的事，不是第三本账；
  `EngineSettings.source_ref` 改为带层名（`profile:<路径>` / `root:<路径>`，两层都出过力
  就都列上），新增 `key_sources`（`(键名, 层名)`）说明每个键落在哪一层。
  默认 scope 的 `HERMES_HOME` 就是 root，此时只有一层，不重复读。
- **AD-124 `model` 映射形态同时取 `provider`；`base_url` 一律不读：**
  `model` 写成模型条目映射时，除 `default` 外再取 `provider`（目录兜底要靠它）。
  同一份映射里的 `base_url` / `api_key` 之流由 `MODEL_ENTRY_NEVER_READ_KEYS`
  显式点名**不读**，因此不可能出现在任何响应里（有用例断言整份序列化结果）。
- **AD-117 修订（模型目录兜底规则 4）：** 动态目录不可用时，可选模型 =
  ① **引擎当前配置的模型**（一定在列，`provider_id` 取配置里的 provider，静态目录
  没有它时 `display_name` 用 id 本身）∪ ② 静态目录里 provider ∈（`providers` 段键名
  ∪ 引擎模型的 provider）的条目。provider 名比较做归一化
  （`normalize_provider`：小写，`-`/`_`/空白/`.` 等价，故 `openai-codex` ≈ `OpenAI Codex`）。
  仍 `degraded=true`，diagnostics 写清「模型列表来自引擎配置 + 本地静态目录，未经引擎确认」。
  两者都没有时才是空目录 +「引擎未报告可用模型」。
  *理由：* 不列出用户正在用的那个模型，等于告诉他「你正在用的东西不存在」；
  而列一份「大部分选了会失败」的静态全集同样不可接受——所以取「知道的那一条 + 有依据的那一批」。
- **AD-125 推理档位兜底 = Hermes 通用档位：** 静态目录说不出某模型的
  `reasoning_levels` 时用 `none|minimal|low|medium|high|xhigh`
  （常量 `engine_settings.HERMES_REASONING_LEVELS`，取证：`server.py` 的
  `_REASONING_EFFORTS` 与 `AGENTS.md`「推理强度属于 agent 运行配置」）。
  推理强度在 Hermes 里是 **agent 级**配置，不是模型条目的属性；静态目录没写不等于
  这台引擎没有这些档位。此前返回空列表，前端按 AD-71 干脆不渲染下拉。
- **安全边界不变：** 只打开 `config.yaml` 这一个文件名（现在是继承链上的两份同名文件）；
  `providers` 段只取键名；疑似密钥的标量值过 `redaction.redact` 判定后直接丢弃；
  `.env` / `credentials/` / `auth*` 一律不碰。测试全部用 `tmp_path` 假家目录。
- **待办（未做）：** `GET /api/model/options` 的真机原始响应取证仍未拿到——动态目录
  一天不可用，上面这套兜底就一天是「未经引擎确认」的推断，diagnostics 必须一直说这句话。
