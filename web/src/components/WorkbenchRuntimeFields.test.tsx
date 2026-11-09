import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { WorkbenchRuntimeFields } from "./WorkbenchRuntimeFields";

const runtime = (connections: { id: string; probeState?: string }[]) => ({
  projects: [{ id: "project:p", slug: "p", displayName: "P" }],
  connections,
  binding: { id: "binding:p:codex", backendId: "backend:codex", projectId: "project:p" },
  choices: [{ value: "binding:p:codex", backendId: "backend:codex" }],
  catalog: null,
  value: { projectId: "project:p", bindingId: "binding:p:codex", modelId: null, providerId: null, reasoningMode: null },
  loading: false, bindingsLoading: false, busy: false, failed: false, ready: true,
  selectProject: vi.fn(), selectEngine: vi.fn(), selectModel: vi.fn(), reload: vi.fn(),
}) as never;

describe("模块配置里的 Agent", () => {
  it("没接通时给出「去连接」，接通后不显示", async () => {
    const onConnect = vi.fn();
    const { rerender } = render(<WorkbenchRuntimeFields runtime={runtime([])} onConnect={onConnect} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "去连接" }));
    expect(onConnect).toHaveBeenCalled();
    rerender(<WorkbenchRuntimeFields runtime={runtime([{ id: "backend:codex", probeState: "unavailable" }])} onConnect={onConnect} />);
    expect(screen.getByRole("button", { name: "去连接" })).toBeInTheDocument();
    rerender(<WorkbenchRuntimeFields runtime={runtime([{ id: "backend:codex", probeState: "available" }])} onConnect={onConnect} />);
    expect(screen.queryByRole("button", { name: "去连接" })).toBeNull();
  });
});
