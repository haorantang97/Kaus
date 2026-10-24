import { afterEach, expect, it, vi } from "vitest";
import { chatcutLaunch, frameEditorBounds, connectChatCutSurface } from "./chatcutSurface";
afterEach(() => { delete window.kausChatCutNative; document.body.innerHTML=""; });
it("only launches the official fixed editor and keeps the boot credential in memory", () => {
 expect(chatcutLaunch("https://app.chatcut.io/zh/editor/project?editor-boot-token=synthetic")).toBe(true);
 for(const url of ["file:///private/a", "https://app.chatcut.io.evil/editor/a", "https://user@app.chatcut.io/editor/a", "https://app.chatcut.io/profile"]) expect(chatcutLaunch(url)).toBe(false);
});
it("translates module bounds without allowing a module to cover host navigation", () => {
 const frame=new DOMRect(200,80,1000,700);
 expect(frameEditorBounds({x:100,y:50,width:800,height:600},frame,1400,900)).toEqual({x:300,y:130,width:800,height:600});
 for(const bounds of [{x:-200,y:0,width:1200,height:600},{x:0,y:0,width:Infinity,height:600},{x:0,y:0,width:1001.1,height:600},{x:0,y:0,width:100,height:100,moduleId:"foreign"}]) expect(frameEditorBounds(bounds,frame,1400,900)).toBeNull();
});
it("hides when disposed and rejects delayed requests from a revoked channel", async () => {
 const send=vi.fn().mockResolvedValue({accepted:true});window.kausChatCutNative={version:1,send};
 const frame=document.createElement("iframe");document.body.append(frame);
 const surface=connectChatCutSurface(frame,"fixed")!;
 await surface.request({action:"open",url:"https://app.chatcut.io/zh"});
 expect(send).toHaveBeenCalledWith({moduleId:"fixed",action:"open",url:"https://app.chatcut.io/zh"});
 await expect(surface.request({action:"open",url:"https://example.org"})).rejects.toThrow("invalid_request");
 surface.dispose();expect(send).toHaveBeenLastCalledWith({moduleId:"fixed",action:"hide"});
 await expect(surface.request({action:"open",url:"https://app.chatcut.io/zh"})).rejects.toThrow("scope_denied");
});
it("revoked grants prevent later native requests",async()=>{
 const send=vi.fn().mockResolvedValue({});window.kausChatCutNative={version:1,send};let allowed=true;
 const frame=document.createElement("iframe");document.body.append(frame);const surface=connectChatCutSurface(frame,"fixed",()=>allowed)!;
 allowed=false;await expect(surface.request({action:"status"})).rejects.toThrow("scope_denied");surface.dispose();
});
