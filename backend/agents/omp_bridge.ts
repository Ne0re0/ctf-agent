// omp extension loaded by OmpSolver (backend/agents/omp_solver.py) with `omp -e`.
//
// It registers the sandbox tools (bash, read_file, submit_flag, ...) and forwards
// every call to the solver's local HTTP bridge, which runs it in the challenge's
// Docker container. omp's own tools are disabled (--tools lists only these), so the
// model never touches the host filesystem.
//
// Environment, set by OmpSolver:
//   CTF_AGENT_BRIDGE_URL    http://127.0.0.1:<port>/tool
//   CTF_AGENT_BRIDGE_TOKEN  bearer token for the bridge
//   CTF_AGENT_TOOLS         JSON list of {name, description, inputSchema}

type ToolSpec = { name: string; description: string; inputSchema: Record<string, unknown> };

const bridgeUrl = process.env.CTF_AGENT_BRIDGE_URL ?? "";
const bridgeToken = process.env.CTF_AGENT_BRIDGE_TOKEN ?? "";
const tools: ToolSpec[] = JSON.parse(process.env.CTF_AGENT_TOOLS ?? "[]");

export default function (pi: any) {
  for (const tool of tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.inputSchema,
      async execute(_toolCallId: string, params: unknown) {
        const response = await fetch(bridgeUrl, {
          method: "POST",
          headers: { "content-type": "application/json", authorization: `Bearer ${bridgeToken}` },
          body: JSON.stringify({ tool: tool.name, args: params ?? {} }),
          // Long sandbox commands: no client-side timeout (Bun option), the bridge enforces its own
          timeout: false,
        } as RequestInit);
        if (!response.ok) {
          throw new Error(`ctf-agent bridge error ${response.status}: ${await response.text()}`);
        }
        const out = (await response.json()) as { text?: string; image?: string; mimeType?: string };
        if (out.image) {
          return { content: [{ type: "image", data: out.image, mimeType: out.mimeType ?? "image/png" }], details: {} };
        }
        return { content: [{ type: "text", text: out.text ?? "" }], details: {} };
      },
    });
  }
}
