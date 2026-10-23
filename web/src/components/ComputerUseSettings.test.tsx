import { expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ComputerUseSettings } from "./ComputerUseSettings";
import { sessionRequest } from "../lib/sessionApi";
vi.mock("../lib/sessionApi", () => ({ sessionRequest: vi.fn() }));
const base = {
  enabled:false,setup:{status:"idle"},
  agents:[{id:"fixture",label:"Test Agent",browser:{status:"ready",requirements:[]},desktop:{status:"unavailable",requirements:["cua_driver_missing"]}}],
  fallbacks:[{provider:"playwright",capability:"browser",status:"unavailable",requirements:["install-provider"]}],pendingResources:[],
};
const ready = {...base,enabled:true,setup:{status:"ready",permissions:{"accessibility":true,"screen-recording":true}}};
const needsPermission = {...base,enabled:true,setup:{status:"needs_permission",permissions:{"accessibility":false,"screen-recording":true}},fallbacks:[
  {provider:"cua-driver",capability:"desktop",status:"needs_setup",requirements:["accessibility"],permissionSetup:{host:"cua-driver",label:"CuaDriver",permissions:["accessibility"]}},
]};
const writingCalls = () => vi.mocked(sessionRequest).mock.calls.filter(([,options]) => options?.method && options.method !== "GET");
const readCalls = () => vi.mocked(sessionRequest).mock.calls.filter(([path,options]) => path.startsWith("/api/computer-use") && !options?.method);
async function expand() { await userEvent.setup().click(screen.getByText("电脑操作")); await waitFor(()=>expect(screen.getByRole("switch",{name:"启用电脑操作"})).toBeEnabled()); }
beforeEach(()=>{vi.clearAllMocks();vi.mocked(sessionRequest).mockResolvedValue(base);});
afterEach(()=>vi.useRealTimers());

