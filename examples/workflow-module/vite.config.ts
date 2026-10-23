import {defineConfig} from "vite";
import react from "@vitejs/plugin-react";
import {readFileSync} from "node:fs";
export default defineConfig({base:"./",plugins:[react(),{name:"module-manifest",generateBundle(){this.emitFile({type:"asset",fileName:"kaus-module.json",source:readFileSync(new URL("./kaus-module.json",import.meta.url),"utf8")});}}],build:{rollupOptions:{input:"module.html"}}});
