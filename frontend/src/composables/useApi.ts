/**
 * Fetch wrapper with cookie auth, error handling, and typed responses.
 */
interface ApiErrorBody {
  detail: string;
}

export class HttpError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

interface ApiOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string>;
}

export function useApi() {
  const baseUrl = "/api";

  async function request<T>(path: string, options: ApiOptions = {}, _isRetry = false): Promise<T> {
    const { method = "GET", body, params } = options;

    let url = `${baseUrl}${path}`;
    if (params) {
      const searchParams = new URLSearchParams(params);
      url += `?${searchParams.toString()}`;
    }

    const fetchOptions: RequestInit = {
      method,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
      },
    };

    if (body) {
      fetchOptions.body = JSON.stringify(body);
    }

    const response = await fetch(url, fetchOptions);

    if (!response.ok) {
      // On first 401, try token refresh + retry before consuming body
      if (response.status === 401 && !_isRetry) {
        const refreshed = await refreshToken();
        if (refreshed) {
          return request<T>(path, options, true);
        }
        // Only redirect if this wasn't a silent auth check
        if (!path.endsWith("/auth/me")) {
          window.location.href = "/login";
        }
      }

      let errorMessage = "Ein Fehler ist aufgetreten.";
      try {
        const errorData: ApiErrorBody = await response.json();
        errorMessage = errorData.detail || errorMessage;
      } catch {
        // Response wasn't JSON
      }

      throw new HttpError(errorMessage, response.status);
    }

    if (response.status === 204) {
      return undefined as T;
    }

    return response.json();
  }

  async function refreshToken(): Promise<boolean> {
    try {
      const resp = await fetch(`${baseUrl}/auth/refresh`, {
        method: "POST",
        credentials: "include",
      });
      return resp.ok;
    } catch {
      return false;
    }
  }

  return {
    get: <T>(path: string, params?: Record<string, string>) =>
      request<T>(path, { params }),

    post: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: "POST", body }),

    put: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: "PUT", body }),

    patch: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: "PATCH", body }),

    del: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: "DELETE", body }),
  };
}
