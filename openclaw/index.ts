import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { readFileSync } from "node:fs";

type JsonObject = Record<string, unknown>;
type JsonSchema = Record<string, unknown>;

const DEFAULT_GATEWAY_URL = "http://127.0.0.1:8330";
const DEFAULT_ADMIN_URL = "http://127.0.0.1:8310";
const DEFAULT_STACK_DIR = "/path/to/claude-stack";

const emptyObjectSchema: JsonSchema = { type: "object", additionalProperties: false, properties: {} };

function objectSchema(properties: Record<string, JsonSchema>, required: string[] = []): JsonSchema {
  const schema: JsonSchema = { type: "object", additionalProperties: false, properties };
  if (required.length) {
    schema.required = required;
  }
  return schema;
}

const stringSchema: JsonSchema = { type: "string" };
const booleanSchema: JsonSchema = { type: "boolean" };
const numberSchema: JsonSchema = { type: "number" };
const argumentsSchema: JsonSchema = { type: "object", additionalProperties: true };

function textResult(payload: unknown) {
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(payload, null, 2),
      },
    ],
  };
}

function stackDir(pluginConfig: JsonObject) {
  const configured = typeof pluginConfig.stackDir === "string" ? pluginConfig.stackDir.trim() : "";
  return configured || process.env.CLAUDE_STACK_DIR || DEFAULT_STACK_DIR;
}

