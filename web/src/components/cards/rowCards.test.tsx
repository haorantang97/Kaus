/* 批次十三第 3 件：五类旧卡片改行语法（DESIGN ★ 定稿 G 的规则）。
 *
 * 每一类一条断言，验的都是同两件事：
 *  ① 折叠态是**一行**，里面没有 `{`（原始 JSON 只能在展开后看到）；
 *  ② 折叠态**没有常显时间戳**（`00:00` 之类），时间只挂在悬停的 `title` 上。
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CardRenderer } from "./CardRenderer";
import { ALL_SUPPORTED_UI_CAPABILITIES } from "./capabilities";
import { questionDigest } from "./QuestionCard";
import type { AnyTimelineItem } from "./timelineReducer";

const base = {
  order: 1,
  lastSequence: 1,
  terminal: true,
  runId: "run:1",
  parentRunId: null,
} as const;

const TIME = "04:05";

function renderItem(item: AnyTimelineItem) {
  return render(
    <CardRenderer item={item} caps={ALL_SUPPORTED_UI_CAPABILITIES} time={TIME} />,
  );
}

/** 折叠态的共同体检：没有 `{`，也没有那个常显的时间戳。 */
function expectCollapsedRow(row: HTMLElement) {
  expect(row.textContent ?? "").not.toContain("{");
  expect(row.textContent ?? "").not.toContain(TIME);
  // 时间没丢——它挂在整行的 title 上，悬停才看得到。
  expect(row).toHaveAttribute("title", TIME);
}

describe("五类旧卡片的行语法（★ G）", () => {
  it("计划：一行 `▸ 计划 · 2/5 完成`，展开才列步骤", async () => {
    const user = userEvent.setup();
    renderItem({
      kind: "plan",
      itemId: "plan:1",
      entries: [
        { content: "看代码", status: "completed" },
        { content: "改代码", status: "completed" },
        { content: "写测试", status: "pending" },
        { content: "跑一遍", status: "pending" },
        { content: "提交", status: "blocked" },
      ],
      ...base,
    });
    const row = screen.getByTestId("plan-row");
    expect(row).toHaveTextContent("计划");
    expect(row).toHaveTextContent("2/5 完成");
    expect(screen.queryByText("写测试")).toBeNull();
    expectCollapsedRow(row);

    await user.click(screen.getByRole("button"));
    expect(screen.getByText("写测试")).toBeInTheDocument();
  });

  it("终端：一行 `▸ 终端 · <命令>`，输出要展开才有", async () => {
    const user = userEvent.setup();
    renderItem({
      kind: "terminal",
      itemId: "terminal:1",
      command: "pwd",
      output: '{"cwd":"/repo"}',
      exitCode: 0,
      status: "completed",
      ...base,
    });
    const row = screen.getByTestId("terminal-row");
    expect(row).toHaveTextContent("终端");
    expect(row).toHaveTextContent("pwd");
    expectCollapsedRow(row);

    await user.click(screen.getByRole("button"));
    expect(screen.getByText('{"cwd":"/repo"}')).toBeInTheDocument();
  });

  it("文件：一行 `▸ 修改了 a.txt`，diff 要展开才有", async () => {
    renderItem({
      kind: "file",
      itemId: "file:1",
      path: "a.txt",
      diff: "@@ -1 +1 @@\n-{旧}\n+新",
      operation: "modified",
      ...base,
    });
    const row = screen.getByTestId("file-row");
    expect(row).toHaveTextContent("修改了 a.txt");
    expectCollapsedRow(row);
  });

  it("产物直接显示文件入口与时间，点击即可预览", () => {
    renderItem({
      kind: "artifact",
      itemId: "artifact:1",
      artifact: { artifactId: "art-1", kind: "file", title: "a.txt", uri: "file:///tmp/a.txt" },
      ...base,
    });
    const row = screen.getByTestId("artifact-row");
    expect(row).toHaveTextContent("a.txt");
    expect(row.querySelector("button")).not.toBeNull();
    expect(row).toHaveTextContent(TIME);
  });

  it("提问：待答时是常展开的卡；回答后收成一行 `✓ 已回答 · <前 20 字>`", () => {
    const prompt = "要不要把这个目录下的全部临时文件都删掉？删了就找不回来了";
    const pending: AnyTimelineItem = {
      kind: "interaction",
      itemId: "interaction:q1",
      interactionKind: "question",
      permission: null,
      question: { requestId: "q1", prompt, options: [], allowFreeText: true },
      authentication: null,
      status: "pending",
      decision: null,
      outcome: null,
      ...base,
    };
    const view = renderItem(pending);
    expect(screen.getByText(prompt)).toBeInTheDocument();
    view.unmount();

    renderItem({ ...pending, status: "resolved" });
    const row = screen.getByTestId("question-resolved");
    expect(row).toHaveTextContent(`已回答 · ${questionDigest(prompt)}`);
    // 收行之后原问题不再整段摊着，也没有常显时间戳。
    expect(screen.queryByText(prompt)).toBeNull();
    expect(row.textContent ?? "").not.toContain(TIME);
    expect(row.textContent ?? "").not.toContain("{");
  });
});

/* batch40 / DESIGN ★L 第 5 条：折起来又已经跑完的那一行，行尾的状态词是废字。
   「完成」与推理行的 `thinking` 在折叠态里不出现；耗时、运行中、失败照旧显示。 */
describe("折叠态里的状态词（★L 第 5 条）", () => {
  it("工具行：跑完又折着 → 没有「完成」；展开后有；失败与运行中照旧常显", async () => {
    const user = userEvent.setup();
    const { ToolCard } = await import("./ToolCard");
    const item = {
      kind: "tool" as const,
      id: "tool:1",
      name: "terminal",
      input: null,
      output: "ok",
      status: "completed",
      progress: null,
      ...base,
    };
    const view = render(<ToolCard item={item as never} />);
    const row = screen.getByTestId("tool-row");
    expect(row).not.toHaveTextContent("完成");
    await user.click(row.querySelector("button") as HTMLButtonElement);
    expect(row).toHaveTextContent("完成");
    view.unmount();

    render(<ToolCard item={{ ...item, status: "running" } as never} />);
    expect(screen.getByTestId("tool-row")).toHaveTextContent("运行中");
  });

  it("工具行：有耗时就照显——那是信息，不是状态词", async () => {
    const { ToolCard } = await import("./ToolCard");
    render(
      <ToolCard
        item={{
          kind: "tool",
          id: "tool:2",
          name: "terminal",
          input: null,
          output: "",
          status: "completed",
          progress: null,
          ...base,
        } as never}
        seconds={1.2}
      />,
    );
    expect(screen.getByTestId("tool-row")).toHaveTextContent("1.2s");
  });

  it("推理行：折着时行尾不写 `thinking`，展开后才写", async () => {
    const user = userEvent.setup();
    const { ReasoningCard } = await import("./ReasoningCard");
    render(<ReasoningCard status="thinking" summary="在规划" seconds={4} />);
    const row = screen.getByTestId("reasoning-row");
    expect(row).toHaveTextContent("思考了 4s");
    expect(row).not.toHaveTextContent("thinking");
    await user.click(row.querySelector("button") as HTMLButtonElement);
    expect(row).toHaveTextContent("thinking");
  });
});
