/**
 * Time formatting utilities enforcing Asia/Kolkata (IST) display time.
 *
 * ARCHITECTURE PRINCIPLE:
 * Scheduling is in ET (US audience). The dashboard displays IST.
 * - The backend scheduler, database, peak_hours, and YouTube upload publishAt anchors
 *   remain strictly in America/New_York (ET) / UTC.
 * - All user-facing times across the dashboard are DISPLAYED and ENTERED in Asia/Kolkata
 *   (IST, UTC+5:30), accurately accounting for US Daylight Saving Time (EDT vs EST)
 *   by resolving slots against real calendar dates via ECMAScript's standard Intl database.
 */

export const CHANNEL_TIMEZONE = 'Asia/Kolkata';
export const DISPLAY_TIMEZONE = 'Asia/Kolkata';
export const TZ_LABEL = 'IST';
export const ET_TIMEZONE = 'America/New_York';

export interface FormatTimeOptions {
  omitZeroMinutes?: boolean;  // If true: 22:00 -> "10 PM IST", 22:30 -> "10:30 PM IST"
  includeLabel?: boolean;     // If true: appends "IST"
  referenceDate?: string | Date | null; // ET calendar date (YYYY-MM-DD or Date) for slot conversion
  showNextDay?: boolean;      // If true and slot falls on next calendar day in IST, appends nextDaySuffix
  nextDaySuffix?: string;     // Suffix when slot crosses into next calendar day in IST (defaults to " (+1 day)")
}

/**
 * Safely parse date or timestamp input into a Date object.
 * Date-only strings ("YYYY-MM-DD") are anchored at 12:00:00 UTC to prevent calendar day shifts
 * across both America/New_York and Asia/Kolkata time zones.
 */
export function parseDateSafe(input: string | Date | null | undefined): Date | null {
  if (!input) return null;
  if (input instanceof Date) return isNaN(input.getTime()) ? null : input;
  if (typeof input !== 'string') return null;

  const trimmed = input.trim();
  if (!trimmed) return null;

  const dateOnlyMatch = trimmed.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (dateOnlyMatch) {
    const dt = new Date(`${trimmed}T12:00:00Z`);
    return isNaN(dt.getTime()) ? null : dt;
  }

  const dt = new Date(trimmed);
  return isNaN(dt.getTime()) ? null : dt;
}

/**
 * Return the ET equivalent string (e.g. "12 PM ET", "2 PM ET", "6:30 PM ET")
 * for use as small secondary helper text next to IST times.
 */
export function formatTimeETHint(
  input: string | Date | number | null | undefined,
  options: { omitZeroMinutes?: boolean } = {}
): string {
  const { omitZeroMinutes = true } = options;
  if (input === null || input === undefined || input === '') return '';

  let h = 12;
  let m = 0;
  if (typeof input === 'number') {
    const clamped = Math.min(1439, Math.max(0, input));
    h = Math.floor(clamped / 60);
    m = clamped % 60;
  } else if (typeof input === 'string') {
    const trimmed = input.trim();
    const hhmmMatch = trimmed.match(/^(\d{1,2}):(\d{2})$/);
    if (hhmmMatch) {
      h = parseInt(hhmmMatch[1], 10);
      m = parseInt(hhmmMatch[2], 10);
    } else {
      const dt = parseDateSafe(trimmed);
      if (!dt) return '';
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: ET_TIMEZONE,
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
      }).formatToParts(dt);
      let hourStr = '12';
      let minStr = '00';
      let period = 'PM';
      for (const p of parts) {
        if (p.type === 'hour') hourStr = p.value;
        if (p.type === 'minute') minStr = p.value;
        if (p.type === 'dayPeriod') period = p.value;
      }
      const mDisplay = omitZeroMinutes && minStr === '00' ? '' : `:${minStr}`;
      return `${hourStr}${mDisplay} ${period} ET`;
    }
  } else if (input instanceof Date) {
    if (isNaN(input.getTime())) return '';
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TIMEZONE,
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }).formatToParts(input);
    let hourStr = '12';
    let minStr = '00';
    let period = 'PM';
    for (const p of parts) {
      if (p.type === 'hour') hourStr = p.value;
      if (p.type === 'minute') minStr = p.value;
      if (p.type === 'dayPeriod') period = p.value;
    }
    const mDisplay = omitZeroMinutes && minStr === '00' ? '' : `:${minStr}`;
    return `${hourStr}${mDisplay} ${period} ET`;
  }

  const period = h >= 12 ? 'PM' : 'AM';
  let h12 = h % 12;
  if (h12 === 0) h12 = 12;
  const mStr = omitZeroMinutes && m === 0 ? '' : `:${m.toString().padStart(2, '0')}`;
  return `${h12}${mStr} ${period} ET`;
}

