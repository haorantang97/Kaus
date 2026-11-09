// Starts a workbench module's own MCP server for an Agent session.
// argv: <module root> <entry relative to root> <module data dir> <module id>
// The module sees the same KAUS_MODULE_* variables its backend gets, so both
// read and write the same data directory.
import path from "node:path";
import { pathToFileURL } from "node:url";

const [root, entry, dataDir, moduleId] = process.argv.slice(2);
const target = path.resolve(root, entry);
if (!root || !entry || !dataDir || !moduleId || !target.startsWith(path.resolve(root) + path.sep)) {
  process.stderr.write("kaus module mcp: invalid arguments\n");
  process.exit(2);
}
process.env.KAUS_MODULE_ID = moduleId;
process.env.KAUS_MODULE_DATA_DIR = dataDir;
process.chdir(root);
process.argv = [process.argv[0], target];
await import(pathToFileURL(target).href);
