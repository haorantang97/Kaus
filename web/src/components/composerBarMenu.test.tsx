import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "./ui";

import {
  BarMenu,
  BAR_MENU_FILTER_THRESHOLD,
  defaultExpandedGroup,
  type BarMenuOption,
} from "./ComposerBar";

/* 批次十六第 5 件（走查 F11）：清单长了就给一个过滤框。
   短清单不给——多一个输入框只会碍事。 */

function options(count: number): BarMenuOption[] {
  return Array.from({ length: count }, (_, index) => ({
    value: `model-${index}`,
    label: index === 0 ? "GPT-5.6-SOL" : `Model ${index}`,
  }));
}

function renderMenu(list: BarMenuOption[], onSelect = vi.fn()) {
  render(
    <BarMenu label="模型" value={list[0]?.value ?? null} display="GPT-5.6-SOL" options={list} onSelect={onSelect} />,
  );
  return onSelect;
}

describe("菜单过滤", () => {
  it("弹窗菜单可选择，Esc 只先关闭菜单", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    const close = vi.fn();
    render(<Modal onClose={close}><BarMenu floating label="模型" value="model-0" display="Model 0" options={options(6)} onSelect={select} /></Modal>);
    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.click(screen.getByRole("option", { name: "Model 5" }));
    expect(select).toHaveBeenCalledWith("model-5");
    expect(close).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(close).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(close).toHaveBeenCalledTimes(1);
  });

  it(`项数 ≤ ${BAR_MENU_FILTER_THRESHOLD} 时没有过滤框`, async () => {
    const user = userEvent.setup();
    renderMenu(options(BAR_MENU_FILTER_THRESHOLD));
    await user.click(screen.getByRole("button", { name: "模型" }));
    expect(screen.queryByTestId("bar-filter")).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(BAR_MENU_FILTER_THRESHOLD);
  });

  it("项数超过阈值：出现过滤框（自动聚焦），输入即过滤", async () => {
    const user = userEvent.setup();
    renderMenu(options(12));
    await user.click(screen.getByRole("button", { name: "模型" }));

    const filter = screen.getByTestId("bar-filter");
    expect(filter).toHaveFocus();
    await user.type(filter, "sol");
    // 大小写不敏感，`value` 也参与匹配。
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByRole("option")).toHaveTextContent("GPT-5.6-SOL");
  });

  it("一条都不匹配时给一句话，不是空菜单", async () => {
    const user = userEvent.setup();
    renderMenu(options(12));
    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.type(screen.getByTestId("bar-filter"), "没有这个东西");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByTestId("menu-filter-empty")).toHaveTextContent("没有匹配项");
  });

  it("Esc 先清过滤词，再按一次才关菜单；关掉后重开是整份清单", async () => {
    const user = userEvent.setup();
    renderMenu(options(12));
    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.type(screen.getByTestId("bar-filter"), "sol");
    expect(screen.getAllByRole("option")).toHaveLength(1);

    await user.keyboard("{Escape}");
    expect(screen.getByTestId("bar-filter")).toHaveValue("");
    expect(screen.getAllByRole("option")).toHaveLength(12);

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("bar-filter")).toBeNull();
  });

  it("选中的还是原来的 value（过滤只影响看得见的行）", async () => {
    const user = userEvent.setup();
    const onSelect = renderMenu(options(12));
    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.type(screen.getByTestId("bar-filter"), "Model 7");
    await user.click(screen.getByRole("option", { name: "Model 7" }));
    expect(onSelect).toHaveBeenCalledWith("model-7");
  });
});

/* batch29（真机 D4 / AD-117 定案 / DESIGN ★I-3）：模型下拉按 provider 分组。
   真机上这枚下拉一次列 47 个跨 provider 的模型，平铺成一列，用户读成「混列」。 */

const GROUPED: BarMenuOption[] = [
  // 顺序由后端定：当前 provider 的组在前，其余按 label 字母序（这里已排好）。
  { value: "gpt-5.5", label: "GPT-5.5", group: "OpenAI Codex" },
  { value: "gpt-5.4-mini", label: "GPT-5.4 Mini", group: "OpenAI Codex" },
  { value: "m-fable", label: "Fable 5", group: "Anthropic" },
  { value: "default", label: "default", group: "Mixture of Agents" },
];

