/**
 * Turning what the server knows into what a person reads.
 *
 * These moved out of `App.tsx` when the interface became master-detail: the
 * same countdown now appears in a list row, in a detail heading and on a plan
 * card, and three copies would have drifted. Everything here is presentation
 * of a fact the API already computed -- nothing decides anything.
 *
 * The relative half of every phrase matters more than the clock time. "16:00"
 * alone still reads like something is wrong; "16:00, in 16 h" is the sentence
 * whose absence cost a night's watching on 2026-08-04.
 */

/** Minutes from now until an ISO moment. Negative once it has passed. */
export function minutesUntil(iso: string): number {
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return NaN;
  return Math.round((at - Date.now()) / 60000);
}

/** "in 3 h", "in 25 days", "any moment now". The right-hand figure in a row. */
export function countdown(iso: string): string {
  const minutes = minutesUntil(iso);
  if (Number.isNaN(minutes)) return "";
  if (minutes <= 0) return "due";
  if (minutes < 2) return "any moment";
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `in ${hours} h`;
  return `in ${Math.round(hours / 24)} days`;
}

/** Whether to print a countdown in the accent: it is the next thing to happen. */
export function isSoon(iso: string): boolean {
  const minutes = minutesUntil(iso);
  return !Number.isNaN(minutes) && minutes >= 0 && minutes <= 12 * 60;
}

/** "Today at 17:30", "Fri 8 Aug at 09:00", "1 September 2026 at 09:00". */
export function longMoment(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const now = new Date();
  const sameDay = at.toDateString() === now.toDateString();
  const time = at.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (sameDay) return `Today at ${time}`;

  const soon = at.getTime() - now.getTime() < 7 * 24 * 3600 * 1000;
  const day = at.toLocaleDateString(undefined, {
    weekday: soon ? "long" : undefined,
    day: "numeric",
    month: "long",
    // A year only when it is not this one -- printing it always is how a
    // date stops being read.
    year: at.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
  return `${day} at ${time}`;
}

/** "17:30 today", "1 Sep, 09:00" -- the compact second line of a list row. */
export function shortMoment(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const now = new Date();
  const time = at.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (at.toDateString() === now.toDateString()) return `${time} today`;
  const day = at.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  return `${day}, ${time}`;
}

/** "3 h ago". Deliberately coarse -- this is context, not a measurement. */
export function since(iso: string): string {
  const minutes = -minutesUntil(iso);
  if (Number.isNaN(minutes)) return "at an unknown time";
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/** "next check 16:00, in 16 h" -- the sentence a silent night paid for. */
export function nextCheckLine(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const minutes = minutesUntil(iso);
  const when = at.toLocaleString(undefined, {
    weekday: minutes > 12 * 60 ? "short" : undefined,
    hour: "2-digit",
    minute: "2-digit",
  });
  return `next check ${when}, ${countdown(iso)}`;
}

/** What the date input and the time input need, from an ISO moment.
 *
 * Split in the reader's own locale rather than by slicing the string, because
 * the stored value carries the *watch's* offset (`+03:00`) and slicing would
 * hand the user whatever the server wrote rather than what their clock says.
 */
export function toDateAndTime(iso: string): { date: string; time: string } {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return { date: "", time: "" };
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`,
    time: `${pad(at.getHours())}:${pad(at.getMinutes())}`,
  };
}

/** The inverse: what the API wants. No offset and no Z -- the zone is a
 * separate field on the watch, and sending one would apply it twice. */
export function toWallClock(date: string, time: string): string {
  return `${date}T${time.length === 5 ? `${time}:00` : time}`;
}

/** How a repeat reads in a sentence. */
export function repeatLine(repeat: string | undefined, expiresAt?: string): string {
  if (repeat === "daily" || repeat === "weekly") {
    const cadence =
      repeat === "daily" ? "Every day at this time." : "Every week on this day.";
    if (!expiresAt) return cadence;
    return `${cadence} It stops on its own around ${new Date(
      expiresAt,
    ).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })}.`;
  }
  return "Once. It finishes after it fires.";
}
