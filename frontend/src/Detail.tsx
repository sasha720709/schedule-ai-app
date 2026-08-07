/**
 * One watch, opened.
 *
 * The right-hand pane on a desktop and the whole screen on a phone. It is
 * built out of the sentences catalogued in `docs/frontend-sentences.md` --
 * each section is an uppercase label and the answer under it, separated by
 * space rather than by a rule or a box, which is the Broadsheet direction.
 *
 * The two traps recorded in `docs/frontend-strategy.md` §7 apply here more
 * than anywhere:
 *
 * - **The plan card is not a form.** It is the system showing its work: what
 *   it read, from where, what it will cost, and what it is unsure about.
 * - **Statuses are not decoration.** `degraded` and `blocked` in particular
 *   must not read as ordinary failure: one means the page changed, the other
 *   means a shop shut us out, and only the second is actionable.
 */

import { useState } from "react";
import {
  type CostEstimate,
  type MatchedItem,
  monthlyCost,
  type PlanQuestion,
  type ReminderEdit,
  type Staleness,
  type Target,
  type Watch,
} from "./api";
import {
  countdown,
  longMoment,
  nextCheckLine,
  repeatLine,
  since,
  toDateAndTime,
  toWallClock,
} from "./format";

/** A reminder is the kind with a clock instead of a condition. */
const isReminder = (watch: Watch) => Boolean(watch.fire_at);

function kindOf(watch: Watch): string {
  if (isReminder(watch)) return "Reminder";
  if (watch.repeating) return "Vacancy watch";
  return "Watch";
}

export interface DetailProps {
  watch: Watch;
  targets?: Target[];
  cost?: CostEstimate | null;
  nextCheck?: string | null;
  staleness?: Staleness[];
  busy: boolean;
  onConfirm: (interval: number, answers: Record<string, string[]>) => void;
  onEdit: (edit: ReminderEdit) => void;
  onPause: () => void;
  onResume: () => void;
  onDelete: () => void;
  onBack: () => void;
}

export default function Detail(props: DetailProps) {
  const { watch, nextCheck, busy } = props;
  const [editing, setEditing] = useState(false);

  const reminder = isReminder(watch);
  const when = watch.fire_at ?? nextCheck ?? null;
  const fired = Number(watch.trigger_count ?? 0);

  return (
    <div className="detail measure">
      <div className="detail-kickers">
        <span className="label accent">{kindOf(watch)}</span>
        <span className="label">{watch.status}</span>
      </div>

      <h1>{watch.reminder_title || watch.prompt}</h1>

      {/* When it happens, and how far off that is. For a reminder this is the
          whole watch; for a condition watch it is when we next look. */}
      {reminder && watch.fire_at ? (
        <>
          <p className="lead">
            {longMoment(watch.fire_at)}
            {watch.fire_timezone && ` (${watch.fire_timezone})`}
          </p>
          {watch.status === "active" && (
            <p className="countdown">{countdown(watch.fire_at)}</p>
          )}
        </>
      ) : (
        <>
          {watch.check_interval_min != null && watch.status !== "proposed" && (
            <p className="lead">Checks every {watch.check_interval_min} min</p>
          )}
          {/* The sentence whose absence cost a night's watching. Only for an
              active watch: a paused one has no schedule, so a time here would
              describe something that does not exist. */}
          {watch.status === "active" && nextCheck && (
            <p className="countdown">{nextCheckLine(nextCheck)}</p>
          )}
        </>
      )}

      {watch.status === "planning" && (
        <div className="sec">
          <p className="answer">
            Working out what to watch and how often. It takes about twenty
            seconds.
          </p>
          <p className="aside">
            Nothing is scheduled and nothing is being spent until you confirm
            it.
          </p>
        </div>
      )}

      {editing && reminder ? (
        <EditReminder
          watch={watch}
          busy={busy}
          onCancel={() => setEditing(false)}
          onSave={(edit) => {
            setEditing(false);
            props.onEdit(edit);
          }}
        />
      ) : (
        <Sections {...props} reminder={reminder} when={when} fired={fired} />
      )}

      {!editing && watch.status === "proposed" && <PlanCard {...props} />}

      {!editing && watch.status !== "proposed" && watch.status !== "planning" && (
        // `pinned`: what you can DO with this watch stays reachable while the
        // page scrolls past it. A detail page runs long -- note, repeat,
        // condition, targets, the request, history -- and on a phone the
        // buttons were a scroll away from everything that explains them.
        <div className="actions pinned">
          {/* The only editable thing in the product, and the only place this
              button appears. A condition watch is defined by what it reads,
              so changing it means describing it again. */}
          {reminder && (
            <button
              className="btn btn-secondary"
              onClick={() => setEditing(true)}
              disabled={busy}
            >
              {watch.status === "triggered" || watch.status === "expired"
                ? "Set it going again"
                : "Change"}
            </button>
          )}
          {watch.status === "active" && (
            <button className="btn btn-secondary" onClick={props.onPause} disabled={busy}>
              Pause
            </button>
          )}
          {watch.status === "paused" && (
            <button className="btn btn-secondary" onClick={props.onResume} disabled={busy}>
              Resume
            </button>
          )}
          <button className="btn btn-ghost" onClick={props.onDelete} disabled={busy}>
            Delete
          </button>
        </div>
      )}
    </div>
  );
}