/**
 * Convert an ET slot time (HH:MM or minutes-from-midnight) on a given ET reference date
 * into the exact Date object (UTC instant), properly handling US EDT/EST daylight saving.
 */
export function etSlotToDate(
  refDate?: string | Date | null,
  hhmm?: string | number
): { dt: Date; baseDate: string } {
  let h = 12;
  let m = 0;
  if (typeof hhmm === 'number') {
    const clamped = Math.min(1439, Math.max(0, hhmm));
    h = Math.floor(clamped / 60);
    m = clamped % 60;
  } else if (typeof hhmm === 'string') {
    const parts = hhmm.trim().split(':');
    h = parseInt(parts[0], 10) || 0;
    m = parseInt(parts[1], 10) || 0;
  }

  let baseDate = typeof refDate === 'string' ? refDate.trim() : null;
  if (!baseDate && refDate instanceof Date && !isNaN(refDate.getTime())) {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TIMEZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(refDate);
    const y = parts.find((p) => p.type === 'year')?.value || '2026';
    const mo = parts.find((p) => p.type === 'month')?.value || '01';
    const d = parts.find((p) => p.type === 'day')?.value || '01';
    baseDate = `${y}-${mo}-${d}`;
  }

  if (!baseDate || !/^\d{4}-\d{2}-\d{2}$/.test(baseDate)) {
    const nowParts = new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TIMEZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date());
    const y = nowParts.find((p) => p.type === 'year')?.value || '2026';
    const mo = nowParts.find((p) => p.type === 'month')?.value || '01';
    const d = nowParts.find((p) => p.type === 'day')?.value || '01';
    baseDate = `${y}-${mo}-${d}`;
  }

  const [year, month, day] = baseDate.split('-').map(Number);

  // Iteratively converge on the UTC timestamp whose America/New_York representation matches year-month-day h:m
  let guess = new Date(Date.UTC(year, month - 1, day, h, m, 0));
  for (let i = 0; i < 3; i++) {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TIMEZONE,
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      second: 'numeric',
      hour12: false,
    }).formatToParts(guess);

    const gY = Number(parts.find((p) => p.type === 'year')?.value || year);
    const gM = Number(parts.find((p) => p.type === 'month')?.value || month);
    const gD = Number(parts.find((p) => p.type === 'day')?.value || day);
    let gH = Number(parts.find((p) => p.type === 'hour')?.value || 0);
    if (gH === 24) gH = 0;
    const gMin = Number(parts.find((p) => p.type === 'minute')?.value || 0);

    const diffMs = Date.UTC(year, month - 1, day, h, m, 0) - Date.UTC(gY, gM - 1, gD, gH, gMin, 0);
    if (diffMs === 0) break;
    guess = new Date(guess.getTime() + diffMs);
  }

  return { dt: guess, baseDate };
}

/**
 * Convert a user-entered IST time (HH:MM or minutes from midnight) on a given ET reference date
 * back into the ET slot time HH:MM format expected by the backend scheduler.
 */
