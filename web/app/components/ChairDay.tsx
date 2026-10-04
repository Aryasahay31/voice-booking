"use client";

import { useEffect, useMemo, useState } from "react";
import {
  CLOSES_AT,
  OPENS_AT,
  clock,
  dayLabel,
  getCalendar,
  minutesOf,
  nextOpenDays,
  type CalendarEntry,
} from "@/lib/api";

// Other parts of the page (the voice call, in step 2d) announce new bookings with this event.
export const CALENDAR_UPDATED_EVENT = "glow:calendar-updated";

const DAY_MINUTES = CLOSES_AT - OPENS_AT;
const HOURS = Array.from({ length: DAY_MINUTES / 60 + 1 }, (_, i) => OPENS_AT + i * 60);
const REFRESH_MS = 30_000;
// Tall enough that even a 15-minute appointment has room for one line of text.
const TIMELINE_HEIGHT = "42rem";

/** "today", "tomorrow", or "Wednesday, 7 October" for the heading. */
function headingDay(day: string): string {
  const label = dayLabel(day, "long");
  return label === "Today" || label === "Tomorrow" ? label.toLowerCase() : label;
}

// The salon has one chair, so the day is one column: each appointment is a block sized
// by how long it takes. Customer names never reach the website (/calendar omits them).
export default function ChairDay() {
  const days = useMemo(() => nextOpenDays(5), []);
  const [day, setDay] = useState(days[0]);
  const [entries, setEntries] = useState<CalendarEntry[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    // "active" ignores responses that arrive after the visitor has switched days, so a slow
    // reply for Monday can never overwrite Tuesday's schedule.
    let active = true;
    const refresh = () => {
      getCalendar(day)
        .then((data) => {
          if (active) {
            setEntries(data);
            setFailed(false);
          }
        })
        .catch(() => {
          if (active) setFailed(true);
        });
    };

    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    const onUpdate = (e: Event) => {
      const changedDay = (e as CustomEvent<{ day?: string }>).detail?.day;
      if (!changedDay || changedDay === day) refresh();
    };
    window.addEventListener("focus", refresh);
    window.addEventListener(CALENDAR_UPDATED_EVENT, onUpdate);
    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("focus", refresh);
      window.removeEventListener(CALENDAR_UPDATED_EVENT, onUpdate);
    };
  }, [day]);

  const booked = entries?.length ?? 0;

  return (
    <section aria-labelledby="chair-heading">
      <h2 id="chair-heading" className="font-display text-2xl font-semibold">
        The chair, {headingDay(day)}
      </h2>
      <p className="mt-1 text-muted">
        One chair, one appointment at a time. Shaded blocks are taken.
      </p>

      <div role="group" aria-label="Choose a day" className="mt-4 flex flex-wrap gap-2">
        {days.map((d) => (
          <button
            key={d}
            type="button"
            onClick={() => {
              if (d !== day) {
                setEntries(null); // show "Loading…" while the new day's schedule arrives
                setDay(d);
              }
            }}
            aria-pressed={d === day}
            className={`rounded-full border px-3 py-1.5 text-sm transition-colors ${
              d === day
                ? "border-ink bg-ink text-mist"
                : "border-line bg-white hover:border-ink"
            }`}
          >
            {dayLabel(d)}
          </button>
        ))}
      </div>

      {failed && (
        <p className="mt-4 text-muted">
          The schedule didn&apos;t load. It will try again on its own in a few seconds.
        </p>
      )}

      <div className="mt-5 flex gap-3" aria-busy={entries === null}>
        {/* hour labels */}
        <div className="relative w-14 shrink-0 text-right text-sm text-muted" style={{ height: TIMELINE_HEIGHT }}>
          {HOURS.map((m) => (
            <span
              key={m}
              className="absolute right-0 -translate-y-1/2"
              style={{ top: `${((m - OPENS_AT) / DAY_MINUTES) * 100}%` }}
            >
              {clock(m)}
            </span>
          ))}
        </div>

        {/* the chair's day */}
        <div
          className="relative flex-1 rounded-xl border-2 border-line bg-white"
          style={{ height: TIMELINE_HEIGHT }}
        >
          {HOURS.slice(1, -1).map((m) => (
            <div
              key={m}
              aria-hidden
              className="absolute inset-x-0 border-t border-line"
              style={{ top: `${((m - OPENS_AT) / DAY_MINUTES) * 100}%` }}
            />
          ))}

          {entries?.map((e) => {
            const start = minutesOf(e.start_time);
            const end = minutesOf(e.end_time);
            const short = end - start < 30; // e.g. a 15-minute beard trim: compact, one line
            return (
              <div
                key={e.id}
                className={`absolute inset-x-2 overflow-hidden rounded-lg bg-rose px-3 ${
                  short ? "flex items-center gap-1.5 py-0 text-xs leading-none" : "py-1 text-sm"
                }`}
                style={{
                  // a 2px gap on each side keeps back-to-back appointments visibly separate
                  top: `calc(${((start - OPENS_AT) / DAY_MINUTES) * 100}% + 2px)`,
                  height: `calc(${((end - start) / DAY_MINUTES) * 100}% - 4px)`,
                }}
              >
                <span className="font-semibold">{e.service}</span>{" "}
                <span className="text-ink/75">
                  {clock(start)} to {clock(end)}
                </span>
              </div>
            );
          })}

          {entries !== null && booked === 0 && (
            <p className="absolute inset-0 grid place-items-center px-6 text-center text-muted">
              Nothing booked yet. Every slot is open.
            </p>
          )}
        </div>
      </div>

      <p className="mt-6 text-sm text-muted" aria-live="polite">
        {entries === null
          ? "Loading the schedule…"
          : `${booked} ${booked === 1 ? "appointment" : "appointments"} booked`}
      </p>
    </section>
  );
}