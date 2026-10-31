/**
 * Date windows: which calendar span a date selection means, and whether an event falls inside it.
 *
 * Independent of the card: no DOM, no feed, no categories. The card asks two questions and nothing else:
 *
 *   resolveWindow(selection, now, timeZone)  -> { startInclusive, endExclusive } (epoch milliseconds), or null for no selection
 *   eventOverlapsWindow(event, window)       -> boolean
 *
 * A selection is a plain object naming a preset, { preset: "this-week" }. Each preset turns today's calendar date into a span of
 * whole calendar days, and resolveWindow turns those days into instants at local midnight in the given IANA time zone, so a window
 * follows real calendar boundaries (month and year ends, daylight-saving changes) rather than a rolling number of hours. Adding a
 * preset is one more entry in PRESETS; a range a person enters can become one more kind of selection that names its
 * own first and last day and goes through the same day-to-instant step and the same matching.
 */

/** A calendar date: { year, month (1-12), day }. Arithmetic is done on UTC dates, which have no time zone and no daylight saving. */
function addDays(date, days) {
  const moved = new Date(Date.UTC(date.year, date.month - 1, date.day + days));
  return { year: moved.getUTCFullYear(), month: moved.getUTCMonth() + 1, day: moved.getUTCDate() };
}

/** 0 = Monday ... 6 = Sunday. */
function weekday(date) {
  return (new Date(Date.UTC(date.year, date.month - 1, date.day)).getUTCDay() + 6) % 7;
}

const formats = new Map();
function format(timeZone) {
  const key = timeZone || "";
  if (!formats.has(key)) {
    formats.set(key, new Intl.DateTimeFormat("en-US", {
      timeZone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric",
    }));
  }
  return formats.get(key);
}

/** The wall-clock reading of an instant in a time zone. */
function wallClock(instant, timeZone) {
  const parts = {};
  for (const part of format(timeZone).formatToParts(new Date(instant))) parts[part.type] = Number(part.value);
  return parts;
}

/** The calendar date of an instant in a time zone. */
export function calendarDate(instant, timeZone) {
  const { year, month, day } = wallClock(instant, timeZone);
  return { year, month, day };
}

/** The time zone's offset from UTC at an instant, in milliseconds. */
function offsetAt(instant, timeZone) {
  const wall = wallClock(instant, timeZone);
  return Date.UTC(wall.year, wall.month - 1, wall.day, wall.hour, wall.minute, wall.second) - Math.floor(instant / 1000) * 1000;
}

/**
 * The first instant of a calendar date in a time zone: local midnight, or, where the clocks skip midnight, the moment they skip to.
 * Only the offsets in force around that day can place its midnight; of those placements, the earliest that falls on the date is it.
 */
export function startOfDay(date, timeZone) {
  const wall = Date.UTC(date.year, date.month - 1, date.day);
  const offsets = new Set([offsetAt(wall - 36 * 3600000, timeZone), offsetAt(wall, timeZone), offsetAt(wall + 36 * 3600000, timeZone)]);
  const onDate = [...offsets].map((offset) => wall - offset).filter((instant) => {
    const local = calendarDate(instant, timeZone);
    return local.year === date.year && local.month === date.month && local.day === date.day;
  });
  return onDate.length ? Math.min(...onDate) : wall - offsetAt(wall, timeZone);
}

/**
 * The presets: each maps today's calendar date to its first day and the day after its last (whole days, end exclusive).
 *   this-week      Monday through Sunday of the current week, including the days already past
 *   this-weekend   Saturday and Sunday: the coming ones on a weekday, the current ones on Saturday or Sunday
 *   this-month     the first of the current month through its last day, including the days already past
 */
export const PRESETS = [
  {
    id: "this-week", label: "This Week",
    days(today) {
      const monday = addDays(today, -weekday(today));
      return { first: monday, after: addDays(monday, 7) };
    },
  },
  {
    id: "this-weekend", label: "This Weekend",
    days(today) {
      const saturday = addDays(today, 5 - weekday(today));   // Monday +5 ... Friday +1, Saturday 0, Sunday -1
      return { first: saturday, after: addDays(saturday, 2) };
    },
  },
  {
    id: "this-month", label: "This Month",
    days(today) {
      const first = { year: today.year, month: today.month, day: 1 };
      return { first, after: today.month === 12 ? { year: today.year + 1, month: 1, day: 1 } : { year: today.year, month: today.month + 1, day: 1 } };
    },
  },
];
const BY_ID = new Map(PRESETS.map((preset) => [preset.id, preset]));

/** A saved or clicked selection, checked: { preset } for a known preset, else null (no date selection). */
export function normalizeSelection(raw) {
  return raw && typeof raw === "object" && !Array.isArray(raw) && BY_ID.has(raw.preset) ? { preset: raw.preset } : null;
}

/** The instants a selection covers in a time zone, today being the calendar date of `now` there; null when nothing is selected. */
export function resolveWindow(selection, now, timeZone) {
  const preset = normalizeSelection(selection) && BY_ID.get(selection.preset);
  if (!preset) return null;
  const { first, after } = preset.days(calendarDate(now, timeZone));
  return { startInclusive: startOfDay(first, timeZone), endExclusive: startOfDay(after, timeZone) };
}

function instant(value) {
  if (value === undefined || value === null || value === "") return NaN;
  return typeof value === "number" ? value : new Date(value).getTime();
}

/**
 * Does an event fall inside a window? Its [start, end) interval overlaps [startInclusive, endExclusive), so a multi-day event counts
 * on every window it touches; an event without a (later) end is a moment, inside when its start is. An event without a readable start
 * is never inside.
 */
export function eventOverlapsWindow(event, window) {
  if (!event || !window) return false;
  const start = instant(event.start);
  if (Number.isNaN(start)) return false;
  const end = instant(event.end);
  if (!Number.isNaN(end) && end > start) return start < window.endExclusive && end > window.startInclusive;
  return start >= window.startInclusive && start < window.endExclusive;
}
