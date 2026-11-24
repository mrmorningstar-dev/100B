/**
 * API configuration and URL builder for Dashboard requests.
 * Uses VITE_API_BASE_URL when deployed to Vercel (pointing to an external backend),
 * falling back to relative paths for local development via Vite proxy.
 */
export const API_BASE_URL: string = (
  typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE_URL
    ? String(import.meta.env.VITE_API_BASE_URL).replace(/\/+$/, '')
    : ''
);

export function getApiUrl(path: string): string {
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  if (!API_BASE_URL) return cleanPath;
  return `${API_BASE_URL}${cleanPath}`;
}

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const url = getApiUrl(path);
  return fetch(url, init);
}