it("starts off and does not install, request permission, or probe before expansion",async()=>{
 render(<ComputerUseSettings/>);expect(sessionRequest).not.toHaveBeenCalled();await expand();expect(screen.getByRole("switch")).not.toBeChecked();expect(readCalls()).toHaveLength(1);expect(writingCalls()).toHaveLength(0);expect(screen.queryByText("CuaDriver")).toBeNull();
 expect(screen.queryByText(/原生权限由 Agent 管理/)).toBeNull();
});
it("identifies externally managed native permissions even while setup is off",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...base,nativePermissionHosts:["Fixture Agent"]});render(<ComputerUseSettings/>);await expand();
 expect(screen.getByText("Fixture Agent 的原生权限由 Agent 管理。")).toBeVisible();expect(screen.getByRole("switch")).not.toBeChecked();expect(writingCalls()).toHaveLength(0);
});
it("one enable action starts setup once and renders download progress",async()=>{
 render(<ComputerUseSettings/>);await expand();vi.mocked(sessionRequest).mockResolvedValueOnce({...base,enabled:true,setup:{status:"downloading"}});
 await userEvent.setup().click(screen.getByRole("switch"));expect(await screen.findByText("正在下载")).toBeVisible();expect(screen.getByRole("switch")).toBeChecked();expect(writingCalls()).toEqual([["/api/computer-use/enabled",{method:"PUT",body:{enabled:true}}]]);expect(screen.queryByText("Peekaboo")).toBeNull();
});
it("polls setup into a single permission guide without requesting OS access",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"installing"}});render(<ComputerUseSettings/>);await expand();vi.useFakeTimers();
 await act(async()=>{fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));});
 vi.mocked(sessionRequest).mockResolvedValue(needsPermission);await act(async()=>{await vi.advanceTimersByTimeAsync(2000);});
 expect(screen.getByRole("region",{name:"电脑操作授权"})).toBeVisible();expect(screen.getByRole("button",{name:"打开辅助功能"})).toBeVisible();expect(screen.queryByRole("button",{name:"打开录屏与系统录音"})).toBeNull();expect(writingCalls()).toHaveLength(0);
 const reads=readCalls().length;await act(async()=>{await vi.advanceTimersByTimeAsync(3000);});expect(readCalls()).toHaveLength(reads+1);
});
it("checks progress every two seconds and stops after ready",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"checking"}});render(<ComputerUseSettings/>);await expand();vi.useFakeTimers();
 await act(async()=>{fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));});
 vi.mocked(sessionRequest).mockResolvedValueOnce({...ready,setup:{status:"downloading"}}).mockResolvedValue(ready);
 const before=readCalls().length;await act(async()=>{await vi.advanceTimersByTimeAsync(2000);});expect(screen.getByText("正在下载")).toBeVisible();expect(readCalls()).toHaveLength(before+1);
 await act(async()=>{await vi.advanceTimersByTimeAsync(2000);});expect(screen.getByRole("status")).toHaveTextContent("可用");const done=readCalls().length;await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});expect(readCalls()).toHaveLength(done);
});
it("one permission action registers the helper, opens the exact pane, and verifies actual grants",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(needsPermission);render(<ComputerUseSettings/>);await expand();expect(writingCalls()).toHaveLength(0);
 vi.mocked(sessionRequest).mockImplementation(async(path)=>path==="/api/computer-use/permissions/request"||path==="/api/computer-use/permissions/accessibility"?{opened:true}:needsPermission);
 await userEvent.setup().click(screen.getByRole("button",{name:"打开辅助功能"}));expect(writingCalls()).toEqual([["/api/computer-use/permissions/request",{method:"POST"}],["/api/computer-use/permissions/accessibility",{method:"POST"}]]);expect(screen.getByRole("region",{name:"电脑操作授权"})).toBeVisible();
 vi.mocked(sessionRequest).mockResolvedValue(ready);await userEvent.setup().click(screen.getByRole("button",{name:"重新检查"}));await waitFor(()=>expect(screen.queryByRole("region",{name:"电脑操作授权"})).toBeNull());expect(screen.getByRole("status")).toHaveTextContent("可用");
});
it("refreshes permission checks when returning to the visible settings",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(needsPermission);render(<ComputerUseSettings/>);await expand();vi.mocked(sessionRequest).mockResolvedValue(ready);fireEvent(window,new Event("focus"));await waitFor(()=>expect(screen.queryByRole("region",{name:"电脑操作授权"})).toBeNull());expect(sessionRequest).toHaveBeenLastCalledWith("/api/computer-use/permissions",{signal:expect.any(AbortSignal)});expect(writingCalls()).toHaveLength(0);
});
it("retries failed setup with concise failure labels and no raw diagnostics",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"failed",error:"integrity_failed"}});render(<ComputerUseSettings/>);await expand();expect(screen.getByText("组件校验未通过，请重新下载。")).toBeVisible();expect(screen.queryByText("integrity_failed")).toBeNull();
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"checking"}});await userEvent.setup().click(screen.getByRole("button",{name:"重试"}));expect(writingCalls()).toEqual([["/api/computer-use/setup",{method:"POST"}]]);expect(screen.getByText("正在检查")).toBeVisible();
});
it("disables future runs without issuing cancellation or permission commands",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(ready);render(<ComputerUseSettings/>);await expand();vi.mocked(sessionRequest).mockResolvedValue({...base,pendingResources:["desktop"]});await userEvent.setup().click(screen.getByRole("switch"));expect(screen.getByRole("switch")).not.toBeChecked();expect(screen.getByRole("switch")).toHaveAttribute("title","关闭仅对后续运行生效。");expect(writingCalls()).toEqual([["/api/computer-use/enabled",{method:"PUT",body:{enabled:false}}]]);
});
it("stops timers and aborts polling when leaving settings",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"installing"}});const view=render(<ComputerUseSettings/>);await expand();vi.useFakeTimers();await act(async()=>{fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));});const reads=readCalls().length;view.unmount();await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});expect(readCalls()).toHaveLength(reads);expect(writingCalls()).toHaveLength(0);
});
it("does not let an aborted poll overwrite a later disable",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(ready);const view=render(<ComputerUseSettings/>);await expand();let resolve!:(value:unknown)=>void;vi.mocked(sessionRequest).mockImplementationOnce(()=>new Promise(r=>{resolve=r;}) as never);fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));const pending=vi.mocked(sessionRequest).mock.calls.at(-1)![1]!.signal!;
 vi.mocked(sessionRequest).mockResolvedValue(base);await userEvent.setup().click(screen.getByRole("switch"));expect(pending.aborted).toBe(true);await act(async()=>resolve(ready));expect(screen.getByRole("switch")).not.toBeChecked();view.unmount();
});
it("stops after a failed poll rather than retrying indefinitely",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"checking"}});render(<ComputerUseSettings/>);await expand();vi.useFakeTimers();vi.mocked(sessionRequest).mockRejectedValue(new Error("/private/credential.json"));await act(async()=>{fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));});const reads=readCalls().length;await act(async()=>{await vi.advanceTimersByTimeAsync(20000);});expect(readCalls()).toHaveLength(reads);expect(screen.getByText("暂时无法检查，请刷新")).toBeVisible();expect(screen.queryByText(/credential/)).toBeNull();
});
it("keeps native capabilities in collapsed details and uses CuaDriver for the desktop provider",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,fallbacks:[{provider:"cua-driver",capability:"desktop",status:"ready",requirements:[]}]});render(<ComputerUseSettings/>);await expand();expect(screen.getByText("优先使用 Agent 原生能力。")).toBeVisible();expect(screen.getByText("Test Agent").closest("details")).not.toHaveAttribute("open");await userEvent.setup().click(screen.getByText("已接入能力"));expect(screen.getByText("原生")).toBeVisible();expect(screen.getByText("CuaDriver")).toBeVisible();expect(screen.queryByText("Peekaboo")).toBeNull();
});
it("keeps permission navigation user-triggered and shows open failures without paths",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(needsPermission);render(<ComputerUseSettings/>);await expand();await userEvent.setup().click(screen.getByText("列表中没有 CuaDriver？"));vi.mocked(sessionRequest).mockResolvedValueOnce({opened:true});await userEvent.setup().click(screen.getByRole("button",{name:"定位应用"}));expect(sessionRequest).toHaveBeenLastCalledWith("/api/computer-use/permissions/hosts/cua-driver/reveal",{method:"POST"});vi.mocked(sessionRequest).mockResolvedValueOnce({opened:true}).mockRejectedValueOnce(new Error("/private/tool"));await userEvent.setup().click(screen.getByRole("button",{name:"打开辅助功能"}));expect(screen.getByText("无法打开系统设置")).toBeVisible();expect(screen.queryByText(/private/)).toBeNull();
});