export function istToETHHMM(
  refDate?: string | Date | null,
  istHHMM?: string | number
): string {
  let istH = 12;
  let istM = 0;
  if (typeof istHHMM === 'number') {
    const clamped = Math.min(1439, Math.max(0, istHHMM));
    istH = Math.floor(clamped / 60);
    istM = clamped % 60;
  } else if (typeof istHHMM === 'string') {
    const parts = istHHMM.trim().split(':');
    istH = parseInt(parts[0], 10) || 0;
    istM = parseInt(parts[1], 10) || 0;
  }

  let baseDate = typeof refDate === 'string' ? refDate.trim() : null;
  if (!baseDate && refDate instanceof Date && !isNaN(refDate.getTime())) {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TIMEZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(refDate);
    const y = parts.find((p) => p.type === 'year')?.value || '2026';
    const mo = parts.find((p) => p.type === 'month')?.value || '01';
    const d = parts.find((p) => p.type === 'day')?.value || '01';
    baseDate = `${y}-${mo}-${d}`;
  }

  if (!baseDate || !/^\d{4}-\d{2}-\d{2}$/.test(baseDate)) {
    const nowParts = new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TIMEZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date());
    const y = nowParts.find((p) => p.type === 'year')?.value || '2026';
    const mo = nowParts.find((p) => p.type === 'month')?.value || '01';
    const d = nowParts.find((p) => p.type === 'day')?.value || '01';
    baseDate = `${y}-${mo}-${d}`;
  }

  const [year, month, day] = baseDate.split('-').map(Number);

  function testCandidate(istDayOffset: number) {
    let guess = new Date(Date.UTC(year, month - 1, day + istDayOffset, istH, istM, 0));
    for (let i = 0; i < 3; i++) {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: DISPLAY_TIMEZONE,
        year: 'numeric',
        month: 'numeric',
        day: 'numeric',
        hour: 'numeric',
        minute: 'numeric',
        second: 'numeric',
        hour12: false,
      }).formatToParts(guess);

      const gY = Number(parts.find((p) => p.type === 'year')?.value || year);
      const gM = Number(parts.find((p) => p.type === 'month')?.value || month);
      const gD = Number(parts.find((p) => p.type === 'day')?.value || day);
      let gH = Number(parts.find((p) => p.type === 'hour')?.value || 0);
      if (gH === 24) gH = 0;
      const gMin = Number(parts.find((p) => p.type === 'minute')?.value || 0);

      const diffMs =
        Date.UTC(year, month - 1, day + istDayOffset, istH, istM, 0) -
        Date.UTC(gY, gM - 1, gD, gH, gMin, 0);
      if (diffMs === 0) break;
      guess = new Date(guess.getTime() + diffMs);
    }

    const etParts = new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TIMEZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).formatToParts(guess);

    const ey = etParts.find((p) => p.type === 'year')?.value;
    const em = etParts.find((p) => p.type === 'month')?.value;
    const ed = etParts.find((p) => p.type === 'day')?.value;
    let eh = etParts.find((p) => p.type === 'hour')?.value || '00';
    if (eh === '24') eh = '00';
    const emin = etParts.find((p) => p.type === 'minute')?.value || '00';
    const etDate = `${ey}-${em}-${ed}`;
    return { etDate, etHHMM: `${eh}:${emin}` };
  }

  const c0 = testCandidate(0);
  if (c0.etDate === baseDate) return c0.etHHMM;
  const c1 = testCandidate(1);
  if (c1.etDate === baseDate) return c1.etHHMM;
  return c0.etHHMM;
}

/**
 * Format any Date, ISO string, "HH:MM", or minutes-from-midnight into IST formatted time.
 * For plain "HH:MM" and minute numbers, converts ET scheduler slot time to IST using the reference date.
 * Never drops non-zero minutes (:30, :15, :45 are always displayed).
 */
