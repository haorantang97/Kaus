import {afterEach,beforeEach,expect,it,vi} from "vitest";
import {act,fireEvent,render,screen} from "@testing-library/react";
import {ModuleFrame} from "./ModuleFrame";
import {openModuleFrame,releaseModuleFrame,renewModuleFrame,type WorkbenchModule} from "../lib/workbenchApi";
import {connectModuleFrame} from "../lib/workbenchBridge";
import html from "../../index.html?raw";
vi.mock("../lib/workbenchBridge",()=>({connectModuleFrame:vi.fn(()=>vi.fn())}));
vi.mock("../lib/workbenchApi",async()=>({...await vi.importActual("../lib/workbenchApi"),openModuleFrame:vi.fn(),renewModuleFrame:vi.fn(),releaseModuleFrame:vi.fn()}));
let issued=0;
beforeEach(()=>{issued=0;vi.mocked(openModuleFrame).mockImplementation(async id=>{const grant=`grant${++issued}`;return {url:`/api/workbench/assets/${id}/~${grant}/index.html`,token:grant,expiresIn:900};});vi.mocked(renewModuleFrame).mockResolvedValue({expiresIn:900});vi.mocked(releaseModuleFrame).mockResolvedValue({released:true});});
afterEach(()=>{vi.useRealTimers();vi.clearAllMocks();});
const settle=()=>act(async()=>{await Promise.resolve();await Promise.resolve();});
it("external modules run in opaque sandbox without provider or session credentials",async()=>{const module={id:"external",name:"应用",assetUrl:"/api/workbench/assets/external/index.html"} as WorkbenchModule;render(<ModuleFrame module={module} onConversation={vi.fn()}/>);const frame=await screen.findByTitle("应用");expect(frame).toHaveAttribute("sandbox","allow-scripts allow-forms");expect(frame).toHaveAttribute("referrerpolicy","no-referrer");expect(frame).toHaveAttribute("src","/api/workbench/assets/external/~grant1/index.html");expect(frame.getAttribute("src")).not.toContain("token");expect(connectModuleFrame).toHaveBeenCalled();});
it("host CSP disallows external frame navigation while preserving native srcdoc/blob previews",()=>{expect(html).toContain('content="frame-src \'self\' blob:"');});
it("never loads manifest-provided remote URLs",async()=>{render(<ModuleFrame module={{id:"bad",name:"远端",assetUrl:"https://outside.example/module"} as WorkbenchModule} onConversation={vi.fn()}/>);await settle();expect(screen.queryByTitle("远端")).toBeNull();expect(openModuleFrame).not.toHaveBeenCalled();});
it("a grant outside the module asset path is not loaded",async()=>{vi.mocked(openModuleFrame).mockResolvedValueOnce({url:"https://outside.example/~x/index.html",token:"x",expiresIn:900});render(<ModuleFrame module={{id:"odd",name:"异常",assetUrl:"/api/workbench/assets/odd/index.html"} as WorkbenchModule} onConversation={vi.fn()}/>);expect(await screen.findByRole("button",{name:"重试"})).toBeVisible();expect(screen.queryByTitle("异常")).toBeNull();});

const local={id:"lazy",name:"延迟连接",assetUrl:"/api/workbench/assets/lazy/index.html"} as WorkbenchModule;
it("loaded static modules stay quiet and permit a later SDK connection",async()=>{
 vi.useFakeTimers();render(<ModuleFrame module={local} onConversation={vi.fn()}/>);await settle();const bridge=vi.mocked(connectModuleFrame).mock.calls.at(-1)![0];fireEvent.load(screen.getByTitle("延迟连接"));act(()=>vi.advanceTimersByTime(12000));expect(screen.queryByText("模块尚未就绪")).toBeNull();act(()=>bridge.onReady?.());expect(screen.queryByRole("button",{name:"重试"})).toBeNull();
});
it("a real load failure offers reload with a fresh grant, while loading later clears only the load error",async()=>{
 vi.useFakeTimers();render(<ModuleFrame module={local} onConversation={vi.fn()}/>);await settle();const first=screen.getByTitle("延迟连接");act(()=>vi.advanceTimersByTime(10001));expect(screen.getByRole("button",{name:"重试"})).toBeVisible();fireEvent.click(screen.getByRole("button",{name:"重试"}));expect(releaseModuleFrame).toHaveBeenCalledWith("lazy","grant1");await settle();const second=screen.getByTitle("延迟连接");expect(second).not.toBe(first);expect(second).toHaveAttribute("src","/api/workbench/assets/lazy/~grant2/index.html");fireEvent.error(second);expect(screen.getByRole("button",{name:"重试"})).toBeVisible();fireEvent.load(second);expect(screen.queryByRole("button",{name:"重试"})).toBeNull();
});
it("a frame navigation revocation is not hidden by its load event",async()=>{
 render(<ModuleFrame module={local} onConversation={vi.fn()}/>);const frame=await screen.findByTitle("延迟连接");const bridge=vi.mocked(connectModuleFrame).mock.calls.at(-1)![0];fireEvent.load(frame);act(()=>bridge.onRevoked?.());fireEvent.load(screen.getByTitle("延迟连接"));expect(screen.getByRole("button",{name:"重试"})).toBeVisible();
});
it("keeps the grant alive while shown and releases it on close",async()=>{
 vi.useFakeTimers();const view=render(<ModuleFrame module={local} onConversation={vi.fn()}/>);await settle();act(()=>vi.advanceTimersByTime(5*60_000));expect(renewModuleFrame).toHaveBeenCalledWith("lazy","grant1");view.unmount();expect(releaseModuleFrame).toHaveBeenCalledWith("lazy","grant1");
});
it("a failed grant offers retry instead of a blank frame",async()=>{
 vi.mocked(openModuleFrame).mockRejectedValueOnce(new Error("offline"));render(<ModuleFrame module={local} onConversation={vi.fn()}/>);expect(await screen.findByRole("button",{name:"重试"})).toBeVisible();fireEvent.click(screen.getByRole("button",{name:"重试"}));expect(await screen.findByTitle("延迟连接")).toHaveAttribute("src","/api/workbench/assets/lazy/~grant1/index.html");
});
