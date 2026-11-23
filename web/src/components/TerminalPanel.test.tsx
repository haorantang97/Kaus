import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

import { TerminalPanel, terminalUrl } from "./TerminalPanel";
import { bootstrapSessionAuth } from "../lib/authorizedFetch";

/* xterm needs a real layout engine; the panel's own logic is what is under test. */
const { terms, FakeTerminal } = vi.hoisted(() => {
  const terms: InstanceType<typeof FakeTerminal>[] = [];
  class FakeTerminal {
    cols = 90; rows = 20; options: Record<string, unknown> = {}; written: (string | Uint8Array)[] = [];
    dataHandler: (data: string) => void = () => {};
    constructor() { terms.push(this); }
    loadAddon() {} open() {} focus() {} dispose() {} reset() { this.written = []; }
    write(data: string | Uint8Array) { this.written.push(data); }
    onData(handler: (data: string) => void) { this.dataHandler = handler; return { dispose() {} }; }
    onBinary() { return { dispose() {} }; }
    onResize() { return { dispose() {} }; }
    text() { return this.written.map(item => typeof item === "string" ? item : new TextDecoder().decode(item)).join(""); }
  }
  return { terms, FakeTerminal };
});
vi.mock("@xterm/xterm", () => ({ Terminal: FakeTerminal }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.mock("../lib/authorizedFetch", () => ({ bootstrapSessionAuth: vi.fn(async () => "tok") }));

const sockets: FakeSocket[] = [];
class FakeSocket {
  static OPEN = 1;
  readyState = 0; sent: string[] = []; binaryType = "";
  onopen: (() => void) | null = null; onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  constructor(public url: string) { sockets.push(this); }
  send(frame: string) { this.sent.push(frame); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.onopen?.(); }
  frames() { return this.sent.map(frame => JSON.parse(frame) as Record<string, unknown>); }
}

beforeEach(() => { terms.length = 0; sockets.length = 0; vi.stubGlobal("WebSocket", FakeSocket); vi.mocked(bootstrapSessionAuth).mockClear(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("终端面板", () => {
  it("连到本对话的工作目录终端：首帧鉴权带尺寸，输出写进终端，按键原样发出", async () => {
    render(<TerminalPanel conversationId="c 1" onClose={vi.fn()} />);
    expect(screen.getByRole("complementary", { name: "终端" })).toBeInTheDocument();
    await waitFor(() => expect(sockets).toHaveLength(1));
    const socket = sockets[0];
    expect(socket.url).toBe(terminalUrl("c 1"));
    expect(socket.url).toMatch(/^ws:\/\/[^/]+\/api\/conversations\/c%201\/terminal$/);
    act(() => socket.open());
    expect(socket.frames()[0]).toEqual({ type: "auth", token: "tok", cols: 90, rows: 20 });
    act(() => socket.onmessage?.({ data: new TextEncoder().encode("~/proj $ ").buffer }));
    expect(terms[0].text()).toContain("~/proj $ ");
    act(() => terms[0].dataHandler("ls\r"));
    expect(socket.frames().at(-1)).toEqual({ type: "input", data: "ls\r" });
  });

  it("进程退出后提示，按任意键开新的 shell", async () => {
    render(<TerminalPanel conversationId="c1" onClose={vi.fn()} />);
    await waitFor(() => expect(sockets).toHaveLength(1));
    act(() => sockets[0].open());
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "exit", code: 0 }) }));
    act(() => sockets[0].onclose?.({ code: 1000 }));
    expect(terms[0].text()).toContain("进程已退出，按任意键重启");
    act(() => terms[0].dataHandler("x"));
    await waitFor(() => expect(sockets).toHaveLength(2));
    expect(sockets[1].sent).toEqual([]);
  });

  it("token 失效重取一次；没有工作目录时说明原因", async () => {
    render(<TerminalPanel conversationId="c1" onClose={vi.fn()} />);
    await waitFor(() => expect(sockets).toHaveLength(1));
    act(() => sockets[0].onclose?.({ code: 4401 }));
    await waitFor(() => expect(sockets).toHaveLength(2));
    expect(bootstrapSessionAuth).toHaveBeenLastCalledWith(true);
    act(() => sockets[1].onclose?.({ code: 4409 }));
    expect(terms[0].text()).toContain("暂无工作目录");
  });
});
