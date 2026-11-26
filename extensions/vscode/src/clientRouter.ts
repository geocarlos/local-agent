// Routes the agent's requests to the host (T20). The agent sends fs/* and terminal/* requests to the client; the host
// answers them itself and passes every other line on to the webview. Pure (no vscode import), so it is tested with
// the plain node test runner.

import * as path from "node:path";

export const INVALID_PARAMS = -32602;
export const METHOD_NOT_FOUND = -32601;
export const INTERNAL_ERROR = -32603;
export const RESOURCE_NOT_FOUND = -32002;

export type JsonRpcId = number | string;

export interface ClientRequest {
  id: JsonRpcId;
  method: string;
  params: unknown;
}

// An error answer to a client request, with the ACP or JSON-RPC code it should carry.
export class ClientMethodError extends Error {
  constructor(
    readonly code: number,
    message: string,
  ) {
    super(message);
    this.name = "ClientMethodError";
  }
}

// The agent asks the host for the diagnostics of the files attached to a prompt (T21). Declared by the host in
// clientCapabilities._meta, so the agent asks only when it is answered.
export const DIAGNOSTICS_METHOD = "_local-agent/diagnostics";

// True for the methods the host answers: fs/*, terminal/* and the diagnostics method. The agent's own requests
// (session/request_permission) and every response or notification go to the webview.
export function isHostMethod(method: string): boolean {
  return method.startsWith("fs/") || method.startsWith("terminal/") || method === DIAGNOSTICS_METHOD;
}

// The request, if `line` is an agent request the host answers itself; null if the line goes to the webview.
export function clientRequestOf(line: string): ClientRequest | null {
  let message: unknown;
  try {
    message = JSON.parse(line);
  } catch {
    return null;
  }
  if (!isRecord(message) || typeof message.method !== "string" || !isHostMethod(message.method)) {
    return null;
  }
  const id = message.id;
  if (typeof id !== "number" && typeof id !== "string") {
    return null; // a notification: there is nobody to answer
  }
  return { id, method: message.method, params: message.params ?? {} };
}

// The reply line for one request. `handler` does the work and returns the result, or throws: a ClientMethodError
// gives its own code, anything else is an internal error whose message is the error's message.
export async function answerClientRequest(
  request: ClientRequest,
  handler: (method: string, params: unknown) => Promise<unknown>,
): Promise<string> {
  try {
    const result = await handler(request.method, request.params);
    return JSON.stringify({ jsonrpc: "2.0", id: request.id, result: result ?? null });
  } catch (error) {
    const code = error instanceof ClientMethodError ? error.code : INTERNAL_ERROR;
    const message = error instanceof Error ? error.message : String(error);
    return JSON.stringify({ jsonrpc: "2.0", id: request.id, error: { code, message } });
  }
}

// --- parameter checks (each throws INVALID_PARAMS with the reason) ---

export function recordParams(params: unknown): Record<string, unknown> {
  if (!isRecord(params)) {
    throw new ClientMethodError(INVALID_PARAMS, "Invalid params: expected an object.");
  }
  return params;
}

// A string param that must be an absolute path.
export function absolutePathParam(params: Record<string, unknown>, name: string): string {
  const value = params[name];
  if (typeof value !== "string" || !path.isAbsolute(value)) {
    throw new ClientMethodError(INVALID_PARAMS, `Invalid params: ${name} must be an absolute path.`);
  }
  return value;
}

// A string param that is required.
export function stringParam(params: Record<string, unknown>, name: string): string {
  const value = params[name];
  if (typeof value !== "string") {
    throw new ClientMethodError(INVALID_PARAMS, `Invalid params: ${name} must be a string.`);
  }
  return value;
}

// An optional integer param (absent or null gives null); it must be at least `minimum`.
export function optionalIntParam(params: Record<string, unknown>, name: string, minimum: number): number | null {
  const value = params[name];
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    throw new ClientMethodError(INVALID_PARAMS, `Invalid params: ${name} must be an integer of at least ${minimum}.`);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