export function formatTimeIST(
  input: string | Date | number | null | undefined,
  options: FormatTimeOptions = {}
): string {
  const {
    omitZeroMinutes = true,
    includeLabel = true,
    referenceDate,
    showNextDay = false,
    nextDaySuffix = ' (+1 day)',
  } = options;

  if (input === null || input === undefined || input === '') {
    return `—${includeLabel ? ` ${TZ_LABEL}` : ''}`;
  }

  let dt: Date;
  let sourceEtDate: string | null = null;

  // 1. Minutes from midnight (number) -> ET slot time
  if (typeof input === 'number') {
    const res = etSlotToDate(referenceDate, input);
    dt = res.dt;
    sourceEtDate = res.baseDate;
  }
  // 2. Simple "HH:MM" string -> ET slot time
  else if (typeof input === 'string' && /^(\d{1,2}):(\d{2})$/.test(input.trim())) {
    const res = etSlotToDate(referenceDate, input.trim());
    dt = res.dt;
    sourceEtDate = res.baseDate;
  }
  // 3. Date / ISO string
  else if (typeof input === 'string') {
    const trimmed = input.trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
      const parsed = parseDateSafe(trimmed);
      if (!parsed) return `${input}${includeLabel ? ` ${TZ_LABEL}` : ''}`;
      dt = parsed;
    } else {
      const parsed = new Date(trimmed);
      if (isNaN(parsed.getTime())) {
        return `${input}${includeLabel ? ` ${TZ_LABEL}` : ''}`;
      }
      dt = parsed;

      // Extract ET calendar date from this instant
      try {
        const parts = new Intl.DateTimeFormat('en-US', {
          timeZone: ET_TIMEZONE,
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
        }).formatToParts(dt);
        const y = parts.find((p) => p.type === 'year')?.value;
        const mo = parts.find((p) => p.type === 'month')?.value;
        const d = parts.find((p) => p.type === 'day')?.value;
        if (y && mo && d) sourceEtDate = `${y}-${mo}-${d}`;
      } catch {
        // ignore
      }
    }
  } else {
    dt = input;
  }

  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: DISPLAY_TIMEZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }).formatToParts(dt);

    let hour = '12';
    let minute = '00';
    let dayPeriod = 'PM';
    let istY = '';
    let istMo = '';
    let istD = '';

    for (const part of parts) {
      if (part.type === 'hour') hour = part.value;
      if (part.type === 'minute') minute = part.value;
      if (part.type === 'dayPeriod') dayPeriod = part.value;
      if (part.type === 'year') istY = part.value;
      if (part.type === 'month') istMo = part.value;
      if (part.type === 'day') istD = part.value;
    }

    const mStr = (omitZeroMinutes && minute === '00') ? '' : `:${minute}`;
    const labelStr = includeLabel ? ` ${TZ_LABEL}` : '';

    let nextDayStr = '';
    if (showNextDay && sourceEtDate && istY && istMo && istD) {
      const istDate = `${istY}-${istMo}-${istD}`;
      if (istDate !== sourceEtDate) {
        nextDayStr = nextDaySuffix;
      }
    }

    return `${hour}${mStr} ${dayPeriod}${labelStr}${nextDayStr}`;
  } catch {
    return `${input}${includeLabel ? ` ${TZ_LABEL}` : ''}`;
  }
}

/**
 * Format weekday in IST. E.g. "Sun", "Mon", "Tue"
 */
export function formatWeekdayIST(input: string | Date | null | undefined): string {
  if (!input) return '';
  try {
    const dt = parseDateSafe(input);
    if (!dt) return '';
    return new Intl.DateTimeFormat('en-US', {
      timeZone: DISPLAY_TIMEZONE,
      weekday: 'short',
    }).format(dt);
  } catch {
    return '';
  }
}

/**
 * Format full date in IST. E.g. "Tuesday, November 17" or "Tue, Nov 17"
 */
export function formatDateIST(
  input: string | Date | null | undefined,
  options: Intl.DateTimeFormatOptions = { weekday: 'short', month: 'short', day: 'numeric' }
): string {
  if (!input) return '';
  try {
    const dt = parseDateSafe(input);
    if (!dt) return String(input);

    if (options.weekday && options.day && !options.month && !options.year) {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: DISPLAY_TIMEZONE,
        ...options,
      }).formatToParts(dt);
      let w = '';
      let d = '';
      for (const p of parts) {
        if (p.type === 'weekday') w = p.value;
        if (p.type === 'day') d = p.value;
      }
      if (w && d) return `${w} ${d}`;
    }

    return new Intl.DateTimeFormat('en-US', {
      timeZone: DISPLAY_TIMEZONE,
      ...options,
    }).format(dt);
  } catch {
    return String(input);
  }
}

// Backward-compatible aliases for smooth transition across modules
export const formatTimeET = formatTimeIST;
export const formatDateET = formatDateIST;
export const formatWeekdayET = formatWeekdayIST;
