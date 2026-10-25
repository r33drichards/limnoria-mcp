// Run via the authenticated companion run_js tool. No raw fs/shell/fetch access.
console.log(mcp.listTools("limnoria"));
console.log(await mcp.callTool("limnoria", "service_status", {}));
console.log(await mcp.callTool("limnoria", "list_scripts", {}));
// Authorized script development, without Python code or a bot restart:
// await mcp.callTool("limnoria", "write_script", {
//   name: "Greeting", source: "reply \"Hello [context_get nick]\""
// });
// await mcp.callTool("limnoria", "validate_script", { name: "Greeting" });
// await mcp.callTool("limnoria", "activate_script", { name: "Greeting" });
// A failed validation/activation preserves the last-good active script.