/** Everything between the heading and the buttons, for a watch not being
 * edited. Split out only so `Detail` stays readable. */
function Sections({
  watch,
  targets,
  staleness,
  reminder,
  fired,
}: DetailProps & { reminder: boolean; when: string | null; fired: number }) {
  return (
    <>
      {watch.reminder_note && (
        <div className="sec">
          <div className="label">Note</div>
          <p className="said">{watch.reminder_note}</p>
        </div>
      )}

      {reminder && (
        <div className="sec">
          <div className="label">Repeat</div>
          <p className="answer">{repeatLine(watch.repeat, watch.expires_at ?? undefined)}</p>
        </div>
      )}

      {watch.condition && (
        <div className="sec">
          <div className="label">What it watches</div>
          <p className="answer">
            Triggers when {watch.condition.metric} {watch.condition.op}{" "}
            {watch.condition.value} {watch.condition.currency ?? ""}
            {/* Which number the threshold is about, when there is more than
                one shop. Without it the user has to guess whether "10%
                cheaper" means cheaper than Ivory, than Amazon, or than some
                average. */}
            {watch.condition.across === "best" &&
              ` — measured across every shop below, whose ${
                watch.condition.op.startsWith(">") ? "highest" : "cheapest"
              } offer is what this watch calls the price`}
            .
          </p>
          {/* A relative watch shows where its threshold came from, so the
              number can be checked against the page it was read off. The
              source is labelled because a threshold 5% below a live price and
              one 5% below Friday's close are arithmetically identical and are
              not the same promise. */}
          {watch.condition.baseline != null && (
            <p className="aside">
              {watch.condition.relative_change_pct
                ? `${watch.condition.relative_change_pct}% from`
                : "Any move below"}{" "}
              the {watch.condition.baseline}
              {watch.condition.baseline_source === "previous_close"
                ? " read at the previous close"
                : " read live at planning"}
              .
            </p>
          )}
        </div>
      )}

      {/* "Any change" is not a condition on a price, it is a guarantee: a
          stock never reopens at the previous close, so this fires in the first
          seconds of the next session. Measured 2026-08-04 — baseline 306.40,
          first check 306.49, fired. */}
      {watch.status === "active" &&
        watch.condition?.baseline != null &&
        !watch.condition.relative_change_pct && (
          <p className="notice">
            Any move at all triggers this — at the next open that is close to
            certain.
          </p>
        )}

      {!!targets?.length && (
        <div className="sec">
          <div className="label">Where it looks</div>
          {targets.map((t) => {
            const stale = staleness?.find((s) => s.target_id === t.target_id);
            return (
              <div className="target" key={t.target_id}>
                <div className="url">
                  <a href={t.url} target="_blank" rel="noreferrer">
                    {t.url}
                  </a>
                </div>
                {t.last_value != null && (
                  <p className="reading">
                    last read {String(t.last_value)}
                    {t.last_checked_at && (
                      <span className="quiet"> · {since(t.last_checked_at)}</span>
                    )}
                  </p>
                )}
                {!!t.last_items?.length && (
                  <Matches items={t.last_items} base={t.url} />
                )}
                {t.last_error && <p className="notice">{t.last_error}</p>}
                {/* Reports and never acts: a still value is normal for most
                    watches, and acting on it would re-create the false
                    positive that `unavailable` exists to prevent. */}
                {stale?.last_changed_at &&
                  (stale.stale ? (
                    <p className="notice">
                      Unchanged for {stale.unchanged_checks} checks — a whole
                      trading session without a tick. Last moved{" "}
                      {since(stale.last_changed_at)}; the feed may be frozen
                      rather than the price.
                    </p>
                  ) : (
                    <p className="aside">
                      last moved {since(stale.last_changed_at)}
                    </p>
                  ))}
              </div>
            );
          })}
        </div>
      )}

      <div className="sec">
        <div className="label">You asked for it like this</div>
        <p className="answer quiet">{watch.prompt}</p>
      </div>

      <div className="sec">
        <div className="label">History</div>
        <p className="answer">
          {watch.status === "degraded"
            ? "Stopped before it could finish."
            : fired === 0
              ? "Not sent yet."
              : `Sent ${fired} time${fired === 1 ? "" : "s"}${
                  watch.last_triggered_at || watch.triggered_at
                    ? `, most recently ${since(
                        watch.last_triggered_at ?? watch.triggered_at!,
                      )}`
                    : ""
                }.`}
        </p>
        {reminder && (
          <p className="aside">
            A calendar entry is attached to the email, so your own calendar
            does the reminding — with your own alert settings, on your own
            devices.
          </p>
        )}
        {watch.repeating && watch.status === "active" && (
          <p className="aside">
            Keeps running — reports each new match once, never the same one
            twice
            {watch.expires_at &&
              `, and stops on its own around ${new Date(
                watch.expires_at,
              ).toLocaleDateString()}`}
            .
          </p>
        )}
      </div>

      {watch.plan_error && (
        <p className="notice">Planning failed: {watch.plan_error}</p>
      )}

      {/* The one status the user did not ask for and cannot act on except by
          recreating the watch. Says plainly that it now costs nothing. */}
      {watch.status === "degraded" && (
        <p className="notice">
          Stopped: the site changed and an automatic repair did not help
          {watch.degraded_reason ? ` — ${watch.degraded_reason}` : ""}. Checking
          has stopped, so this costs nothing; describe it again to rebuild
          against the new page.
        </p>
      )}

      {watch.status === "expired" && (
        <p className="aside">
          Finished: it ran its full term and stopped. Nothing went wrong — a
          watch that keeps running rather than stopping at its first result is
          given an end date so a forgotten one cannot check for years.
        </p>
      )}
    </>
  );
}

