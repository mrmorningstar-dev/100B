import React, { useState, useEffect, useRef, useCallback } from 'react';
import { ExternalLink, Play, RefreshCw, Calendar as CalendarIcon, AlertCircle, Eye, ThumbsUp } from 'lucide-react';
import { Thumb } from '../components/Thumb';
import { StatusPill } from '../components/StatusPill';
import { ClaimChip } from '../components/ClaimChip';
import { formatTimeIST, formatTimeETHint } from '../utils/timeFormat';
import { stripHashtags } from '../utils/textFormat';
import { apiFetch, getApiUrl, refreshMediaToken } from '../utils/api';

export interface YouTubeClipItem {
  post_id: string;
  video_id: string | null;
  title: string;
  uploader: string;
  slot_time: string;
  time_display: string;
  status: string;
  status_detail: string;
  claim_status?: string;
  rights_status?: string;
  rights_risk?: number;
  views?: string | number | null;
  likes?: string | number | null;
  youtube_url: string | null;
  studio_url: string | null;
  thumb_url: string;
  stream_url: string;
  has_local_file: boolean;
}

interface YouTubeDayResponse {
  date: string;
  day_header: string;
  is_late_broadcast_window?: boolean;
  published_count: number;
  on_air_count: number;
  scheduled_count: number;
  total_views: number;
  clips: YouTubeClipItem[];
}

interface YouTubeViewProps {
  initialPostId?: string | null;
  onClearInitialPostId?: () => void;
}

