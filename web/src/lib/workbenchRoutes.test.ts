import { expect, it } from "vitest";
import {parseRoute,workbenchPath,routeView} from "./routes";
it("workbench library and module deep links round-trip without hijacking old routes",()=>{expect(parseRoute("/workbench")).toEqual({name:"workbench",moduleId:null});expect(parseRoute(workbenchPath("my app"))).toEqual({name:"workbench",moduleId:"my app"});expect(routeView(parseRoute("/workbench"))).toBeNull();expect(parseRoute("/workbench/a/b").name).toBe("unknown");expect(parseRoute("/config")).toEqual({name:"config"});});
