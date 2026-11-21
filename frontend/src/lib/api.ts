import axios, { AxiosError } from "axios";
import { API_BASE_URL, REQUEST_TIMEOUT_MS } from "./config";
import type { ApiErrorBody } from "./types";

/**
 * One axios instance for the whole app.
 *
 * `baseURL` comes from VITE_API_BASE_URL. Nothing else in the codebase is
 * allowed to create its own client, so timeouts, headers and error handling
 * stay in one place.
 */
export const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: REQUEST_TIMEOUT_MS,
  headers: { Accept: "application/json" },
});

/** Normalised error surfaced to the UI. */
export class ApiError extends Error {
  readonly status?: number;
  readonly code?: string;
  readonly details?: unknown;

  constructor(message: string, options: { status?: number; code?: string; details?: unknown } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.details = options.details;
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  return typeof value === "object" && value !== null;
}

/**
 * Turn any axios failure into an `ApiError` that carries the backend's own
 * message. The orchestrator returns `{ error: { code, message } }` for
 * `OrchestratorError`; FastAPI returns `{ detail: ... }` for validation
 * failures. Both are handled, and network failures get a plain-English
 * message that names the base URL so misconfiguration is obvious.
 */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;

  if (axios.isAxiosError(error)) {
    const axiosError = error as AxiosError<unknown>;
    const status = axiosError.response?.status;
    const body = axiosError.response?.data;

    if (isApiErrorBody(body)) {
      const nested = body.error;
      if (nested && typeof nested === "object") {
        const message =
          typeof nested.message === "string" && nested.message.length > 0
            ? nested.message
            : `Request failed with status ${status ?? "unknown"}`;
        return new ApiError(message, {
          status,
          code: typeof nested.code === "string" ? nested.code : undefined,
          details: nested.details,
        });
      }
      if (typeof body.detail === "string") {
        return new ApiError(body.detail, { status, code: "VALIDATION_ERROR" });
      }
      if (Array.isArray(body.detail)) {
        return new ApiError("The request was rejected by the API.", {
          status,
          code: "VALIDATION_ERROR",
          details: body.detail,
        });
      }
    }

    if (axiosError.code === "ECONNABORTED") {
      return new ApiError(`The API did not respond within ${REQUEST_TIMEOUT_MS / 1000}s.`, {
        code: "TIMEOUT",
      });
    }
    if (!axiosError.response) {
      return new ApiError(
        `Cannot reach the orchestrator at ${API_BASE_URL}. Is the backend running?`,
        { code: "NETWORK_ERROR" }
      );
    }
    return new ApiError(`Request failed with status ${status ?? "unknown"}.`, {
      status,
      code: axiosError.code,
    });
  }

  if (error instanceof Error) return new ApiError(error.message);
  return new ApiError("An unexpected error occurred.");
}