describe("菜单分组（provider）", () => {
  it("组头按后端给的顺序渲染：当前 provider 的组排第一", async () => {
    const user = userEvent.setup();
    renderMenu(GROUPED);
    await user.click(screen.getByRole("button", { name: "模型" }));
    // batch30：组头一个不少，顺序照旧；折着的那几个头上多一句条数。
    expect(screen.getAllByTestId("menu-group").map((node) => node.firstChild?.textContent)).toEqual([
      "OpenAI Codex",
      "Anthropic",
      "Mixture of Agents",
    ]);
    // 组内顺序也没被前端动过（引擎自己的排序）；只是别家的组默认折着。
    expect(screen.getAllByRole("option").map((node) => node.textContent)).toEqual([
      "GPT-5.5",
      "GPT-5.4 Mini",
    ]);
  });

  it("组头文字 = providerLabel 原文，且它不是一个可点的选项", async () => {
    const user = userEvent.setup();
    renderMenu(GROUPED);
    await user.click(screen.getByRole("button", { name: "模型" }));
    const head = screen.getAllByTestId("menu-group")[0];
    expect(head).toHaveTextContent("OpenAI Codex");
    expect(head.getAttribute("role")).toBeNull();
    expect(screen.queryByRole("option", { name: "OpenAI Codex" })).toBeNull();
  });

  it("过滤跨组匹配，筛空的组连组头一起不渲染（AD-71）", async () => {
    const user = userEvent.setup();
    // 过滤框要项数超过阈值才出，所以这里把清单撑够，且新增的都归第四个组。
    const many: BarMenuOption[] = [
      ...GROUPED,
      ...Array.from({ length: 8 }, (_, index) => ({
        value: `gemini-${index}`,
        label: `Gemini ${index}`,
        group: "Google Gemini",
      })),
    ];
    renderMenu(many);
    await user.click(screen.getByRole("button", { name: "模型" }));
    expect(screen.getAllByTestId("menu-group")).toHaveLength(4);

    await user.type(screen.getByTestId("bar-filter"), "gpt");
    // 命中只在 OpenAI Codex 那一组 ⇒ 另外三个组头一起消失。
    expect(screen.getAllByTestId("menu-group").map((node) => node.textContent)).toEqual([
      "OpenAI Codex",
    ]);
    expect(screen.getAllByRole("option")).toHaveLength(2);

    // 跨组匹配：换个词就命中另一组（`value` 也参与匹配）。
    await user.clear(screen.getByTestId("bar-filter"));
    await user.type(screen.getByTestId("bar-filter"), "fable");
    expect(screen.getAllByTestId("menu-group").map((node) => node.textContent)).toEqual([
      "Anthropic",
    ]);
  });

  it("只有一个 provider（或压根没有 providerLabel）时不出任何组头", async () => {
    const user = userEvent.setup();
    const single: BarMenuOption[] = [
      { value: "a", label: "A", group: "Anthropic" },
      { value: "b", label: "B", group: "Anthropic" },
    ];
    const { unmount } = render(
      <BarMenu label="模型" value="a" display="A" options={single} onSelect={vi.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "模型" }));
    expect(screen.queryAllByTestId("menu-group")).toHaveLength(0);
    expect(screen.getAllByRole("option")).toHaveLength(2);
    unmount();

    // 老后端不给 `providerLabel` ⇒ 一列到底，形态与 batch29 之前一模一样。
    renderMenu(options(4));
    await user.click(screen.getByRole("button", { name: "模型" }));
    expect(screen.queryAllByTestId("menu-group")).toHaveLength(0);
    expect(screen.getAllByRole("option")).toHaveLength(4);
  });
});

/* batch30 第 4 件（真机 P2 / DESIGN ★I-3）：47 个模型即使分了组，摊开仍旧长得
   要滚很久。默认只展开**引擎此刻在用的那家**，别家折成一行组头 + 条数。 */

/** 与真机同形：当前那家由后端的 `isCurrentProvider` 说了算，不是"选中项在哪组"。 */
const WITH_CURRENT: BarMenuOption[] = [
  { value: "sonnet", label: "Sonnet", group: "Anthropic" },
  { value: "opus", label: "Opus", group: "Anthropic" },
  { value: "gpt-5.5", label: "GPT-5.5", group: "OpenAI Codex", groupCurrent: true },
  { value: "gpt-5.4-mini", label: "GPT-5.4 Mini", group: "OpenAI Codex" },
  { value: "gemini", label: "Gemini", group: "Google" },
];

