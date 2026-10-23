import {afterEach,expect,it,vi} from "vitest";
import {act,fireEvent,render,screen} from "@testing-library/react";
import {ModuleFrame} from "./ModuleFrame";
import type {WorkbenchModule} from "../lib/workbenchApi";
import {connectModuleFrame} from "../lib/workbenchBridge";
import html from "../../index.html?raw";
vi.mock("../lib/workbenchBridge",()=>({connectModuleFrame:vi.fn(()=>vi.fn())}));
it("external modules run in opaque sandbox without provider or session credentials",()=>{const module={id:"external",name:"应用",assetUrl:"/api/workbench/assets/external/index.html"} as WorkbenchModule;render(<ModuleFrame module={module} onConversation={vi.fn()}/>);const frame=screen.getByTitle("应用");expect(frame).toHaveAttribute("sandbox","allow-scripts allow-forms");expect(frame).toHaveAttribute("referrerpolicy","no-referrer");expect(frame.getAttribute("src")).not.toContain("token");expect(connectModuleFrame).toHaveBeenCalled();});
it("host CSP disallows external frame navigation while preserving native srcdoc/blob previews",()=>{expect(html).toContain('content="frame-src \'self\' blob:"');});
it("never loads manifest-provided remote URLs",()=>{render(<ModuleFrame module={{id:"bad",name:"远端",assetUrl:"https://outside.example/module"} as WorkbenchModule} onConversation={vi.fn()}/>);expect(screen.queryByTitle("远端")).toBeNull();});

afterEach(()=>vi.useRealTimers());
const local={id:"lazy",name:"延迟连接",assetUrl:"/api/workbench/assets/lazy/index.html"} as WorkbenchModule;
it("loaded static modules stay quiet and permit a later SDK connection",()=>{
 vi.useFakeTimers();render(<ModuleFrame module={local} onConversation={vi.fn()}/>);const bridge=vi.mocked(connectModuleFrame).mock.calls.at(-1)![0];fireEvent.load(screen.getByTitle("延迟连接"));act(()=>vi.advanceTimersByTime(12000));expect(screen.queryByText("模块尚未就绪")).toBeNull();act(()=>bridge.onReady?.());expect(screen.queryByRole("button",{name:"重试"})).toBeNull();
});
it("a real load failure offers reload, while loading later clears only the load error",()=>{
 vi.useFakeTimers();render(<ModuleFrame module={local} onConversation={vi.fn()}/>);const first=screen.getByTitle("延迟连接");act(()=>vi.advanceTimersByTime(10001));expect(screen.getByRole("button",{name:"重试"})).toBeVisible();fireEvent.click(screen.getByRole("button",{name:"重试"}));const second=screen.getByTitle("延迟连接");expect(second).not.toBe(first);fireEvent.error(second);expect(screen.getByRole("button",{name:"重试"})).toBeVisible();fireEvent.load(second);expect(screen.queryByRole("button",{name:"重试"})).toBeNull();
});
it("a frame navigation revocation is not hidden by its load event",()=>{
 render(<ModuleFrame module={local} onConversation={vi.fn()}/>);const bridge=vi.mocked(connectModuleFrame).mock.calls.at(-1)![0];fireEvent.load(screen.getByTitle("延迟连接"));act(()=>bridge.onRevoked?.());fireEvent.load(screen.getByTitle("延迟连接"));expect(screen.getByRole("button",{name:"重试"})).toBeVisible();
});
