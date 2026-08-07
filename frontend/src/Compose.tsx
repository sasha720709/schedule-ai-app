/**
 * Describing something to watch.
 *
 * Laid out as a transcript because that is what the exchange *is* — you say
 * a sentence, the system says what it worked out, and you confirm. It is
 * deliberately **not** a chat: there is one turn each way, the reply is not
 * generated conversationally, and nothing here keeps a conversation. Real
 * multi-turn chat is sub-phase 4d and does not exist, so this must not look
 * like it does — an interface that invites a follow-up the API cannot answer
 * is worse than one that does not invite it.
 *
 * What the user sees while the Planner runs is the honest part: about twenty
 * seconds, nothing scheduled, nothing spent.
 */

import { useEffect, useRef, useState } from "react";
import type { Watch } from "./api";

export interface ComposeProps {
  /** The watch this overlay is waiting on, once one exists. */
  pending?: Watch;
  busy: boolean;
  error: string | null;
  onSend: (prompt: string) => void;
  onClose: () => void;
  /** Rendered under the transcript once the plan is ready — the same card the
   * detail pane shows, passed in rather than duplicated. */
  children?: React.ReactNode;
}

export default function Compose({
  pending,
  busy,
  error,
  onSend,
  onClose,
  children,
}: ComposeProps) {
  const [draft, setDraft] = useState("");
  const [said, setSaid] = useState<string | null>(null);
  const foot = useRef<HTMLDivElement>(null);

  // Keep the newest turn in view as the plan arrives.
  useEffect(() => {
    foot.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [pending?.status, said]);

  function send() {
    const text = draft.trim();
    if (!text) return;
    setSaid(text);
    setDraft("");
    onSend(text);
  }

  const planning = pending?.status === "planning" || (busy && said && !pending);
  const failed = pending?.status === "failed";

  return (
    <div className="overlay">
      <div className="masthead">
        <div className="thick" />
        <div className="line">
          <span className="label">New watch</span>
          <button className="btn btn-ghost label" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="thin" />
      </div>

      <div className="transcript">
        <div className="measure">
          {!said && (
            <div className="turn">
              <div className="label accent">ScheduleAI</div>
              <p className="text">
                Say what you want watched, or when you want reminding. A price,
                a vacancy, a share, a date — the same sentence you would say to
                a person.
              </p>
            </div>
          )}

          {said && (
            <div className="turn">
              <div className="label accent">You</div>
              <p className="said">{said}</p>
            </div>
          )}

          {planning && (
            <div className="turn">
              <div className="label accent">ScheduleAI</div>
              <p className="text">
                Working out what to watch and how often. It takes about twenty
                seconds.
              </p>
              <p className="aside quiet">
                Nothing is scheduled and nothing is being spent until you
                confirm it.
              </p>
            </div>
          )}

          {failed && (
            <div className="turn">
              <div className="label accent">ScheduleAI</div>
              <p className="notice">
                {pending?.plan_error ??
                  "That could not be turned into a watch."}{" "}
                Try describing it a different way.
              </p>
            </div>
          )}

          {error && (
            <div className="turn">
              <p className="notice">{error}</p>
            </div>
          )}

          {children}

          <div ref={foot} />
        </div>
      </div>

      <div className="composer">
        <div className="measure">
          <textarea
            className="input"
            rows={2}
            placeholder="remind me every day at 9pm to learn English"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends, shift+Enter breaks the line. The composer is one
              // sentence long in almost every real use.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            disabled={busy || Boolean(said)}
          />
          <button
            className="btn btn-primary"
            onClick={send}
            disabled={busy || !draft.trim()}
            style={{ padding: "12px 22px" }}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
