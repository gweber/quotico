/**
 * Fetch wrapper with cookie auth, error handling, and typed responses.
 */
interface ApiError {
  detail: string;
}

interface ApiOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string>;
}

export function useApi() {
  const baseUrl = "/api";

  async function request<T>(path: string, options: ApiOptions = {}): Promise<T> {
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
      let errorMessage = "Ein Fehler ist aufgetreten.";
      try {
        const errorData: ApiError = await response.json();
        errorMessage = errorData.detail || errorMessage;
      } catch {
        // Response wasn't JSON
      }

      if (response.status === 401) {
        // Try to refresh token
        const refreshed = await refreshToken();
        if (refreshed) {
          // Retry original request
          const retryResponse = await fetch(url, fetchOptions);
          if (retryResponse.ok) {
            return retryResponse.json();
          }
        }
        // Only redirect if this wasn't a silent auth check
        if (!path.endsWith("/auth/me")) {
          window.location.href = "/login";
        }
      }

      throw new Error(errorMessage);
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

    patch: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: "PATCH", body }),

    del: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: "DELETE", body }),
  };
}