describe("batch30：provider 组默认折叠（★I-3）", () => {
  it("defaultExpandedGroup：先认 groupCurrent，其次是选中项那组，再次是第一组", () => {
    expect(defaultExpandedGroup(WITH_CURRENT, "sonnet")).toBe("OpenAI Codex");
    // 后端没给 `groupCurrent` 时的兜底：至少让用户看见自己现在用的那一档。
    const noCurrent = WITH_CURRENT.map(({ groupCurrent: _drop, ...rest }) => rest);
    expect(defaultExpandedGroup(noCurrent, "gemini")).toBe("Google");
    expect(defaultExpandedGroup(noCurrent, null)).toBe("Anthropic");
    // 一个有名字的组都没有 ⇒ 压根没有组头，也就无所谓折叠。
    expect(defaultExpandedGroup(options(3), null)).toBeNull();
  });

  it("打开时只展开当前那家，别家折成组头 + 条数", async () => {
    const user = userEvent.setup();
    render(
      <BarMenu label="模型" value="sonnet" display="Sonnet" options={WITH_CURRENT} onSelect={vi.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "模型" }));

    expect(screen.getAllByRole("option").map((node) => node.textContent)).toEqual([
      "GPT-5.5",
      "GPT-5.4 Mini",
    ]);
    const heads = screen.getAllByTestId("menu-group");
    expect(heads.map((node) => node.getAttribute("aria-expanded"))).toEqual(["false", "true", "false"]);
    // 折着的组把条数说出来：不然用户不知道这一行后面藏了多少东西。
    expect(heads[0].querySelector(".kaus-bar-pop-count")).toHaveTextContent("2");
    expect(heads[2].querySelector(".kaus-bar-pop-count")).toHaveTextContent("1");
  });

  it("点组头展开它；再点收起。什么都不持久化——关掉重开又是默认那一档", async () => {
    const user = userEvent.setup();
    render(
      <BarMenu label="模型" value="sonnet" display="Sonnet" options={WITH_CURRENT} onSelect={vi.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "模型" }));

    await user.click(screen.getAllByTestId("menu-group")[0]);
    expect(screen.getAllByRole("option").map((node) => node.textContent)).toEqual([
      "Sonnet",
      "Opus",
      "GPT-5.5",
      "GPT-5.4 Mini",
    ]);
    await user.click(screen.getAllByTestId("menu-group")[0]);
    expect(screen.queryByRole("option", { name: "Sonnet" })).toBeNull();

    // 关掉再打开：折叠状态不跨"这次打开"（★I-3：不持久化任何东西）。
    await user.click(screen.getAllByTestId("menu-group")[0]);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "模型" }));
    expect(screen.getAllByRole("option").map((node) => node.textContent)).toEqual([
      "GPT-5.5",
      "GPT-5.4 Mini",
    ]);
  });

  it("一开始筛就全展开：这时用户要的是「哪儿有命中」，折叠只会把答案藏起来", async () => {
    const user = userEvent.setup();
    // 过滤框要项数超过阈值才出。
    const many: BarMenuOption[] = [
      ...WITH_CURRENT,
      ...Array.from({ length: 6 }, (_, index) => ({
        value: `qwen-${index}`,
        label: `Qwen ${index}`,
        group: "Qwen",
      })),
    ];
    render(<BarMenu label="模型" value="sonnet" display="Sonnet" options={many} onSelect={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "模型" }));
    expect(screen.getAllByRole("option")).toHaveLength(2);

    await user.type(screen.getByTestId("bar-filter"), "n");
    // 跨组命中全部摊开（筛空的组连组头一起不渲染，那一条没变）。
    expect(screen.getAllByRole("option").map((node) => node.textContent)).toEqual([
      "Sonnet",
      "GPT-5.4 Mini",
      "Gemini",
      "Qwen 0",
      "Qwen 1",
      "Qwen 2",
      "Qwen 3",
      "Qwen 4",
      "Qwen 5",
    ]);

    // 清掉过滤词就回到默认那一档（展开是过滤带来的，不是用户点出来的）。
    await user.clear(screen.getByTestId("bar-filter"));
    expect(screen.getAllByRole("option")).toHaveLength(2);
  });
});

/* ------------------------------------------------------------------ *
 * batch40 / DESIGN ★L 第 5、7 条：工具栏 chip 只显示**值**
 * ------------------------------------------------------------------ *
 * 一行工具栏上「工作目录 /code/pronto · 引擎 Mock · 模型 Mock Small」这种写法，
 * 一半的像素花在标签上，而标签每次都一样。所以 chip 里只留取值，标签退到
 * `aria-label` / `title`（读屏与悬停仍然拿得到）；工作目录再截成末两级。 */
describe("工具栏 chip 只显示值（★L 第 5、7 条）", () => {
  const setting = (value: string) => ({ value, source: "binding" as const });

  it("工作目录：显示末两级，完整路径在 title 里；标签只在 aria-label 上", async () => {
    const { tailSegments, WorkspacePill } = await import("./ComposerBar");
    expect(tailSegments("/Users/example/code/pronto")).toBe("code/pronto");
    expect(tailSegments("/code/pronto")).toBe("/code/pronto");

    render(<WorkspacePill setting={setting("/Users/example/code/pronto")} />);
    const pill = screen.getByTitle(/\/Users\/example\/code\/pronto/);
    expect(pill).toHaveTextContent("code/pronto");
    // chip 上没有「工作目录」这四个字——它只在无障碍名/悬停里。
    expect(pill.textContent).not.toContain("工作目录");
  });

  it("整条工具栏上一个标签都不显示：引擎 / 模型 / 推理强度 chip 里只有取值", async () => {
    const { ComposerBar } = await import("./ComposerBar");
    render(
      <ComposerBar
        engineName="Mock"
        workspaceRoot={setting("/Users/example/code/pronto")}
        model={{ ...setting("mock-small") }}
        modelOptions={[{ value: "mock-small", label: "Mock Small" }]}
        reasoning={{ ...setting("medium"), levels: ["low", "medium"] }}
        approval={{ ...setting("ask"), options: [] }}
      />,
    );
    const bar = screen.getByTestId("composer-bar");
    for (const label of ["工作目录", "引擎", "模型", "推理强度"]) {
      expect(bar.textContent).not.toContain(label);
    }
    expect(bar).toHaveTextContent("code/pronto");
    expect(bar).toHaveTextContent("Mock");
    expect(bar).toHaveTextContent("Mock Small");
    expect(bar).toHaveTextContent("medium");
  });
});
