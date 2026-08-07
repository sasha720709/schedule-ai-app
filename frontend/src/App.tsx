/**
 * ScheduleAI.
 *
 * Master-detail, soonest first: a list of everything being watched, and one
 * of them opened. The same four moves at both sizes — the list, a watch
 * opened in full, the exchange that creates one, and the plan card that has
 * to be confirmed before anything is scheduled. On a desktop the list and the
 * detail sit side by side; on a phone they are two screens with a back link.
 * **One app, re-laid rather than re-designed** — there is no separate mobile
 * build and no device sniffing, only a width query in `index.css`.
 *
 * The visual direction is Broadsheet (see `docs/frontend-strategy.md`): the
 * serif is the chrome, sections are separated by space rather than by boxes,
 * and the accents are used like spot colour — cyan for what happens next,
 * magenta only for what you should read before going further.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  clearSession,
  completeSignIn,
  currentToken,
  signIn,
  signOut as forgetSession,
} from "./auth";
import {
  ApiError,
  confirmWatch,
  type CostEstimate,
  createWatch,
  deleteWatch,
  editReminder,
  getWatch,
  listWatches,
  type ReminderEdit,
  setWatchStatus,
  type Staleness,
  type Target,
  type Watch,
} from "./api";
import Compose from "./Compose";
import Detail from "./Detail";
import { countdown, isSoon, shortMoment } from "./format";

/** When a watch will next do something, whichever kind it is. */
function nextMomentOf(watch: Watch, nextCheck?: string | null): string | null {
  return watch.fire_at ?? nextCheck ?? null;
}

/**
 * Soonest first, which is the only ordering that answers the question the
 * list is for: what happens next. Anything with no next moment — proposed,
 * triggered, degraded — sorts after everything that has one, because a
 * finished watch is history and an unconfirmed one has not started.
 */
function bySoonest(nextChecks: Record<string, string | null>) {
  return (a: Watch, b: Watch) => {
    const at = nextMomentOf(a, nextChecks[a.watch_id]);
    const bt = nextMomentOf(b, nextChecks[b.watch_id]);
    if (at && bt) return new Date(at).getTime() - new Date(bt).getTime();
    if (at) return -1;
    if (bt) return 1;
    // Both dormant: newest first, so a plan just made is at the top.
    return (b.created_at ?? "").localeCompare(a.created_at ?? "");
  };
}

/** The second line of a list row: what kind of thing this is, in a few words. */
function metaOf(watch: Watch): string {
  if (watch.fire_at) {
    const repeat =
      watch.repeat === "daily"
        ? "every day"
        : watch.repeat === "weekly"
          ? "every week"
          : "once";
    return `${shortMoment(watch.fire_at)} · ${repeat}`;
  }
  if (watch.status !== "active") return watch.status;
  const cadence = watch.check_interval_min
    ? `every ${watch.check_interval_min} min`
    : "scheduled";
  return watch.repeating ? `keeps running · ${cadence}` : cadence;
}

