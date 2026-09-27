import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { GroupMaterialsPane } from "./GroupMaterialsPane";
import { setLocalePref } from "../i18n";
import { fetchGroupMaterials, saveGroupMaterial, type GroupWire } from "../lib/groupsApi";
vi.mock("../lib/groupsApi", async (original) => ({ ...await original<object>(), fetchGroupMaterials: vi.fn(), saveGroupMaterial: vi.fn(), deleteGroupMaterial: vi.fn() }));
const group = { id: "group-a", title: "A", status: "active", thread: null } as GroupWire;
const bundle = { groupId: "group-a", revision: 2, items: [], charCount: 0, maxChars: 12000, maxItems: 20 };
const props = { group, members: [], changeToken: 0, capture: null, onCaptured: vi.fn() };
beforeEach(() => { vi.resetAllMocks(); setLocalePref("zh"); vi.mocked(fetchGroupMaterials).mockResolvedValue(bundle); });
afterEach(cleanup);
async function newDraft() {
  await waitFor(() => expect(screen.getByRole("button", { name: "添加资料" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "添加资料" }));
  fireEvent.change(screen.getByLabelText("标题"), { target: { value: "背景" } });
  fireEvent.change(screen.getByLabelText("内容"), { target: { value: "选择的内容" } });
}
describe("Group materials boundaries", () => {
  it("saves only explicit selection with the revision the editor opened", async () => {
    vi.mocked(saveGroupMaterial).mockResolvedValue({ ...bundle, revision: 3 });
    render(<GroupMaterialsPane {...props} />); await newDraft();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(saveGroupMaterial).toHaveBeenCalledWith("group-a", {
      title: "背景", content: "选择的内容", sourceKind: "note", sourceId: null, sourceLabel: null, expectedRevision: 2,
    }, undefined));
  });
  it("keeps the user's edit and revision when another window has updated materials", async () => {
    vi.mocked(saveGroupMaterial).mockRejectedValue(new Error("资料已更新，请刷新后重试"));
    render(<GroupMaterialsPane {...props} />); await newDraft();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("资料已更新");
    expect(screen.getByLabelText("内容")).toHaveValue("选择的内容");
  });
  it("refuses oversized drafts instead of silently truncating", async () => {
    render(<GroupMaterialsPane {...props} />); await newDraft();
    fireEvent.change(screen.getByLabelText("内容"), { target: { value: "中".repeat(12000) } });
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    expect(screen.getByLabelText("内容")).toHaveValue("中".repeat(12000));
    expect(saveGroupMaterial).not.toHaveBeenCalled();
  });
  it("does not show a late response in a newly selected group", async () => {
    let finish!: (value: typeof bundle) => void;
    vi.mocked(fetchGroupMaterials).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view=render(<GroupMaterialsPane key="a" {...props} />);
    vi.mocked(fetchGroupMaterials).mockResolvedValue({ ...bundle, groupId: "group-b" });
    view.rerender(<GroupMaterialsPane key="b" {...props} group={{ ...group, id: "group-b" }} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "添加资料" })).toBeEnabled());
    await act(async () => finish({ ...bundle, charCount: 999 }));
    expect(screen.queryByText("999 / 12000 字符")).not.toBeInTheDocument();
    await newDraft(); vi.mocked(saveGroupMaterial).mockResolvedValue(bundle);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(saveGroupMaterial).toHaveBeenCalledWith("group-b", expect.anything(), undefined));
  });
});

it("explicit refresh lets a preserved draft be saved against the latest version", async () => {
  vi.mocked(saveGroupMaterial).mockRejectedValueOnce(new Error("资料已更新，请刷新后重试"));
  render(<GroupMaterialsPane {...props} />); await newDraft();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByRole("alert");
  vi.mocked(fetchGroupMaterials).mockResolvedValue({ ...bundle, revision: 3 });
  fireEvent.click(screen.getByRole("button", { name: "刷新资料" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  expect(screen.getByLabelText("内容")).toHaveValue("选择的内容");
  vi.mocked(saveGroupMaterial).mockResolvedValue({ ...bundle, revision: 4 });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saveGroupMaterial).toHaveBeenLastCalledWith("group-a", expect.objectContaining({ content: "选择的内容", expectedRevision: 3 }), undefined));
});
