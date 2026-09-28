# Agent 产品与接入方式

2026-09-28：普通界面以产品为单位展示。Hermes HTTP 和 ACP 均显示为 **Hermes**；官方 DSH ACP 与社区 deepseek-acp 均显示为 **DeepSeek Harness**。模型列表中的 DeepSeek 是模型厂家，与 Agent 产品选择独立。

接入目录由后台标注产品及推荐连接：已知可用优先，其次已接入、已安装，最后采用产品默认适配。新建对话的第一个选择器负责项目与分身配置，分身缩进显示在所属项目下；第二个选择器只显示 Agent 产品，同一产品一项，不附带项目名、分身名或协议。选择分身沿用它的实际绑定和配置；切换 Agent 使用该项目中对应 Agent 的配置，草稿内切回时恢复先前选择的分身。点击当前项目行可回到该 Agent 的项目基础配置。普通界面不要求选择协议。

统一名称不迁移数据库里的 backendId、bindingId、原生会话、历史和权限设置。已开始的对话固定原接入；新对话在创建时选择并固定实际接入。每条接入继续上报自己的模型、工作模式、权限、恢复及工具能力，不能将两个适配器的能力拼成虚假全集。

DeepSeek Harness 是 DeepSeek 官方维护的 Agent 运行系统。`deepseek-acp` 是社区维护、面向编辑器交互的适配器，复用 Harness 内核；它提供流式输出、工具展示、会话恢复和配置入口。官方 DSH ACP 当前定位于自动化传输，两者仍由不同驱动预设维护。没有既有接入时保留 Kaus 对社区交互适配器的优先选择；有可用原生接入时沿用用户现有环境。

来源：[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)、[官方 ACP 说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md)、[社区 deepseek-acp](https://github.com/xintaofei/deepseek-acp)。
