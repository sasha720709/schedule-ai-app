/**
 * ScheduleAI, minimal UI.
 *
 * Deliberately unstyled beyond legibility -- this exists to prove hosting,
 * real-browser CORS and the deploy pipeline, and to drive the lifecycle API
 * from something other than curl. The designed version is 4c.
 *
 * The one piece of real product thinking here is the proposed-plan card: a
 * watch the Planner has finished thinking about is shown with its targets,
 * its interval and the monthly cost of that interval, and nothing is
 * scheduled until "Start watching" is pressed.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  confirmWatch,
  createWatch,
  deleteWatch,
  getWatch,
  type CostEstimate,
  listWatches,
  monthlyCost,
  setWatchStatus,
  type Staleness,
  type MatchedItem,
  type Target,
  type Watch,
} from "./api";

const PASSCODE_KEY = "schedule-ai-passcode";

export default function App() {
  const [passcode, setPasscode] = useState(
    () => localStorage.getItem(PASSCODE_KEY) ?? "",
  );
  const [watches, setWatches] = useState<Watch[]>([]);
  const [targets, setTargets] = useState<Record<string, Target[]>>({});
  // The per-check rate is the server's to know: it depends on the fetch
  // method and on whether the target carries a compiled extractor.
  const [costs, setCosts] = useState<Record<string, CostEstimate | null>>({});
  // When each active watch next runs. Held here rather than on the Watch row
  // because it is computed, not stored: it is correct for about one interval
  // and a stale copy in the table would be worse than no copy at all.
  const [nextChecks, setNextChecks] = useState<Record<string, string | null>>({});
  // Whether each target has stopped moving. Computed by the server from the
  // current interval, so it cannot be cached alongside the target row.
  const [staleness, setStaleness] = useState<Record<string, Staleness[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const signOut = useCallback(() => {
    localStorage.removeItem(PASSCODE_KEY);
    setPasscode("");
    setWatches([]);
    setTargets({});
    setNextChecks({});
    setLoaded(false);
  }, []);

  const handle = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.unauthorized) {
        // 401 (header absent) and 403 (header wrong) both land here.
        setError("That passcode was rejected.");
        signOut();
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    },
    [signOut],
  );

  const refresh = useCallback(async () => {
    if (!passcode) return;
    try {
      const listed = await listWatches(passcode);
      setWatches(listed.watches);
      setError(null);
      setLoaded(true);

      // Targets carry the per-check detail the list rows do not: last value,
      // the extraction hint, http vs browser. Pull them for anything the user
      // has to act on or read.
      const interesting = listed.watches.filter(
        (w) => w.status === "proposed" || w.status === "triggered",
      );
      const fetched = await Promise.all(
        interesting.map((w) =>
          getWatch(passcode, w.watch_id).then(
            (r) => [w.watch_id, r] as const,
          ),
        ),
      );
      setTargets((prev) => ({
        ...prev,
        ...Object.fromEntries(fetched.map(([id, r]) => [id, r.targets])),
      }));
      setCosts((prev) => ({
        ...prev,
        ...Object.fromEntries(fetched.map(([id, r]) => [id, r.cost])),
      }));
    } catch (err) {
      handle(err);
    }
  }, [passcode, handle]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Planning takes ~20s, so a just-created watch needs polling until it
  // settles. Stop the moment nothing is mid-flight rather than polling
  // forever: every request is billed and the stage is throttled to 10 rps.
  const planning = watches.some((w) => w.status === "planning");
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!planning) return;
    const id = window.setInterval(() => void refreshRef.current(), 3000);
    return () => window.clearInterval(id);
  }, [planning]);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (err) {
      handle(err);
    } finally {
      setBusy(false);
    }
  }

  async function onCreate(event: React.FormEvent) {
    event.preventDefault();
    const text = prompt.trim();
    if (!text) return;
    await act(async () => {
      await createWatch(passcode, text);
      setPrompt("");
    });
  }

  if (!passcode) {
    return (
      <main>
        <h1>ScheduleAI</h1>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const value = new FormData(e.currentTarget).get("passcode");
            if (typeof value === "string" && value.trim()) {
              localStorage.setItem(PASSCODE_KEY, value.trim());
              setPasscode(value.trim());
              setError(null);
            }
          }}
        >
          <label>
            Passcode{" "}
            <input
              name="passcode"
              type="password"
              autoFocus
              autoComplete="off"
            />
          </label>{" "}
          <button type="submit">Enter</button>
        </form>
        {error && <p className="error">{error}</p>}
      </main>
    );
  }

  return (
    <main>
      <header>
        <h1>ScheduleAI</h1>
        <button onClick={signOut}>Forget passcode</button>
      </header>

      <form onSubmit={onCreate}>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={2}
          placeholder="tell me when a Steam Deck OLED drops under $450"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !prompt.trim()}>
          Plan a watch
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      <h2>Watches{planning && <span className="muted"> · planning…</span>}</h2>

      {!loaded && <p className="muted">Loading…</p>}
      {loaded && watches.length === 0 && (
        <p className="muted">Nothing yet. Describe something to watch above.</p>
      )}

      <ul className="watches">
        {watches.map((watch) => (
          <WatchRow
            key={watch.watch_id}
            watch={watch}
            targets={targets[watch.watch_id]}
            cost={costs[watch.watch_id]}
            nextCheck={nextChecks[watch.watch_id]}
            staleness={staleness[watch.watch_id]}
            busy={busy}
            onConfirm={(interval) =>
              void act(async () => {
                const done = await confirmWatch(
                  passcode,
                  watch.watch_id,
                  interval,
                );
                setNextChecks((prev) => ({
                  ...prev,
                  [watch.watch_id]: done.next_check_at,
                }));
              })
            }
            onPause={() =>
              void act(() => setWatchStatus(passcode, watch.watch_id, "paused"))
            }
            onResume={() =>
              void act(async () => {
                const done = await setWatchStatus(
                  passcode,
                  watch.watch_id,
                  "active",
                );
                setNextChecks((prev) => ({
                  ...prev,
                  [watch.watch_id]: done.next_check_at,
                }));
              })
            }
            onDelete={() => void act(() => deleteWatch(passcode, watch.watch_id))}
            onExpand={() =>
              void act(async () => {
                const detail = await getWatch(passcode, watch.watch_id);
                setTargets((prev) => ({
                  ...prev,
                  [watch.watch_id]: detail.targets,
                }));
                setCosts((prev) => ({
                  ...prev,
                  [watch.watch_id]: detail.cost,
                }));
                setNextChecks((prev) => ({
                  ...prev,
                  [watch.watch_id]: detail.next_check_at,
                }));
                setStaleness((prev) => ({
                  ...prev,
                  [watch.watch_id]: detail.staleness,
                }));
              })
            }
          />
        ))}
      </ul>
    </main>
  );
}

/**
 * "next check at 16:00, in 16 hours" -- the sentence whose absence cost a
 * night's watching.
 *
 * A market watch confirmed at 23:33 local time was three minutes past the last
 * slot of its trading-hours window, so it would not run until the New York
 * open sixteen hours later. It was correct, and it was silent, and silence and
 * broken look identical from the outside. The relative half matters more than
 * the clock time: "16:00" alone still reads like something is wrong.
 */
