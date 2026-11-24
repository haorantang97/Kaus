import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DataLocationSetting, shortPath } from "./DataLocationSetting";

const bridge = (extra: Record<string, unknown>) => {
  window.kausDesktop = { version: 1, chooseDirectory: vi.fn(), chooseExecutable: vi.fn(), loginQuotaConnection: vi.fn(), ...extra };
};
afterEach(() => { delete window.kausDesktop; });

describe("数据位置", () => {
  it("浏览器和开发环境里不显示", async () => {
    const { container, rerender } = render(<DataLocationSetting />);
    expect(container).toBeEmptyDOMElement();
    bridge({ dataLocation: vi.fn().mockResolvedValue({ path: "/repo/state", movable: false }) });
    rerender(<DataLocationSetting key="dev" />);
    await waitFor(() => expect(window.kausDesktop?.dataLocation).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("安装版显示当前位置；更改成功后显示新位置，失败时说明数据仍在原处", async () => {
    const user = userEvent.setup();
    const moveData = vi.fn().mockResolvedValueOnce({ path: "/Users/a/Data/Kaus" }).mockResolvedValueOnce({ error: "move_failed" });
    bridge({ dataLocation: vi.fn().mockResolvedValue({ path: "/Users/a/Library/Application Support/Kaus/Data", home: "/Users/a", movable: true }), moveData });
    render(<DataLocationSetting />);
    expect(await screen.findByTitle("/Users/a/Library/Application Support/Kaus/Data")).toHaveTextContent("~/Library/Application Support/Kaus/Data");
    await user.click(screen.getByRole("button", { name: "更改" }));
    expect(moveData).toHaveBeenCalledWith();
    expect(await screen.findByTitle("/Users/a/Data/Kaus")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "更改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("移动失败，数据仍在原位置");
  });
});

it("家目录显示为 ~，其他位置原样", () => {
  expect(shortPath("/Users/a/Data/Kaus", "/Users/a")).toBe("~/Data/Kaus");
  expect(shortPath("/Users/ab/x", "/Users/a")).toBe("/Users/ab/x");
  expect(shortPath("/Volumes/X/Kaus")).toBe("/Volumes/X/Kaus");
});
