/**
 * Sign-in, in one file, with no dependencies.
 *
 * ## Why this is hand-rolled and why that is not reckless
 *
 * The dangerous half of OAuth is *verifying* a token, and this file never does
 * it. API Gateway checks the signature, the issuer, the audience and the
 * expiry against Cognito's published keys before a request reaches any code we
 * wrote. What is left here is an exchange: send the browser somewhere, come
 * back with a code, swap it for tokens. A library would wrap that in
 * abstractions the frontend rewrite would then have to unpick.
 *
 * The UI around this is deliberately throwaway -- 4c replaces it. **This file
 * is not**, so the security-relevant parts are done properly:
 *
 * - **PKCE.** A public client cannot keep a secret, so the code alone must not
 *   be enough. The verifier is 32 random bytes from the platform CSPRNG, and
 *   only its SHA-256 goes out in the first redirect; an attacker who steals
 *   the code off the return URL has nothing to exchange it with.
 * - **`state`.** Random per attempt, checked on return. Without it, someone
 *   can hand you a link that completes *their* sign-in in your browser.
 * - **The authorization-code flow, never implicit.** Implicit puts the token
 *   in the URL fragment, which lands in history and in referrers.
 * - **The code is scrubbed from the URL** the moment it is used, so a
 *   screenshot or a shared link carries nothing.
 *
 * ## Where the tokens live, stated rather than buried
 *
 * `localStorage`, which any script running on this origin can read. That is a
 * real weakness and it is *still* a large improvement on what it replaces: the
 * shared passcode also sat in `localStorage` and never expired, so reading it
 * once was permanent access. An ID token is valid for an hour.
 *
 * The refresh token is the uncomfortable one -- thirty days. `sessionStorage`
 * would drop it when the tab closes, at the cost of signing in on every visit.
 * For a single-user application behind an allow-list that trade is not obviously
 * right, so it is written down instead of decided quietly. The genuinely better
 * answer is a backend session cookie, which is a different architecture.
 */

const DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN as string;
const CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID as string;

/** Where Cognito sends the browser back. Must match a callback URL on the
 * app client exactly, including the trailing slash. */
const REDIRECT = `${window.location.origin}/`;

const TOKENS_KEY = "schedule-ai-tokens";
const VERIFIER_KEY = "schedule-ai-pkce-verifier";
const STATE_KEY = "schedule-ai-oauth-state";

export interface Tokens {
  idToken: string;
  refreshToken: string;
  /** Epoch milliseconds. Compared against, never trusted as identity -- the
   * server decides what is expired; this only decides when to refresh. */
  expiresAt: number;
}

function randomString(bytes = 32): string {
  const raw = new Uint8Array(bytes);
  crypto.getRandomValues(raw);
  // base64url: the verifier travels in a URL and in a form body.
  return btoa(String.fromCharCode(...raw))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export function loadTokens(): Tokens | null {
  try {
    const raw = localStorage.getItem(TOKENS_KEY);
    return raw ? (JSON.parse(raw) as Tokens) : null;
  } catch {
    return null;
  }
}

function store(tokens: Tokens) {
  localStorage.setItem(TOKENS_KEY, JSON.stringify(tokens));
}

/**
 * Drop local tokens without touching Cognito or Google. For a session that
 * ended on its own -- expired refresh token, a 401 mid-request -- where
 * redirecting the browser away to an external logout page would be a jarring
 * response to what the user experiences as an ordinary API error.
 */
export function clearSession() {
  localStorage.removeItem(TOKENS_KEY);
  sessionStorage.removeItem(VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
}

/**
 * Clear local tokens and end the Cognito session too, then send the browser
 * to Cognito's `/logout`. Clearing only `localStorage` (the old behaviour)
 * left Cognito's own session alive, so the next `signIn()` silently resumed
 * it -- "sign out" without ever actually being signed out.
 *
 * This does not touch Google's own session cookie -- Google has no logout
 * endpoint a relying party can call on a user's behalf. `prompt=select_account`
 * in `signIn()` is what stops that cookie from being used silently.
 *
 * Only for the user-initiated "Sign out" button. A session that ends on its
 * own uses `clearSession()` instead -- see there for why.
 */
export function signOut() {
  clearSession();
  const params = new URLSearchParams({ client_id: CLIENT_ID, logout_uri: REDIRECT });
  window.location.assign(`${DOMAIN}/logout?${params}`);
}

/** Send the browser to Google, by way of Cognito. */
export async function signIn() {
  const verifier = randomString();
  const state = randomString(16);
  // sessionStorage, not localStorage: both are single-use and scoped to this
  // one attempt in this one tab, and neither should outlive it.
  sessionStorage.setItem(VERIFIER_KEY, verifier);
  sessionStorage.setItem(STATE_KEY, state);

  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: REDIRECT,
    state,
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256",
    // Straight to Google. Cognito's own chooser would show one option.
    identity_provider: "Google",
    // Without this, Google silently reuses whatever session cookie is
    // already in the browser -- no chooser, no consent, just the same
    // account every time, even right after signOut(). Cognito passes this
    // through to Google's own /authorize endpoint unchanged.
    prompt: "select_account",
  });
  window.location.assign(`${DOMAIN}/oauth2/authorize?${params}`);
}

