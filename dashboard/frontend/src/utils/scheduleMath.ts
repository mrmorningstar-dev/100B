/**
 * Utilities and pure math functions for schedule times, presets, and snapping.
 */

export {
  formatTimeIST,
  formatWeekdayIST,
  formatDateIST,
  formatTimeETHint,
  formatTimeET,
  formatWeekdayET,
  formatDateET,
  CHANNEL_TIMEZONE,
  DISPLAY_TIMEZONE,
  TZ_LABEL,
  ET_TIMEZONE,
  etSlotToDate,
  istToETHHMM,
} from './timeFormat';

export function parseHHMM(timeStr: string): number {
  if (!timeStr) return 0;
  const parts = timeStr.trim().split(':');
  const h = parseInt(parts[0], 10) || 0;
  const m = parseInt(parts[1], 10) || 0;
  return Math.min(1439, Math.max(0, h * 60 + m));
}

export function minutesToHHMM(totalMinutes: number): string {
  const clamped = Math.min(1439, Math.max(0, totalMinutes));
  const h = Math.floor(clamped / 60);
  const m = clamped % 60;
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`;
}

export function minutesTo12Hour(totalMinutes: number): string {
  const clamped = Math.min(1439, Math.max(0, totalMinutes));
  const h24 = Math.floor(clamped / 60);
  const m = clamped % 60;
  const period = h24 >= 12 ? 'PM' : 'AM';
  let h12 = h24 % 12;
  if (h12 === 0) h12 = 12;
  return `${h12}:${m.toString().padStart(2, '0')} ${period}`;
}

export function parse12Hour(str: string): number | null {
  if (!str) return null;
  const clean = str.trim().toUpperCase();
  // Check for 12-hour pattern like "6:30 PM", "12:15 AM", "6:30PM", "6 PM", "6:30"
  const match12 = clean.match(/^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?$/);
  if (!match12) return null;

  let h = parseInt(match12[1], 10);
  const m = match12[2] ? parseInt(match12[2], 10) : 0;
  const period = match12[3];

  if (m < 0 || m > 59) return null;

  if (period) {
    if (h < 1 || h > 12) return null;
    if (period === 'AM') {
      if (h === 12) h = 0;
    } else {
      if (h !== 12) h += 12;
    }
  } else {
    // 24-hour style
    if (h < 0 || h > 23) return null;
  }

  return h * 60 + m;
}

export function snapTo15Minutes(minutes: number): number {
  const snapped = Math.round(minutes / 15) * 15;
  // Limit to 23:45 (1425 minutes) so it doesn't wrap to next day
  return Math.min(1425, Math.max(0, snapped));
}

export function clampSlotsCount(count: number): number {
  return Math.min(12, Math.max(1, count));
}

export function sortSlots(slots: string[]): string[] {
  return [...slots].sort((a, b) => parseHHMM(a) - parseHHMM(b));
}

export function findNextAvailableSlot(existingSlots: string[]): string {
  const existingSet = new Set(existingSlots.map(s => parseHHMM(s)));
  
  // Preferred hours to try first (peak YouTube Shorts viewing hours)
  const candidateHours = [18, 19, 20, 21, 17, 16, 15, 14, 12, 11, 10, 22, 13, 9, 8, 23];
  for (const h of candidateHours) {
    for (const m of [0, 30, 15, 45]) {
      const mins = h * 60 + m;
      if (!existingSet.has(mins)) {
        return minutesToHHMM(mins);
      }
    }
  }

  // Fallback linear scan from 00:00 to 23:45
  for (let mins = 0; mins <= 1425; mins += 15) {
    if (!existingSet.has(mins)) {
      return minutesToHHMM(mins);
    }
  }

  return "12:00";
}

export type PresetType = 'evening_heavy' | 'even_spread' | 'morning_and_night';

export function generatePreset(preset: PresetType, count: number): string[] {
  const n = clampSlotsCount(count);
  const result: number[] = [];

  if (preset === 'even_spread') {
    // Spread evenly between 09:00 (540m) and 22:00 (1320m)
    if (n === 1) {
      result.push(18 * 60); // 18:00
    } else {
      const start = 9 * 60; // 09:00
      const end = 22 * 60; // 22:00
      const step = (end - start) / (n - 1);
      for (let i = 0; i < n; i++) {
        result.push(snapTo15Minutes(start + i * step));
      }
    }
  } else if (preset === 'evening_heavy') {
    // Focus in afternoon/evening: 15:00 - 23:00
    const anchorTimes = [
      15 * 60, // 3:00 PM
      17 * 60, // 5:00 PM
      18 * 60 + 30, // 6:30 PM
      19 * 60 + 45, // 7:45 PM
      21 * 60, // 9:00 PM
      22 * 60 + 15, // 10:15 PM
      14 * 60, // 2:00 PM
      16 * 60, // 4:00 PM
      20 * 60 + 30, // 8:30 PM
      23 * 60, // 11:00 PM
      13 * 60, // 1:00 PM
      12 * 60, // 12:00 PM
    ];
    for (let i = 0; i < n; i++) {
      result.push(anchorTimes[i % anchorTimes.length]);
    }
  } else if (preset === 'morning_and_night') {
    // Half morning (8:00 - 11:30), half night (19:00 - 22:30)
    const morningAnchors = [8 * 60, 9 * 60 + 30, 11 * 60, 7 * 60, 10 * 60, 6 * 60 + 30];
    const nightAnchors = [19 * 60, 20 * 60 + 30, 22 * 60, 18 * 60, 21 * 60 + 15, 23 * 60];
    
    let mIdx = 0;
    let nIdx = 0;
    for (let i = 0; i < n; i++) {
      if (i % 2 === 0 && mIdx < morningAnchors.length) {
        result.push(morningAnchors[mIdx++]);
      } else if (nIdx < nightAnchors.length) {
        result.push(nightAnchors[nIdx++]);
      } else {
        result.push(morningAnchors[mIdx++]);
      }
    }
  }

  // Deduplicate and ensure exactly n unique slots
  const uniqueSet = new Set<number>();
  for (const t of result) {
    if (uniqueSet.size < n) uniqueSet.add(snapTo15Minutes(t));
  }

  // Fill in any shortfall
  let candidate = 8 * 60;
  while (uniqueSet.size < n && candidate <= 1425) {
    uniqueSet.add(candidate);
    candidate += 45;
  }

  const sorted = Array.from(uniqueSet).sort((a, b) => a - b).slice(0, n);
  return sorted.map(mins => minutesToHHMM(mins));
}
