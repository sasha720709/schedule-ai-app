/**
 * Client for the watch lifecycle API.
 *
 * The passcode travels in an Authorization header on every request -- there
 * is no session, no cookie and no refresh. Note that API Gateway answers 401
 * when the header is absent and 403 when it is present but wrong, so both
 * have to be treated as "the passcode is bad".
 */

const BASE = import.meta.env.VITE_API_BASE as string;

export type Status =
  | "planning"
  | "proposed"
  | "active"
  | "paused"
  | "triggered"
  | "failed"
  // 8d: the watch could no longer read its target, repair did not help, and
  // checking was stopped so it bills nothing. Not an error status of ours --
  // the page changed underneath the extractor.
  | "degraded"
  // A repeating watch that ran out its term. Terminal, and NOT a fault --
  // it is the guard that stops a forgotten vacancy watch checking for years.
  | "expired";

export interface Watch {
  watch_id: string;
  prompt: string;
  status: Status;
  user_id: string;
  created_at: string;
  check_interval_min?: number;
  condition?: {
    metric: string;
    op: string;
    value: number;
    currency: string | null;
    // Present on relative watches ("goes down from the current"): the value
    // that was actually read at plan time, which the threshold was computed
    // from. Shown so the number on screen can be checked against reality.
    baseline?: number;
    relative_change_pct?: number;
    /** When that baseline was read, and whether the market was open at the
     * time. A threshold 5% below a live price and one 5% below Friday's close
     * are arithmetically identical and are not the same promise. */
    baseline_at?: string;
    baseline_source?: "live" | "previous_close";
    /** "best" when the watch has several shops and one best reading: the
     * threshold is measured from the best offer across all of them, not from
     * whichever shop's page happened to load first. Absent for a single
     * target and for `==`/`!=`, where "cheapest" means nothing. */
    across?: "best";
  };
  planned_at?: string;
  confirmed_at?: string;
  /** Does firing once finish the job? False for a price crossing a threshold,
   * true for a vacancy -- a job search is a stream, not an event. */
  repeating?: boolean;
  questions?: PlanQuestion[];
  /** A time-triggered watch: no targets, no condition, and the schedule
   * firing IS the event. `fire_at` is the local wall-clock moment and
   * `fire_timezone` is the zone it is read in -- shown together, because
   * there is no user profile yet and the zone is a deployment setting. */
  fire_at?: string;
  fire_timezone?: string;
  reminder_title?: string;
  reminder_note?: string;
  /** Does the reminder come back? Asked on the plan card when the request did
   * not say, because guessing "once" for something meant to repeat is a
   * reminder that silently never comes again. */
  repeat?: "once" | "daily" | "weekly";
  /** Places that were looked at and not kept. A watch that quietly became two
   * shops instead of three tells the user nothing, and "Amazon prices in USD"
   * is information they can act on. */
  rejected?: { url: string; reason: string }[];
  /** Only a repeating watch has one: it is the only thing here that does not
   * stop by itself. */
  expires_at?: string | null;
  last_triggered_at?: string;
  trigger_count?: number;
  triggered_at?: string;
  plan_error?: string;
  degraded_at?: string;
  degraded_reason?: string;
  condition_baseline?: never; // baseline lives inside condition, see below
}

/**
 * What the server says a check costs. Produced by `shared/cost.py`, which is
 * the single definition of that in this project -- see the note on
 * `monthlyCost` below for why the frontend must not have a second one.
 */
export interface CostEstimate {
  interval_min: number;
  targets: number;
  fetch_method: string;
  cost_per_check_usd: number;
  estimated_monthly_usd: number;
  monthly_budget_usd: number;
  min_interval_min: number;
  within_budget: boolean;
}

/** A question the plan card asks, built from what the search actually
 * returned. Each option carries the ids of the items it covers, so narrowing
 * the list is an exact set operation rather than re-matching text. */
export interface PlanQuestion {
  id: string;
  question: string;
  options: { value: string; label: string; items: string[] }[];
}

export interface MatchedItem {
  id: string;
  text: string;
  href: string;
  /** Set only on items a model has judged against the original request --
   * which happens per notification, not per check. Absent when ranking was
   * skipped, over budget, or failed: it must never block a notification. */
  score?: number;
  why?: string;
  /** Products only: what this offer costs, and in which currency. Prices from
   * different shops are never compared as one number. */
  price?: number;
  currency?: string;
  in_stock?: boolean;
}