async function exchange(body: Record<string, string>): Promise<Tokens> {
  const response = await fetch(`${DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CLIENT_ID, ...body }),
  });
  if (!response.ok) throw new Error(`token exchange failed: ${response.status}`);

  const data = await response.json();
  const tokens: Tokens = {
    idToken: data.id_token,
    // A refresh grant does not return a new refresh token, so keep the one we
    // have rather than storing undefined and silently signing the user out an
    // hour later.
    refreshToken: data.refresh_token ?? loadTokens()?.refreshToken ?? "",
    // A minute short, so a request is never sent with a token that expires
    // while it is in flight.
    expiresAt: Date.now() + (data.expires_in - 60) * 1000,
  };
  store(tokens);
  return tokens;
}

/** Raised when the sign-in came back refused rather than merely broken. */
export class NotPermitted extends Error {}

/**
 * What Cognito says when the allow-list turned someone away.
 *
 * `gatekeeper/` is a pre-sign-up trigger, so it runs at the moment Cognito
 * would create the user -- *after* Google has authenticated them. Cognito
 * returns the Lambda's exception on the redirect back here, wrapped in its own
 * prefix: `PreSignUp failed with error <our message>.` The prefix is Cognito
 * describing its own plumbing and means nothing to the person reading it.
 *
 * This is the missing half of the bug reported on 2026-08-07: signing in with
 * a second Google account "did not succeed", and the only explanation was
 * whatever Cognito chose to render on its own domain. When it hands the reason
 * back, the app is a far better place to say it.
 */
function describeAuthError(code: string, description: string | null): Error {
  const raw = (description ?? "").replace(/\+/g, " ").trim();
  const unwrapped = raw
    .replace(/^PreSignUp failed with error\s*/i, "")
    .replace(/\.$/, "")
    .trim();

  // The allow-list is the only refusal this app produces on purpose, and it is
  // a different event from a broken sign-in: nothing is wrong, the answer is
  // just no.
  if (/not permitted|not allowed|access denied/i.test(unwrapped)) {
    return new NotPermitted(
      unwrapped || "That account is not on the list for this app.",
    );
  }
  if (code === "access_denied") {
    // The user pressed cancel on Google's own screen.
    return new NotPermitted("Sign-in was cancelled.");
  }
  return new Error(unwrapped || `sign-in failed (${code})`);
}

/**
 * Finish a sign-in if this page load is the return leg. Returns the tokens if
 * it was, null if this is an ordinary visit.
 */
export async function completeSignIn(): Promise<Tokens | null> {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  const failure = params.get("error");

  if (!code && !failure) return null;

  const expected = sessionStorage.getItem(STATE_KEY);
  const verifier = sessionStorage.getItem(VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(VERIFIER_KEY);

  // Scrub before anything else can throw. A code left in the address bar
  // outlives the error that stopped it being used -- and an `error_description`
  // left there is the refusal reappearing on every refresh.
  window.history.replaceState({}, "", window.location.pathname);

  // Checked before `state`, because a refusal is the answer to the question
  // and a state mismatch on a request that already failed is noise.
  if (failure) throw describeAuthError(failure, params.get("error_description"));

  if (!expected || params.get("state") !== expected) {
    throw new Error("sign-in did not match this browser — start again");
  }
  if (!verifier) throw new Error("sign-in was started somewhere else");
  // Unreachable: the only way past the guard above with no code is a `failure`,
  // which already threw. Stated so the type narrows without a cast.
  if (!code) throw new Error("sign-in came back with nothing to exchange");

  return exchange({
    grant_type: "authorization_code",
    code,
    redirect_uri: REDIRECT,
    code_verifier: verifier,
  });
}

/**
 * The ID token to send, refreshed if it is about to expire. Null when there is
 * no usable session, which the caller shows as "signed out" rather than
 * treating as an error.
 */
export async function currentToken(): Promise<string | null> {
  const tokens = loadTokens();
  if (!tokens) return null;
  if (Date.now() < tokens.expiresAt) return tokens.idToken;
  if (!tokens.refreshToken) {
    clearSession();
    return null;
  }

  try {
    const refreshed = await exchange({
      grant_type: "refresh_token",
      refresh_token: tokens.refreshToken,
    });
    return refreshed.idToken;
  } catch {
    // A refresh token that no longer works means the session is over --
    // revoked, expired, or the user signed out elsewhere. Clearing it is what
    // stops every subsequent request retrying a dead credential. Local only:
    // this runs mid-request, not from the "Sign out" button, so it must not
    // redirect the browser away from whatever it was doing.
    clearSession();
    return null;
  }
}
