# 模型目录架构

> 本文档定义 dashboard 模型配置的完整规范。后续添加/修改模型时，严格按此流程。

## 核心文件

唯一数据源：`~/.hermes/dashboard/model-options.json`

## 模型条目 schema

每个模型条目是一个 JSON 对象，包含以下字段：

```json
{
  "default": "gpt-5.6-sol",          // ★必填：模型 ID（hermes -m 参数值）
  "provider": "openai-codex",        // ★必填：provider 名
  "base_url": "https://...",         // ★必填：API base URL
  "context_window": 272000,           // provider-aware 上下文窗口（tokens）
  "reasoning_levels": ["none", ...], // 该模型支持的推理强度级别列表
  "fast_mode": true,                 // 是否支持 /fast 切换
  "family": "GPT-5.6",               // 模型家族名（前端分组用）
  "tier": "旗舰（Sol）",              // 模型档次
  "description": "..."               // 一句话描述（前端 tooltip 显示）
}
```

### 字段说明

| 字段 | 类型 | 必填 | 用途 |
|------|------|------|------|
| `default` | string | ✅ | Hermes `-m` 接受的模型 ID |
| `provider` | string | ✅ | provider 名（如 `openai-codex`、`deepseek`） |
| `base_url` | string | ✅ | API 端点 |
| `context_window` | number | 推荐 | 当前 provider 路径实际可用的上下文窗口；Session Governor V2 据此计算占用率 |
| `reasoning_levels` | string[] | 推荐 | 该模型支持的推理强度。未填则回退到全部 Hermes 级别 |
| `fast_mode` | boolean | 推荐 | 是否支持 `/fast` 切换 |
| `family` | string | 可选 | 家族（如 GPT-5.6 / DeepSeek V4） |
| `tier` | string | 可选 | 档次（如 旗舰 / Mini / Pro） |
| `description` | string | 可选 | 一句话描述 |

## 推理强度级别

Hermes 原生支持的推理强度（`hermes_constants.VALID_REASONING_EFFORTS`）：

```
none, minimal, low, medium, high, xhigh
```

### 各模型支持的推理级别（已验证）

| 模型 | 支持级别 | 说明 |
|------|----------|------|
| gpt-5.6-sol | none, minimal, low, medium, high, xhigh | GPT-5.6 还额外支持 max/ultra（API 层面，Hermes 尚未适配） |
| gpt-5.6-terra | none, minimal, low, medium, high, xhigh | 同 Sol |
| gpt-5.6-luna | none, minimal, low, medium, high, xhigh | 同 Sol |
| gpt-5.5 | none, minimal, low, medium, high, xhigh | |
| gpt-5.4 | none, minimal, low, medium, high, xhigh | |
| gpt-5.4-mini | none, minimal, low, medium, high | 不支持 xhigh |
| gpt-5.3-codex-spark | none, medium, high | 仅有限级别 |
| deepseek-v4-pro | none, medium, high, xhigh | DeepSeek 深度思考 |
| deepseek-v4-flash | none, medium, high | 快速模式，推理受限 |

### 快速模式（/fast）

- Codex ChatGPT 登录路径支持 GPT-5.6 / 5.5 / 5.4；约 1.5× 速度，5.6/5.5 约消耗 2.5× credits
- Dashboard 写 `agent.service_tier: fast`，Hermes 运行时映射并由 Codex Responses adapter 透传
- OpenAI API key 直连使用 Priority processing，按 API token 费率计费，不使用 ChatGPT credits
- Anthropic 的 fast 能力按具体模型/adapter gate 判断，不能按整个 Claude 家族一概开启
- DeepSeek 模型不支持

## 添加新模型的流程

1. **确认模型 ID**：用 `hermes -z "OK" -m <model_id> --provider <provider>` 验证
2. **确认推理级别**：测试各推理级别是否可用（直接咨询 provider 文档或实测）
3. **更新 `model-options.json`**：按上述 schema 添加条目
4. **刷新前端**：dashboard 前端自动读取新条目，无需重启后端
5. **验证**：在 dashboard 中切换到新模型，检查推理强度下拉是否显示正确级别

## 后端实现

### model-options.json 读取路径

```
model-options.json
  → server.py: _model_entry()          # 解析单条（兼容旧格式）
  → server.py: _collect_known_models() # 去重收集全部
  → server.py: _model_for_id()         # 按 ID 查找
  → server.py: _model_reasoning_levels() # 查模型支持的推理级别
```

### API 响应

`GET /api/agent/{name}/levers` 返回：

```json
{
  "model": { "default": "gpt-5.6-sol", ... },
  "model_reasoning_levels": ["none", "minimal", "low", "medium", "high", "xhigh"],
  "model_fast_mode": true,
  "fast_mode": false,
  "known_models": [ ... ],  // 增强后的模型目录
  ...
}
```

`POST /api/agent/{name}/levers` 校验规则：
- `reasoning_effort` 先通过全局 `_REASONING_EFFORTS` 校验
- 再通过模型感知二次校验：当前 agent 所用模型不支持该级别时返回 400
- `fast_mode` 只允许目录中 `fast_mode: true` 的模型开启；持久化到 `agent.service_tier`
- 切到不支持极速模式的模型时，后端自动把 `service_tier` 归为 `standard`

### 前端

- `Conversation.tsx` 的 Lever 接口已扩展：`model_reasoning_levels`、`model_fast_mode`
- 推理强度下拉只展示 `model_reasoning_levels` 中的级别
- 模型下拉显示 family + tier 信息，tooltip 显示描述和支持的推理级别
- 旧版 `model-options.json`（只有 default/provider/base_url）仍兼容——推理级别回退到全部 Hermes 级别

## Session Governor V2

会话健康不再使用固定字符阈值。V2 使用与 Hermes 预检一致的廉价估算
`ceil(chars / 4)`，再除以模型条目的 `context_window`：

| 状态 | 估算窗口占用率 |
|------|----------------|
| 正常 | < 50% |
| 观察 | ≥ 50% |
| 偏肥 | ≥ 70% |
| 过肥 | ≥ 85% |

工具输出达到模型窗口的 25% 时会附加“tool-heavy”原因，但不会单独把会话
升级为偏肥/过肥。未知模型回退为 256K tokens。`context_window` 必须填写当前
provider 路径的实际上限，例如 GPT-5.6 Sol 直连 API 是 1.05M，但
`openai-codex` OAuth 路径由 Hermes provider-aware resolver 判定为 272K。