export const YouTubeView: React.FC<YouTubeViewProps> = ({
  initialPostId,
  onClearInitialPostId,
}) => {
  const [selectedDayKey, setSelectedDayKey] = useState<'today' | 'yesterday' | 'custom'>('today');
  const [customDate, setCustomDate] = useState<string>('');
  const [data, setData] = useState<YouTubeDayResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedClip, setSelectedClip] = useState<YouTubeClipItem | null>(null);
  const [embedFailed, setEmbedFailed] = useState(false);
  const [videoStreamUrl, setVideoStreamUrl] = useState<string | null>(null);
  const [videoPosterUrl, setVideoPosterUrl] = useState<string | null>(null);

  const dateInputRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const lastHandledErrorTimeRef = useRef<number>(0);

  // Keep video stream link fixed per clip; do not re-generate on periodic token renewal or background polling
  useEffect(() => {
    if (!selectedClip?.stream_url) {
      setVideoStreamUrl(null);
      setVideoPosterUrl(null);
      return;
    }
    setVideoStreamUrl(getApiUrl(selectedClip.stream_url, { isMedia: true }));
    setVideoPosterUrl(selectedClip.thumb_url ? getApiUrl(selectedClip.thumb_url, { isMedia: true }) : null);
  }, [selectedClip?.post_id, selectedClip?.stream_url, selectedClip?.thumb_url]);

  // Refresh token and stream URL only on error (e.g. paused for >10 mins then seeking -> 401)
  const handleVideoError = async () => {
    const video = videoRef.current;
    if (!video || !selectedClip?.stream_url) return;

    // Avoid rapid retry loops
    const now = Date.now();
    if (now - lastHandledErrorTimeRef.current < 2000) return;
    lastHandledErrorTimeRef.current = now;

    const savedTime = video.currentTime;
    const wasPaused = video.paused;

    const newToken = await refreshMediaToken();
    if (newToken) {
      const freshUrl = getApiUrl(selectedClip.stream_url, { isMedia: true, token: newToken });
      setVideoStreamUrl(freshUrl);

      const handleLoaded = () => {
        if (videoRef.current) {
          videoRef.current.currentTime = savedTime;
          if (!wasPaused) {
            videoRef.current.play().catch(() => {});
          }
        }
        video.removeEventListener('loadedmetadata', handleLoaded);
      };
      video.addEventListener('loadedmetadata', handleLoaded);
    }
  };

  // Compute query param for date
  const dateParam = selectedDayKey === 'custom' && customDate ? customDate : selectedDayKey;

  const fetchDayData = useCallback(async (isBackground = false) => {
    if (!isBackground) {
      setLoading(true);
      setEmbedFailed(false);
    }
    setError(null);
    try {
      const resp = await apiFetch(`/api/youtube/day/${dateParam}`);
      if (!resp.ok) throw new Error('Failed to load YouTube status for this date');
      const json: YouTubeDayResponse = await resp.json();
      setData(json);

      // Select initial clip if provided, or preserve selection, or pick first clip
      if (initialPostId && json.clips.some(c => c.post_id === initialPostId)) {
        const found = json.clips.find(c => c.post_id === initialPostId);
        if (found) setSelectedClip(found);
        if (onClearInitialPostId) onClearInitialPostId();
      } else {
        setSelectedClip(prev => {
          if (!prev) return json.clips[0] || null;
          const match = json.clips.find(c => c.post_id === prev.post_id);
          return match || json.clips[0] || null;
        });
      }
    } catch (err: any) {
      setError(err.message || 'Error fetching YouTube data');
    } finally {
      if (!isBackground) setLoading(false);
    }
  }, [dateParam, initialPostId, onClearInitialPostId]);

  // Initial and param-change fetch
  useEffect(() => {
    fetchDayData();
  }, [fetchDayData]);

  // Tab visibility tracking and 30-second interval refresh
  useEffect(() => {
    const interval = setInterval(() => {
      // Pause refreshes when tab is hidden
      if (document.visibilityState === 'visible') {
        fetchDayData(true);
      }
    }, 30000);

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        fetchDayData(true);
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      clearInterval(interval);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [fetchDayData]);

  const handleSelectDay = (key: 'today' | 'yesterday') => {
    setSelectedDayKey(key);
    setEmbedFailed(false);
  };

  const handleCustomDateChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.value) {
      setCustomDate(e.target.value);
      setSelectedDayKey('custom');
      setEmbedFailed(false);
    }
  };

  const handleClipSelect = (clip: YouTubeClipItem) => {
    setSelectedClip(clip);
    setEmbedFailed(false);
  };

  return (
    <div className="flex-1 p-[24px] lg:p-[40px] max-w-[1400px] mx-auto flex flex-col gap-[32px] w-full">
      {/* Header section with Date Switcher */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-[20px] border-b border-[#D5DBD5]/60 pb-[24px]">
        <div>
          <h1 className="font-heading font-extrabold text-[32px] lg:text-[38px] text-[#1D3B33] tracking-tight leading-none">
            YouTube
          </h1>
          <p className="font-ui text-[14px] text-[#6F7A73] mt-[8px]">
            {data?.date ? `Batch of ${data.date} (starts 10:30 PM IST)` : (data?.day_header || 'Daily broadcast rundown')} · Live playback and view metrics
          </p>
          {data?.is_late_broadcast_window && (
            <p className="font-ui text-[12px] text-[#2E7D4F] font-semibold mt-[4px] flex items-center gap-[6px]">
              <span className="size-[6px] rounded-full bg-[#2E7D4F] animate-pulse" />
              Note: Previous batch is still broadcasting its final slots (until ~7:30 AM IST).
            </p>
          )}
        </div>

        {/* Date switcher pill group */}
        <div className="flex items-center gap-[8px] bg-white p-[4px] rounded-[12px] border border-[#D5DBD5] shadow-xs">
          <button
            type="button"
            onClick={() => handleSelectDay('today')}
            className={`px-[16px] py-[8px] rounded-[8px] font-ui text-[13.5px] font-medium transition-colors cursor-pointer
              ${selectedDayKey === 'today' ? 'bg-[#1D3B33] text-white' : 'text-[#6F7A73] hover:text-[#1D3B33] hover:bg-[#EDF0EC]'}`}
          >
            Today
          </button>
          <button
            type="button"
            onClick={() => handleSelectDay('yesterday')}
            className={`px-[16px] py-[8px] rounded-[8px] font-ui text-[13.5px] font-medium transition-colors cursor-pointer
              ${selectedDayKey === 'yesterday' ? 'bg-[#1D3B33] text-white' : 'text-[#6F7A73] hover:text-[#1D3B33] hover:bg-[#EDF0EC]'}`}
          >
            Yesterday
          </button>

          {/* Date Picker trigger */}
          <div className="relative">
            <button
              type="button"
              onClick={() => dateInputRef.current?.showPicker ? dateInputRef.current.showPicker() : dateInputRef.current?.focus()}
              className={`px-[14px] py-[8px] rounded-[8px] font-ui text-[13.5px] font-medium transition-colors cursor-pointer flex items-center gap-[6px]
                ${selectedDayKey === 'custom' ? 'bg-[#1D3B33] text-white' : 'text-[#6F7A73] hover:text-[#1D3B33] hover:bg-[#EDF0EC]'}`}
            >
              <CalendarIcon className="size-[14px]" />
              <span>{selectedDayKey === 'custom' && customDate ? customDate : 'Pick a date'}</span>
            </button>
            <input
              ref={dateInputRef}
              type="date"
              aria-label="Pick date"
              onChange={handleCustomDateChange}
              value={customDate}
              className="absolute inset-0 opacity-0 pointer-events-none w-full"
            />
          </div>
        </div>
      </div>

      {error && (
        <div className="p-[16px] rounded-[12px] bg-[#D4412F]/10 border border-[#D4412F]/30 flex items-start gap-[12px] text-[#D4412F]">
          <AlertCircle className="size-[20px] shrink-0 mt-[2px]" />
          <p className="font-ui text-[14px]">{error}</p>
        </div>
      )}

      {/* Main Two-Column Layout (Clips list + Player) */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-[28px] items-start">
        {/* Left Column: Clips List (7 cols) */}
        <div className="lg:col-span-7 flex flex-col gap-[20px]">
          {/* Summary stats bar */}
          {data && (
            <div className="grid grid-cols-3 gap-[12px]">
              <div className="bg-white rounded-[14px] p-[16px] border border-[#D5DBD5] shadow-xs">
                <span className="font-ui text-[12px] text-[#6F7A73] font-medium uppercase tracking-wider">
                  Published / Live
                </span>
                <p className="font-heading font-extrabold text-[24px] text-[#1D3B33] mt-[2px]">
                  {(data.published_count || 0) + (data.on_air_count || 0)}
                </p>
              </div>
              <div className="bg-white rounded-[14px] p-[16px] border border-[#D5DBD5] shadow-xs">
                <span className="font-ui text-[12px] text-[#6F7A73] font-medium uppercase tracking-wider">
                  Scheduled
                </span>
                <p className="font-heading font-extrabold text-[24px] text-[#D99A1E] mt-[2px]">
                  {data.scheduled_count || 0}
                </p>
              </div>
              <div className="bg-white rounded-[14px] p-[16px] border border-[#D5DBD5] shadow-xs">
                <span className="font-ui text-[12px] text-[#6F7A73] font-medium uppercase tracking-wider">
                  Total Views
                </span>
                <p className="font-heading font-extrabold text-[24px] text-[#2E7D4F] mt-[2px]">
                  {data.total_views > 0 ? data.total_views.toLocaleString() : '—'}
                </p>
              </div>
            </div>
          )}

          {/* Clips List Container */}
          <div className="bg-white rounded-[16px] border border-[#D5DBD5] p-[20px] lg:p-[24px] shadow-xs">
            <h2 className="font-heading font-extrabold text-[18px] text-[#1D3B33] mb-[16px]">
              Daily Rundown ({data?.clips.length || 0} videos)
            </h2>

            {loading ? (
              <div className="py-[60px] flex items-center justify-center gap-[12px] text-[#6F7A73]">
                <RefreshCw className="size-[20px] animate-spin text-[#1D3B33]" />
                <span className="font-ui text-[14px]">Loading videos for {dateParam}...</span>
              </div>
            ) : !data?.clips || data.clips.length === 0 ? (
              <div className="py-[60px] text-center text-[#6F7A73] font-ui text-[14px]">
                No videos found or scheduled for this date.
              </div>
            ) : (
              <div className="divide-y divide-[#D5DBD5]/60">
                {data.clips.map((clip) => {
                  const isSelected = selectedClip?.post_id === clip.post_id;
                  const viewsFormatted = clip.views ? Number(clip.views).toLocaleString() : '—';
                  const likesFormatted = clip.likes ? Number(clip.likes).toLocaleString() : '—';

                  return (
                    <div
                      key={clip.post_id}
                      onClick={() => handleClipSelect(clip)}
                      className={`py-[14px] first:pt-0 last:pb-0 px-[12px] -mx-[12px] rounded-[12px] flex items-center gap-[16px] cursor-pointer transition-colors
                        ${isSelected ? 'bg-[#DCEBE0]/60 ring-1 ring-[#2E7D4F]/30' : 'hover:bg-[#EDF0EC]/60'}`}
                    >
                      {/* Slot time */}
                      <div className="flex flex-col w-[115px] shrink-0">
                        <span className="font-ui font-bold text-[13px] text-[#1D3B33] leading-tight">
                          {formatTimeIST(clip.slot_time, { referenceDate: data?.date || selectedDayKey, showNextDay: true })}
                        </span>
                        {formatTimeETHint(clip.slot_time) && (
                          <span className="font-mono text-[11px] text-[#8A958E] leading-tight mt-[1px]">
                            ({formatTimeETHint(clip.slot_time)})
                          </span>
                        )}
                      </div>

                      {/* 9:16 Thumb */}
                      <div className="w-[42px] shrink-0">
                        <Thumb
                          src={clip.thumb_url}
                          alt={stripHashtags(clip.title)}
                          className="w-full aspect-[9/16] rounded-[6px]"
                        />
                      </div>

                      {/* Title & Uploader */}
                      <div className="flex-1 min-w-0">
                        <p className="font-ui font-semibold text-[14px] text-[#1D3B33] truncate">
                          {stripHashtags(clip.title)}
                        </p>
                        <p className="font-ui text-[12px] text-[#6F7A73] truncate mt-[1px]">
                          by {clip.uploader}
                        </p>
                        <p className="font-ui text-[11.5px] text-[#6F7A73] mt-[2px]">
                          {clip.status_detail}
                        </p>
                      </div>

                      {/* Metrics: views & likes */}
                      <div className="hidden sm:flex flex-col items-end gap-[4px] shrink-0 text-right pr-[8px]">
                        <span className="font-ui font-semibold text-[13px] text-[#1D3B33] flex items-center gap-[4px]">
                          <Eye className="size-[13px] text-[#6F7A73]" />
                          {viewsFormatted}
                        </span>
                        <span className="font-ui text-[11.5px] text-[#6F7A73] flex items-center gap-[4px]">
                          <ThumbsUp className="size-[11px]" />
                          {likesFormatted}
                        </span>
                      </div>

                      {/* Status Pill & Rights Badges */}
                      <div className="shrink-0 flex items-center gap-[6px]">
                        <ClaimChip status={clip.claim_status} />
                        {clip.rights_status === 'held' && (
                          <span className="font-ui text-[11px] font-bold px-[8px] py-[3px] rounded-[6px] bg-[#D99A1E]/15 text-[#D99A1E] border border-[#D99A1E]/30 uppercase tracking-wider">
                            Held
                          </span>
                        )}
                        {clip.rights_status === 'needs_check' && (
                          <span className="font-ui text-[11px] font-bold px-[8px] py-[3px] rounded-[6px] bg-[#3B82F6]/15 text-[#3B82F6] border border-[#3B82F6]/30 uppercase tracking-wider">
                            Needs Check
                          </span>
                        )}
                        <StatusPill status={clip.status} />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* Right Column: Player Panel (5 cols) */}
        <div className="lg:col-span-5 flex flex-col gap-[20px] sticky top-[24px]">
          <div className="bg-white rounded-[16px] border border-[#D5DBD5] p-[24px] flex flex-col gap-[20px] shadow-xs">
            {selectedClip ? (
              <>
                {/* Clip info header */}
                <div className="flex items-start justify-between gap-[12px]">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-[8px] flex-wrap">
                      <div className="flex items-center gap-[6px] flex-wrap">
                        <span className="font-ui font-bold text-[13px] text-[#1D3B33]">
                          {formatTimeIST(selectedClip.slot_time, { referenceDate: data?.date || selectedDayKey, showNextDay: true })}
                        </span>
                        {formatTimeETHint(selectedClip.slot_time) && (
                          <span className="font-mono text-[11px] text-[#8A958E]">
                            ({formatTimeETHint(selectedClip.slot_time)})
                          </span>
                        )}
                      </div>
                      {selectedClip.claim_status === 'claimed' && (
                        <span className="font-ui text-[11px] font-bold px-[8px] py-[3px] rounded-[6px] bg-[#D4412F]/15 text-[#D4412F] border border-[#D4412F]/30 uppercase tracking-wider">
                          Claimed
                        </span>
                      )}
                      {selectedClip.rights_status === 'held' && (
                        <span className="font-ui text-[11px] font-bold px-[8px] py-[3px] rounded-[6px] bg-[#D99A1E]/15 text-[#D99A1E] border border-[#D99A1E]/30 uppercase tracking-wider">
                          Held
                        </span>
                      )}
                      {selectedClip.rights_status === 'needs_check' && (
                        <span className="font-ui text-[11px] font-bold px-[8px] py-[3px] rounded-[6px] bg-[#3B82F6]/15 text-[#3B82F6] border border-[#3B82F6]/30 uppercase tracking-wider">
                          Needs Check
                        </span>
                      )}
                      <StatusPill status={selectedClip.status} />
                    </div>
                    <h3 className="font-heading font-extrabold text-[18px] text-[#1D3B33] mt-[6px] line-clamp-2 leading-tight">
                      {stripHashtags(selectedClip.title)}
                    </h3>
                    <p className="font-ui text-[12.5px] text-[#6F7A73] mt-[4px]">
                      Uploader credit: <span className="font-medium text-[#1D3B33]">{selectedClip.uploader}</span>
                    </p>
                  </div>
                </div>

                {/* 9:16 Video Player Area */}
                <div className="w-full aspect-[9/16] max-h-[500px] mx-auto rounded-[12px] bg-[#1D3B33] overflow-hidden relative shadow-inner flex items-center justify-center">
                  {/* If public on YouTube and embed didn't fail */}
                  {selectedClip.video_id && (selectedClip.status === 'Published' || selectedClip.status === 'On air') && !embedFailed ? (
                    <iframe
                      title={stripHashtags(selectedClip.title)}
                      src={`https://www.youtube.com/embed/${selectedClip.video_id}?autoplay=0&rel=0`}
                      className="w-full h-full border-0"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                      allowFullScreen
                      onError={() => setEmbedFailed(true)}
                    />
                  ) : selectedClip.has_local_file ? (
                    /* Local HTML5 video with Range stream support */
                    <video
                      ref={videoRef}
                      controls
                      playsInline
                      preload="metadata"
                      poster={videoPosterUrl || undefined}
                      src={videoStreamUrl || undefined}
                      onError={handleVideoError}
                      className="w-full h-full object-contain"
                    >
                      Your browser does not support the video tag.
                    </video>
                  ) : (
                    /* No stream available fallback */
                    <div className="flex flex-col items-center justify-center p-[20px] text-center text-white/80">
                      <Play className="size-[36px] text-white/40 mb-[10px]" />
                      <p className="font-ui text-[13px] font-medium">Local clip file not found on disk</p>
                      <p className="font-ui text-[11px] text-white/50 mt-[4px]">Post ID: {selectedClip.post_id}</p>
                    </div>
                  )}
                </div>

                {/* Status details & metrics */}
                <div className="p-[14px] rounded-[10px] bg-[#EDF0EC] text-[#1D3B33] font-ui text-[13px] flex flex-col gap-[4px]">
                  <p className="font-medium">{selectedClip.status_detail}</p>
                  {selectedClip.claim_status === 'claimed' && (
                    <p className="text-[12px] text-[#D4412F] font-semibold">
                      Content ID claim detected. Visit the Claims tab to review claimant or take action.
                    </p>
                  )}
                  {selectedClip.views && (
                    <p className="text-[12px] text-[#6F7A73]">
                      Live views: {Number(selectedClip.views).toLocaleString()} · Likes: {selectedClip.likes ? Number(selectedClip.likes).toLocaleString() : '—'}
                    </p>
                  )}
                </div>

                {/* Action buttons (Open on YouTube / Open in Studio) */}
                <div className="grid grid-cols-2 gap-[12px]">
                  <a
                    href={selectedClip.youtube_url || '#'}
                    target="_blank"
                    rel="noreferrer"
                    aria-disabled={!selectedClip.youtube_url}
                    className={`px-[14px] py-[10px] rounded-[10px] font-ui text-[13px] font-semibold flex items-center justify-center gap-[6px] transition-colors
                      ${selectedClip.youtube_url
                        ? 'bg-[#1D3B33] text-white hover:bg-[#2A4F43]'
                        : 'bg-[#EDF0EC] text-[#6F7A73] opacity-50 cursor-not-allowed pointer-events-none'}`}
                  >
                    <span>Open on YouTube</span>
                    <ExternalLink className="size-[13px]" />
                  </a>

                  <a
                    href={selectedClip.studio_url || '#'}
                    target="_blank"
                    rel="noreferrer"
                    aria-disabled={!selectedClip.studio_url}
                    className={`px-[14px] py-[10px] rounded-[10px] border border-[#D5DBD5] font-ui text-[13px] font-medium flex items-center justify-center gap-[6px] transition-colors
                      ${selectedClip.studio_url
                        ? 'bg-white text-[#1D3B33] hover:bg-[#EDF0EC]'
                        : 'bg-white text-[#6F7A73] opacity-50 cursor-not-allowed pointer-events-none'}`}
                  >
                    <span>Open in Studio</span>
                    <ExternalLink className="size-[13px]" />
                  </a>
                </div>
              </>
            ) : (
              <div className="py-[60px] text-center text-[#6F7A73] font-ui text-[14px]">
                Select a video to view player and metrics.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
