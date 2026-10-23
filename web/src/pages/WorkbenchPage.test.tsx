import {beforeEach,expect,it,vi} from "vitest";
import {fireEvent,render,screen,waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {WorkbenchPage} from "./WorkbenchPage";
import * as api from "../lib/workbenchApi";
import {chooseNativeWorkspace} from "../lib/workspaceApi";
vi.mock("../lib/workbenchApi",async()=>({...await vi.importActual("../lib/workbenchApi"),dismissWorkbenchIntake:vi.fn(),fetchWorkbenchIntakes:vi.fn(),fetchWorkbenchIntake:vi.fn(),inspectWorkbenchProject:vi.fn(),installWorkbenchModule:vi.fn(),patchWorkbenchModule:vi.fn(),removeWorkbenchModule:vi.fn()}));
vi.mock("../lib/workspaceApi",()=>({chooseNativeWorkspace:vi.fn()}));
vi.mock("../components/WorkbenchConfig",()=>({WorkbenchConfig:({onClose}:{onClose:()=>void})=> <div>配置窗口<button onClick={onClose}>关闭配置</button></div>}));
vi.mock("../components/WorkbenchIntake",()=>({WorkbenchIntake:({initial,onClose,onInstalled,onConversation}:{initial:api.WorkbenchIntake;onClose:()=>void;onInstalled:(m:api.WorkbenchModule)=>void;onConversation:(c:string,i:string)=>void})=><div role="dialog" aria-label="接入向导">{initial.displayName}<button onClick={onClose}>关闭向导</button><button onClick={()=>onInstalled({...module,grants:["data:read"]})}>完成添加</button><button onClick={()=>onConversation("approval-c",initial.id)}>查看审批</button></div>}));
vi.mock("../components/ModuleFrame",()=>({ModuleFrame:()=> <div>模块页面</div>}));
const module={id:"notes",name:"笔记",kind:"external",enabled:true,pinned:false,permissions:["data:read"],grants:[],builtin:false,assetUrl:"/api/workbench/assets/notes/index.html"} as unknown as api.WorkbenchModule;
const props={moduleId:null,modules:[module],loading:false,failed:false,onRefresh:vi.fn(),navigate:vi.fn(),onConversation:vi.fn()};
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api.fetchWorkbenchIntakes).mockResolvedValue({intakes:[]});});
it("library navigates to modules and drag payload identifies only the module",async()=>{const {container}=render(<WorkbenchPage {...props}/>);fireEvent.click(screen.getByRole("button",{name:"笔记 独立应用"}));expect(props.navigate).toHaveBeenCalledWith("/workbench/notes");const setData=vi.fn();fireEvent.dragStart(container.querySelector("article")!,{dataTransfer:{setData}});expect(setData).toHaveBeenCalledWith("application/x-kaus-module","notes");});
it("ungranted module opens settings before mounting third-party UI",async()=>{const user=userEvent.setup();render(<WorkbenchPage {...props} moduleId="notes"/>);expect(screen.queryByText("模块页面")).toBeNull();await user.click(screen.getAllByRole("button",{name:"模块设置"})[0]);expect(screen.getByText("配置窗口")).toBeVisible();});
it("native picker cancellation does not install, and install errors do not expose paths",async()=>{vi.mocked(chooseNativeWorkspace).mockResolvedValueOnce(null).mockResolvedValueOnce("/private/my-module");vi.mocked(api.inspectWorkbenchProject).mockRejectedValue(new Error("/private/my-module raw traceback"));const user=userEvent.setup();render(<WorkbenchPage {...props}/>);await user.click(screen.getByRole("button",{name:"添加模块"}));expect(api.inspectWorkbenchProject).not.toHaveBeenCalled();await user.click(screen.getByRole("button",{name:"添加模块"}));await waitFor(()=>expect(api.inspectWorkbenchProject).toHaveBeenCalledWith("/private/my-module"));expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法识别此项目，请重新选择。");expect(screen.queryByText(/private/)).toBeNull();});
it("disabled modules retain their entry and can be enabled",async()=>{const user=userEvent.setup();render(<WorkbenchPage {...props} modules={[{...module,enabled:false}]} moduleId="notes"/>);await user.click(screen.getByRole("button",{name:"启用"}));expect(api.patchWorkbenchModule).toHaveBeenCalledWith("notes",{enabled:true});});

it("opening and closing settings never duplicates the module frame",async()=>{
  const user=userEvent.setup();render(<WorkbenchPage {...props} modules={[{...module,grants:["data:read"]}]} moduleId="notes"/>);
  for(let n=0;n<3;n++) {await user.click(screen.getByRole("button",{name:"模块设置"}));expect(screen.getAllByText("模块页面")).toHaveLength(1);await user.click(screen.getByRole("button",{name:"关闭配置"}));expect(screen.getAllByText("模块页面")).toHaveLength(1);}
});

it("a new library has no default business modules",()=>{render(<WorkbenchPage {...props} modules={[]}/>);expect(screen.getByText("添加你的第一个模块")).toBeVisible();expect(screen.queryByRole("article")).toBeNull();});

const intake={id:"local-a",displayName:"本地应用",status:"running",kind:"frontend",configuration:null} as api.WorkbenchIntake;
it("inspects native selection before opening the single setup flow and opens the installed module",async()=>{
 const user=userEvent.setup();vi.mocked(chooseNativeWorkspace).mockResolvedValue("/local/application");vi.mocked(api.inspectWorkbenchProject).mockResolvedValue(intake);
 render(<WorkbenchPage {...props}/>);await user.click(screen.getByRole("button",{name:"添加模块"}));expect(await screen.findByRole("dialog",{name:"接入向导"})).toBeVisible();expect(api.installWorkbenchModule).not.toHaveBeenCalled();await user.click(screen.getByRole("button",{name:"完成添加"}));expect(props.navigate).toHaveBeenLastCalledWith("/workbench/notes");expect(screen.queryByRole("dialog")).toBeNull();
});
it("recovers pending setup from its durable task row and keeps approval return identity",async()=>{
 vi.mocked(api.fetchWorkbenchIntakes).mockResolvedValue({intakes:[intake]});const open=vi.fn(),user=userEvent.setup();render(<WorkbenchPage {...props} onIntakeConversation={open}/>);
 await user.click(await screen.findByRole("button",{name:/本地应用.*正在适配/}));await user.click(screen.getByRole("button",{name:"查看审批"}));expect(open).toHaveBeenCalledWith("approval-c","local-a");
});
it("restores an intake directly from the approval return URL",async()=>{
 vi.mocked(api.fetchWorkbenchIntake).mockResolvedValue(intake);render(<WorkbenchPage {...props} intakeId="local-a"/>);expect(await screen.findByRole("dialog",{name:"接入向导"})).toBeVisible();expect(api.fetchWorkbenchIntake).toHaveBeenCalledWith("local-a",expect.any(AbortSignal));
});

it("hides only inactive setup records and keeps the installed module",async()=>{
 const user=userEvent.setup();vi.mocked(api.fetchWorkbenchIntakes).mockResolvedValue({intakes:[{...intake,status:"failed"},{...intake,id:"active",displayName:"进行中"}]});vi.mocked(api.dismissWorkbenchIntake).mockResolvedValue({dismissed:true});render(<WorkbenchPage {...props}/>);
 await user.click(await screen.findByRole("button",{name:"本地应用 · 隐藏记录"}));expect(api.dismissWorkbenchIntake).toHaveBeenCalledWith("local-a");expect(screen.queryByRole("button",{name:/本地应用/})).toBeNull();expect(screen.queryByRole("button",{name:"进行中 · 隐藏记录"})).toBeNull();expect(screen.getByRole("button",{name:"笔记 独立应用"})).toBeVisible();expect(api.removeWorkbenchModule).not.toHaveBeenCalled();
});
it("keeps a setup record when the server says it is still running",async()=>{
 const user=userEvent.setup();vi.mocked(api.fetchWorkbenchIntakes).mockResolvedValue({intakes:[{...intake,status:"ready"}]});vi.mocked(api.dismissWorkbenchIntake).mockRejectedValue({code:"intake_busy"});render(<WorkbenchPage {...props}/>);await user.click(await screen.findByRole("button",{name:"本地应用 · 隐藏记录"}));expect(await screen.findByRole("alert")).toHaveTextContent("项目正在适配。");expect(screen.getByRole("button",{name:"本地应用 · 隐藏记录"})).toBeVisible();
});
