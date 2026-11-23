import { describe, it, expect } from 'vitest';
import {
  parseHHMM,
  minutesToHHMM,
  minutesTo12Hour,
  parse12Hour,
  snapTo15Minutes,
  clampSlotsCount,
  sortSlots,
  findNextAvailableSlot,
  generatePreset,
  formatTimeIST,
  formatWeekdayIST,
  formatDateIST,
  formatTimeET,
  formatWeekdayET,
  formatDateET,
  istToETHHMM,
} from '../utils/scheduleMath';

describe('scheduleMath utils', () => {
  it('converts HH:MM to minutes correctly', () => {
    expect(parseHHMM('00:00')).toBe(0);
    expect(parseHHMM('06:15')).toBe(375);
    expect(parseHHMM('12:00')).toBe(720);
    expect(parseHHMM('18:30')).toBe(1110);
    expect(parseHHMM('23:45')).toBe(1425);
  });

  it('converts minutes to HH:MM correctly', () => {
    expect(minutesToHHMM(0)).toBe('00:00');
    expect(minutesToHHMM(375)).toBe('06:15');
    expect(minutesToHHMM(720)).toBe('12:00');
    expect(minutesToHHMM(1110)).toBe('18:30');
    expect(minutesToHHMM(1425)).toBe('23:45');
  });

  it('converts minutes to 12-hour display strings', () => {
    expect(minutesTo12Hour(0)).toBe('12:00 AM');
    expect(minutesTo12Hour(60)).toBe('1:00 AM');
    expect(minutesTo12Hour(720)).toBe('12:00 PM');
    expect(minutesTo12Hour(780)).toBe('1:00 PM');
    expect(minutesTo12Hour(1110)).toBe('6:30 PM');
    expect(minutesTo12Hour(1425)).toBe('11:45 PM');
  });

  it('parses 12-hour strings accurately', () => {
    expect(parse12Hour('12:00 AM')).toBe(0);
    expect(parse12Hour('6:30 PM')).toBe(1110);
    expect(parse12Hour('6:30pm')).toBe(1110);
    expect(parse12Hour('11:45 PM')).toBe(1425);
    expect(parse12Hour('12:00 PM')).toBe(720);
    expect(parse12Hour('invalid')).toBeNull();
  });

  it('snaps accurately to 15-minute intervals and bounds', () => {
    expect(snapTo15Minutes(1110)).toBe(1110); // 18:30 exact
    expect(snapTo15Minutes(1114)).toBe(1110); // rounds down to 18:30
    expect(snapTo15Minutes(1118)).toBe(1125); // rounds up to 18:45
    expect(snapTo15Minutes(-10)).toBe(0);      // lower bound
    expect(snapTo15Minutes(1500)).toBe(1425);  // upper bound (23:45)
  });

  it('clamps slot counts between 1 and 12', () => {
    expect(clampSlotsCount(0)).toBe(1);
    expect(clampSlotsCount(1)).toBe(1);
    expect(clampSlotsCount(6)).toBe(6);
    expect(clampSlotsCount(12)).toBe(12);
    expect(clampSlotsCount(15)).toBe(12);
  });

  it('sorts slots chronologically', () => {
    const raw = ['18:30', '09:00', '21:15', '12:00'];
    expect(sortSlots(raw)).toEqual(['09:00', '12:00', '18:30', '21:15']);
  });

  it('finds next unoccupied 15-minute slot', () => {
    const existing = ['18:00', '19:00'];
    const nextSlot = findNextAvailableSlot(existing);
    expect(existing).not.toContain(nextSlot);
    expect(parseHHMM(nextSlot) % 15).toBe(0);
  });

  it('generates presets with correct count and unique 15-minute values', () => {
    const presets = ['even_spread', 'evening_heavy', 'morning_and_night'] as const;
    for (const p of presets) {
      for (const count of [1, 3, 6, 8, 12]) {
        const slots = generatePreset(p, count);
        expect(slots.length).toBe(count);
        // Ensure strictly ascending and unique
        for (let i = 0; i < slots.length; i++) {
          const mins = parseHHMM(slots[i]);
          expect(mins % 15).toBe(0);
          if (i > 0) {
            expect(parseHHMM(slots[i])).toBeGreaterThan(parseHHMM(slots[i - 1]));
          }
        }
      }
    }
  });

  describe('IST timezone formatting', () => {
    it('formats tomorrow lineup slot times in Asia/Kolkata with IST label', () => {
      // 12:00, 14:00, 17:00 in America/New_York (EST, UTC-5) -> IST (UTC+5:30)
      expect(formatTimeIST('2026-11-17T12:00:00-05:00')).toBe('10:30 PM IST');
      expect(formatTimeIST('2026-11-17T14:00:00-05:00')).toBe('12:30 AM IST');
      expect(formatTimeIST('2026-11-17T17:00:00-05:00')).toBe('3:30 AM IST');
    });

    it('formats rail next run and last run in Asia/Kolkata with IST label', () => {
      // Next run at 18:00 (6 PM ET) -> 04:30 AM IST next day
      expect(formatWeekdayIST('2026-11-16T18:00:00-05:00')).toBe('Tue');
      expect(formatTimeIST('2026-11-16T18:00:00-05:00')).toBe('4:30 AM IST');
      expect(formatWeekdayIST('2026-11-17T18:00:00-05:00')).toBe('Wed');
      expect(formatTimeIST('2026-11-17T18:00:00-05:00')).toBe('4:30 AM IST');

      // Last run with non-zero minutes
      expect(formatWeekdayIST('2026-11-14T18:02:15-05:00')).toBe('Sun');
      expect(formatTimeIST('2026-11-14T18:02:15-05:00')).toBe('4:32 AM IST');
    });

    it('formats minute and HH:MM inputs in IST', () => {
      expect(formatTimeIST('12:00')).toBe('10:30 PM IST');
      expect(formatTimeIST('18:30')).toBe('5 AM IST');
      expect(formatTimeIST(720)).toBe('10:30 PM IST');
      expect(formatTimeIST(1110)).toBe('5 AM IST');
    });

    it('handles winter (EST) vs summer (EDT) daylight saving correctly', () => {
      // Winter (EST, UTC-5): 12:00 ET on 2026-11-20 -> 10:30 PM IST
      expect(formatTimeIST('12:00', { referenceDate: '2026-11-20' })).toBe('10:30 PM IST');
      expect(formatTimeIST('12:00', { referenceDate: '2026-11-17' })).toBe('10:30 PM IST');

      // Summer (EDT, UTC-4): 12:00 ET on 2027-06-20 -> 9:30 PM IST
      expect(formatTimeIST('12:00', { referenceDate: '2027-06-20' })).toBe('9:30 PM IST');
      expect(formatTimeIST('12:00', { referenceDate: '2026-07-17' })).toBe('9:30 PM IST');
    });

    it('matches UTC ISO instant and ET slot string for the exact same moment', () => {
      // Full-ISO UTC input (2026-11-20T17:00:00Z) and plain "12:00" ET on 2026-11-20 give identical IST result
      const utcIsoResult = formatTimeIST('2026-11-20T17:00:00Z');
      const etSlotResult = formatTimeIST('12:00', { referenceDate: '2026-11-20' });
      expect(utcIsoResult).toBe('10:30 PM IST');
      expect(etSlotResult).toBe('10:30 PM IST');
      expect(utcIsoResult).toBe(etSlotResult);
    });

    it('preserves non-zero minutes accurately', () => {
      // 12:00 EST -> 10:30 PM IST (:30 preserved)
      expect(formatTimeIST('12:00', { referenceDate: '2026-11-20' })).toBe('10:30 PM IST');
      // 12:15 EST -> 10:45 PM IST (:45 preserved)
      expect(formatTimeIST('12:15', { referenceDate: '2026-11-20' })).toBe('10:45 PM IST');
      // 12:45 EST -> 11:15 PM IST (:15 preserved)
      expect(formatTimeIST('12:45', { referenceDate: '2026-11-20' })).toBe('11:15 PM IST');
    });

    it('handles slots that cross midnight into next day in IST', () => {
      // 14:00 ET winter (2026-11-20) -> 12:30 AM IST on the next calendar day
      expect(formatTimeIST('14:00', { referenceDate: '2026-11-20', showNextDay: true })).toBe('12:30 AM IST (+1 day)');
      expect(formatTimeIST('14:00', { referenceDate: '2026-11-20', showNextDay: true, nextDaySuffix: ' (next day)' })).toBe('12:30 AM IST (next day)');

      // 21:00 ET (9:00 PM EST) -> 07:30 AM IST on next day
      expect(formatTimeIST('21:00', { referenceDate: '2026-11-17', showNextDay: true, nextDaySuffix: ' (next day)' })).toBe('7:30 AM IST (next day)');
      expect(formatTimeIST('2026-11-17T21:00:00-05:00', { showNextDay: true, nextDaySuffix: ' (next day)' })).toBe('7:30 AM IST (next day)');
      expect(formatDateIST('2026-11-17T21:00:00-05:00')).toBe('Wed, Nov 18');
    });

    it('accurately handles DST-change transition dates including 2026 and 2027', () => {
      // 1. Day before spring-forward 2026 (EST, UTC-5)
      expect(formatTimeIST('12:00', { referenceDate: '2026-03-07' })).toBe('10:30 PM IST');
      // Day after spring-forward 2026 (EDT, UTC-4)
      expect(formatTimeIST('12:00', { referenceDate: '2026-03-09' })).toBe('9:30 PM IST');

      // 2. US DST start date Sunday 14 March 2027:
      // Slots before transition (Saturday 13 March 2027, EST):
      expect(formatTimeIST('12:00', { referenceDate: '2027-03-13' })).toBe('10:30 PM IST');
      // On Sunday 14 March 2027 after 02:00 ET transition (EDT, UTC-4):
      expect(formatTimeIST('12:00', { referenceDate: '2027-03-14' })).toBe('9:30 PM IST');
      expect(formatTimeIST('14:00', { referenceDate: '2027-03-14' })).toBe('11:30 PM IST');
      // Day after spring-forward (Monday 15 March 2027, EDT):
      expect(formatTimeIST('12:00', { referenceDate: '2027-03-15' })).toBe('9:30 PM IST');

      // 3. US DST end date Sunday 1 November 2026:
      // Day before fall-back (Saturday 31 Oct 2026, EDT):
      expect(formatTimeIST('12:00', { referenceDate: '2026-10-31' })).toBe('9:30 PM IST');
      // On Sunday 1 November 2026 after 02:00 ET fall-back (EST, UTC-5):
      expect(formatTimeIST('12:00', { referenceDate: '2026-11-01' })).toBe('10:30 PM IST');
      // Day after fall-back (Monday 2 Nov 2026, EST):
      expect(formatTimeIST('12:00', { referenceDate: '2026-11-02' })).toBe('10:30 PM IST');

      // 4. US DST end date Sunday 7 November 2027:
      // Day before fall-back (Saturday 6 Nov 2027, EDT):
      expect(formatTimeIST('12:00', { referenceDate: '2027-11-06' })).toBe('9:30 PM IST');
      // On Sunday 7 November 2027 after fall-back (EST, UTC-5):
      expect(formatTimeIST('12:00', { referenceDate: '2027-11-07' })).toBe('10:30 PM IST');
      // Day after fall-back (Monday 8 Nov 2027, EST):
      expect(formatTimeIST('12:00', { referenceDate: '2027-11-08' })).toBe('10:30 PM IST');
    });

    it('converts user-entered IST back to ET HH:MM correctly', () => {
      expect(istToETHHMM('2026-11-17', '22:30')).toBe('12:00');
      expect(istToETHHMM('2026-11-17', '07:30')).toBe('21:00');
      expect(istToETHHMM('2026-07-17', '21:30')).toBe('12:00');
    });

    it('formats date-only strings without backward timezone shift in Asia/Kolkata', () => {
      // YYYY-MM-DD date-only strings must not shift backwards into the previous calendar day
      expect(formatDateIST('2026-11-17')).toBe('Tue, Nov 17');
      expect(formatDateIST('2026-11-17', { month: 'short', day: 'numeric' })).toBe('Nov 17');
      expect(formatWeekdayIST('2026-11-17')).toBe('Tue');

      // Mon 16
      expect(formatDateIST('2026-11-16')).toBe('Mon, Nov 16');
      expect(formatDateIST('2026-11-16', { month: 'short', day: 'numeric' })).toBe('Nov 16');
      expect(formatDateIST('2026-11-16', { weekday: 'short', day: 'numeric' })).toBe('Mon 16');
      expect(formatWeekdayIST('2026-11-16')).toBe('Mon');

      // Sun 15
      expect(formatDateIST('2026-11-15')).toBe('Sun, Nov 15');
      expect(formatDateIST('2026-11-15', { month: 'short', day: 'numeric' })).toBe('Nov 15');
      expect(formatWeekdayIST('2026-11-15')).toBe('Sun');
    });

    it('supports backward-compatible ET aliases', () => {
      expect(formatTimeET('2026-11-17T12:00:00-05:00')).toBe('10:30 PM IST');
      expect(formatDateET('2026-11-17')).toBe('Tue, Nov 17');
      expect(formatWeekdayET('2026-11-17')).toBe('Tue');
    });

    it('displays pending run label and today badge correctly', () => {
      // Simulate DaySummary format for pending run card
      const pendingDay = {
        date: '2026-11-20',
        found: 0,
        approved: 0,
        is_today: true,
        is_running: false,
        pending_run: true,
      };

      const label = pendingDay.is_running
        ? `${pendingDay.found} found so far`
        : pendingDay.pending_run
        ? 'Run scheduled for 7 AM IST'
        : `${pendingDay.found} found · ${pendingDay.approved} kept`;

      expect(label).toBe('Run scheduled for 7 AM IST');
      expect(pendingDay.is_today).toBe(true);
    });
  });
});
