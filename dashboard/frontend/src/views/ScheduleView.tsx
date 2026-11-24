import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { apiFetch } from '../utils/api';
import { Minus, Plus, Trash2, Clock, Check, AlertCircle, Sparkles, RefreshCw } from 'lucide-react';
import { Thumb } from '../components/Thumb';
import { StatusPill } from '../components/StatusPill';
import { formatTimeIST, formatTimeETHint } from '../utils/timeFormat';
import { stripHashtags } from '../utils/textFormat';
import {
  parseHHMM,
  minutesToHHMM,
  snapTo15Minutes,
  clampSlotsCount,
  sortSlots,
  findNextAvailableSlot,
  generatePreset,
  type PresetType,
} from '../utils/scheduleMath';

interface TomorrowSlotItem {
  slot_time?: string;
  slot_hour?: number;
  slot_hhmm?: string;
  post_id?: string;
  title?: string;
  uploader?: string;
  status?: string;
  video_id?: string;
  publish_at?: string;
  time_display?: string;
  thumb_url?: string;
}

interface ScheduleApiResponse {
  videos_per_day: number;
  slots: string[];
  timezone: string;
  display_timezone?: string;
  tomorrow_date: string;
  tomorrow_day_name: string;
  tomorrow_slots: TomorrowSlotItem[];
  can_retime_live: boolean;
}

// Generate valid 15-minute time options in a day for the select dropdown (shown in IST with matching ET hour)
const getTimeOptions = (refDate?: string) => {
  const options: { value: string; label: string; minutes: number }[] = [];
  for (let m = 0; m <= 1425; m += 15) {
    const etVal = minutesToHHMM(m);
    const istLabel = formatTimeIST(m, { referenceDate: refDate, showNextDay: true });
    const etHint = formatTimeETHint(etVal);
    options.push({
      value: etVal,
      label: `${istLabel} (${etHint})`,
      minutes: m,
    });
  }
  return options;
};

