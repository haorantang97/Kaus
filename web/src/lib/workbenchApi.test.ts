import {expect,it,vi} from "vitest";
import {sessionRequest} from "./sessionApi";
import {startModuleJob,runWorkflow,reorderWorkbenchPins,moduleBackendRequest,inspectWorkbenchProject,fetchWorkbenchIntake,startWorkbenchIntake,retryWorkbenchIntake,cancelWorkbenchIntake,dismissWorkbenchIntake,installWorkbenchIntake} from "./workbenchApi";
vi.mock("./sessionApi",()=>({sessionRequest:vi.fn()}));
it("uses shared authenticated requests and fixed module endpoints",async()=>{await startModuleJob("app/x","task","key");expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/modules/app%2Fx/jobs",{method:"POST",body:{prompt:"task",idempotencyKey:"key"}});await runWorkflow("automation","input","run");expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/modules/automation/workflow/run",{method:"POST",body:{input:"input",idempotencyKey:"run"}});await reorderWorkbenchPins(["b","a"]);expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/pins",{method:"PUT",body:{moduleIds:["b","a"]}});await moduleBackendRequest("app",{method:"GET",path:"/api/topics"});expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/modules/app/backend/request",{method:"POST",body:{method:"GET",path:"/api/topics"}});});

it("onboarding uses the authenticated intake API and preserves request identity and explicit access",async()=>{
 const config={projectId:"p",bindingId:"b",modelId:"m",providerId:"v",reasoningMode:null};const start={...config,idempotencyKey:"retry-key"};
 await inspectWorkbenchProject("/local/project");expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/intakes/inspect",{method:"POST",body:{path:"/local/project"}});
 await fetchWorkbenchIntake("x/y");expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/intakes/x%2Fy",{signal:undefined});
 await startWorkbenchIntake("i",start);expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/intakes/i/start",{method:"POST",body:start});
 await retryWorkbenchIntake("i",start);expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/intakes/i/retry",{method:"POST",body:start});
 await cancelWorkbenchIntake("i");expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/intakes/i/cancel",{method:"POST"});
 await dismissWorkbenchIntake("x/y");expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/intakes/x%2Fy",{method:"DELETE"});
 await installWorkbenchIntake("i",{...config,grants:["backend:run"]});expect(sessionRequest).toHaveBeenLastCalledWith("/api/workbench/intakes/i/install",{method:"POST",body:{...config,grants:["backend:run"]}});
});
