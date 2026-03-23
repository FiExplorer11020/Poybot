export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

const API_AUTH_TOKEN = process.env.NEXT_PUBLIC_API_AUTH_TOKEN?.trim();

export const apiUrl = (path: string) => `${API_BASE}${path}`;

export const apiHeaders = (base: Record<string, string> = {}) => {
  if (!API_AUTH_TOKEN) {
    return base;
  }

  return {
    ...base,
    "x-api-token": API_AUTH_TOKEN,
  };
};
