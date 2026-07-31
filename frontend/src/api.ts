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
  | "failed";

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
  };
  planned_at?: string;
  confirmed_at?: string;
  triggered_at?: string;
  plan_error?: string;
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

export interface Target {
  target_id: string;
  watch_id: string;
  url: string;
  extract_hint: string;
  fetch_method: "http" | "browser";
  last_value?: string | null;
  last_checked_at?: string;
  last_note?: string;
  last_error?: string;
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

export const getWatch = (passcode: string, id: string) =>
  request<{ watch: Watch; targets: Target[]; cost: CostEstimate | null }>(
    passcode,
    `/watches/${id}`,
  );

export const createWatch = (passcode: string, prompt: string) =>
  request<{ watch_id: string; status: Status }>(passcode, "/watches", {
    method: "POST",
    body: JSON.stringify({ request: prompt }),
  });

export const confirmWatch = (
  passcode: string,
  id: string,
  checkIntervalMin: number,
) =>
  request<{ watch_id: string; check_interval_min: number }>(
    passcode,
    `/watches/${id}/confirm`,
    {
      method: "POST",
      body: JSON.stringify({ check_interval_min: checkIntervalMin }),
    },
  );

export const setWatchStatus = (
  passcode: string,
  id: string,
  status: "paused" | "active",
) =>
  request<{ watch: Watch }>(passcode, `/watches/${id}`, {
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
