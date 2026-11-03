import { afterEach, expect, it, vi } from "vitest";
import { surfaceLaunch, frameEditorBounds, connectSiteSurface, nativeSurfaceSites } from "./siteSurface";
afterEach(() => { delete window.kausSurfaceNative; document.body.innerHTML=""; });
const native=(send=vi.fn().mockResolvedValue({accepted:true}),sites=["chatcut","midjourney"])=>{window.kausSurfaceNative={version:2,sites,send};return send;};
it("only launches the official fixed editor and keeps the boot credential in memory", () => {
 expect(surfaceLaunch("chatcut","https://app.chatcut.io/zh/editor/project?editor-boot-token=synthetic")).toBe(true);
 for(const url of ["file:///private/a", "https://app.chatcut.io.evil/editor/a", "https://user@app.chatcut.io/editor/a", "https://app.chatcut.io/profile"]) expect(surfaceLaunch("chatcut",url)).toBe(false);
});
it("keeps launch pages per site and ignores unknown sites", () => {
 expect(surfaceLaunch("midjourney","https://www.midjourney.com/imagine")).toBe(true);
 for(const [site,url] of [["midjourney","https://www.midjourney.com/account"],["midjourney","https://app.chatcut.io/zh"],["chatcut","https://www.midjourney.com/imagine"],["__proto__","https://www.midjourney.com/imagine"],["other","https://example.com"]]) expect(surfaceLaunch(site,url)).toBe(false);
 native(undefined,["midjourney","browser"]);expect(nativeSurfaceSites()).toEqual(["midjourney"]);
});
it("translates module bounds without allowing a module to cover host navigation", () => {
 const frame=new DOMRect(200,80,1000,700);
 expect(frameEditorBounds({x:100,y:50,width:800,height:600},frame,1400,900)).toEqual({x:300,y:130,width:800,height:600});
 for(const bounds of [{x:-200,y:0,width:1200,height:600},{x:0,y:0,width:Infinity,height:600},{x:0,y:0,width:1001.1,height:600},{x:0,y:0,width:100,height:100,moduleId:"foreign"}]) expect(frameEditorBounds(bounds,frame,1400,900)).toBeNull();
});
it("hides when disposed and rejects delayed requests from a revoked channel", async () => {
 const send=native();
 const frame=document.createElement("iframe");document.body.append(frame);
 const surface=connectSiteSurface(frame,"fixed")!;
 await surface.request("chatcut",{action:"open",url:"https://app.chatcut.io/zh"});
 expect(send).toHaveBeenCalledWith({moduleId:"fixed",site:"chatcut",action:"open",url:"https://app.chatcut.io/zh"});
 await expect(surface.request("chatcut",{action:"open",url:"https://example.org"})).rejects.toThrow("invalid_request");
 surface.dispose();expect(send).toHaveBeenLastCalledWith({moduleId:"fixed",site:"chatcut",action:"hide"});
 await expect(surface.request("chatcut",{action:"open",url:"https://app.chatcut.io/zh"})).rejects.toThrow("scope_denied");
});
it("revoked grants prevent later native requests",async()=>{
 native(vi.fn().mockResolvedValue({}));let allowed=true;
 const frame=document.createElement("iframe");document.body.append(frame);const surface=connectSiteSurface(frame,"fixed",()=>allowed)!;
 allowed=false;await expect(surface.request("chatcut",{action:"status"})).rejects.toThrow("scope_denied");surface.dispose();
});
it("grants are checked per site",async()=>{
 const send=native();const frame=document.createElement("iframe");document.body.append(frame);
 const surface=connectSiteSurface(frame,"fixed",site=>site==="midjourney")!;
 await expect(surface.request("chatcut",{action:"status"})).rejects.toThrow("scope_denied");
 await surface.request("midjourney",{action:"status"});expect(send).toHaveBeenCalledWith({moduleId:"fixed",site:"midjourney",action:"status"});
 await expect(surface.request("browser",{action:"status"})).rejects.toThrow("invalid_request");surface.dispose();
});
it("fills only input sites with bounded text and png or jpeg images",async()=>{
 const send=native();const frame=document.createElement("iframe");document.body.append(frame);const surface=connectSiteSurface(frame,"fixed")!;
 await surface.request("midjourney",{action:"fill",text:"a cat --ar 16:9"});
 expect(send).toHaveBeenLastCalledWith({moduleId:"fixed",site:"midjourney",action:"fill",text:"a cat --ar 16:9"});
 await surface.request("midjourney",{action:"fill",image:"data:image/png;base64,iVBORw0KGgo="});
 expect(send).toHaveBeenLastCalledWith({moduleId:"fixed",site:"midjourney",action:"fill",image:"data:image/png;base64,iVBORw0KGgo="});
 for(const p of [{action:"fill",text:""},{action:"fill",text:"x".repeat(6001)},{action:"fill",image:"data:image/svg+xml;base64,PHN2Zz4="},{action:"fill",text:"a",submit:true},{action:"fill"}]) await expect(surface.request("midjourney",p)).rejects.toThrow("invalid_request");
 await expect(surface.request("chatcut",{action:"fill",text:"a cat"})).rejects.toThrow("invalid_request");
 surface.dispose();
});
it("without a native host there is no surface",()=>{
 const frame=document.createElement("iframe");document.body.append(frame);expect(connectSiteSurface(frame,"fixed")).toBeNull();
 window.kausSurfaceNative={version:1,sites:["chatcut"],send:vi.fn()} as unknown as Window["kausSurfaceNative"];expect(connectSiteSurface(frame,"fixed")).toBeNull();
});
