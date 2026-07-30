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
  listWatches,
  monthlyCost,
  setWatchStatus,
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
  const [error, setError] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const signOut = useCallback(() => {
    localStorage.removeItem(PASSCODE_KEY);
    setPasscode("");
    setWatches([]);
    setTargets({});
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
            (r) => [w.watch_id, r.targets] as const,
          ),
        ),
      );
      setTargets((prev) => ({ ...prev, ...Object.fromEntries(fetched) }));
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
            busy={busy}
            onConfirm={(interval) =>
              void act(() => confirmWatch(passcode, watch.watch_id, interval))
            }
            onPause={() =>
              void act(() => setWatchStatus(passcode, watch.watch_id, "paused"))
            }
            onResume={() =>
              void act(() => setWatchStatus(passcode, watch.watch_id, "active"))
            }
            onDelete={() => void act(() => deleteWatch(passcode, watch.watch_id))}
            onExpand={() =>
              void act(async () => {
                const detail = await getWatch(passcode, watch.watch_id);
                setTargets((prev) => ({
                  ...prev,
                  [watch.watch_id]: detail.targets,
                }));
              })
            }
          />
        ))}
      </ul>
    </main>
  );
}

function WatchRow({
  watch,
  targets,
  busy,
  onConfirm,
  onPause,
  onResume,
  onDelete,
  onExpand,
}: {
  watch: Watch;
  targets?: Target[];
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
          {watch.check_interval_min != null && watch.status !== "proposed" && (
            <> · every {watch.check_interval_min} min</>
          )}
        </p>
      )}

      {watch.plan_error && (
        <p className="error">planning failed: {watch.plan_error}</p>
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
            ≈ ${monthlyCost(interval, targets?.length ?? 1).toFixed(2)}/month
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
              {t.last_note && <p className="muted">{t.last_note}</p>}
              {t.last_error && <p className="error">{t.last_error}</p>}
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
