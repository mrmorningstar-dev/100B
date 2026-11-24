/**
 * API configuration, URL builder, and authenticated fetch utility for the Dashboard.
 * - Respects VITE_API_BASE_URL for Vercel deployment pointing to external backend.
 * - Transparently attaches X-Dashboard-Token from sessionStorage (key entered on unlock screen).
 * - Manages short-lived signed HMAC tokens for <img> and <video> media endpoints.
 * - Zero secrets are hardcoded or baked into build artifacts.
 */

const AUTH_TOKEN_KEY = 'dashboard_auth_token';
const MEDIA_TOKEN_KEY = 'dashboard_media_token';
const MEDIA_EXPIRY_KEY = 'dashboard_media_token_exp';

export const API_BASE_URL: string = (
  typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE_URL
    ? String(import.meta.env.VITE_API_BASE_URL).replace(/\/+$/, '')
    : ''
);

export function getAuthToken(): string | null {
  try {
    return sessionStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setAuthToken(token: string): void {
  try {
    sessionStorage.setItem(AUTH_TOKEN_KEY, token.trim());
  } catch {
    // ignore
  }
}

export function clearAuthToken(): void {
  try {
    sessionStorage.removeItem(AUTH_TOKEN_KEY);
    sessionStorage.removeItem(MEDIA_TOKEN_KEY);
    sessionStorage.removeItem(MEDIA_EXPIRY_KEY);
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('dashboard:media-token-updated', { detail: { token: null, expiresAt: 0 } }));
    }
  } catch {
    // ignore
  }
}

export function getCachedMediaToken(): string | null {
  try {
    const token = sessionStorage.getItem(MEDIA_TOKEN_KEY);
    const expStr = sessionStorage.getItem(MEDIA_EXPIRY_KEY);
    if (!token || !expStr) return null;
    const now = Math.floor(Date.now() / 1000);
    // Reuse token if it has at least 30 seconds of life remaining
    if (parseInt(expStr, 10) - now > 30) {
      return token;
    }
  } catch {
    // ignore
  }
  return null;
}

export function setCachedMediaToken(token: string, expiresAt: number): void {
  try {
    sessionStorage.setItem(MEDIA_TOKEN_KEY, token);
    sessionStorage.setItem(MEDIA_EXPIRY_KEY, String(expiresAt));
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('dashboard:media-token-updated', { detail: { token, expiresAt } }));
    }
  } catch {
    // ignore
  }
}

/**
 * Fetch a fresh short-lived signed media token from the backend.
 * Valid for ~10 minutes.
 */
export async function refreshMediaToken(): Promise<string | null> {
  try {
    const resp = await apiFetch('/api/auth/media-token');
    if (resp.ok) {
      const data = await resp.json();
      if (data && data.token) {
        setCachedMediaToken(data.token, data.expires_at);
        return data.token;
      }
    }
  } catch {
    // ignore
  }
  return null;
}

/**
 * Build fully-qualified URL for any API endpoint or media asset.
 * If isMedia is true, appends the signed ?token= parameter if available.
 */
export function getApiUrl(path: string, options?: { isMedia?: boolean; token?: string | null }): string {
  if (!path) return '';
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  let fullUrl = API_BASE_URL ? `${API_BASE_URL}${cleanPath}` : cleanPath;

  if (options?.isMedia) {
    const mediaToken = options.token !== undefined ? options.token : getCachedMediaToken();
    if (mediaToken) {
      const sep = fullUrl.includes('?') ? '&' : '?';
      fullUrl = `${fullUrl}${sep}token=${encodeURIComponent(mediaToken)}`;
    }
  }
  return fullUrl;
}

/**
 * Authenticated fetch helper.
 * - Automatically prepends API_BASE_URL
 * - Injects X-Dashboard-Token from sessionStorage
 * - Dispatches 'dashboard:unauthorized' on 401 response
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const url = getApiUrl(path);
  const headers = new Headers(init?.headers || {});

  const token = getAuthToken();
  if (token && !headers.has('X-Dashboard-Token')) {
    headers.set('X-Dashboard-Token', token);
  }

  const mergedInit: RequestInit = {
    ...init,
    headers,
  };

  const response = await fetch(url, mergedInit);

  if (response.status === 401 && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('dashboard:unauthorized'));
  }

  return response;
}
