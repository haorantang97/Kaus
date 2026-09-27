/* batch37 第 2 件（外部评审 R8 的后半段）：**渲染失败要局限在一条消息里**。
 *
 * R8 记的是 `renderMarkdown` 会抛；根因在 `lib/md.ts` 已经修掉。但真正要守住的是
 * 后果：现有错误边界包的是整棵应用树（`main.tsx`），一条正文排版失败就换来整屏
 * 恢复界面。这里验的是那层小边界——它塌了只塌自己，原文照常看得见，同一条时间线上
 * 的别的东西一个不掉。 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { MarkdownBoundary, TextCard } from "./TextCard";
import type { MessageItem } from "./timelineReducer";

/** React 会把被边界接住的异常再打一遍 console.error：测试里没必要看。 */
function silenceConsole() {
  return vi.spyOn(console, "error").mockImplementation(() => {});
}

afterEach(() => vi.restoreAllMocks());

function Boom(): never {
  throw new Error("排版炸了");
}

function message(text: string): MessageItem {
  return {
    kind: "message",
    itemId: "message:m1",
    role: "assistant",
    order: 1,
    lastSequence: 1,
    terminal: true,
    runId: "run:1",
    parentRunId: null,
    deltaChunks: {},
    finalText: text,
    reasoningChunks: {},
  };
}

describe("单条消息的渲染边界", () => {
  it("正常时原样渲染子树", () => {
    render(
      <MarkdownBoundary text="原文">
        <span data-testid="ok">排好了</span>
      </MarkdownBoundary>,
    );
    expect(screen.getByTestId("ok")).toBeInTheDocument();
  });

  it("子树抛异常 → 中性一句 + 原文，异常不再往上走", () => {
    silenceConsole();
    render(
      <div>
        <span data-testid="neighbour">同一屏的别的东西</span>
        <MarkdownBoundary text="**原始文本** 还在">
          <Boom />
        </MarkdownBoundary>
      </div>,
    );
    expect(screen.getByText("这条消息没能渲染成格式，下面是原文")).toBeInTheDocument();
    expect(screen.getByText("**原始文本** 还在")).toBeInTheDocument();
    // 边界之外的东西一个不掉：这正是"局部失败"与全局恢复界面的区别。
    expect(screen.getByTestId("neighbour")).toBeInTheDocument();
  });

  it("助手正文照常走 Markdown（链接仍然被约束在 href 里）", () => {
    render(<TextCard item={message('看 [这里](https://a.example/" onmouseover="x)')} />);
    const prose = screen.getByTestId("assistant-prose");
    expect(prose.querySelector("[onmouseover]")).toBeNull();
    expect(prose.querySelector("script")).toBeNull();
    expect(prose.querySelector("a")?.getAttribute("href")).toBe("https://a.example/");
  });

  it("正文里的 __FENCE_0__ 不再把这条消息炸掉", () => {
    render(<TextCard item={message("__FENCE_0__")} />);
    expect(screen.getByTestId("assistant-prose").textContent).toContain("FENCE_0");
    expect(screen.queryByText("这条消息没能渲染成格式，下面是原文")).toBeNull();
  });
});
