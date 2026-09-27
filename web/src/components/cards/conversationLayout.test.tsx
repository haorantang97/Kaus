import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ToolCard, ToolGroup, toolPreview } from "./ToolCard";
import { ReasoningCard } from "./ReasoningCard";
import { PermissionCard } from "./PermissionCard";
import { TextCard } from "./TextCard";
import { UsagePill } from "./UsageBar";
import type { InteractionItem, MessageItem, ToolItem } from "./timelineReducer";

/* 会话页版式（★ 定稿 G）的卡片侧断言。页面侧（自动跟随 / 停止按钮）在
   pages/ConversationPage.test.tsx 里。 */

const base = {
  order: 1,
  lastSequence: 1,
  terminal: true,
  runId: "run:1",
  parentRunId: null,
} as const;

function tool(overrides: Partial<ToolItem> = {}): ToolItem {
  return {
    kind: "tool",
    itemId: "tool:1",
    name: "terminal",
    input: null,
    output: null,
    progress: null,
    status: "completed",
    ...base,
    ...overrides,
  };
}

function message(overrides: Partial<MessageItem> = {}): MessageItem {
  return {
    kind: "message",
    itemId: "message:1",
    role: "assistant",
    deltaChunks: {},
    finalText: "回答正文",
    reasoningChunks: {},
    ...base,
    ...overrides,
  };
}

function permission(overrides: Partial<InteractionItem> = {}): InteractionItem {
  return {
    kind: "interaction",
    itemId: "interaction:req-1",
    interactionKind: "permission",
    permission: { requestId: "req-1", title: "terminal", detail: null, options: [] },
    question: null,
    authentication: null,
    status: "resolved",
    decision: "once",
    outcome: null,
    ...base,
    ...overrides,
  };
}