function envValue(name: string, pluginConfig: JsonObject) {
  const direct = process.env[name]?.trim();
  if (direct) return direct;
  try {
    const envText = readFileSync(`${stackDir(pluginConfig).replace(/\/+$/, "")}/.env`, "utf8");
    for (const line of envText.split(/\r?\n/)) {
      if (line.startsWith(`${name}=`)) {
        return line.split("=", 2)[1]?.trim().replace(/^['"]|['"]$/g, "") || "";
      }
    }
  } catch {
    // OpenClaw can still call unauthenticated local services; the gateway will
    // return 401 if the local stack requires a token and none is readable.
  }
  return "";
}

function gatewayToken(pluginConfig: JsonObject) {
  return envValue("CHATGPT_ACTIONS_API_KEY", pluginConfig);
}

async function requestJson(method: string, url: string, payload?: JsonObject, bearerToken = "") {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (bearerToken) {
    headers.Authorization = `Bearer ${bearerToken}`;
  }
  const init: RequestInit = {
    method,
    headers,
  };
  if (payload !== undefined) {
    init.body = JSON.stringify(payload);
    init.headers = { ...headers, "Content-Type": "application/json" };
  }
  const started = Date.now();
  try {
    const response = await fetch(url, init);
    const text = await response.text();
    let data: unknown = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text };
    }
    return {
      ok: response.ok,
      status: response.status,
      latency_ms: Date.now() - started,
      data,
    };
  } catch (error) {
    return {
      ok: false,
      status: 0,
      latency_ms: Date.now() - started,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

function loopbackUrl(value: unknown, fallback: string) {
  const raw = typeof value === "string" ? value.trim() : "";
  const url = raw || fallback;
  if (!url.startsWith("http://127.0.0.1:")) {
    return fallback;
  }
  return url.replace(/\/+$/, "");
}

function urls(pluginConfig: JsonObject) {
  const gatewayUrl = loopbackUrl(pluginConfig.gatewayUrl, DEFAULT_GATEWAY_URL);
  const adminUrl = loopbackUrl(pluginConfig.adminUrl, DEFAULT_ADMIN_URL);
  return { gatewayUrl, adminUrl };
}

async function invokeTool(toolName: string, params: JsonObject, pluginConfig: JsonObject) {
  const { gatewayUrl } = urls(pluginConfig);
  const payload: JsonObject = {
    ...params,
    dry_run: params.dry_run !== false,
    explicit_approval: params.explicit_approval === true,
  };
  if (params.idempotency_key) {
    payload.idempotency_key = params.idempotency_key;
  }
  return requestJson("POST", `${gatewayUrl}/${encodeURIComponent(toolName)}/invoke`, payload, gatewayToken(pluginConfig));
}

export default definePluginEntry({
  id: "myos",
  name: "MyOS",
  description: "OpenClaw bridge for MyOS integrations, sessions, policy, and autonomous work loops.",
  register(api) {
    const pluginConfig = (api.pluginConfig || {}) as JsonObject;

    api.registerTool({
      name: "myos_health",
      description: "Check MyOS Admin and Integration Gateway health.",
      parameters: emptyObjectSchema,
      async execute() {
        const { gatewayUrl, adminUrl } = urls(pluginConfig);
        const [gateway, admin] = await Promise.all([
          requestJson("GET", `${gatewayUrl}/health`, undefined, gatewayToken(pluginConfig)),
          requestJson("GET", `${adminUrl}/health`),
        ]);
        return textResult({ ok: gateway.ok && admin.ok, gateway, admin, urls: { gatewayUrl, adminUrl } });
      },
    });

    api.registerTool({
      name: "myos_policy_get",
      description: "Read MyOS Admin policy, including IT/NO-IT mode, model policy, write policy, and autonomy limits.",
      parameters: emptyObjectSchema,
      async execute() {
        const { adminUrl } = urls(pluginConfig);
        return textResult(await requestJson("GET", `${adminUrl}/policy`));
      },
    });

    api.registerTool({
      name: "myos_openclaw_identity",
      description: "Read the configured OpenClaw runtime identity, command owner, approval actor, and approval capabilities.",
      parameters: emptyObjectSchema,
      async execute() {
        const { adminUrl } = urls(pluginConfig);
        return textResult(await requestJson("GET", `${adminUrl}/openclaw/identity`));
      },
    });

    api.registerTool({
      name: "myos_sessions_inventory",
      description: "Inventory retained browser sessions without copying, deleting, or reauthenticating cookies.",
      parameters: emptyObjectSchema,
      async execute() {
        const { adminUrl } = urls(pluginConfig);
        return textResult(await requestJson("GET", `${adminUrl}/sessions`));
      },
    });

    api.registerTool({
      name: "myos_revenue_queue",
      description: "Queue latest governed revenue actions into the MyOS approval ledger. This does not send or post anything.",
      parameters: objectSchema({
        refresh: booleanSchema,
        limit: numberSchema,
        show: numberSchema,
      }),
      async execute(_id, params) {
        const { adminUrl } = urls(pluginConfig);
        return textResult(
          await requestJson("POST", `${adminUrl}/revenue/queue`, {
            refresh: params.refresh !== false,
            limit: Number(params.limit || 40),
            show: Number(params.show || 10),
          }),
        );
      },
    });

    api.registerTool({
      name: "myos_approvals_list",
      description: "List MyOS approval-ledger actions for operator review or OpenClaw dry-run planning.",
      parameters: objectSchema({
        status: stringSchema,
        pipeline: stringSchema,
        limit: numberSchema,
      }),
      async execute(_id, params) {
        const { adminUrl } = urls(pluginConfig);
        const query = new URLSearchParams();
        if (params.status) query.set("status", String(params.status));
        if (params.pipeline) query.set("pipeline", String(params.pipeline));
        query.set("limit", String(Number(params.limit || 100)));
        return textResult(await requestJson("GET", `${adminUrl}/approvals?${query.toString()}`));
      },
    });

    api.registerTool({
      name: "myos_approval_dry_run",
      description: "Dry-run one MyOS approval action through the guarded gateway without executing it.",
      parameters: objectSchema({ action_id: stringSchema }, ["action_id"]),
      async execute(_id, params) {
        const { adminUrl } = urls(pluginConfig);
        return textResult(await requestJson("POST", `${adminUrl}/approvals/${encodeURIComponent(String(params.action_id))}/dry-run`, {}));
      },
    });

    api.registerTool({
      name: "myos_approval_execute",
      description: "Execute one already-approved or policy-authorized MyOS approval action. This cannot approve actions by itself.",
      parameters: objectSchema({ action_id: stringSchema, actor: stringSchema }, ["action_id"]),
      async execute(_id, params) {
        const { adminUrl } = urls(pluginConfig);
        return textResult(
          await requestJson("POST", `${adminUrl}/approvals/${encodeURIComponent(String(params.action_id))}/execute`, {
            actor: params.actor || "openclaw",
          }),
        );
      },
    });

    api.registerTool({
      name: "myos_tools_search",
      description: "Search MyOS tools exposed through the OpenAPI Integration Gateway.",
      parameters: objectSchema({
        query: stringSchema,
      }),
      async execute(_id, params) {
        const { gatewayUrl } = urls(pluginConfig);
        const response = await requestJson("GET", `${gatewayUrl}/tools`, undefined, gatewayToken(pluginConfig));
        const data = response.data as JsonObject;
        const tools = Array.isArray(data?.tools) ? data.tools : [];
        const query = String(params.query || "").toLowerCase();
        const filtered = tools.filter((tool) => {
          const row = tool as JsonObject;
          const haystack = `${row.name || ""} ${row.description || ""}`.toLowerCase();
          return !query || haystack.includes(query);
        });
        return textResult({ ok: response.ok, query: params.query || "", count: filtered.length, tools: filtered });
      },
    });

    api.registerTool(
      {
        name: "myos_tool_invoke",
        description: "Invoke any MyOS tool through the guarded gateway. Defaults to dry-run; writes need explicit approval and idempotency.",
        parameters: objectSchema(
          {
            tool_name: stringSchema,
            arguments: argumentsSchema,
            dry_run: booleanSchema,
            explicit_approval: booleanSchema,
            idempotency_key: stringSchema,
          },
          ["tool_name"],
        ),
        async execute(_id, params) {
          const args: JsonObject = { ...(params.arguments || {}) };
          if (params.dry_run !== undefined) {
            args.dry_run = params.dry_run;
          }
          if (params.explicit_approval !== undefined) {
            args.explicit_approval = params.explicit_approval;
          }
          if (params.idempotency_key) {
            args.idempotency_key = params.idempotency_key;
          }
          return textResult(await invokeTool(params.tool_name, args, pluginConfig));
        },
      },
      { optional: true },
    );

    api.registerTool({
      name: "myos_content_generator_status",
      description: "Read local content/image generator status, configured auto mode, recent renders, and ComfyUI/A1111/Swarm readiness.",
      parameters: objectSchema({
        limit: numberSchema,
      }),
      async execute(_id, params) {
        return textResult(await invokeTool("image_production_status", { limit: Number(params.limit || 8), dry_run: true }, pluginConfig));
      },
    });

    api.registerTool({
      name: "myos_content_generator_configure",
      description: "Configure the local content generator for auto/manual mode, backend auto/comfy/sdxl/pony/swarm/a1111/card, checkpoint, and default custom prompt. Writes require dry_run=false and explicit_approval=true.",
      parameters: objectSchema({
        mode: stringSchema,
        auto_enabled: booleanSchema,
        backend: stringSchema,
        model_profile: stringSchema,
        checkpoint: stringSchema,
        custom_prompt: stringSchema,
        negative: stringSchema,
        dry_run: booleanSchema,
        explicit_approval: booleanSchema,
      }),
      async execute(_id, params) {
        return textResult(
          await invokeTool("image_production_configure", {
            mode: params.mode || "auto",
            auto_enabled: params.auto_enabled !== false,
            backend: params.backend || "auto",
            model_profile: params.model_profile || "",
            checkpoint: params.checkpoint || "",
            custom_prompt: params.custom_prompt || "",
            negative: params.negative || "",
            dry_run: params.dry_run === false ? false : true,
            explicit_approval: params.explicit_approval === true,
          }, pluginConfig),
        );
      },
    });

    api.registerTool({
      name: "myos_content_generate",
      description: "Generate a private local image/content artifact from a custom prompt using ComfyUI SDXL/Pony, SwarmUI, AUTOMATIC1111, or auto fallback. Writes require dry_run=false and explicit_approval=true.",
      parameters: objectSchema({
        prompt: stringSchema,
        title: stringSchema,
        width: numberSchema,
        height: numberSchema,
        backend: stringSchema,
        model_profile: stringSchema,
        checkpoint: stringSchema,
        negative: stringSchema,
        timeout: numberSchema,
        dry_run: booleanSchema,
        explicit_approval: booleanSchema,
      }, ["prompt"]),
      async execute(_id, params) {
        return textResult(
          await invokeTool("image_production_create", {
            prompt: params.prompt,
            title: params.title || "",
            width: Number(params.width || 1080),
            height: Number(params.height || 1080),
            backend: params.backend || "auto",
            model_profile: params.model_profile || "",
            checkpoint: params.checkpoint || "",
            negative: params.negative || "",
            timeout: Number(params.timeout || 120),
            dry_run: params.dry_run === false ? false : true,
            explicit_approval: params.explicit_approval === true,
          }, pluginConfig),
        );
      },
    });

    api.registerTool({
      name: "myos_travel_flight_search",
      description: "Search flights through MyOS travel tooling. This prepares options; final purchase remains governed by MyOS Admin policy.",
      parameters: objectSchema(
        {
          origin: stringSchema,
          destination: stringSchema,
          depart_date: stringSchema,
          return_date: stringSchema,
          direct_only: booleanSchema,
          airline: stringSchema,
          cabin_class: stringSchema,
        },
        ["origin", "destination", "depart_date"],
      ),
      async execute(_id, params) {
        return textResult(
          await invokeTool("travel_flight_search", {
            origin: params.origin,
            destination: params.destination,
            depart_date: params.depart_date,
            return_date: params.return_date || "",
            direct_only: params.direct_only === true,
            airline: params.airline || "",
            cabin_class: params.cabin_class || "economy",
            dry_run: true,
          }, pluginConfig),
        );
      },
    });

    api.registerTool({
      name: "myos_sales_pipeline_report",
      description: "Read the current revenue funnel and pipeline status.",
      parameters: objectSchema({
        format: { type: "string", enum: ["summary", "detailed", "json"] },
      }),
      async execute(_id, params) {
        return textResult(await invokeTool("revenue_funnel_report", { format: params.format || "summary", dry_run: true }, pluginConfig));
      },
    });

    api.registerTool({
      name: "myos_autonomy_contract",
      description: "Explain the local autonomy contract between OpenClaw and MyOS.",
      parameters: emptyObjectSchema,
      async execute() {
        return textResult({
          ok: true,
          runtime: "OpenClaw owns long-running autonomous agents, retries, objectives, and channel execution.",
          custody: "MyOS owns integrations, browser sessions, cookies, credentials, audit, IT/NO-IT mode, and write gates.",
          revenue_loop: [
            "pull lead and opportunity queues",
            "research and score accounts",
            "draft and queue outreach",
            "watch replies and intent",
            "book meetings and update CRM/tasks within policy",
          ],
        });
      },
    });
  },
});