export interface Target {
  target_id: string;
  watch_id: string;
  url: string;
  extract_hint: string;
  fetch_method: "http" | "browser";
  // Tier 0 writes real numbers where the model path wrote strings.
  last_value?: string | number | null;
  last_checked_at?: string;
  last_note?: string;
  last_error?: string;
  last_status?: string;
  // Phase 8b: what the compiled extractor read at plan time, verbatim. This
  // is the difference between "I intend to read the price" and "I read
  // $333.43 just now" on the plan card.
  verified_raw?: string | number;
  verified_at?: string;
  /** What a `count` matched, rather than merely how many. This is what turns
   * "1" into a job you can click. */
  verified_items?: MatchedItem[];
  last_items?: MatchedItem[];
  /** How many items the list holds ignoring the text filter. "47 listed here
   * today, 3 of which match" is the only honest thing to say about a count
   * that verified at zero. */
  unfiltered_count?: number;
  /** Quotes only: which instrument the ticker actually resolved to. A bare
   * ticker for a foreign company returns the US depositary receipt -- a
   * different security, in dollars -- and there was no way to notice. */
  instrument_name?: string;
  exchange?: string;
  currency?: string;
}

/** Thrown for any non-2xx. `unauthorized` means the passcode needs re-entering. */
export class ApiError extends Error {
  status: number;
  unauthorized: boolean;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.unauthorized = status === 401 || status === 403;
  }
}

async function request<T>(
  passcode: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      Authorization: passcode,
      ...init?.headers,
    },
  });

  if (!response.ok) {
    // An authorizer rejection has no JSON body of our making, so don't
    // assume one is there.
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body?.error) detail = body.error;
    } catch {
      /* no body, keep the status */
    }
    throw new ApiError(response.status, detail);
  }

  return response.json() as Promise<T>;
}

export const listWatches = (passcode: string) =>
  request<{ watches: Watch[] }>(passcode, "/watches");

/**
 * Whether a target has stopped moving, and whether that means anything.
 *
 * Reported, never acted on. A value sitting still is the normal case for most
 * watches -- a shop price waiting weeks for a drop, a vacancy count that is
 * zero until the day it is not -- so `stale` exists only where a trading
 * window defines what "should have moved by now" means.
 */
export interface Staleness {
  target_id: string;
  last_changed_at: string | null;
  unchanged_checks: number;
  /** Checks in one full trading session, or null for a continuous schedule. */
  checks_per_session: number | null;
  stale: boolean;
}

export const getWatch = (passcode: string, id: string) =>
  request<{
    watch: Watch;
    targets: Target[];
    cost: CostEstimate | null;
    staleness: Staleness[];
    /** When the schedule next runs, ISO-8601 UTC. Null when the watch is not
     * active, or when the server cannot say -- never a guess. */
    next_check_at: string | null;
  }>(passcode, `/watches/${id}`);

export const createWatch = (passcode: string, prompt: string) =>
  request<{ watch_id: string; status: Status }>(passcode, "/watches", {
    method: "POST",
    body: JSON.stringify({ request: prompt }),
  });

export const confirmWatch = (
  passcode: string,
  id: string,
  checkIntervalMin: number,
  /** Optional throughout. Confirming without answering behaves exactly as it
   * did before questions existed. */
  answers?: Record<string, string[]>,
) =>
  request<{
    watch_id: string;
    /** May differ from what was asked: a windowed watch runs on a cron grid,
     * so the server rounds up to a cadence it can actually express. */
    check_interval_min: number;
    next_check_at: string | null;
    repeating: boolean;
    expires_at: string | null;
  }>(
    passcode,
    `/watches/${id}/confirm`,
    {
      method: "POST",
      body: JSON.stringify({
        check_interval_min: checkIntervalMin,
        ...(answers && Object.keys(answers).length ? { answers } : {}),
      }),
    },
  );

export const setWatchStatus = (
  passcode: string,
  id: string,
  status: "paused" | "active",
) =>
  request<{ watch: Watch; next_check_at: string | null }>(passcode, `/watches/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });

export const deleteWatch = (passcode: string, id: string) =>
  request<{ deleted: boolean }>(passcode, `/watches/${id}`, {
    method: "DELETE",
  });

/**
 * Monthly cost at a chosen interval, using the server's own per-check rate.
 *
 * This function used to carry its own constant -- `checksPerMonth * 0.0057` --
 * a second copy of a number that `shared/cost.py` calls the single definition
 * of what a check costs. It then went stale in the worst possible direction:
 * $0.0057 was the price of a check when every tick paid for a Haiku call, and
 * Phase 8b cut that to $0.0000041 for an HTTP target. The plan card was
 * quoting **$300 a month for a watch that costs eighteen cents**, which is not
 * a rounding error but a reason not to create the watch at all.
 *
 * So the rate is no longer here. `cost_per_check_usd` comes from the API,
 * which computes it from the real fetch method and whether the target carries
 * a compiled extractor -- neither of which the browser can know. Only the
 * arithmetic for a not-yet-saved interval stays client-side, because the input
 * has to respond as it is typed.
 */
export function monthlyCost(
  costPerCheckUsd: number,
  intervalMin: number,
  targets = 1,
): number {
  if (!Number.isFinite(intervalMin) || intervalMin <= 0) return 0;
  const checksPerMonth = (60 / intervalMin) * 24 * 30;
  return checksPerMonth * costPerCheckUsd * targets;
}
