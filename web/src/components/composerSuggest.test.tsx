import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Composer } from "./Composer";
import { buildSuggestions } from "../lib/composerSuggestions";

const setting = { value: null, source: "engine" as const };

function Harness({ onEscape, onSend }: { onEscape: () => boolean; onSend: () => void }) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <Composer
        bar={{ workspaceRoot: setting, model: setting, modelOptions: [], reasoning: { ...setting, levels: [] }, approval: { ...setting, options: [] } }}
        onFiles={() => {}} filesBlocked={false} fileInputRef={fileInputRef} chips={[]}
        textareaRef={textareaRef} text={text} onTextChange={setText} placeholder="" rows={2} disabled={false}
        onSend={onSend} onEscape={onEscape}
        suggest={(value, caret) => buildSuggestions(value, caret, {
          commands: [{ name: "review", description: "Review changes", hint: null }, { name: "init", description: "", hint: null }],
          files: ["src/app.ts", "src/api.ts"],
        })}
      />
      <output data-testid="text">{text}</output>
    </>
  );
}

describe("输入框候选与 Esc", () => {
  it("↓ 选第二条、Enter 插入，不发送", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Harness onEscape={() => false} onSend={onSend} />);
    await user.type(screen.getByRole("textbox"), "看 @src/a");
    expect(screen.getByRole("listbox", { name: "文件" })).toBeInTheDocument();
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("text")).toHaveTextContent("看 @src/api.ts");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("`/` 命令用 Tab 插入；Esc 先收起列表，再按才交给页面", async () => {
    const user = userEvent.setup();
    const onEscape = vi.fn(() => true);
    render(<Harness onEscape={onEscape} onSend={() => {}} />);
    const box = screen.getByRole("textbox");
    await user.type(box, "/re");
    expect(screen.getByRole("option", { name: /review/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onEscape).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(onEscape).toHaveBeenCalledTimes(1);
    await user.type(box, "v");
    await user.keyboard("{Tab}");
    expect(screen.getByTestId("text").textContent).toBe("/review ");
  });
});