function describeNextCheck(iso: string): string {
  const at = new Date(iso);
  const minutes = Math.round((at.getTime() - Date.now()) / 60000);
  const when = at.toLocaleString(undefined, {
    weekday: minutes > 12 * 60 ? "short" : undefined,
    hour: "2-digit",
    minute: "2-digit",
  });
  if (minutes <= 1) return `next check ${when}, any moment now`;
  if (minutes < 60) return `next check ${when}, in ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `next check ${when}, in ${hours} h`;
  return `next check ${when}, in ${Math.round(hours / 24)} days`;
}

/**
 * The postings themselves, with links.
 *
 * This replaces the worst thing the product did: a `count` extractor returned
 * an integer, so a vacancy watch told the user "1" and linked to the search
 * page, leaving them to go and find the job -- which is most of the work they
 * asked to be spared.
 */
function Matches({ items, base }: { items: MatchedItem[]; base: string }) {
  return (
    <ul className="matches">
      {items.map((item) => (
        <li key={item.id}>
          {/* Ranked items lead with their score, because that is what gets
              scanned for. Unranked ones look exactly as they did before: an
              email or a list may carry both, since ranking is allowed to fail
              and must never withhold a result. */}
          {typeof item.score === "number" && (
            <span className="score">{item.score}/10</span>
          )}
          {item.href ? (
            <a href={new URL(item.href, base).href} target="_blank" rel="noreferrer">
              {item.text || "(untitled)"}
            </a>
          ) : (
            item.text || "(untitled)"
          )}
          {item.why && <span className="muted"> — {item.why}</span>}
        </li>
      ))}
    </ul>
  );
}

/** "3 h ago". Deliberately coarse -- this is context, not a measurement. */
function describeSince(iso: string): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function WatchRow({
  watch,
  targets,
  cost,
  nextCheck,
  staleness,
  busy,
  onConfirm,
  onPause,
  onResume,
  onDelete,
  onExpand,
}: {
  watch: Watch;
  targets?: Target[];
  cost?: CostEstimate | null;
  nextCheck?: string | null;
  staleness?: Staleness[];
  busy: boolean;
  onConfirm: (interval: number) => void;
  onPause: () => void;
  onResume: () => void;
  onDelete: () => void;
  onExpand: () => void;
}) {
  const [interval, setIntervalValue] = useState(watch.check_interval_min ?? 60);

  return (
    <li>
      <div className="row">
        <span className={`status ${watch.status}`}>{watch.status}</span>
        <span className="prompt">{watch.prompt}</span>
      </div>

      {watch.condition && (
        <p className="muted">
          trigger when {watch.condition.metric} {watch.condition.op}{" "}
          {watch.condition.value} {watch.condition.currency ?? ""}
          {/* A relative watch shows where its threshold came from, so the
              number can be checked against the page it was read off. */}
          {watch.condition.baseline != null && (
            <>
              {" "}
              (
              {watch.condition.relative_change_pct
                ? `${watch.condition.relative_change_pct}% from`
                : "any move below"}{" "}
              the {watch.condition.baseline}
              {/* Which reading it came from. The owner accepted a previous
                  close as a baseline, which is exactly why it has to be
                  labelled: the two are identical on screen and are not the
                  same promise. */}
              {watch.condition.baseline_source === "previous_close"
                ? " read at the previous close"
                : " read live at planning"}
              )
            </>
          )}
          {watch.check_interval_min != null && watch.status !== "proposed" && (
            <> · every {watch.check_interval_min} min</>
          )}
        </p>
      )}

      {/* "Any change" is not a condition on a price, it is a guarantee: a
          stock never reopens at the previous close, so this fires in the
          first seconds of the next session. Measured 2026-08-04 -- baseline
          306.40, first check 306.49, fired. Say so rather than quietly
          picking a percentage nobody asked for. */}
      {watch.status === "proposed" &&
        watch.condition?.baseline != null &&
        !watch.condition.relative_change_pct && (
          <p className="muted">
            any move at all triggers this — at the next open that is close to
            certain. Say a size (&ldquo;5% down&rdquo;) if you meant one.
          </p>
        )}

      {/* Only for an active watch: a paused one has no schedule, so a time
          here would describe something that does not exist. */}
      {watch.status === "active" && nextCheck && (
        <p className="muted">{describeNextCheck(nextCheck)}</p>
      )}

      {/* The difference a person needs to know before confirming: this one
          does not stop at the first result, and therefore has an end date. */}
      {watch.repeating && watch.status !== "expired" && (
        <p className="muted">
          keeps running — reports each new match once, never the same one twice
          {watch.trigger_count ? ` · ${watch.trigger_count} reported so far` : ""}
          {watch.expires_at
            ? ` · runs until ${new Date(watch.expires_at).toLocaleDateString()}`
            : ""}
        </p>
      )}

      {watch.status === "expired" && (
        <p className="muted">
          finished: this watch ran its full term and stopped
          {watch.trigger_count
            ? `, after telling you about ${watch.trigger_count} thing${
                watch.trigger_count === 1 ? "" : "s"
              }`
            : ""}
          . Nothing went wrong — a watch that keeps running rather than
          stopping at its first result is given an end date so a forgotten one
          cannot check for years. Describe it again to restart it.
        </p>
      )}

      {watch.plan_error && (
        <p className="error">planning failed: {watch.plan_error}</p>
      )}

      {watch.status === "degraded" && (
        <p className="error">
          stopped: the site changed and automatic repair did not help —{" "}
          {watch.degraded_reason ?? "no reason recorded"}. Checking has
          stopped, so this costs nothing; delete it and describe it again to
          rebuild against the new page.
        </p>
      )}

      {/* The point of plan-then-confirm: nothing is scheduled yet, and the
          cost of the Planner's chosen interval is on screen before it runs. */}
      {watch.status === "proposed" && (
        <div className="plan">
          {targets?.map((t) => (
            <div key={t.target_id} className="target">
              <a href={t.url} target="_blank" rel="noreferrer">
                {t.url}
              </a>
              <span className="muted"> · {t.fetch_method}</span>
              {/* Asking for a foreign company by its bare ticker returns the
                  US depositary receipt. This line is how that becomes
                  visible before confirming instead of never. */}
              {t.instrument_name && (
                <p className="muted">
                  {t.instrument_name}
                  {t.exchange && <> · {t.exchange}</>}
                  {t.currency && <> · {t.currency}</>}
                </p>
              )}
              {t.verified_raw != null && (
                <p>
                  read <strong>{String(t.verified_raw)}</strong> just now —
                  this is what will be watched
                </p>
              )}
              {/* A count verified at zero is honest and says nothing on its
                  own. This is what lets someone judge the filter before
                  paying for a schedule. */}
              {t.unfiltered_count != null && (
                <p className="muted">
                  {t.unfiltered_count} item
                  {t.unfiltered_count === 1 ? "" : "s"} listed on this page
                  today, {String(t.verified_raw ?? 0)} of which match
                </p>
              )}
              {!!t.verified_items?.length && (
                <Matches items={t.verified_items} base={t.url} />
              )}
              <p className="muted">{t.extract_hint}</p>
            </div>
          ))}

          <label>
            check every{" "}
            <input
              type="number"
              min={1}
              max={1440}
              value={interval}
              onChange={(e) => setIntervalValue(Number(e.target.value))}
              disabled={busy}
            />{" "}
            min
          </label>{" "}
          <span className="muted">
            {cost ? (
              <>
                ≈ $
                {monthlyCost(
                  cost.cost_per_check_usd,
                  interval,
                  targets?.length ?? 1,
                ).toFixed(2)}
                /month
                {/* The budget-derived floor, so an interval the server will
                    refuse is visible before the button is pressed rather than
                    as a 409 afterwards. */}
                {interval < cost.min_interval_min && (
                  <> · below the {cost.min_interval_min} min this budget allows</>
                )}
              </>
            ) : (
              <>cost unknown until a target is verified</>
            )}
            {watch.check_interval_min !== interval && (
              <> · planner suggested {watch.check_interval_min}</>
            )}
          </span>

          <div>
            <button onClick={() => onConfirm(interval)} disabled={busy}>
              Start watching
            </button>{" "}
            <button onClick={onDelete} disabled={busy}>
              Discard
            </button>
          </div>
        </div>
      )}

      {targets && watch.status !== "proposed" && (
        <ul className="targets">
          {targets.map((t) => (
            <li key={t.target_id}>
              <span className="muted">{t.url}</span>
              {t.last_value && (
                <>
                  {" "}
                  — last read <strong>{t.last_value}</strong>
                </>
              )}
              {!!t.last_items?.length && (
                <Matches items={t.last_items} base={t.url} />
              )}
              {t.last_note && <p className="muted">{t.last_note}</p>}
              {t.last_error && <p className="error">{t.last_error}</p>}
              {(() => {
                const s = staleness?.find((x) => x.target_id === t.target_id);
                if (!s?.last_changed_at) return null;
                // Stated for every target, flagged only where a trading window
                // makes "should have moved by now" a claim anyone can check:
                // a whole session without a single tick is a frozen feed.
                const moved = describeSince(s.last_changed_at);
                return s.stale ? (
                  <p className="error">
                    unchanged for {s.unchanged_checks} checks — a whole trading
                    session without a tick. Last moved {moved}; the feed may be
                    frozen rather than the price.
                  </p>
                ) : (
                  <p className="muted">last moved {moved}</p>
                );
              })()}
            </li>
          ))}
        </ul>
      )}

      {watch.status !== "proposed" && watch.status !== "planning" && (
        <div>
          {watch.status === "active" && (
            <button onClick={onPause} disabled={busy}>
              Pause
            </button>
          )}
          {watch.status === "paused" && (
            <button onClick={onResume} disabled={busy}>
              Resume
            </button>
          )}{" "}
          {!targets && (
            <button onClick={onExpand} disabled={busy}>
              Show detail
            </button>
          )}{" "}
          <button onClick={onDelete} disabled={busy}>
            Delete
          </button>
        </div>
      )}
    </li>
  );
}
