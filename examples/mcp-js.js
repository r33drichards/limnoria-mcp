// Execute using the companion's authenticated run_js tool.
// Native filesystem, subprocess and fetch capabilities are intentionally denied.
console.log(mcp.listTools("limnoria"));
console.log(await mcp.callTool("limnoria", "service_status", {}));
console.log(await mcp.callTool("limnoria", "list_plugins", {}));
// Explicitly requested administrative actions only:
// await mcp.callTool("limnoria", "service_action", { action: "restart" });
// await mcp.callTool("limnoria", "read_plugin_file", {
//   plugin: "AgentExample", filename: "plugin.py"
// });
