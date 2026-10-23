import { beforeEach, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WorkbenchIntake } from "./WorkbenchIntake";
import * as session from "../lib/sessionApi";
import * as api from "../lib/workbenchApi";
vi.mock("../lib/sessionApi", async () => ({ ...await vi.importActual("../lib/sessionApi"), fetchProjects:vi.fn(), fetchConnectedBackends:vi.fn(), fetchProjectBindings:vi.fn(), fetchModelCatalog:vi.fn(), ensureProjectBinding:vi.fn() }));
vi.mock("../lib/workbenchApi", async () => ({ ...await vi.importActual("../lib/workbenchApi"), fetchWorkbenchIntake:vi.fn(), startWorkbenchIntake:vi.fn(), retryWorkbenchIntake:vi.fn(), cancelWorkbenchIntake:vi.fn(), installWorkbenchIntake:vi.fn() }));
const config = {projectId:"p",bindingId:"binding-a",modelId:null,providerId:null,reasoningMode:null};
const binding = {id:"binding-a",projectId:"p",backendId:"mock",enabled:true,displayName:"测试",isDefault:true} as session.BindingWire;
const initial:api.WorkbenchIntake = {id:"i",displayName:"我的应用",moduleName:null,kind:"frontend",status:"detected",requiresAdaptation:true,configuration:config,conversationId:null,moduleId:null,errorCode:null,manifestPermissions:[],executionPolicy:null,createdAt:"now",updatedAt:"now"};
const module = {id:"my-app",name:"我的应用",permissions:["backend:run"],grants:["backend:run"]} as api.WorkbenchModule;
const props = {onClose:vi.fn(),onUpdate:vi.fn(),onInstalled:vi.fn(),onConversation:vi.fn()};
beforeEach(() => {
 vi.clearAllMocks();
 vi.mocked(session.fetchProjects).mockResolvedValue({projects:[{id:"p",slug:"writing",displayName:"写作",status:"active"} as session.ProjectWire],roots:["p"],count:1});
 vi.mocked(session.fetchConnectedBackends).mockResolvedValue({backends:[{id:"mock",displayName:"测试",status:"available"} as unknown as session.BackendWire],count:1});
 vi.mocked(session.fetchProjectBindings).mockResolvedValue({projectId:"p",bindings:[binding],count:1});
 vi.mocked(session.ensureProjectBinding).mockResolvedValue(binding);
 vi.mocked(session.fetchModelCatalog).mockResolvedValue({bindingId:"binding-a",models:[{modelId:"same",displayName:"一号模型",providerId:"one"},{modelId:"same",displayName:"二号模型",providerId:"two"}],defaultModelId:null} as session.ModelCatalogWire);
 vi.mocked(api.fetchWorkbenchIntake).mockResolvedValue({...initial,status:"running",conversationId:"c"});
 vi.mocked(api.startWorkbenchIntake).mockResolvedValue({...initial,status:"queued"});
 vi.mocked(api.cancelWorkbenchIntake).mockResolvedValue({...initial,status:"cancelled"});
 vi.mocked(api.installWorkbenchIntake).mockResolvedValue({intake:{...initial,status:"installed",moduleId:"my-app"},module});
});
it("adds an existing module only after explicit service consent, without starting adaptation", async () => {
 const user=userEvent.setup();render(<WorkbenchIntake {...props} initial={{...initial,kind:"module",status:"ready",requiresAdaptation:false,manifestPermissions:["backend:run","agent:run"]}}/>);
 await screen.findByRole("option",{name:"写作"});
 expect(screen.getByRole("checkbox",{name:"运行模块服务"})).not.toBeChecked();expect(screen.getByRole("checkbox",{name:"调用 Agent 执行任务"})).not.toBeChecked();
 await user.click(screen.getByRole("checkbox",{name:"运行模块服务"}));await user.click(screen.getByRole("button",{name:"添加并打开"}));
 await waitFor(()=>expect(api.installWorkbenchIntake).toHaveBeenCalledWith("i",{...config,grants:["backend:run"]}));expect(props.onInstalled).toHaveBeenCalledWith(module);expect(api.startWorkbenchIntake).not.toHaveBeenCalled();
});
it("uses shared model/provider choices then waits for validation and new permissions", async () => {
 const user=userEvent.setup();vi.mocked(api.fetchWorkbenchIntake).mockResolvedValue({...initial,status:"ready",manifestPermissions:["backend:run"]});render(<WorkbenchIntake {...props} initial={initial}/>);
 await screen.findByRole("option",{name:"二号模型"});await user.selectOptions(screen.getByRole("combobox",{name:"模型"}),'["two","same"]');await user.type(screen.getByRole("textbox",{name:"适配要求"}),"保留原有样式");await user.click(screen.getByRole("button",{name:"适配并添加"}));
 expect(api.startWorkbenchIntake).toHaveBeenCalledWith("i",{...config,modelId:"same",providerId:"two",instruction:"保留原有样式",idempotencyKey:expect.any(String)});
 await screen.findByRole("checkbox",{name:"运行模块服务"},{timeout:2500});expect(api.installWorkbenchIntake).not.toHaveBeenCalled();expect(screen.getByRole("checkbox",{name:"运行模块服务"})).not.toBeChecked();
 await user.click(screen.getByRole("checkbox",{name:"运行模块服务"}));await user.click(screen.getByRole("button",{name:"添加并打开"}));expect(api.installWorkbenchIntake).toHaveBeenCalledWith("i",{...config,modelId:"same",providerId:"two",grants:["backend:run"]});
});
it("supports approvals and cancels the persistent task, without treating dialog closure as cancellation",async()=>{
 const user=userEvent.setup();const view=render(<WorkbenchIntake {...props} initial={{...initial,status:"waiting",conversationId:"approval"}}/>);
 await user.click(screen.getByRole("button",{name:"处理审批"}));expect(props.onConversation).toHaveBeenCalledWith("approval","i");await user.click(screen.getByRole("button",{name:"停止"}));expect(api.cancelWorkbenchIntake).toHaveBeenCalledWith("i");await screen.findByText("已停止");expect(screen.getByRole("button",{name:"重新适配"})).toBeVisible();view.unmount();expect(api.cancelWorkbenchIntake).toHaveBeenCalledOnce();
});
it("reopens a running task without restarting it and closes without cancelling",async()=>{
 const user=userEvent.setup();render(<WorkbenchIntake {...props} initial={{...initial,status:"running"}}/>);await user.click(screen.getByRole("button",{name:"关闭"}));expect(props.onClose).toHaveBeenCalledOnce();expect(api.startWorkbenchIntake).not.toHaveBeenCalled();expect(api.cancelWorkbenchIntake).not.toHaveBeenCalled();
});
it("retains saved adaptation instructions when reopening a paused task",async()=>{
 render(<WorkbenchIntake {...props} initial={{...initial,status:"cancelled",instruction:"保留原有品牌色"}}/>);
 expect(screen.getByRole("textbox",{name:"适配要求"})).toHaveValue("保留原有品牌色");
});
it("retries a failed adaptation and reuses idempotency on a transport failure",async()=>{
 const user=userEvent.setup();vi.mocked(api.retryWorkbenchIntake).mockRejectedValueOnce(new Error("/secret/path traceback")).mockResolvedValueOnce({...initial,status:"queued"});render(<WorkbenchIntake {...props} initial={{...initial,status:"failed",errorCode:"validation_failed"}}/>);
 await waitFor(()=>expect(screen.getByRole("button",{name:"重新适配"})).toBeEnabled());await user.click(screen.getByRole("button",{name:"重新适配"}));expect(await screen.findByRole("alert")).toHaveTextContent("操作未完成，请重试。");expect(screen.queryByText(/secret/)).toBeNull();await user.click(screen.getByRole("button",{name:"重新适配"}));const calls=vi.mocked(api.retryWorkbenchIntake).mock.calls;expect(calls).toHaveLength(2);expect(calls[1][1].idempotencyKey).toEqual(calls[0][1].idempotencyKey);expect(api.startWorkbenchIntake).not.toHaveBeenCalled();
});
it("cannot run an adaptation or grant Agent access without valid configuration",async()=>{
 const user=userEvent.setup();const view=render(<WorkbenchIntake {...props} initial={{...initial,configuration:null}}/>);await screen.findByRole("option",{name:"写作"});expect(screen.getByRole("button",{name:"适配并添加"})).toBeDisabled();view.unmount();render(<WorkbenchIntake {...props} initial={{...initial,status:"ready",configuration:null,manifestPermissions:["agent:run"]}}/>);await screen.findByRole("option",{name:"写作"});await user.click(screen.getByRole("checkbox",{name:"调用 Agent 执行任务"}));expect(screen.getByRole("button",{name:"添加并打开"})).toBeDisabled();
});
it("ignores a stale poll result after cancellation",async()=>{
 let resolve!:(value:api.WorkbenchIntake)=>void;vi.mocked(api.fetchWorkbenchIntake).mockReturnValue(new Promise(r=>{resolve=r;}));const user=userEvent.setup();render(<WorkbenchIntake {...props} initial={{...initial,status:"running"}}/>);
 await waitFor(()=>expect(api.fetchWorkbenchIntake).toHaveBeenCalled(),{timeout:2000});await user.click(screen.getByRole("button",{name:"停止"}));await screen.findByText("已停止");await act(async()=>resolve({...initial,status:"running"}));expect(screen.queryByText("正在适配")).toBeNull();expect(screen.getByText("已停止")).toBeVisible();
});
it("uses a new request identity after a failed request's configuration changes",async()=>{
 const user=userEvent.setup();vi.mocked(api.retryWorkbenchIntake).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({...initial,status:"queued"});render(<WorkbenchIntake {...props} initial={{...initial,status:"failed"}}/>);
 await screen.findByRole("option",{name:"二号模型"});await user.click(screen.getByRole("button",{name:"重新适配"}));await screen.findByRole("alert");await user.selectOptions(screen.getByRole("combobox",{name:"模型"}),'["two","same"]');await user.click(screen.getByRole("button",{name:"重新适配"}));const calls=vi.mocked(api.retryWorkbenchIntake).mock.calls;expect(calls).toHaveLength(2);expect(calls[1][1].idempotencyKey).not.toEqual(calls[0][1].idempotencyKey);expect(calls[1][1].providerId).toEqual("two");
});
it("does not navigate away after installation finishes in a closed dialog",async()=>{
 let resolve!:(value:{intake:api.WorkbenchIntake;module:api.WorkbenchModule})=>void;vi.mocked(api.installWorkbenchIntake).mockReturnValue(new Promise(r=>{resolve=r;}));const user=userEvent.setup();const view=render(<WorkbenchIntake {...props} initial={{...initial,status:"ready",manifestPermissions:[]}}/>);
 await screen.findByRole("option",{name:"写作"});await user.click(screen.getByRole("button",{name:"添加并打开"}));view.unmount();await act(async()=>resolve({intake:{...initial,status:"installed",moduleId:"my-app"},module}));expect(props.onInstalled).not.toHaveBeenCalled();
});
