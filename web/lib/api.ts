// Talks to the Glow Studio backend's PUBLIC endpoints only (no API key, no customer data).
// NEXT_PUBLIC_ values are baked into the browser code at build time, so never put a secret here.
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export const SALON_TIME_ZONE = "Asia/Kolkata";
export const OPENS_AT = 10 * 60; // minutes after midnight (10:00)
export const CLOSES_AT = 19 * 60; // 19:00
const CLOSED_WEEKDAY = 0; // Sunday

export type Service = {
  id: number;
  name: string;
  duration_min: number;
  price: number;
};

export type CalendarEntry = {
  id: number;
  service: string;
  start_time: string; // "YYYY-MM-DDTHH:MM" in salon time
  end_time: string;
};

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json() as Promise<T>;
}

export const getServices = () => getJson<Service[]>("/services");
export const getCalendar = (day: string) =>
  getJson<CalendarEntry[]>(`/calendar?day=${encodeURIComponent(day)}`);

// ---------- dates, always in the salon's time zone ----------

/** Today's date in the salon, as "YYYY-MM-DD" (en-CA formats dates that way). */
export function salonToday(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: SALON_TIME_ZONE }).format(new Date());
}

/** Parse "YYYY-MM-DD" as a calendar date (UTC midnight, so no time-zone drift). */
function asDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

const iso = (d: Date) => d.toISOString().slice(0, 10);

/** The next `count` days the salon is open, starting today. */
export function nextOpenDays(count: number): string[] {
  const days: string[] = [];
  const d = asDate(salonToday());
  while (days.length < count) {
    if (d.getUTCDay() !== CLOSED_WEEKDAY) days.push(iso(d));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return days;
}

export function dayLabel(day: string, style: "short" | "long" = "short"): string {
  const today = salonToday();
  if (day === today) return "Today";
  const tomorrow = asDate(today);
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  if (day === iso(tomorrow)) return "Tomorrow";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "UTC",
    weekday: style === "long" ? "long" : "short",
    day: "numeric",
    month: style === "long" ? "long" : "short",
  }).format(asDate(day));
}

// ---------- formatting ----------

/** "14:30" -> minutes after midnight */
export function minutesOf(dateTime: string): number {
  const [h, m] = dateTime.slice(11, 16).split(":").map(Number);
  return h * 60 + m;
}

/** minutes after midnight -> "2:30 pm" */
export function clock(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  const suffix = h >= 12 ? "pm" : "am";
  const h12 = h % 12 || 12;
  return m === 0 ? `${h12} ${suffix}` : `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
}

export function duration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} hr ${m} min` : `${h} hr`;
}

export const rupees = (amount: number) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(amount);