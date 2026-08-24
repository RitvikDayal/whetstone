// How the session token gets from `whetstone ui` into this page, and why it
// travels the way it does.
//
// THE FRAGMENT, not the query string. `whetstone ui` opens
// `http://127.0.0.1:<port>/#t=<token>`. Browsers never send a fragment to a
// server and never put one in a `Referer`, so the token does not land in
// uvicorn's access log, in a proxy log, or in an outbound request header. A
// query string does all three.
//
// `sessionStorage`, not memory, and not `localStorage`. Memory alone meant F5
// destroyed the session and left a blank page with no recovery -- the one
// thing a user does when a page looks wrong is reload it. `sessionStorage` is
// scoped to this tab, survives a reload, and is gone when the tab closes.
//
// The XSS argument for keeping it out of storage entirely is backwards: an XSS
// in THIS page runs in the context that already holds the live token and can
// call the API directly, so it never needs to read storage. The control for
// that is the Content-Security-Policy the server sends, plus never rendering
// any of this content as HTML.

const KEY = 'whetstone.token'

/** The token for this tab, or null when there is none to be had. */
export function readToken(): string | null {
  const fromHash = new URLSearchParams(location.hash.slice(1)).get('t')
  if (fromHash) {
    sessionStorage.setItem(KEY, fromHash)
    // Strip it so a screenshot or a shared address bar does not carry it.
    // Purely cosmetic -- the fragment never reached the network anyway.
    history.replaceState(null, '', location.pathname + location.search)
    return fromHash
  }
  return sessionStorage.getItem(KEY)
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

/**
 * A GET against the API, carrying the token in a custom header.
 *
 * THE HEADER IS THE CSRF CONTROL and it is the only one. A cross-origin
 * `<form>` cannot set a custom header, and a cross-origin `fetch` that tries
 * triggers a preflight the server answers with no CORS headers at all. Note
 * what this does NOT rely on: the absence of CORS headers stops an attacker
 * READING a response, never the request arriving. For writes, the token is the
 * whole defence.
 */
export async function apiGet<T>(path: string, token: string): Promise<T> {
  const response = await fetch(path, {
    headers: { 'X-Whetstone-Token': token },
    // No cookies are involved anywhere in this app; saying so explicitly means
    // a future same-site cookie cannot quietly become ambient authority.
    credentials: 'omit',
    cache: 'no-store',
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body && typeof body.error === 'string') detail = body.error
    } catch {
      // A non-JSON error body is the 403 from the Host guard, which is plain
      // text on purpose. The status carries the meaning.
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}