/**
 * Changing a reminder that already exists.
 *
 * Structured fields rather than another sentence to the model (decided
 * 2026-08-07): it is exact, it is instant, it costs nothing, and it cannot
 * misread you. Three fields, because those are the three the owner chose —
 * the title is what the calendar entry is called, and the timezone is a
 * profile setting waiting on a user record.
 *
 * The date and time are split out of the stored moment **in the reader's own
 * locale**, never by slicing the ISO string: the stored value carries the
 * watch's offset, and slicing would show whatever the server wrote rather
 * than what the user's clock says.
 */
function EditReminder({
  watch,
  busy,
  onSave,
  onCancel,
}: {
  watch: Watch;
  busy: boolean;
  onSave: (edit: ReminderEdit) => void;
  onCancel: () => void;
}) {
  const initial = toDateAndTime(watch.fire_at!);
  const [date, setDate] = useState(initial.date);
  const [time, setTime] = useState(initial.time);
  const [repeat, setRepeat] = useState(watch.repeat ?? "once");
  const [note, setNote] = useState(watch.reminder_note ?? "");

  const revived = watch.status === "triggered" || watch.status === "expired";
  const moved = date !== initial.date || time !== initial.time;

  function save() {
    const edit: ReminderEdit = {};
    if (moved) edit.fire_at = toWallClock(date, time);
    if (repeat !== (watch.repeat ?? "once")) edit.repeat = repeat;
    if (note !== (watch.reminder_note ?? "")) edit.reminder_note = note;
    onSave(edit);
  }

  return (
    <div className="sec">
      <div className="label accent">
        {revived ? "Set it going again" : "Change this reminder"}
      </div>

      {/* A reminder that already fired has no schedule left — `at(...)`
          deleted its own on the way out — so a new time is the one thing this
          form cannot do without. Said here rather than as a 409 afterwards. */}
      {revived && (
        <p className="aside">
          This one has already {watch.status === "triggered" ? "fired" : "finished"}.
          Give it a new time and it starts again, keeping its note and its
          history.
        </p>
      )}

      <div className="field" style={{ marginTop: "var(--space-4)" }}>
        <label className="label" htmlFor="edit-date">When</label>
        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
          <input
            id="edit-date"
            className="input"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            disabled={busy}
            style={{ width: "auto", flex: "1 1 10rem" }}
          />
          <input
            className="input"
            type="time"
            value={time}
            onChange={(e) => setTime(e.target.value)}
            disabled={busy}
            aria-label="Time"
            style={{ width: "auto", flex: "0 1 7rem" }}
          />
        </div>
        {/* Named because it is a deployment setting, not the user's own: a
            wrong one has to cost a glance now rather than a reminder at 6am. */}
        {watch.fire_timezone && (
          <p className="aside">{watch.fire_timezone}</p>
        )}
      </div>

      <div className="field">
        <span className="label">Repeat</span>
        <div className="seg" style={{ marginTop: "var(--space-1)" }}>
          {(["once", "daily", "weekly"] as const).map((value) => (
            <label className="seg-opt" key={value}>
              <input
                type="radio"
                name="repeat"
                value={value}
                checked={repeat === value}
                onChange={() => setRepeat(value)}
                disabled={busy}
              />
              {value === "once"
                ? "Just once"
                : value === "daily"
                  ? "Every day"
                  : "Every week"}
            </label>
          ))}
        </div>
        {repeat !== "once" && (watch.repeat ?? "once") === "once" && (
          <p className="aside">
            A repeat runs for 90 days and then stops itself, so a forgotten one
            cannot arrive every evening for years.
          </p>
        )}
      </div>

      <div className="field">
        <label className="label" htmlFor="edit-note">Note</label>
        <textarea
          id="edit-note"
          className="input"
          rows={3}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={busy}
          placeholder="anything worth keeping — a phone number, an address"
        />
        <p className="aside">Carried into the email and the calendar entry.</p>
      </div>

      {/* Pinned on every size, not just on a phone: a form whose Save button
          has scrolled off is a form you cannot tell is finished. */}
      <div className="actions pinned">
        <button
          className="btn btn-primary"
          onClick={save}
          disabled={busy || (revived && !moved)}
        >
          Save changes
        </button>
        <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * The plan, awaiting a yes. The one boxed component this system allows,
 * because it is a genuinely discrete item: nothing is scheduled and nothing
 * is being spent until the button is pressed.
 */
function PlanCard({
  watch,
  targets,
  cost,
  busy,
  onConfirm,
  onDelete,
}: DetailProps) {
  const [interval, setInterval] = useState(watch.check_interval_min ?? 60);
  const [answers, setAnswers] = useState<Record<string, string[]>>({});
  const questions = watch.questions ?? [];
  const reminder = isReminder(watch);

  // Narrowing today's list is an exact set intersection, because each option
  // already carries the ids of the items it covers. Tomorrow's postings cannot
  // be filtered this way -- they do not exist yet -- so the same answers go to
  // the server as ranking preferences instead.
  const allowed = questions.reduce<Set<string> | null>((keep, q) => {
    const chosen = answers[q.id] ?? [];
    if (!chosen.length) return keep;
    const covered = new Set(
      q.options.filter((o) => chosen.includes(o.value)).flatMap((o) => o.items),
    );
    return keep ? new Set([...keep].filter((id) => covered.has(id))) : covered;
  }, null);

  const toggle = (q: PlanQuestion, value: string) =>
    setAnswers((prev) => {
      const chosen = prev[q.id] ?? [];
      return {
        ...prev,
        [q.id]: chosen.includes(value)
          ? chosen.filter((v) => v !== value)
          : [...chosen, value],
      };
    });

  const overBudget = cost ? interval < cost.min_interval_min : false;

  return (
    <div className="sec plan">
      <div className="label accent">The plan</div>
      <div className="card" style={{ marginTop: "8px" }}>
        {/* A shop that was looked at and dropped. Silence here is the same
            failure as the missing email: the user asked about Amazon, Amazon
            is not in the list, and nothing said why. */}
        {!!watch.rejected?.length &&
          watch.rejected.map((r) => (
            <p className="notice" key={r.url} style={{ marginTop: 0 }}>
              Not watching {r.url} — {r.reason}
            </p>
          ))}

        {targets?.map((t) => (
          <div className="target" key={t.target_id}>
            <div className="url">
              <a href={t.url} target="_blank" rel="noreferrer">
                {t.url}
              </a>
              <span className="quiet"> · {t.fetch_method}</span>
            </div>

            {t.verified_raw != null && (
              <p className="reading">
                read {String(t.verified_raw)} just now — this is what will be
                watched
              </p>
            )}

            {/* Asking for a foreign company by its bare ticker returns the US
                depositary receipt: "SAP" is the NYSE ADR in USD, not
                Frankfurt. Only visible in the second before confirming. */}
            {t.instrument_name && (
              <p className="notice">
                {t.instrument_name}
                {t.exchange && ` · ${t.exchange}`}
                {t.currency && ` · ${t.currency}`} — check this is the listing
                you meant
              </p>
            )}

            {/* A count verified at zero is honest and says nothing on its own.
                This is what lets someone judge the filter before paying. */}
            {t.unfiltered_count != null && (
              <p className="aside">
                {t.unfiltered_count} item{t.unfiltered_count === 1 ? "" : "s"}{" "}
                listed on this page today, {String(t.verified_raw ?? 0)} of
                which match
              </p>
            )}

            {!!t.verified_items?.length && (
              <Matches
                items={
                  allowed
                    ? t.verified_items.filter((i) => allowed.has(i.id))
                    : t.verified_items
                }
                base={t.url}
              />
            )}
          </div>
        ))}

        {/* Built from what the search actually returned, so every option has
            real items behind it. A generic form would ask about hours no
            posting mentions and a city every result already shares. */}
        {questions.map((q) => (
          <div className="sec" key={q.id}>
            <p className="answer">{q.question}</p>
            <div className="chips">
              {q.options.map((o) => (
                <button
                  key={o.value}
                  className={(answers[q.id] ?? []).includes(o.value) ? "chip on" : "chip"}
                  onClick={() => toggle(q, o.value)}
                  disabled={busy}
                >
                  {o.label} <span className="n">{o.items.length}</span>
                </button>
              ))}
            </div>
          </div>
        ))}

        {questions.length > 0 && (
          // The two kinds mean genuinely different things by an answer, and
          // saying the wrong one would be a lie about what happens next.
          <p className="aside">
            {watch.repeating
              ? "Answering narrows what is shown here, and tells the watch what to prefer later — it never hides a future posting, only ranks it lower."
              : "Answering narrows what is shown here, and pins what gets watched. Leave it blank and the watch follows the cheapest thing on the page, which is usually an accessory."}
          </p>
        )}

        <div className="rule" />

        {reminder ? (
          <p className="terms">
            One schedule, no checks — a reminder reads nothing, so it costs
            nothing to keep. A calendar entry comes attached to the email.
          </p>
        ) : (
          <>
            <label className="label" htmlFor="plan-interval">
              How often to look
            </label>
            <div
              style={{
                display: "flex",
                gap: "var(--space-2)",
                alignItems: "baseline",
                marginTop: "var(--space-1)",
                flexWrap: "wrap",
              }}
            >
              <input
                id="plan-interval"
                className="input"
                type="number"
                min={1}
                max={1440}
                value={interval}
                onChange={(e) => setInterval(Number(e.target.value))}
                disabled={busy}
                style={{ width: "6rem" }}
              />
              <span>min</span>
              <span className="quiet">
                {cost ? (
                  <>
                    ≈ $
                    {monthlyCost(
                      cost.cost_per_check_usd,
                      interval,
                      targets?.length ?? 1,
                    ).toFixed(2)}
                    /month
                  </>
                ) : (
                  "cost unknown until a target is verified"
                )}
                {watch.check_interval_min !== interval &&
                  ` · planner suggested ${watch.check_interval_min}`}
              </span>
            </div>
            {/* Visible before the button is pressed rather than as a 409
                afterwards. */}
            {overBudget && cost && (
              <p className="notice">
                Below the {cost.min_interval_min} min this budget allows —
                starting it will be refused.
              </p>
            )}
          </>
        )}

        <div className="actions" style={{ marginTop: "22px" }}>
          <button
            className="btn btn-primary"
            onClick={() => onConfirm(interval, answers)}
            disabled={busy}
          >
            {reminder ? "Set the reminder" : "Start watching"}
          </button>
          <button className="btn btn-ghost" onClick={onDelete} disabled={busy}>
            Discard
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * The postings or offers themselves, with links.
 *
 * This replaces the worst thing the product did: a `count` extractor returned
 * an integer, so a vacancy watch said "1" and linked to the search page,
 * leaving the reader to go and find the job — which is most of the work they
 * asked to be spared. Ranked items lead with their score, because that is
 * what gets scanned for; unranked ones look exactly as they did before, since
 * ranking is allowed to fail and must never withhold a result.
 */
function Matches({ items, base }: { items: MatchedItem[]; base: string }) {
  if (!items.length) return null;
  return (
    <ul className="matches">
      {items.map((item) => (
        <li key={item.id}>
          {typeof item.score === "number" && (
            <span className="score">{item.score}/10 </span>
          )}
          {typeof item.price === "number" && (
            <span className="score">
              {item.price.toLocaleString()} {item.currency ?? ""}{" "}
            </span>
          )}
          {item.href ? (
            <a href={new URL(item.href, base).href} target="_blank" rel="noreferrer">
              {item.text || "(untitled)"}
            </a>
          ) : (
            item.text || "(untitled)"
          )}
          {item.why && <span className="quiet"> — {item.why}</span>}
        </li>
      ))}
    </ul>
  );
}