describe("工具行（G-3）", () => {
  /* batch17 第 2 件把"展开后才看得到 JSON"收得更紧：展开态也不摆原始 JSON，
     入参改成键值对（对象再列一层）。 */
  it("折叠态与展开态都不出现 `{`；展开后入参是键值对", async () => {
    const item = tool({ input: { command: "pwd", options: { cwd: "/tmp" } }, output: "/tmp" });
    render(<ToolCard item={item} showOutput />);
    const row = screen.getByTestId("tool-row");
    expect(row.textContent ?? "").not.toContain("{");
    expect(row.textContent ?? "").not.toContain("}");

    await userEvent.click(screen.getByRole("button"));
    const opened = screen.getByTestId("tool-row");
    expect(opened.textContent ?? "").not.toContain("{");
    // 键值对：顶层两个键，`options` 再展开一层。
    expect(screen.getAllByTestId("tool-arg")).toHaveLength(2);
    expect(screen.getByTestId("tool-args")).toHaveTextContent("command");
    expect(screen.getByTestId("tool-args")).toHaveTextContent("cwd");
    expect(screen.getByTestId("tool-args")).toHaveTextContent("/tmp");
  });

  /* batch17 第 2 件（走查 verify5 ①）：Hermes 的 SSE 只带 preview，展开后
     CALL 区不许再出现 `{"preview":…}`。 */
  it("只有 preview 时展开是一行「命令：…」，不是原始 JSON", async () => {
    render(<ToolCard item={tool({ input: { preview: "pwd + 2 commands" }, output: null })} showOutput />);
    await userEvent.click(screen.getByRole("button"));

    expect(screen.getByTestId("tool-command")).toHaveTextContent("命令：pwd + 2 commands");
    expect(screen.getByTestId("tool-row").textContent ?? "").not.toContain("{");
    // 没有 output 就没有输出区（AD-71 同精神）。
    expect(screen.queryByText("输出")).toBeNull();
  });

  it("回填到达后按键值对渲染入参，行尾多一个「已从原生历史补齐」的小点", async () => {
    const item = tool({
      input: { preview: "pwd + 2 commands" },
      output: "/Users/example",
      progress: {
        source: "native_history",
        nativeCallId: "call_1",
        name: "terminal",
        input: { command: "pwd", timeout: 120 },
      },
    });
    render(<ToolCard item={item} showOutput />);

    const dot = screen.getByTestId("tool-backfilled");
    expect(dot).toHaveAttribute("title", "已从原生历史补齐");

    await userEvent.click(screen.getByRole("button"));
    expect(screen.getAllByTestId("tool-arg")).toHaveLength(2);
    expect(screen.getByTestId("tool-args")).toHaveTextContent("command");
    expect(screen.getByTestId("tool-args")).toHaveTextContent("pwd");
    expect(screen.getByTestId("tool-args")).toHaveTextContent("timeout");
    // 回填标记本身不再当作"进度"打印出来。
    expect(screen.getByTestId("tool-row").textContent ?? "").not.toContain("native_history");
    expect(screen.getByText("/Users/example")).toBeInTheDocument();
  });

  /* batch18 第 3 件：`command` 压过 `preview`——`preview` 是引擎给的概括串，
     有真命令时折叠态那一行就该是那条命令。 */
  it("预览串优先取 input.command，其次才是 input.preview", () => {
    const view = render(
      <ToolCard item={tool({ input: { preview: "pwd + 1 command", command: "pwd" } })} showOutput />,
    );
    expect(screen.getByTestId("tool-preview")).toHaveTextContent("pwd");
    view.rerender(<ToolCard item={tool({ input: { preview: "pwd + 1 command" } })} showOutput />);
    expect(screen.getByTestId("tool-preview")).toHaveTextContent("pwd + 1 command");
  });

  /* batch18 第 3 件：回填带来的 `progress.input` 才是真入参，折叠态预览也该用它。
     真机上事件只带 `preview:"pwd + 2 commands"`，回填之后才知道命令是 `pwd`。 */
  it("回填的 progress.input 是 {command} 时，折叠态预览换成那条命令", () => {
    render(
      <ToolCard
        item={tool({
          input: { preview: "pwd + 2 commands" },
          progress: { source: "native_history", input: { command: "pwd && ls | head -5" } },
        })}
        showOutput
      />,
    );
    expect(screen.getByTestId("tool-preview")).toHaveTextContent("pwd && ls | head -5");
  });

  /* batch18 第 3 件：真机的工具输出是 `{"output":"a\nb\nc"}`。直接 pretty-print
     会把换行转义掉；解包之后交给 `MonoBlock`（pre-wrap），换行照旧看得见。 */
  it("输出是 {output:\"…\"} 的壳时解包成文本，换行留住且不摆 JSON", async () => {
    render(
      <ToolCard
        item={tool({ input: { command: "ls" }, output: { output: "/Users/x\nAGENTS.md\nweb" } })}
        showOutput
      />,
    );
    await userEvent.click(screen.getByRole("button"));
    const block = screen.getByText(/AGENTS\.md/);
    expect(block.textContent).toBe("/Users/x\nAGENTS.md\nweb");
    expect(block.className).toContain("whitespace-pre-wrap");
    expect(screen.getByTestId("tool-row").textContent ?? "").not.toContain('"output"');
  });

  it("多字段的输出对象仍旧 pretty-print（键名本身也是信息）", async () => {
    render(
      <ToolCard
        item={tool({ input: { command: "ls" }, output: { output: "a", exitCode: 1 } })}
        showOutput
      />,
    );
    await userEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/exitCode/)).toBeInTheDocument();
  });

  it("没有 preview 时回退到首个字符串字段；全是结构体时只剩工具名", () => {
    const view = render(<ToolCard item={tool({ input: { command: "ls -al" } })} showOutput />);
    expect(screen.getByTestId("tool-preview")).toHaveTextContent("ls -al");

    view.rerender(<ToolCard item={tool({ input: { args: [1, 2, 3], nested: { a: 1 } } })} showOutput />);
    expect(screen.queryByTestId("tool-preview")).toBeNull();
    expect(screen.getByTestId("tool-row")).toHaveTextContent("terminal");
  });

  it("toolPreview 不返回带花括号的串（哪怕它是个字符串字段）", () => {
    expect(toolPreview({ preview: '{"cmd":"pwd"}' })).toBeNull();
    expect(toolPreview('{"raw":true}')).toBeNull();
    expect(toolPreview({ preview: "pwd" })).toBe("pwd");
    expect(toolPreview(null)).toBeNull();
  });

  it("耗时拿得到就显示 `1.2s`，拿不到就显示状态词", () => {
    const view = render(<ToolCard item={tool()} showOutput seconds={1.23} />);
    expect(screen.getByTestId("tool-row")).toHaveTextContent("1.2s");
    view.rerender(<ToolCard item={tool({ status: "failed" })} showOutput />);
    expect(screen.getByTestId("tool-row")).toHaveTextContent("失败");
  });

  it("出错只把状态点染成 --danger，行文字不变色", () => {
    const { container } = render(<ToolCard item={tool({ status: "failed" })} showOutput />);
    expect(container.querySelector(".kaus-tool-dot.is-failed")).not.toBeNull();
    expect(container.querySelector(".kaus-tool-name")).not.toHaveStyle({ color: "var(--danger)" });
  });

  it("工具组：组头写「运行了 N 个工具」，默认折叠，展开后逐行", async () => {
    render(
      <ToolGroup count={3}>
        <ToolCard item={tool({ itemId: "tool:a", name: "alpha" })} showOutput />
        <ToolCard item={tool({ itemId: "tool:b", name: "beta" })} showOutput />
        <ToolCard item={tool({ itemId: "tool:c", name: "gamma" })} showOutput />
      </ToolGroup>,
    );
    expect(screen.getByTestId("tool-group")).toHaveTextContent("运行了 3 个工具");
    expect(screen.queryAllByTestId("tool-row")).toHaveLength(0);

    await userEvent.click(screen.getAllByRole("button")[0]);
    expect(screen.queryAllByTestId("tool-row")).toHaveLength(3);
  });
});