export const ScheduleView: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [timezone, setTimezone] = useState('America/New_York');
  const [canRetimeLive, setCanRetimeLive] = useState(false);
  const [tomorrowDate, setTomorrowDate] = useState('');
  const [tomorrowSlots, setTomorrowSlots] = useState<TomorrowSlotItem[]>([]);

  // Original server state
  const [savedSlots, setSavedSlots] = useState<string[]>([]);
  const [savedCount, setSavedCount] = useState(6);

  // Working state (editable)
  const [slots, setSlots] = useState<string[]>([]);
  const [videosPerDay, setVideosPerDay] = useState(6);

  // Save operation state
  const [saving, setSaving] = useState(false);
  const [saveSuccessMsg, setSaveSuccessMsg] = useState<string | null>(null);

  // Timeline dragging state
  const [activeDragIdx, setActiveDragIdx] = useState<number | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);

  const timeOptions = useMemo(() => getTimeOptions(tomorrowDate), [tomorrowDate]);

  const hasUnsavedChanges =
    videosPerDay !== savedCount ||
    slots.length !== savedSlots.length ||
    slots.some((s, i) => s !== savedSlots[i]);

  const fetchSchedule = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiFetch('/api/schedule');
      if (!resp.ok) throw new Error('Failed to load schedule config');
      const data: ScheduleApiResponse = await resp.json();

      setTimezone(data.timezone);
      setCanRetimeLive(data.can_retime_live);
      setTomorrowDate(data.tomorrow_date);
      setTomorrowSlots(data.tomorrow_slots || []);

      const sorted = sortSlots(data.slots || []);
      const count = clampSlotsCount(data.videos_per_day || sorted.length || 6);

      setSavedSlots(sorted);
      setSavedCount(count);
      setSlots(sorted);
      setVideosPerDay(count);
    } catch (err: any) {
      setError(err.message || 'Error fetching schedule');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSchedule();
  }, [fetchSchedule]);

  // Stepper handlers
  const handleDecrement = () => {
    if (videosPerDay <= 1) return;
    const newCount = videosPerDay - 1;
    setVideosPerDay(newCount);
    // Remove last slot
    const newSlots = slots.slice(0, newCount);
    setSlots(newSlots);
  };

  const handleIncrement = () => {
    if (videosPerDay >= 12) return;
    const newCount = videosPerDay + 1;
    setVideosPerDay(newCount);
    const nextSlot = findNextAvailableSlot(slots);
    const newSlots = sortSlots([...slots, nextSlot]);
    setSlots(newSlots);
  };

  // Preset handlers
  const handleApplyPreset = (type: PresetType) => {
    const newSlots = generatePreset(type, videosPerDay);
    setSlots(newSlots);
  };

  // Slot removal
  const handleRemoveSlot = (indexToRemove: number) => {
    if (slots.length <= 1) return;
    const newSlots = slots.filter((_, idx) => idx !== indexToRemove);
    setSlots(newSlots);
    setVideosPerDay(newSlots.length);
  };

  // Slot time update
  const handleSlotTimeChange = (index: number, newTime: string) => {
    const newSlots = [...slots];
    newSlots[index] = newTime;
    // Auto-sort to keep order consistent
    setSlots(sortSlots(newSlots));
  };

  // Timeline dragging handlers
  const handlePointerDownMarker = (e: React.PointerEvent, idx: number) => {
    e.stopPropagation();
    e.preventDefault();
    setActiveDragIdx(idx);
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };

  const handlePointerMoveMarker = (e: React.PointerEvent) => {
    if (activeDragIdx === null || !timelineRef.current) return;
    const rect = timelineRef.current.getBoundingClientRect();
    const clampedX = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
    const fraction = clampedX / rect.width;
    const minutes = snapTo15Minutes(fraction * 1440);
    const timeStr = minutesToHHMM(minutes);

    if (slots[activeDragIdx] !== timeStr) {
      const updated = [...slots];
      updated[activeDragIdx] = timeStr;
      // Do not reorder while dragging to avoid index jump; re-sort on pointer up
      setSlots(updated);
    }
  };

  const handlePointerUpMarker = (e: React.PointerEvent) => {
    if (activeDragIdx !== null) {
      try {
        (e.target as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        // ignore
      }
      setActiveDragIdx(null);
      // Re-sort after release
      setSlots((prev) => sortSlots(prev));
    }
  };

  // Keyboard accessibility for markers (Arrow keys move by 15 mins)
  const handleMarkerKeyDown = (e: React.KeyboardEvent, idx: number) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
      e.preventDefault();
      const currentMins = parseHHMM(slots[idx]);
      const newMins = snapTo15Minutes(Math.max(0, currentMins - 15));
      const updated = [...slots];
      updated[idx] = minutesToHHMM(newMins);
      setSlots(sortSlots(updated));
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
      e.preventDefault();
      const currentMins = parseHHMM(slots[idx]);
      const newMins = snapTo15Minutes(Math.min(1425, currentMins + 15));
      const updated = [...slots];
      updated[idx] = minutesToHHMM(newMins);
      setSlots(sortSlots(updated));
    }
  };

  // Save changes
  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSaveSuccessMsg(null);
    try {
      const sorted = sortSlots(slots);
      const resp = await apiFetch('/api/schedule', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({
          videos_per_day: videosPerDay,
          slots: sorted,
        }),
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || 'Failed to save schedule');
      }

      const resData = await resp.json();
      setSavedSlots(sorted);
      setSavedCount(videosPerDay);
      setSlots(sorted);
      setSaveSuccessMsg(resData.notice || 'Schedule saved successfully!');
      setTimeout(() => setSaveSuccessMsg(null), 6000);
    } catch (err: any) {
      setError(err.message || 'Error saving schedule');
    } finally {
      setSaving(false);
    }
  };

  // Discard changes
  const handleDiscard = () => {
    setSlots([...savedSlots]);
    setVideosPerDay(savedCount);
    setError(null);
  };

  // Pair tomorrow's clips to current slots:
  // 1. Leave every clip whose slot time is unchanged
  // 2. Match only leftover old clips to leftover new slots in order
  const pairedTomorrowClips = React.useMemo(() => {
    const assigned = new Map<number, TomorrowSlotItem>();
    const claimedIndices = new Set<number>();
    const leftoverClips: TomorrowSlotItem[] = [];

    const getClipHHMM = (clip: TomorrowSlotItem): string => {
      if (clip.slot_hhmm) return clip.slot_hhmm;
      if (clip.slot_time) {
        try {
          const match = clip.slot_time.match(/T(\d{2}):(\d{2})/);
          if (match) return `${match[1]}:${match[2]}`;
        } catch {}
      }
      return `${(clip.slot_hour ?? 12).toString().padStart(2, '0')}:00`;
    };

    // First pass: match clips whose slot is unchanged in slots
    for (const clip of tomorrowSlots) {
      const clipTime = getClipHHMM(clip);
      const matchIdx = slots.findIndex((s, i) => s === clipTime && !claimedIndices.has(i));
      if (matchIdx !== -1) {
        claimedIndices.add(matchIdx);
        assigned.set(matchIdx, clip);
      } else {
        leftoverClips.push(clip);
      }
    }

    // Second pass: match leftover clips to leftover slots in order
    const leftoverSlotIndices = slots
      .map((_, i) => i)
      .filter((i) => !claimedIndices.has(i));

    for (let i = 0; i < Math.min(leftoverClips.length, leftoverSlotIndices.length); i++) {
      assigned.set(leftoverSlotIndices[i], leftoverClips[i]);
    }

    return slots.map((_, i) => assigned.get(i));
  }, [slots, tomorrowSlots]);

  if (loading) {
    return (
      <div className="flex-1 p-[40px] flex items-center justify-center">
        <div className="flex items-center gap-[12px] text-[#6F7A73]">
          <RefreshCw className="size-[20px] animate-spin text-[#1D3B33]" />
          <span className="font-ui text-[15px]">Loading schedule settings...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 p-[24px] lg:p-[40px] max-w-[1400px] mx-auto flex flex-col gap-[32px] w-full">
      {/* Header section */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-[20px] border-b border-[#D5DBD5]/60 pb-[24px]">
        <div>
          <div className="flex items-center gap-[12px]">
            <h1 className="font-heading font-extrabold text-[32px] lg:text-[38px] text-[#1D3B33] tracking-tight leading-none">
              Schedule
            </h1>
            {hasUnsavedChanges && (
              <span className="inline-flex items-center gap-[6px] px-[10px] py-[3px] rounded-full text-[12px] font-ui font-semibold bg-[#F7E9C8] text-[#7A5600] border border-[#D99A1E]/30 animate-pulse">
                <span className="size-[6px] rounded-full bg-[#D99A1E]" />
                Unsaved changes
              </span>
            )}
          </div>
          <p className="font-ui text-[14px] text-[#6F7A73] mt-[8px]">
            Channel timezone: <span className="font-medium text-[#1D3B33]">{timezone}</span> · Displaying in <span className="font-medium text-[#2E7D4F]">IST (UTC+5:30)</span> · Scheduled slots map to ET broadcast hours
          </p>
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-[12px]">
          <button
            type="button"
            onClick={handleDiscard}
            disabled={!hasUnsavedChanges || saving}
            className="px-[16px] py-[10px] rounded-[10px] border border-[#D5DBD5] bg-white font-ui text-[14px] font-medium text-[#6F7A73] hover:text-[#1D3B33] hover:bg-[#EDF0EC] disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer transition-colors"
          >
            Discard
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!hasUnsavedChanges || saving}
            className="px-[20px] py-[10px] rounded-[10px] bg-[#1D3B33] font-ui text-[14px] font-semibold text-white hover:bg-[#2A4F43] disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer transition-all shadow-sm flex items-center gap-[8px]"
          >
            {saving ? (
              <>
                <RefreshCw className="size-[16px] animate-spin" />
                Saving...
              </>
            ) : (
              <>
                <Check className="size-[16px]" />
                Save schedule
              </>
            )}
          </button>
        </div>
      </div>

      {/* Error and Success alerts */}
      {error && (
        <div className="p-[16px] rounded-[12px] bg-[#D4412F]/10 border border-[#D4412F]/30 flex items-start gap-[12px] text-[#D4412F]">
          <AlertCircle className="size-[20px] shrink-0 mt-[2px]" />
          <div className="font-ui text-[14px]">
            <p className="font-semibold">Unable to save schedule</p>
            <p className="mt-[2px]">{error}</p>
          </div>
        </div>
      )}

      {saveSuccessMsg && (
        <div className="p-[16px] rounded-[12px] bg-[#DCEBE0] border border-[#2E7D4F]/30 flex items-start gap-[12px] text-[#2E7D4F]">
          <Check className="size-[20px] shrink-0 mt-[2px]" />
          <p className="font-ui text-[14px] font-medium">{saveSuccessMsg}</p>
        </div>
      )}

      {/* Amber Notice Card (from design) */}
      <div className="p-[18px] lg:p-[20px] rounded-[14px] bg-[#F7E9C8] border border-[#D99A1E]/40 flex items-start gap-[14px] text-[#7A5600] shadow-sm">
        <Sparkles className="size-[20px] text-[#D99A1E] shrink-0 mt-[2px]" />
        <div className="font-ui text-[13.5px] leading-relaxed">
          {canRetimeLive ? (
            <p>
              <span className="font-bold">Live YouTube Retiming Enabled:</span> Clips already uploaded for tomorrow will be automatically retimed in YouTube (cost ~50 API quota units each).
            </p>
          ) : (
            <p>
              <span className="font-bold">Future Runs Notice:</span> Changes will apply to future pipeline runs. To retime existing scheduled clips live on YouTube, run{' '}
              <code className="px-[6px] py-[2px] bg-white/70 rounded text-[12.5px] font-mono text-[#1D3B33] font-semibold border border-[#D99A1E]/30">
                python upload.py auth --manage
              </code>
            </p>
          )}
        </div>
      </div>

      {/* Top Controls: Stepper and Preset chips */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-[20px]">
        {/* Stepper Card */}
        <div className="bg-white rounded-[16px] p-[24px] lg:p-[28px] border border-[#D5DBD5] flex flex-col justify-between shadow-xs">
          <div>
            <span className="font-ui text-[13px] font-semibold uppercase tracking-wider text-[#6F7A73]">
              Daily Volume
            </span>
            <h2 className="font-heading font-extrabold text-[20px] text-[#1D3B33] mt-[2px]">
              Videos per day
            </h2>
          </div>

          <div className="flex items-center gap-[20px] mt-[20px]">
            <button
              type="button"
              onClick={handleDecrement}
              disabled={videosPerDay <= 1}
              aria-label="Decrease videos per day"
              className="size-[48px] rounded-[12px] border border-[#D5DBD5] flex items-center justify-center text-[#1D3B33] hover:bg-[#EDF0EC] disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer transition-colors"
            >
              <Minus className="size-[20px]" />
            </button>
            <div className="flex items-baseline gap-[6px]">
              <span className="font-heading font-extrabold text-[44px] text-[#1D3B33] leading-none">
                {videosPerDay}
              </span>
              <span className="font-ui font-medium text-[16px] text-[#6F7A73]">
                {videosPerDay === 1 ? 'slot' : 'slots'}
              </span>
            </div>
            <button
              type="button"
              onClick={handleIncrement}
              disabled={videosPerDay >= 12}
              aria-label="Increase videos per day"
              className="size-[48px] rounded-[12px] border border-[#D5DBD5] flex items-center justify-center text-[#1D3B33] hover:bg-[#EDF0EC] disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer transition-colors"
            >
              <Plus className="size-[20px]" />
            </button>
          </div>
        </div>

        {/* Presets Card */}
        <div className="bg-white rounded-[16px] p-[24px] lg:p-[28px] border border-[#D5DBD5] flex flex-col justify-between shadow-xs">
          <div>
            <span className="font-ui text-[13px] font-semibold uppercase tracking-wider text-[#6F7A73]">
              Quick Templates
            </span>
            <h2 className="font-heading font-extrabold text-[20px] text-[#1D3B33] mt-[2px]">
              Preset distributions
            </h2>
          </div>

          <div className="flex flex-wrap gap-[10px] mt-[20px]">
            <button
              type="button"
              onClick={() => handleApplyPreset('evening_heavy')}
              className="px-[16px] py-[10px] rounded-full border border-[#D5DBD5] bg-[#EDF0EC] hover:bg-[#DCEBE0] hover:border-[#2E7D4F]/40 font-ui text-[13.5px] font-medium text-[#1D3B33] cursor-pointer transition-colors"
            >
              Evening heavy
            </button>
            <button
              type="button"
              onClick={() => handleApplyPreset('even_spread')}
              className="px-[16px] py-[10px] rounded-full border border-[#D5DBD5] bg-[#EDF0EC] hover:bg-[#DCEBE0] hover:border-[#2E7D4F]/40 font-ui text-[13.5px] font-medium text-[#1D3B33] cursor-pointer transition-colors"
            >
              Even spread
            </button>
            <button
              type="button"
              onClick={() => handleApplyPreset('morning_and_night')}
              className="px-[16px] py-[10px] rounded-full border border-[#D5DBD5] bg-[#EDF0EC] hover:bg-[#DCEBE0] hover:border-[#2E7D4F]/40 font-ui text-[13.5px] font-medium text-[#1D3B33] cursor-pointer transition-colors"
            >
              Morning and night
            </button>
          </div>
        </div>
      </div>

      {/* 24-Hour Timeline Track Card */}
      <div className="bg-white rounded-[16px] p-[24px] lg:p-[28px] border border-[#D5DBD5] flex flex-col gap-[20px] shadow-xs">
        <div className="flex items-center justify-between">
          <div>
            <span className="font-ui text-[13px] font-semibold uppercase tracking-wider text-[#6F7A73]">
              Visual Distribution
            </span>
            <h2 className="font-heading font-extrabold text-[20px] text-[#1D3B33] mt-[2px]">
              24-Hour timeline
            </h2>
          </div>
          <span className="font-ui text-[13px] text-[#6F7A73]">
            Drag markers or use Left/Right arrow keys (15m step)
          </span>
        </div>

        {/* Timeline wrapper */}
        <div className="flex flex-col gap-[8px] pt-[20px] pb-[10px]">
          {/* Dayparts header banner */}
          <div className="grid grid-cols-4 text-center font-ui text-[12px] font-semibold text-[#6F7A73] pb-[4px]">
            <div className="border-r border-[#D5DBD5]/60 pr-[8px] text-left pl-[4px]">Night (12A - 6A)</div>
            <div className="border-r border-[#D5DBD5]/60 px-[8px]">Morning (6A - 12P)</div>
            <div className="border-r border-[#D5DBD5]/60 px-[8px]">Afternoon (12P - 6P)</div>
            <div className="pl-[8px] text-right pr-[4px]">Evening (6P - 12A)</div>
          </div>

          {/* Interactive timeline bar */}
          <div
            ref={timelineRef}
            className="relative w-full h-[54px] rounded-[12px] bg-[#EDF0EC] border border-[#D5DBD5] select-none cursor-crosshair overflow-visible"
            onPointerMove={handlePointerMoveMarker}
            onPointerUp={handlePointerUpMarker}
          >
            {/* 4 day-part background shading / lines */}
            <div className="absolute inset-0 grid grid-cols-4 divide-x divide-[#D5DBD5] pointer-events-none rounded-[12px] overflow-hidden">
              <div className="bg-transparent" />
              <div className="bg-[#FFFFFF]/40" />
              <div className="bg-[#FFFFFF]/40" />
              <div className="bg-[#DCEBE0]/30" />
            </div>

            {/* Draggable Markers */}
            {slots.map((slotTime, idx) => {
              const mins = parseHHMM(slotTime);
              const percent = (mins / 1440) * 100;
              const isDragging = activeDragIdx === idx;
              const istLabel = formatTimeIST(mins, { referenceDate: tomorrowDate, showNextDay: true });

              return (
                <div
                  key={`marker-${idx}`}
                  role="slider"
                  tabIndex={0}
                  aria-label={`Slot ${idx + 1} at ${istLabel} (${slotTime} ET)`}
                  aria-valuenow={mins}
                  aria-valuemin={0}
                  aria-valuemax={1425}
                  onPointerDown={(e) => handlePointerDownMarker(e, idx)}
                  onKeyDown={(e) => handleMarkerKeyDown(e, idx)}
                  style={{ left: `${percent}%` }}
                  className={`absolute top-1/2 -translate-y-1/2 -translate-x-1/2 z-20 group outline-none
                              cursor-grab active:cursor-grabbing transition-transform ${isDragging ? 'scale-110 z-30' : ''}`}
                >
                  {/* Pin badge */}
                  <div
                    className={`size-[32px] rounded-full flex items-center justify-center font-heading font-bold text-[12px] shadow-md transition-colors
                                ${isDragging ? 'bg-[#2E7D4F] text-white ring-4 ring-[#DCEBE0]' : 'bg-[#1D3B33] text-white hover:bg-[#2A4F43] focus:ring-2 focus:ring-[#2E7D4F]'}`}
                  >
                    {idx + 1}
                  </div>

                  {/* Tooltip on hover or drag */}
                  <div
                    className={`absolute bottom-[36px] left-1/2 -translate-x-1/2 px-[8px] py-[3px] rounded-[6px] bg-[#1D3B33] text-white text-[11px] font-ui font-semibold whitespace-nowrap shadow-md pointer-events-none transition-opacity
                                ${isDragging ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`}
                  >
                    {istLabel} · {slotTime} ET
                  </div>
                </div>
              );
            })}
          </div>

          {/* Hour labels below track (IST display times across broadcast day) */}
          <div className="relative w-full h-[18px] text-[10.5px] font-ui text-[#6F7A73] flex justify-between px-[4px] mt-[4px]">
            {[0, 3, 6, 9, 12, 15, 18, 21, 24].map((h, i) => {
              const mins = (h % 24) * 60;
              const label = formatTimeIST(mins, {
                referenceDate: tomorrowDate,
                omitZeroMinutes: false,
                includeLabel: i === 0 || i === 8,
              });
              return <span key={h}>{label}</span>;
            })}
          </div>
        </div>
      </div>

      {/* Slots List & Tomorrow's Alignment */}
      <div className="bg-white rounded-[16px] p-[24px] lg:p-[28px] border border-[#D5DBD5] flex flex-col gap-[20px] shadow-xs">
        <div className="flex items-center justify-between">
          <div>
            <span className="font-ui text-[13px] font-semibold uppercase tracking-wider text-[#6F7A73]">
              Upload Schedule
            </span>
            <h2 className="font-heading font-extrabold text-[20px] text-[#1D3B33] mt-[2px]">
              Configured Slots ({slots.length})
            </h2>
            <p className="font-ui text-[12.5px] text-[#6F7A73] mt-[2px]">
              Slots are fixed to US (ET) hours so your US audience timing stays the same. IST times shift by 1 hour when US daylight saving changes.
            </p>
          </div>
          {tomorrowDate && (
            <span className="font-ui text-[13px] text-[#6F7A73]">
              Tomorrow: <span className="font-medium text-[#1D3B33]">{tomorrowDate}</span>
            </span>
          )}
        </div>

        <div className="divide-y divide-[#D5DBD5]/60">
          {slots.map((slotTime, idx) => {
            // Match with tomorrow's assigned clip: unchanged slots keep clip, moved slots pair leftover
            const assignedClip = pairedTomorrowClips[idx];

            return (
              <div
                key={`slot-row-${idx}`}
                className="py-[16px] first:pt-0 last:pb-0 flex flex-col md:flex-row md:items-center justify-between gap-[16px]"
              >
                {/* Left: Slot index & Time selector */}
                <div className="flex items-center gap-[16px]">
                  <div className="size-[36px] rounded-[10px] bg-[#EDF0EC] border border-[#D5DBD5] flex items-center justify-center font-heading font-bold text-[14px] text-[#1D3B33] shrink-0">
                    {idx + 1}
                  </div>

                  {/* 12-Hour Dropdown selector */}
                  <div className="relative">
                    <select
                      value={slotTime}
                      onChange={(e) => handleSlotTimeChange(idx, e.target.value)}
                      aria-label={`Time for Slot ${idx + 1}`}
                      className="px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] bg-white font-ui font-semibold text-[14px] text-[#1D3B33] hover:border-[#1D3B33] focus:outline-none focus:ring-2 focus:ring-[#2E7D4F] cursor-pointer appearance-none pr-[32px]"
                    >
                      {timeOptions.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                    <Clock className="size-[14px] text-[#6F7A73] absolute right-[10px] top-1/2 -translate-y-1/2 pointer-events-none" />
                  </div>

                  <span className="hidden sm:inline font-ui text-[12px] text-[#6F7A73] bg-[#EDF0EC] px-[8px] py-[5px] rounded-[6px] shrink-0 font-medium">
                    ({formatTimeETHint(slotTime)})
                  </span>
                </div>

                {/* Center: Tomorrow's Assigned Clip preview */}
                <div className="flex-1 md:px-[20px] flex items-center gap-[14px]">
                  {assignedClip ? (
                    <>
                      <div className="w-[38px] shrink-0">
                        <Thumb
                          src={assignedClip.thumb_url}
                          alt={stripHashtags(assignedClip.title)}
                          className="w-full aspect-[9/16] rounded-[6px]"
                        />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="font-ui font-medium text-[13.5px] text-[#1D3B33] truncate">
                          {stripHashtags(assignedClip.title) || 'Untitled Short'}
                        </p>
                        <p className="font-ui text-[12px] text-[#6F7A73] truncate">
                          by {assignedClip.uploader || 'unknown'}
                        </p>
                      </div>
                      <StatusPill status={assignedClip.status || 'Scheduled'} />
                    </>
                  ) : (
                    <div className="flex items-center gap-[8px] text-[#6F7A73] font-ui text-[13px] italic">
                      <span>No clip assigned yet for tomorrow</span>
                    </div>
                  )}
                </div>

                {/* Right: Remove slot button */}
                <div className="flex items-center justify-end">
                  <button
                    type="button"
                    onClick={() => handleRemoveSlot(idx)}
                    disabled={slots.length <= 1}
                    aria-label={`Remove Slot ${idx + 1}`}
                    title={slots.length <= 1 ? 'Minimum 1 slot required' : 'Remove slot'}
                    className="p-[8px] rounded-[8px] text-[#6F7A73] hover:text-[#D4412F] hover:bg-[#D4412F]/10 disabled:opacity-20 disabled:cursor-not-allowed cursor-pointer transition-colors"
                  >
                    <Trash2 className="size-[16px]" />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};
