import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProjectInstructionsEditor } from "./ProjectInstructionsEditor";
import type { EffectiveCapabilitiesWire } from "../lib/sessionApi";

const api = vi.hoisted(() => ({ fetchInstructionFiles: vi.fn(), importInstructionFile: vi.fn(), putProjectCapability: vi.fn(), deleteProjectCapability: vi.fn(), fetchEffectiveCapabilities: vi.fn() }));
vi.mock("../lib/sessionApi", async importOriginal => ({ ...(await importOriginal<typeof import("../lib/sessionApi")>()), ...api }));

const effective = (withSection: boolean): EffectiveCapabilitiesWire => ({
  projectId: "project:site", backendKey: null, ancestry: ["project:site"], blocked: [], counts: { entries: 0, blocked: 0 },
  entries: withSection ? [{ capabilityType: "instructions", capabilityId: "section-a", config: { body: "先给结论", title: "沟通" }, version: "1",
    sourceProjectId: "project:site", inherited: false, overridden: false, contributingProjectIds: ["project:site"], blocked: false }] : [],
});

beforeEach(() => { Object.values(api).forEach(fn => fn.mockReset()); });

describe("指令送达", () => {
  it("文件里已有内容：问一次导入还是保留，导入后刷新", async () => {
    api.fetchInstructionFiles.mockResolvedValue({ workspaceRoot: "/w", workspaceDenied: null, files: [{
      file: "AGENTS.md", backends: ["backend:codex"], deliveryChannel: "launch", hasManagedBlock: false, managedByOtherProject: false,
      hasUserContent: true, preview: "用 pnpm", decision: null, pending: true }] });
    api.importInstructionFile.mockResolvedValue({});
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<ProjectInstructionsEditor projectId="project:site" effective={effective(false)} projectLabel={id => id} onChanged={onChanged} />);
    expect(await screen.findByText("AGENTS.md")).toBeInTheDocument();
    expect(screen.getByText("启动时注入")).toBeInTheDocument();
    expect(screen.getByText("用 pnpm")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "导入" }));
    await waitFor(() => expect(api.importInstructionFile).toHaveBeenCalledWith("project:site", "AGENTS.md", "import"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("没有工作目录：有指令时提醒，没有指令时不出声", async () => {
    const { SessionApiError } = await import("../lib/sessionApi");
    api.fetchInstructionFiles.mockRejectedValue(new SessionApiError(409, "workspace_required", "x"));
    const { rerender } = render(<ProjectInstructionsEditor projectId="project:site" effective={effective(true)} projectLabel={id => id} onChanged={() => {}} />);
    expect(await screen.findByText(/还没有工作目录/)).toBeInTheDocument();
    rerender(<ProjectInstructionsEditor projectId="project:site" effective={effective(false)} projectLabel={id => id} onChanged={() => {}} />);
    await waitFor(() => expect(screen.queryByText(/还没有工作目录/)).toBeNull());
  });
});