describe("推理行（G-4）", () => {
  it("有耗时写「思考了 Ns」，没耗时只写「思考」", () => {
    const view = render(<ReasoningCard text="想了想" seconds={4.2} />);
    expect(screen.getByTestId("reasoning-row")).toHaveTextContent("思考了 4s");
    view.rerender(<ReasoningCard text="想了想" />);
    expect(screen.getByTestId("reasoning-row")).toHaveTextContent("思考");
    expect(screen.getByTestId("reasoning-row")).not.toHaveTextContent("思考了");
  });

  it("运行中显示「正在思考…」，并复用现有 pulse 点", () => {
    const { container } = render(<ReasoningCard running />);
    expect(screen.getByTestId("reasoning-row")).toHaveTextContent("正在思考…");
    expect(container.querySelector(".kaus-run-dot")).not.toBeNull();
  });

  it("默认折叠：推理文本要点开才出现", async () => {
    render(<ReasoningCard text="这段是推理正文" seconds={2} />);
    expect(screen.queryByText("这段是推理正文")).toBeNull();
    await userEvent.click(screen.getByRole("button"));
    expect(screen.getByText("这段是推理正文")).toBeInTheDocument();
  });
});

describe("审批卡（G-5）", () => {
  it("回答后收成一行 `✓ 已允许 · <工具名>`，按钮不再出现", () => {
    render(<PermissionCard item={permission({ decision: "once" })} />);
    const row = screen.getByTestId("permission-resolved");
    expect(row).toHaveAttribute("data-decision", "allowed");
    expect(row).toHaveTextContent("✓");
    expect(row).toHaveTextContent("已允许");
    expect(row).toHaveTextContent("terminal");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("拒绝时换成 `✕ 已拒绝`", () => {
    render(<PermissionCard item={permission({ decision: "deny" })} />);
    const row = screen.getByTestId("permission-resolved");
    expect(row).toHaveAttribute("data-decision", "denied");
    expect(row).toHaveTextContent("已拒绝");
  });

  it("待答的审批仍是常展开的卡，按钮在", () => {
    const item = permission({
      status: "pending",
      decision: null,
      permission: {
        requestId: "req-1",
        title: "允许运行 terminal？",
        detail: null,
        options: [{ optionId: "allow", label: "允许" }],
      },
    });
    render(<PermissionCard item={item} onRespond={() => undefined} />);
    expect(screen.queryByTestId("permission-resolved")).toBeNull();
    expect(screen.getByRole("button", { name: "允许" })).toBeInTheDocument();
  });
});

describe("消息（G-2）", () => {
  it("用户消息没有 `YOU` 标签，也没有那条角色小字", () => {
    const { container } = render(<TextCard item={message({ role: "user", finalText: "我说的话" })} />);
    expect(container.querySelector(".kaus-msg-role")).toBeNull();
    expect(screen.queryByText("我")).toBeNull();
    expect(container.querySelector(".kaus-msg.is-user")).not.toBeNull();
  });

  it("助手正文走 .kaus-prose，不套卡片表头（没有「回复」二字）", () => {
    const { container } = render(<TextCard item={message()} time="14:32" />);
    expect(screen.getByTestId("assistant-prose")).toBeInTheDocument();
    expect(screen.queryByText("回复")).toBeNull();
    expect(container.querySelector("section")).toBeNull();
  });

  it("时间戳挂 .kaus-msg-time，靠 hover 才显示（平时 opacity:0）", () => {
    const { container } = render(<TextCard item={message()} time="14:32" />);
    const time = container.querySelector(".kaus-msg-time");
    expect(time).not.toBeNull();
    expect(time).toHaveTextContent("14:32");
    // 显示与否由 CSS 的 `.kaus-msg:hover` 决定，这里只钉住 class 契约。
    expect(container.querySelector(".kaus-msg")?.contains(time!)).toBe(true);
  });
});

describe("用量 Pill（G-8）", () => {
  it("悬停浮层与 title 都写 in / out / total", () => {
    const { container } = render(
      <UsagePill usage={{ inputTokens: 12000, outputTokens: 3400, totalTokens: 15400 }} />,
    );
    const pill = screen.getByTestId("usage-pill");
    expect(pill).toHaveTextContent("总 15.4k");
    expect(container.querySelector(".kaus-usage-pop")).toHaveTextContent("输入 12.0k · 输出 3400 · 合计 15.4k");
    expect(pill.querySelector("[title]")).toHaveAttribute(
      "title",
      "输入 12.0k · 输出 3400 · 合计 15.4k",
    );
  });

  it("一个数都没有时整枚不渲染（AD-71）", () => {
    const { container } = render(<UsagePill usage={null} />);
    expect(container.firstChild).toBeNull();
  });
});
