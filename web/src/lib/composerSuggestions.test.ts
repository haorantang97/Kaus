import { describe, expect, it } from "vitest";

import { activeToken, agentCommands, buildSuggestions } from "./composerSuggestions";
import type { AnyTimelineItem } from "./timeline/reducer";

const commandsEvent = (names: string[]) => ({
  kind: "extension", namespace: "acp", name: "available_commands_update", itemId: names.join(), order: 1,
  data: { sessionUpdate: "available_commands_update", availableCommands: names.map(name => ({ name, description: `${name} desc`, input: name === "review" ? { hint: "file" } : null })) },
}) as unknown as AnyTimelineItem;

describe("输入框候选", () => {
  it("只认最近一次命令列表", () => {
    expect(agentCommands([commandsEvent(["old"]), commandsEvent(["review", "init"])]).map(c => c.name)).toEqual(["review", "init"]);
    expect(agentCommands([])).toEqual([]);
  });

  it("`/` 只在开头，`@` 在任意词首", () => {
    expect(activeToken("/rev", 4)).toEqual({ kind: "command", from: 0, query: "rev" });
    expect(activeToken("看看 /rev", 7)).toBeNull();
    expect(activeToken("看看 @src/ind", 11)).toEqual({ kind: "file", from: 3, query: "src/ind" });
    expect(activeToken("a@b", 3)).toBeNull();
  });

  it("文件名开头匹配优先，最多 8 条，插入带空格", () => {
    const files = ["docs/index.md", "src/pages/index.tsx", "src/indexer.ts", "README.md"];
    const set = buildSuggestions("改 @ind", 6, { commands: [], files });
    expect(set).toMatchObject({ kind: "file", from: 2, to: 6 });
    expect(set!.items.map(i => i.detail)).toEqual(["docs/index.md", "src/indexer.ts", "src/pages/index.tsx"]);
    expect(set!.items[0].insert).toBe("@docs/index.md ");
    expect(buildSuggestions("@", 1, { commands: [], files: Array.from({ length: 20 }, (_, i) => `f${i}`) })!.items).toHaveLength(8);
  });

  it("命令显示参数提示，没有匹配时不弹", () => {
    const commands = agentCommands([commandsEvent(["review", "init"])]);
    expect(buildSuggestions("/re", 3, { commands, files: [] })!.items).toEqual([{ key: "review", label: "/review", detail: "file", insert: "/review " }]);
    expect(buildSuggestions("/zzz", 4, { commands, files: [] })).toBeNull();
    expect(buildSuggestions("普通文字", 4, { commands, files: [] })).toBeNull();
  });
});