it("collapsing pauses checks and reopening rechecks an enabled setup",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"installing"}});render(<ComputerUseSettings/>);await expand();vi.useFakeTimers();await act(async()=>{fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));});
 const details=screen.getByText("电脑操作").closest("details")!;await act(async()=>{details.open=false;fireEvent(details,new Event("toggle"));});const paused=readCalls().length;await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});expect(readCalls()).toHaveLength(paused);
 vi.mocked(sessionRequest).mockResolvedValue(ready);await act(async()=>{details.open=true;fireEvent(details,new Event("toggle"));});expect(sessionRequest).toHaveBeenLastCalledWith("/api/computer-use/permissions",{signal:expect.any(AbortSignal)});expect(screen.getByRole("status")).toHaveTextContent("可用");
});
it("aborts an in-flight check on unmount and ignores its eventual response",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(ready);const view=render(<ComputerUseSettings/>);await expand();let resolve!:(value:unknown)=>void;vi.mocked(sessionRequest).mockImplementationOnce(()=>new Promise(r=>{resolve=r;}) as never);fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));const signal=vi.mocked(sessionRequest).mock.calls.at(-1)![1]!.signal!;view.unmount();expect(signal.aborted).toBe(true);await act(async()=>resolve(needsPermission));expect(screen.queryByRole("region",{name:"电脑操作授权"})).toBeNull();
});
it("continues polling after consecutive instant responses with the same setup status",async()=>{
 vi.mocked(sessionRequest).mockImplementation(async()=>({...ready,setup:{status:"checking"}}));render(<ComputerUseSettings/>);await expand();vi.useFakeTimers();await act(async()=>{fireEvent.click(screen.getByRole("button",{name:"刷新电脑工具"}));});
 const before=readCalls().length;
 for(let i=1;i<=3;i++){await act(async()=>{await vi.advanceTimersByTimeAsync(2000);});expect(readCalls()).toHaveLength(before+i);expect(screen.getByText("正在检查")).toBeVisible();}
 vi.mocked(sessionRequest).mockResolvedValue(ready);await act(async()=>{await vi.advanceTimersByTimeAsync(2000);});expect(screen.getByRole("status")).toHaveTextContent("可用");const done=readCalls().length;await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});expect(readCalls()).toHaveLength(done);
});
it("reports a runtime connection failure without treating it as missing permissions",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue({...ready,setup:{status:"failed",error:"runtime_check_failed"}});render(<ComputerUseSettings/>);await expand();expect(screen.getByText("组件暂时无法连接，请重试。")).toBeVisible();expect(screen.queryByRole("region",{name:"电脑操作授权"})).toBeNull();expect(screen.queryByText("runtime_check_failed")).toBeNull();expect(screen.getByRole("button",{name:"重试"})).toBeEnabled();
});

it("shows individual grants and verifies screen access only on an explicit click",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(ready);render(<ComputerUseSettings/>);await expand();
 await userEvent.setup().click(screen.getByRole('button',{name:'授权'}));
 expect(screen.getAllByText('已开启')).toHaveLength(2);expect(screen.queryByText('去开启')).toBeNull();
 expect(writingCalls()).toHaveLength(0);
 vi.mocked(sessionRequest).mockImplementation(async(path)=>path.endsWith('/verify')?{opened:true}:ready);
 await userEvent.setup().click(screen.getByRole('button',{name:'测试屏幕读取'}));
 expect(await screen.findByText('屏幕读取正常')).toBeVisible();
 expect(writingCalls()).toEqual([['/api/computer-use/permissions/verify',{method:'POST'}]]);
});

it("still opens the settings pane if the OS registration helper fails",async()=>{
 vi.mocked(sessionRequest).mockResolvedValue(needsPermission);render(<ComputerUseSettings/>);await expand();
 vi.mocked(sessionRequest).mockImplementation(async(path)=>{
  if(path.endsWith('/request')) throw new Error('helper unavailable');
  return path.endsWith('/accessibility')?{opened:true}:needsPermission;
 });
 await userEvent.setup().click(screen.getByRole('button',{name:'打开辅助功能'}));
 expect(writingCalls().map(([path])=>path)).toEqual(['/api/computer-use/permissions/request','/api/computer-use/permissions/accessibility']);
 expect(screen.queryByText('未能发起授权，请重试')).toBeNull();
});