export default function App() {
  // The token is held in state only so React re-renders when it appears or
  // goes. It is never *sent* from here -- `auth()` asks auth.ts for a current
  // one on every call, because the one in state may have expired while the
  // tab sat open.
  const [token, setToken] = useState<string | null>(null);
  const [checkedSession, setCheckedSession] = useState(false);
  const [watches, setWatches] = useState<Watch[]>([]);
  const [targets, setTargets] = useState<Record<string, Target[]>>({});
  const [costs, setCosts] = useState<Record<string, CostEstimate | null>>({});
  // Computed, never stored: right for about one interval, and a stale copy in
  // the table would be worse than no copy at all.
  const [nextChecks, setNextChecks] = useState<Record<string, string | null>>({});
  const [staleness, setStaleness] = useState<Record<string, Staleness[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);
  const [pendingId, setPendingId] = useState<string | null>(null);

  const resetLocalState = useCallback(() => {
    setToken(null);
    setWatches([]);
    setTargets({});
    setNextChecks({});
    setSelectedId(null);
    setLoaded(false);
  }, []);

  // A session that ends on its own (expired refresh token, a 401 mid-request)
  // clears local state only -- redirecting to Cognito's logout page in
  // response to an ordinary API error would be a jarring surprise.
  const endSession = useCallback(() => {
    clearSession();
    resetLocalState();
  }, [resetLocalState]);

  // The "Sign out" button: a real logout, so the next "Sign in" does not
  // silently resume this same session.
  const signOut = useCallback(() => {
    resetLocalState();
    forgetSession();
  }, [resetLocalState]);

  const handle = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.unauthorized) {
        setError("Your session ended. Sign in again.");
        endSession();
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    },
    [endSession],
  );

  const auth = useCallback(async () => {
    const fresh = await currentToken();
    if (!fresh) {
      setToken(null);
      throw new ApiError(401, "Your session ended. Sign in again.");
    }
    return fresh;
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        await completeSignIn();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
      setToken(await currentToken());
      setCheckedSession(true);
    })();
  }, []);

  /** Pull one watch's detail: targets, cost, next check, staleness. */
  const loadDetail = useCallback(
    async (id: string) => {
      const detail = await getWatch(await auth(), id);
      setTargets((prev) => ({ ...prev, [id]: detail.targets }));
      setCosts((prev) => ({ ...prev, [id]: detail.cost }));
      setNextChecks((prev) => ({ ...prev, [id]: detail.next_check_at }));
      setStaleness((prev) => ({ ...prev, [id]: detail.staleness }));
    },
    [auth],
  );

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      const listed = await listWatches(await auth());
      setWatches(listed.watches);
      setError(null);
      setLoaded(true);

      // Detail for anything the user has to act on or is looking at. A
      // reminder carries its own `fire_at` on the list row, so the common
      // case needs no extra call.
      const wanted = listed.watches.filter(
        (w) => w.status === "proposed" || w.watch_id === selectedId,
      );
      await Promise.all(wanted.map((w) => loadDetail(w.watch_id)));
    } catch (err) {
      handle(err);
    }
  }, [token, auth, handle, loadDetail, selectedId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Planning takes ~20s, so a just-created watch needs polling until it
  // settles. Stop the moment nothing is mid-flight: every request is billed
  // and the stage is throttled to 10 rps.
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

  const ordered = useMemo(
    () => [...watches].sort(bySoonest(nextChecks)),
    [watches, nextChecks],
  );

  const selected = watches.find((w) => w.watch_id === selectedId) ?? null;
  const pending = watches.find((w) => w.watch_id === pendingId) ?? undefined;

  // A plan that is ready closes the loop: the overlay shows it, and confirming
  // or discarding is what dismisses the overlay.
  const planReady = pending?.status === "proposed";

  if (!checkedSession) {
    return (
      <div className="app">
        <Masthead />
        <div className="pane-detail">
          <p className="quiet">Checking your session…</p>
        </div>
      </div>
    );
  }

  if (!token) {
    return (
      <div className="app">
        <Masthead />
        <div className="pane-detail">
          <div className="measure detail">
            <h1>Watch something, and be told when it changes.</h1>
            <p className="lead">
              A price, a vacancy, a share, or a date you do not want to miss.
            </p>
            <p className="aside" style={{ marginTop: "var(--space-4)" }}>
              Sign in with the Google account this was set up for. Anyone else
              is turned away before an account is created.
            </p>
            <div className="actions">
              <button className="btn btn-primary" onClick={() => void signIn()}>
                Sign in with Google
              </button>
            </div>
            {error && <p className="notice">{error}</p>}
          </div>
        </div>
      </div>
    );
  }

  function selectWatch(id: string) {
    setSelectedId(id);
    if (!targets[id]) void act(() => loadDetail(id));
  }

  const detailFor = (watch: Watch) => (
    <Detail
      watch={watch}
      targets={targets[watch.watch_id]}
      cost={costs[watch.watch_id]}
      nextCheck={nextChecks[watch.watch_id]}
      staleness={staleness[watch.watch_id]}
      busy={busy}
      onBack={() => setSelectedId(null)}
      onConfirm={(interval, answers) =>
        void act(async () => {
          const done = await confirmWatch(
            await auth(),
            watch.watch_id,
            interval,
            answers,
          );
          setNextChecks((prev) => ({
            ...prev,
            [watch.watch_id]: done.next_check_at,
          }));
          if (watch.watch_id === pendingId) {
            setComposing(false);
            setPendingId(null);
            setSelectedId(watch.watch_id);
          }
        })
      }
      onEdit={(edit: ReminderEdit) =>
        void act(async () => {
          const done = await editReminder(await auth(), watch.watch_id, edit);
          setNextChecks((prev) => ({
            ...prev,
            [watch.watch_id]: done.next_check_at,
          }));
        })
      }
      onPause={() =>
        void act(() =>
          auth().then((t) => setWatchStatus(t, watch.watch_id, "paused")),
        )
      }
      onResume={() =>
        void act(async () => {
          const done = await setWatchStatus(await auth(), watch.watch_id, "active");
          setNextChecks((prev) => ({
            ...prev,
            [watch.watch_id]: done.next_check_at,
          }));
        })
      }
      onDelete={() =>
        void act(async () => {
          await deleteWatch(await auth(), watch.watch_id);
          if (watch.watch_id === selectedId) setSelectedId(null);
          if (watch.watch_id === pendingId) {
            setComposing(false);
            setPendingId(null);
          }
        })
      }
    />
  );

  return (
    <div className="app">
      <Masthead onSignOut={signOut} />

      <div className="split">
        {/* On a phone these two are alternatives; the width query in
            index.css is what turns them from panes into screens. */}
        <div className="pane-list" data-hidden={Boolean(selected)}>
          <div className="compose-dock">
            <button
              className="btn btn-primary btn-block"
              onClick={() => {
                setComposing(true);
                setPendingId(null);
              }}
            >
              New watch
            </button>
          </div>
          <p className="aside quiet" style={{ marginBottom: "var(--space-6)" }}>
            A price, a vacancy, a share, or a reminder.
          </p>

          {error && <p className="notice">{error}</p>}

          <div className="label" style={{ marginBottom: "14px" }}>
            {planning ? "Planning…" : "Soonest first"}
          </div>

          {!loaded && <p className="quiet">Loading…</p>}
          {loaded && !watches.length && (
            <p className="quiet">
              Nothing yet. Describe the first thing you want watched.
            </p>
          )}

          <div className="rows">
            {ordered.map((watch) => {
              const moment = nextMomentOf(watch, nextChecks[watch.watch_id]);
              return (
                <button
                  key={watch.watch_id}
                  onClick={() => selectWatch(watch.watch_id)}
                  aria-current={watch.watch_id === selectedId}
                >
                  <div className="row-head">
                    <span className="row-title">
                      {watch.reminder_title || watch.prompt}
                    </span>
                    {moment && (
                      <span
                        className={`row-when${isSoon(moment) ? " soon" : ""}`}
                      >
                        {countdown(moment)}
                      </span>
                    )}
                  </div>
                  <div className="row-meta">{metaOf(watch)}</div>
                </button>
              );
            })}
          </div>
        </div>

        <div className="pane-detail" data-hidden={!selected}>
          {selected ? (
            <>
              <button
                className="btn btn-ghost back-link label"
                onClick={() => setSelectedId(null)}
                style={{ marginBottom: "var(--space-3)", paddingLeft: 0 }}
              >
                ← All watches
              </button>
              {detailFor(selected)}
            </>
          ) : (
            <div className="measure">
              <p className="quiet">
                {watches.length
                  ? "Pick something on the left to see what it knows."
                  : ""}
              </p>
            </div>
          )}
        </div>
      </div>

      {composing && (
        <Compose
          pending={pending}
          busy={busy}
          error={error}
          onClose={() => {
            setComposing(false);
            setPendingId(null);
          }}
          onSend={(prompt) =>
            void act(async () => {
              const made = await createWatch(await auth(), prompt);
              setPendingId(made.watch_id);
            })
          }
        >
          {planReady && pending ? detailFor(pending) : null}
        </Compose>
      )}
    </div>
  );
}

/** The thick-thin rule pair: the one place this system prints a rule, as
 * front-page furniture rather than as a divider between sections. */
function Masthead({ onSignOut }: { onSignOut?: () => void }) {
  const today = new Date().toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
  return (
    <div className="masthead">
      <div className="thick" />
      <div className="line">
        <span className="label">ScheduleAI</span>
        <nav>
          <span className="label">{today}</span>
          {onSignOut && (
            <button className="btn btn-ghost label" onClick={onSignOut}>
              Sign out
            </button>
          )}
        </nav>
      </div>
      <div className="thin" />
    </div>
  );
}
