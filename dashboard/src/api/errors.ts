import type { ErrorBody } from './types';

/**
 * Every non-2xx API response is `{"error": {code, message, details}}` (API.md).
 * The UI shows `message` inline — never alert(), never a stack trace.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** START refused with 409 `blocked` carries `details.blockers: Issue[]`. */
  get blockers(): unknown[] | null {
    const b = this.details?.['blockers'];
    return Array.isArray(b) ? b : null;
  }

  static async fromResponse(res: Response): Promise<ApiError> {
    let body: { error?: ErrorBody } | null = null;
    try {
      body = (await res.json()) as { error?: ErrorBody };
    } catch {
      // non-JSON error body (proxy hiccup, HTML 502, …)
    }
    const err = body?.error;
    return new ApiError(
      res.status,
      err?.code ?? `http_${res.status}`,
      err?.message ?? `Request failed (HTTP ${res.status})`,
      (err?.details as Record<string, unknown> | null | undefined) ?? null,
    );
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
