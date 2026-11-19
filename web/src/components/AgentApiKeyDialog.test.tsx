import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/sessionApi", async (original) => ({
  ...(await original<typeof import("../lib/sessionApi")>()),
  clearAgentApiKey: vi.fn(async () => ({ configured: false })),
  saveAgentApiKey: vi.fn(async () => ({ configured: true })),
}));

import { clearAgentApiKey } from "../lib/sessionApi";
import { AgentApiKeyDialog } from "./AgentApiKeyDialog";
import { ConfirmHost } from "./ui";

it("清除 API key 前先确认；取消时不清除", async () => {
  const user = userEvent.setup();
  const close = vi.fn();
  render(<><AgentApiKeyDialog backendId="codex" name="Codex" envName="OPENAI_API_KEY" configured onClose={close} /><ConfirmHost /></>);
  await user.click(screen.getByRole("button", { name: "清除 API key" }));
  expect(screen.getByRole("dialog", { name: "清除 Codex 的 API key？" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(clearAgentApiKey).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "清除 API key" }));
  const confirm = screen.getByRole("dialog", { name: "清除 Codex 的 API key？" });
  await user.click(confirm.querySelector<HTMLButtonElement>(".kaus-button--danger")!);
  await waitFor(() => expect(clearAgentApiKey).toHaveBeenCalledWith("codex"));
  expect(close).toHaveBeenCalledWith(true);
});
