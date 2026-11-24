import React, { useState, useEffect } from 'react';
import {
  ShieldAlert,
  ShieldCheck,
  AlertTriangle,
  AlertCircle,
  ExternalLink,
  CheckCircle2,
  RefreshCw,
  Plus,
  X,
  Info,
  Mail,
  FileText,
  RotateCcw,
  Check,
} from 'lucide-react';
import { Thumb } from '../components/Thumb';
import { ClaimChip } from '../components/ClaimChip';
import { formatTimeIST, formatDateIST, formatTimeETHint } from '../utils/timeFormat';
import { stripHashtags } from '../utils/textFormat';
import { apiFetch } from '../utils/api';

interface RightsStatusData {
  is_local_only: boolean;
  unreviewed_policy: string;
  active_layers: Record<string, boolean>;
  providers?: {
    shazam?: {
      configured?: boolean;
      calls_today?: number;
      errors_today?: number;
      daily_cap?: number;
      status?: string;
      error_message?: string;
    };
    google_vision?: {
      configured?: boolean;
      calls_today?: number;
      errors_today?: number;
      daily_cap?: number;
      monthly_free_tier?: number;
    };
  };
  stats: {
    total_claims: number;
    total_published: number;
    claims_per_100_published: number;
    claims_by_subreddit: Record<string, number>;
  };
}

interface ClaimsSyncStatus {
  gmail_connected: boolean;
  gmail_error?: string | null;
  last_sync_at?: string | null;
  processed_count: number;
  unmatched_count: number;
  dry_run: boolean;
  auto_action_scheduled: string;
  auto_action_public: string;
  max_auto_actions_per_day: number;
  today_actions_count: number;
}

interface ClaimActionRecord {
  id: number;
  claim_id?: number;
  post_id: string;
  video_id?: string;
  action_type: string;
  reason: string;
  dry_run: number;
  result: string;
  created_at: string;
}

interface UnmatchedEmailRecord {
  message_id: string;
  received_at: string;
  subject: string;
  sender: string;
  raw_snippet: string;
}

interface ClaimRecord {
  id: number;
  video_id?: string;
  post_id: string;
  claim_type: string;
  claimant: string;
  asset_title?: string;
  track_title?: string;
  track_artist?: string;
  found_in?: string;
  action?: string;
  state?: string;
  source?: string;
  affects_reach?: number | boolean;
  detected_at?: string;
  released_at?: string;
  last_seen_at?: string;
  source_name?: string;
  uploader?: string;
  notes?: string;
  verify_in_studio_nudge?: boolean;
  confirm_release_prompt?: boolean;
}

interface ReviewClip {
  post_id: string;
  video_id?: string;
  target_date?: string;
  title: string;
  uploader?: string;
  slot_time?: string;
  rights_status?: string;
  rights_risk: number;
  decision: string;
  reasons: string[];
  layers: Record<string, any>;
  thumb_url: string;
  privacy?: string;
  claim_chip?: string;
  claim?: ClaimRecord;
}

interface PublishedClip {
  post_id: string;
  video_id?: string;
  target_date: string;
  slot_time?: string;
  title: string;
  privacy: string;
  live_privacy?: string;
  live_status?: string;
  is_missing?: boolean;
  claim_status: string;
  claim_chip?: string;
  claim_action?: string;
  claimant?: string;
  claim_type?: string;
  rights_status?: string;
  rights_risk: number;
  thumb_url: string;
  views?: number;
  likes?: number;
  claim?: ClaimRecord;
}

interface BlocklistData {
  active: Array<{ id: number; type: string; value: string; status: string }>;
  pending: Array<{ id: number; type: string; value: string; status: string }>;
  disabled: Array<{ id: number; type: string; value: string; status: string }>;
}

export const ClaimsView: React.FC = () => {
  const [status, setStatus] = useState<RightsStatusData | null>(null);
  const [syncStatus, setSyncStatus] = useState<ClaimsSyncStatus | null>(null);
  const [reviewClips, setReviewClips] = useState<ReviewClip[]>([]);
  const [publishedClips, setPublishedClips] = useState<PublishedClip[]>([]);
  const [claims, setClaims] = useState<ClaimRecord[]>([]);
  const [claimActions, setClaimActions] = useState<ClaimActionRecord[]>([]);
  const [blocklist, setBlocklist] = useState<BlocklistData>({ active: [], pending: [], disabled: [] });

  const [, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: 'success' | 'error' } | null>(null);

  // Modals state
  const [claimModalClip, setClaimModalClip] = useState<ReviewClip | PublishedClip | null>(null);
  const [claimForm, setClaimForm] = useState({
    claim_type: 'audio',
    claimant: '',
    asset_title: '',
    found_in: '',
    action: 'held',
    notes: '',
  });

  const [replaceModalClip, setReplaceModalClip] = useState<ReviewClip | null>(null);
  const [replaceAction, setReplaceAction] = useState<'held' | 'delete'>('held');
  const [deleteConfirmationText, setDeleteConfirmationText] = useState('');

  const [deleteModalClip, setDeleteModalClip] = useState<PublishedClip | null>(null);
  const [deleteModalConfirm, setDeleteModalConfirm] = useState('');

  const [newBlockType, setNewBlockType] = useState('text');
  const [newBlockValue, setNewBlockValue] = useState('');

  // Phase 6.1 Studio Paste Modal state
  const [studioPasteModalOpen, setStudioPasteModalOpen] = useState(false);
  const [studioPasteText, setStudioPasteText] = useState('');
  const [studioParsed, setStudioParsed] = useState<any>(null);
  const [studioTargetPostId, setStudioTargetPostId] = useState('');
  const [parsingStudio, setParsingStudio] = useState(false);

  // Phase 6.1 Unmatched Emails Modal
  const [unmatchedModalOpen, setUnmatchedModalOpen] = useState(false);
  const [unmatchedList, setUnmatchedList] = useState<UnmatchedEmailRecord[]>([]);

  const loadAllData = async () => {
    try {
      setLoading(true);
      const [resStatus, resSync, resReview, resPub, resClaims, resActions, resBlock] = await Promise.all([
        apiFetch('/api/rights/status').then((r) => r.json()),
        apiFetch('/api/rights/claims-sync-status').then((r) => r.json()),
        apiFetch('/api/rights/review').then((r) => r.json()),
        apiFetch('/api/rights/published').then((r) => r.json()),
        apiFetch('/api/rights/claims').then((r) => r.json()),
        apiFetch('/api/rights/claim-actions').then((r) => r.json()),
        apiFetch('/api/rights/blocklist').then((r) => r.json()),
      ]);

      setStatus(resStatus);
      setSyncStatus(resSync);
      setReviewClips(resReview.clips || []);
      setPublishedClips(resPub || []);
      setClaims(resClaims || []);
      setClaimActions(resActions || []);
      setBlocklist(resBlock || { active: [], pending: [], disabled: [] });
    } catch (err: any) {
      setMessage({ text: 'Failed to load claims data: ' + err.message, type: 'error' });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAllData();
  }, []);

  const handleSyncNow = async () => {
    try {
      setSyncing(true);
      const res = await apiFetch('/api/rights/claims-sync/trigger', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || 'Sync failed');

      setMessage({
        text: `Sync completed! Processed: ${data.processed_count || 0}, Unmatched: ${data.unmatched_count || 0}, Missing videos: ${data.missing_videos_detected || 0}`,
        type: 'success',
      });
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setSyncing(false);
    }
  };

  const handleToggleDryRun = async (newVal: boolean) => {
    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/claims-sync/toggle-dry-run', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({ dry_run: newVal }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Toggle failed');

      setMessage({
        text: `Dry run mode is now ${data.dry_run ? 'ON (Simulated Deletions)' : 'OFF (Live YouTube Deletions Enabled)'}`,
        type: 'success',
      });
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleParseStudioPaste = async () => {
    if (!studioPasteText.trim()) return;
    try {
      setParsingStudio(true);
      const res = await apiFetch('/api/rights/parse-studio-paste', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: studioPasteText }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Parse failed');
      setStudioParsed(data.parsed);
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setParsingStudio(false);
    }
  };

  const handleSaveStudioClaim = async () => {
    if (!studioTargetPostId) {
      alert('Please select a target clip for this claim.');
      return;
    }
    try {
      setActionLoading(true);
      const payload = {
        post_id: studioTargetPostId,
        asset_title: studioParsed?.asset_title || 'Unknown Asset',
        track_artist: studioParsed?.track_artist,
        claimant: studioParsed?.claimant || 'Unknown Claimant',
        claim_type: studioParsed?.claim_type || 'audio',
        affects_reach: Boolean(studioParsed?.affects_reach),
        notes: 'Pasted from YouTube Studio',
      };
      const res = await apiFetch('/api/rights/save-manual-claim', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Failed to save studio claim');

      setMessage({ text: 'Manual claim successfully saved from Studio text!', type: 'success' });
      setStudioPasteModalOpen(false);
      setStudioPasteText('');
      setStudioParsed(null);
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleMarkClaimReleased = async (claimId?: number, postId?: string) => {
    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/mark-claim-released', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({ claim_id: claimId, post_id: postId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Failed to mark claim released');

      setMessage({ text: 'Claim marked as released!', type: 'success' });
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleUndoAutoHold = async (postId: string) => {
    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/undo-auto-hold', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({ post_id: postId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Undo auto-hold failed');

      setMessage({
        text: `Auto-hold undone for ${postId}! Restored scheduled publish at ${data.publish_at || 'next free slot'}`,
        type: 'success',
      });
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleOpenUnmatchedModal = async () => {
    try {
      const res = await apiFetch('/api/rights/claim-emails/unmatched');
      const data = await res.json();
      setUnmatchedList(data || []);
      setUnmatchedModalOpen(true);
    } catch (err: any) {
      setMessage({ text: 'Failed to load unmatched emails: ' + err.message, type: 'error' });
    }
  };

  const handleMarkClear = async (postId: string) => {
    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/mark-clear', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({ post_id: postId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Mark clear failed');

      setMessage({ text: `Clip ${postId} cleared and scheduled for ${data.publish_at || 'next slot'}`, type: 'success' });
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleHold = async (postId: string, videoId?: string) => {
    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/hold', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({ post_id: postId, video_id: videoId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Hold failed');

      setMessage({ text: `Clip ${postId} set to private with no publishAt (held)`, type: 'success' });
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleSaveClaim = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!claimModalClip) return;
    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/mark-claimed', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({
          post_id: claimModalClip.post_id,
          video_id: claimModalClip.video_id,
          ...claimForm,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Failed to save claim');

      setMessage({
        text: `Claim registered! Generated ${data.suggestions_created} blocklist suggestions for review.`,
        type: 'success',
      });
      setClaimModalClip(null);
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleReplaceFromReserve = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!replaceModalClip) return;
    if (replaceAction === 'delete' && deleteConfirmationText !== 'DELETE') {
      alert("Must type 'DELETE' to confirm deletion.");
      return;
    }

    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/replace-from-reserve', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({
          post_id: replaceModalClip.post_id,
          action_on_claimed: replaceAction,
          delete_confirmation: deleteConfirmationText,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Replacement failed');

      setMessage({
        text: `Successfully replaced ${data.replaced_post_id} with reserve clip ${data.new_post_id}!`,
        type: 'success',
      });
      setReplaceModalClip(null);
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handlePublishedAction = async (postId: string, videoId: string | undefined, action: string) => {
    try {
      setActionLoading(true);
      const res = await apiFetch('/api/rights/action-published', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dashboard-Request': '1',
        },
        body: JSON.stringify({
          post_id: postId,
          video_id: videoId,
          action,
          delete_confirmation: deleteModalConfirm,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Action failed');

      const actionMsg = action === 'remove_record'
        ? `Record ${postId} removed from database`
        : action === 'delete'
        ? `Video ${postId} deleted from YouTube and removed from database`
        : `Action '${action}' applied to ${postId}`;
      setMessage({ text: actionMsg, type: 'success' });
      setDeleteModalClip(null);
      setDeleteModalConfirm('');
      await loadAllData();
    } catch (err: any) {
      setMessage({ text: err.message, type: 'error' });
    } finally {
      setActionLoading(false);
    }
  };

  const handleBlocklistApprove = async (id: number) => {
    await apiFetch(`/api/rights/blocklist/${id}/approve`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Dashboard-Request': '1',
      },
    });
    await loadAllData();
  };

  const handleBlocklistDismiss = async (id: number) => {
    await apiFetch(`/api/rights/blocklist/${id}/dismiss`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Dashboard-Request': '1',
      },
    });
    await loadAllData();
  };

  const handleBlocklistToggle = async (id: number) => {
    await apiFetch(`/api/rights/blocklist/${id}/toggle`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Dashboard-Request': '1',
      },
    });
    await loadAllData();
  };

  const handleAddManualBlocklist = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newBlockValue.trim()) return;
    await apiFetch('/api/rights/blocklist/add', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Dashboard-Request': '1',
      },
      body: JSON.stringify({ type: newBlockType, value: newBlockValue.trim() }),
    });
    setNewBlockValue('');
    await loadAllData();
  };

  // Find car clip confirmation prompt
  const carClipPending = claims.find((c) => c.confirm_release_prompt || c.state === 'unknown_pending_confirmation');

  return (
    <div className="flex-1 flex flex-col gap-[28px] p-[20px] lg:p-[36px] max-w-[1440px] mx-auto w-full">
      {/* View Header */}
      <div className="flex flex-col gap-[6px]">
        <div className="flex items-center gap-[12px]">
          <div className="size-[36px] rounded-[10px] bg-[#D4412F] flex items-center justify-center text-white shadow-sm">
            <ShieldAlert size={20} />
          </div>
          <h1 className="font-heading font-extrabold text-[28px] lg:text-[32px] text-[#1D3B33] tracking-tight">
            Rights Check & Claims
          </h1>
          {status?.is_local_only && (
            <span className="font-ui text-[12px] font-bold px-[8px] py-[3px] rounded-full bg-[#D97706]/15 text-[#D97706] border border-[#D97706]/30">
              Local-Only Protection
            </span>
          )}
        </div>
        <p className="font-ui text-[15px] text-[#6F7A73]">
          Pre-upload risk detection (5 layers), automated Gmail claim sync, YouTube scheduled review gate, and learned Content ID memory.
        </p>
      </div>

      {message && (
        <div
          className={`p-[14px] rounded-[12px] font-ui text-[14px] flex items-center justify-between border ${
            message.type === 'success'
              ? 'bg-[#2E7D4F]/10 text-[#2E7D4F] border-[#2E7D4F]/30'
              : 'bg-[#D4412F]/10 text-[#D4412F] border-[#D4412F]/30'
          }`}
        >
          <span>{message.text}</span>
          <button onClick={() => setMessage(null)} className="cursor-pointer">
            <X size={16} />
          </button>
        </div>
      )}

      {/* ONE-TIME PROMPT: Car Clip Release Prompt */}
      {carClipPending && (
        <div className="p-[16px] rounded-[14px] bg-[#FEF3C7] border border-[#F59E0B]/50 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-[14px] text-[#92400E] shadow-sm animate-fade-in">
          <div className="flex items-center gap-[12px]">
            <AlertTriangle className="size-[22px] text-[#D97706] shrink-0" />
            <div>
              <p className="font-heading font-semibold text-[15px]">
                Studio shows no claim, mark released?
              </p>
              <p className="font-ui text-[13px] text-[#B45309]">
                Clip &quot;My Car Is Playing A Cooking Channel Instead Of Reversing&quot; (video <code>xwJJMYnUgSY</code>, post <code>{carClipPending.post_id}</code>) shows no active claim in YouTube Studio.
              </p>
            </div>
          </div>
          <button
            onClick={() => handleMarkClaimReleased(carClipPending.id, carClipPending.post_id)}
            disabled={actionLoading}
            className="px-[16px] py-[8px] rounded-[8px] bg-[#2E7D4F] text-white font-ui text-[13px] font-bold hover:bg-[#256841] transition-all cursor-pointer shrink-0 shadow-sm flex items-center gap-[6px]"
          >
            <Check size={16} /> Mark Released
          </button>
        </div>
      )}

      {/* SECTION 0: CLAIMS SYNC CARD (GMAIL & AUTOMATIC POLICY) */}
      <div className="bg-white rounded-[16px] p-[20px] lg:p-[28px] border border-[#D5DBD5]/60 flex flex-col gap-[20px] shadow-sm">
        <div className="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-[16px]">
          <div>
            <div className="flex items-center gap-[10px]">
              <div className="p-1.5 rounded-lg bg-[#1D3B33]/10 text-[#1D3B33]">
                <Mail size={18} />
              </div>
              <h2 className="font-heading font-semibold text-[20px] text-[#1D3B33]">
                Claims Sync (Gmail API)
              </h2>
              {syncStatus?.gmail_connected ? (
                <span className="font-ui text-[12px] font-bold px-[8px] py-[3px] rounded-full bg-[#2E7D4F]/15 text-[#2E7D4F] border border-[#2E7D4F]/30 flex items-center gap-[4px]">
                  <span className="size-2 rounded-full bg-[#2E7D4F] animate-pulse" /> Connected
                </span>
              ) : (
                <span className="font-ui text-[12px] font-bold px-[8px] py-[3px] rounded-full bg-[#D4412F]/15 text-[#D4412F] border border-[#D4412F]/30 flex items-center gap-[4px]">
                  <span className="size-2 rounded-full bg-[#D4412F]" /> Disconnected / Revoked
                </span>
              )}
            </div>
            <p className="font-ui text-[13px] text-[#6F7A73] mt-[4px]">
              Read-only Content ID detection from YouTube emails, automatic safe action policy, and Studio paste fallback.
            </p>
          </div>

          {/* Sync actions */}
          <div className="flex flex-wrap items-center gap-[10px]">
            <button
              onClick={() => {
                setStudioTargetPostId(reviewClips[0]?.post_id || publishedClips[0]?.post_id || '');
                setStudioPasteModalOpen(true);
              }}
              className="px-[14px] py-[8px] rounded-[10px] border border-[#D5DBD5] font-ui text-[13px] font-semibold text-[#1D3B33] bg-white hover:bg-gray-50 flex items-center gap-[6px] cursor-pointer shadow-xs"
            >
              <FileText size={15} /> Paste Claim from Studio
            </button>

            <button
              onClick={() => handleToggleDryRun(!syncStatus?.dry_run)}
              disabled={actionLoading}
              className={`px-[14px] py-[8px] rounded-[10px] font-ui text-[13px] font-bold border transition-colors cursor-pointer shadow-xs ${
                syncStatus?.dry_run
                  ? 'bg-[#2E7D4F]/10 border-[#2E7D4F]/40 text-[#2E7D4F] hover:bg-[#2E7D4F]/20'
                  : 'bg-[#D4412F]/10 border-[#D4412F]/40 text-[#D4412F] hover:bg-[#D4412F]/20'
              }`}
            >
              {syncStatus?.dry_run ? 'Dry-Run: ON (Simulated)' : 'Dry-Run: OFF (Live Deletions)'}
            </button>

            <button
              onClick={handleSyncNow}
              disabled={syncing}
              className="px-[16px] py-[8px] rounded-[10px] bg-[#1D3B33] font-ui text-[13px] font-semibold text-white hover:bg-[#2A4F43] disabled:opacity-50 flex items-center gap-[6px] cursor-pointer shadow-xs"
            >
              <RefreshCw size={15} className={syncing ? 'animate-spin' : ''} />
              {syncing ? 'Syncing...' : 'Sync Now'}
            </button>
          </div>
        </div>

        {/* Sync Status Metrics Grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-[12px] pt-[4px]">
          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[2px]">
            <span className="font-ui text-[11px] uppercase tracking-wider text-[#6F7A73] font-semibold">Last Synced</span>
            <span className="font-heading font-bold text-[16px] text-[#1D3B33]">
              {syncStatus?.last_sync_at ? formatTimeIST(syncStatus.last_sync_at) : 'Never'}
            </span>
          </div>

          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[2px]">
            <span className="font-ui text-[11px] uppercase tracking-wider text-[#6F7A73] font-semibold">Emails Processed</span>
            <span className="font-heading font-bold text-[16px] text-[#1D3B33]">
              {syncStatus?.processed_count ?? 0}
            </span>
          </div>

          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[2px]">
            <span className="font-ui text-[11px] uppercase tracking-wider text-[#6F7A73] font-semibold">Unmatched Emails</span>
            <div className="flex items-center justify-between">
              <span className="font-heading font-bold text-[16px] text-[#1D3B33]">
                {syncStatus?.unmatched_count ?? 0}
              </span>
              {(syncStatus?.unmatched_count ?? 0) > 0 && (
                <button
                  onClick={handleOpenUnmatchedModal}
                  className="text-[11px] font-semibold text-[#2E7D4F] hover:underline cursor-pointer"
                >
                  View
                </button>
              )}
            </div>
          </div>

          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[2px]">
            <span className="font-ui text-[11px] uppercase tracking-wider text-[#6F7A73] font-semibold">Today&apos;s Auto Actions</span>
            <span className="font-heading font-bold text-[16px] text-[#1D3B33]">
              {syncStatus?.today_actions_count ?? 0} / {syncStatus?.max_auto_actions_per_day ?? 6}
            </span>
          </div>
        </div>
      </div>

      {/* SECTION 1: Protection Status Card */}
      <div className="bg-white rounded-[16px] p-[20px] lg:p-[28px] border border-[#D5DBD5]/40 flex flex-col gap-[20px] shadow-sm">
        <div className="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-[16px]">
          <div>
            <h2 className="font-heading font-semibold text-[20px] text-[#1D3B33]">
              Protection Status & Limits
            </h2>
            <p className="font-ui text-[13px] text-[#6F7A73]">
              Multi-layer detection status, daily provider quotas, and historical claim frequency.
            </p>
          </div>
          <div className="flex items-center gap-[20px]">
            <div className="text-right">
              <span className="font-heading font-bold text-[24px] text-[#1D3B33] leading-none">
                {status?.stats.claims_per_100_published ?? 0}%
              </span>
              <p className="font-ui text-[12px] text-[#6F7A73]">claims / 100 published</p>
            </div>
            <div className="h-[32px] w-px bg-[#D5DBD5]" />
            <div className="text-right">
              <span className="font-heading font-bold text-[24px] text-[#D4412F] leading-none">
                {status?.stats.total_claims ?? 0}
              </span>
              <p className="font-ui text-[12px] text-[#6F7A73]">total claims noted</p>
            </div>
          </div>
        </div>

        {status?.is_local_only && (
          <div className="p-[14px] rounded-[12px] bg-[#D97706]/10 border border-[#D97706]/30 flex items-start gap-[12px]">
            <Info className="text-[#D97706] shrink-0 mt-0.5" size={18} />
            <div className="font-ui text-[13px] text-[#1D3B33] leading-relaxed">
              <strong>Reduced Protection (Local Only Mode):</strong> Cloud Vision web detection and paid API calls are
              disabled ($0.00 spend). Local OCR (RapidOCR), YAMNet music detection, and historical perceptual hash matching
              are fully active.
            </div>
          </div>
        )}

        {(status?.providers?.shazam?.status === "error" || (status?.providers?.shazam?.errors_today ?? 0) > 0 || status?.providers?.shazam?.error_message) && (
          <div className="p-[14px] rounded-[12px] bg-[#FEE2E2] border border-[#DC2626]/30 flex items-start gap-[12px]">
            <AlertCircle className="text-[#DC2626] shrink-0 mt-0.5" size={18} />
            <div className="font-ui text-[13px] text-[#991B1B] leading-relaxed">
              <strong>Shazam Provider Error:</strong> Most of today's track-recognition requests failed (Shazam may be rate-limiting or blocking this connection). Track fingerprinting is paused; music-heavy clips are routed to review with <code className="px-1.5 py-0.5 bg-white/70 rounded text-[12px] font-mono">provider_error_review</code>.
            </div>
          </div>
        )}

        {/* Layer Badges & Daily Caps */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-[14px]">
          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[6px]">
            <span className="font-ui text-[12px] text-[#6F7A73] font-medium">Layer 1: Agency Blocklist & OCR</span>
            <div className="flex items-center gap-[6px] text-[#2E7D4F] font-semibold text-[14px]">
              <CheckCircle2 size={16} /> Active (Local RapidOCR)
            </div>
          </div>

          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[6px]">
            <span className="font-ui text-[12px] text-[#6F7A73] font-medium">Layer 2: Shazam Track Fingerprint</span>
            <div className="flex items-center justify-between">
              <span className={`font-semibold text-[14px] ${status?.providers?.shazam?.status === 'error' ? 'text-[#DC2626]' : 'text-[#1D3B33]'}`}>
                {status?.providers?.shazam?.status === 'error' ? 'Invalid Token' : status?.providers?.shazam?.configured ? 'Active' : 'Not Configured'}
              </span>
              <span className="font-ui text-[12px] text-[#6F7A73]">
                {status?.providers?.shazam?.calls_today ?? 0} / {status?.providers?.shazam?.daily_cap ?? 40}
              </span>
            </div>
          </div>

          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[6px]">
            <span className="font-ui text-[12px] text-[#6F7A73] font-medium">Layer 3: YAMNet / Cloud Vision</span>
            <div className="flex items-center justify-between">
              <span className="font-semibold text-[14px] text-[#1D3B33]">Local YAMNet</span>
              <span className="font-ui text-[12px] text-[#6F7A73]">
                {status?.providers?.google_vision?.calls_today ?? 0} / {status?.providers?.google_vision?.daily_cap ?? 30}
              </span>
            </div>
          </div>

          <div className="p-[14px] rounded-[12px] bg-[#EDF0EC] flex flex-col gap-[6px]">
            <span className="font-ui text-[12px] text-[#6F7A73] font-medium">Layer 5: Claimed Hash Memory</span>
            <div className="flex items-center gap-[6px] text-[#2E7D4F] font-semibold text-[14px]">
              <CheckCircle2 size={16} /> Active ({claims.length} learned)
            </div>
          </div>
        </div>
      </div>

      {/* SECTION 2: Needs Review Queue */}
      <div className="bg-white rounded-[16px] p-[20px] lg:p-[28px] border border-[#D5DBD5]/40 flex flex-col gap-[16px] shadow-sm">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="font-heading font-semibold text-[20px] text-[#1D3B33]">
              Needs Review ({reviewClips.length})
            </h2>
            <p className="font-ui text-[13px] text-[#6F7A73]">
              All scheduled clips not yet public (today, tomorrow, upcoming). Review Content ID risk, hold, clear, or replace before airtime.
            </p>
          </div>
          <button
            onClick={loadAllData}
            className="flex items-center gap-[6px] px-[12px] py-[6px] rounded-[8px] border border-[#D5DBD5] font-ui text-[13px] text-[#1D3B33] hover:bg-gray-50 cursor-pointer"
          >
            <RefreshCw size={14} /> Refresh
          </button>
        </div>

        <div className="flex flex-col divide-y divide-[#D5DBD5]">
          {reviewClips.length === 0 ? (
            <div className="py-[32px] text-center text-[#6F7A73] font-ui text-[14px]">
              No scheduled clips pending review.
            </div>
          ) : (
            reviewClips.map((clip) => {
              const isHigh = clip.rights_risk >= 80 || clip.decision === 'block';
              const isReview = clip.rights_risk >= 30 && clip.rights_risk < 80;

              return (
                <div key={clip.post_id} className="py-[16px] flex flex-col gap-[12px]">
                  <div className="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-[16px]">
                    <div className="flex items-center gap-[16px]">
                      <div className="flex flex-col text-left shrink-0 min-w-[125px]">
                        <span className="font-heading font-medium text-[14px] text-[#1D3B33] leading-tight">
                          {formatTimeIST(clip.slot_time, { referenceDate: clip.target_date, showNextDay: true })}
                        </span>
                        {formatTimeETHint(clip.slot_time) && (
                          <span className="font-mono text-[11px] text-[#8A958E] leading-tight mt-[1px]">
                            ({formatTimeETHint(clip.slot_time)})
                          </span>
                        )}
                        <span className="font-ui text-[11px] text-[#6F7A73] mt-[2px]">
                          {clip.target_date ? `Batch of ${formatDateIST(clip.target_date, { day: 'numeric', month: 'short' })} (US day)` : ''}
                        </span>
                      </div>
                      <Thumb src={clip.thumb_url} alt={stripHashtags(clip.title)} />
                      <div className="flex flex-col gap-[3px]">
                        <div className="flex items-center gap-[8px] flex-wrap">
                          <span className="font-ui font-semibold text-[15px] text-[#1D3B33]">
                            {stripHashtags(clip.title)}
                          </span>
                          <ClaimChip status={clip.claim_chip || (clip.claim ? clip.claim.state : clip.rights_status)} />
                        </div>
                        <div className="flex items-center gap-[8px] font-ui text-[12px] text-[#6F7A73]">
                          <span>{clip.post_id}</span>
                          <span>·</span>
                          <span>u/{clip.uploader || 'unknown'}</span>
                          <span>·</span>
                          {clip.rights_status === 'cleared' ? (
                            <span className="text-[#2E7D4F] font-semibold">Cleared</span>
                          ) : clip.rights_status === 'held' ? (
                            <span className="text-[#D97706] font-semibold">Held (Private)</span>
                          ) : (
                            <span className="text-[#D4412F] font-semibold">Needs Check</span>
                          )}
                        </div>
                      </div>
                    </div>

                    {/* Risk Badge & Actions */}
                    <div className="flex flex-wrap items-center gap-[10px] w-full lg:w-auto justify-end">
                      {isHigh ? (
                        <span className="px-[10px] py-[3px] rounded-full text-[12px] font-bold bg-[#D4412F]/15 text-[#D4412F] border border-[#D4412F]/30 flex items-center gap-[4px]">
                          <AlertTriangle size={13} /> High Risk ({clip.rights_risk})
                        </span>
                      ) : isReview ? (
                        <span className="px-[10px] py-[3px] rounded-full text-[12px] font-bold bg-[#D97706]/15 text-[#D97706] border border-[#D97706]/30 flex items-center gap-[4px]">
                          <ShieldAlert size={13} /> Needs Review ({clip.rights_risk})
                        </span>
                      ) : (
                        <span className="px-[10px] py-[3px] rounded-full text-[12px] font-bold bg-[#2E7D4F]/15 text-[#2E7D4F] border border-[#2E7D4F]/30 flex items-center gap-[4px]">
                          <ShieldCheck size={13} /> Cleared ({clip.rights_risk})
                        </span>
                      )}

                      {clip.video_id && (
                        <a
                          href={`https://studio.youtube.com/video/${clip.video_id}/edit`}
                          target="_blank"
                          rel="noreferrer"
                          className="flex items-center gap-[4px] px-[10px] py-[6px] rounded-[8px] border border-[#D5DBD5] text-[#1D3B33] text-[12px] font-semibold hover:bg-gray-50"
                        >
                          Studio <ExternalLink size={12} />
                        </a>
                      )}

                      {clip.rights_status === 'held' && (
                        <button
                          onClick={() => handleUndoAutoHold(clip.post_id)}
                          disabled={actionLoading}
                          className="px-[10px] py-[6px] rounded-[8px] bg-blue-600 text-white text-[12px] font-semibold hover:bg-blue-700 transition-colors cursor-pointer flex items-center gap-[4px]"
                        >
                          <RotateCcw size={13} /> Undo Hold
                        </button>
                      )}

                      <button
                        onClick={() => handleMarkClear(clip.post_id)}
                        disabled={actionLoading}
                        className="px-[10px] py-[6px] rounded-[8px] bg-[#2E7D4F] text-white text-[12px] font-semibold hover:bg-[#256841] transition-colors cursor-pointer"
                      >
                        Mark Clear
                      </button>

                      <button
                        onClick={() => handleHold(clip.post_id, clip.video_id)}
                        disabled={actionLoading}
                        className="px-[10px] py-[6px] rounded-[8px] bg-[#D97706]/15 border border-[#D97706]/40 text-[#D97706] text-[12px] font-semibold hover:bg-[#D97706]/25 transition-colors cursor-pointer"
                      >
                        Hold
                      </button>

                      {(clip.claim_chip === 'claimed' || clip.claim?.state === 'active') && (
                        <button
                          onClick={() => handleMarkClaimReleased(clip.claim?.id, clip.post_id)}
                          disabled={actionLoading}
                          className="px-[10px] py-[6px] rounded-[8px] bg-gray-200 text-[#1D3B33] text-[12px] font-semibold hover:bg-gray-300 transition-colors cursor-pointer"
                        >
                          Mark Released
                        </button>
                      )}

                      <button
                        onClick={() => {
                          setClaimModalClip(clip);
                          setClaimForm({
                            claim_type: 'visual',
                            claimant: '',
                            asset_title: '',
                            found_in: '0:02 - 0:18',
                            action: 'held',
                            notes: '',
                          });
                        }}
                        className="px-[10px] py-[6px] rounded-[8px] bg-[#D4412F]/10 border border-[#D4412F]/30 text-[#D4412F] text-[12px] font-semibold hover:bg-[#D4412F]/20 transition-colors cursor-pointer"
                      >
                        Mark Claimed
                      </button>

                      <button
                        onClick={() => {
                          setReplaceModalClip(clip);
                          setReplaceAction('held');
                          setDeleteConfirmationText('');
                        }}
                        className="px-[10px] py-[6px] rounded-[8px] bg-[#1D3B33] text-white text-[12px] font-semibold hover:bg-[#132722] transition-colors cursor-pointer"
                      >
                        Replace from Reserve
                      </button>
                    </div>
                  </div>

                  {/* Claim Details if present */}
                  {clip.claim && (
                    <div className="ml-[102px] p-[10px] rounded-[8px] bg-red-50 border border-red-200 text-[12px] text-[#1D3B33] flex flex-wrap items-center gap-[12px]">
                      <span className="font-bold text-[#D4412F]">Claim Details:</span>
                      <span>Asset: <strong>{clip.claim.asset_title || clip.claim.track_title || 'N/A'}</strong></span>
                      {clip.claim.track_artist && <span>Artist: <strong>{clip.claim.track_artist}</strong></span>}
                      <span>Claimant: <strong>{clip.claim.claimant}</strong></span>
                      <span>Type: <strong>{clip.claim.claim_type}</strong></span>
                      <span>Reach: <strong>{clip.claim.affects_reach ? 'Blocked / Restricted' : 'No Impact'}</strong></span>
                      <span className="text-[11px] text-[#6F7A73]">Source: {clip.claim.source || 'email'}</span>
                    </div>
                  )}

                  {/* Findings breakdown */}
                  {clip.reasons && clip.reasons.length > 0 && (
                    <div className="ml-[102px] p-[10px] rounded-[8px] bg-gray-50 border border-gray-200 text-[12px] text-[#1D3B33] flex flex-wrap gap-[8px]">
                      <span className="font-semibold text-[#D4412F]">Flags:</span>
                      {clip.reasons.map((r, i) => (
                        <span key={i} className="px-[6px] py-[1px] rounded bg-white border border-gray-300 font-mono text-[11px]">
                          {r}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* SECTION 3: Recently Published Queue */}
      <div className="bg-white rounded-[16px] p-[20px] lg:p-[28px] border border-[#D5DBD5]/40 flex flex-col gap-[16px] shadow-sm">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="font-heading font-semibold text-[20px] text-[#1D3B33]">
              Recently Published ({publishedClips.length})
            </h2>
            <p className="font-ui text-[13px] text-[#6F7A73]">
              Clips live or uploaded in the past 14 days. Monitor for post-publish Content ID claims.
            </p>
          </div>
        </div>

        <div className="flex flex-col divide-y divide-[#D5DBD5]">
          {publishedClips.length === 0 ? (
            <div className="py-[32px] text-center text-[#6F7A73] font-ui text-[14px]">
              No published clips recorded.
            </div>
          ) : (
            publishedClips.map((clip) => {
              const isClaimed = clip.claim_status === 'claimed' || clip.claim_chip === 'claimed';
              const isMissing = clip.is_missing || clip.live_status === 'missing' || clip.privacy === 'missing';

              return (
                <div key={clip.post_id} className={`py-[14px] flex flex-col gap-[8px] ${isMissing ? 'opacity-70 bg-gray-50/50 rounded-lg p-2' : ''}`}>
                  <div className="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-[16px]">
                    <div className="flex items-center gap-[16px]">
                      <Thumb src={clip.thumb_url} alt={stripHashtags(clip.title)} />
                      <div className="flex flex-col gap-[2px]">
                        <div className="flex items-center gap-[8px] flex-wrap">
                          <span className={`font-ui font-semibold text-[15px] ${isMissing ? 'line-through text-gray-500' : 'text-[#1D3B33]'}`}>
                            {stripHashtags(clip.title)}
                          </span>
                          <ClaimChip status={clip.claim_chip || clip.claim_status} />
                          {isMissing && (
                            <span className="px-[6px] py-[1px] rounded text-[10px] font-bold uppercase bg-red-100 text-red-700 border border-red-200">
                              Missing on YouTube
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-[8px] font-ui text-[12px] text-[#6F7A73]">
                          <span>{clip.target_date}</span>
                          <span>·</span>
                          <span>{clip.post_id}</span>
                          <span>·</span>
                          <span>Status: <strong>{clip.live_status || clip.privacy}</strong></span>
                        </div>
                      </div>
                    </div>

                    <div className="flex items-center gap-[10px]">
                      {clip.video_id && (
                        <a
                          href={`https://studio.youtube.com/video/${clip.video_id}/edit`}
                          target="_blank"
                          rel="noreferrer"
                          className="flex items-center gap-[4px] px-[10px] py-[6px] rounded-[8px] border border-[#D5DBD5] text-[#1D3B33] text-[12px] font-semibold hover:bg-gray-50"
                        >
                          Studio <ExternalLink size={12} />
                        </a>
                      )}

                      {isClaimed && (
                        <button
                          onClick={() => handleMarkClaimReleased(clip.claim?.id, clip.post_id)}
                          disabled={actionLoading}
                          className="px-[10px] py-[6px] rounded-[8px] bg-gray-200 text-[#1D3B33] text-[12px] font-semibold hover:bg-gray-300 transition-colors cursor-pointer"
                        >
                          Mark Released
                        </button>
                      )}

                      <button
                        onClick={() => handlePublishedAction(clip.post_id, clip.video_id, 'keep')}
                        disabled={actionLoading || isMissing}
                        className="px-[10px] py-[6px] rounded-[8px] border border-[#D5DBD5] text-[#1D3B33] text-[12px] font-semibold hover:bg-gray-50 transition-colors cursor-pointer"
                      >
                        Keep
                      </button>

                      <button
                        onClick={() => handlePublishedAction(clip.post_id, clip.video_id, 'make_private')}
                        disabled={actionLoading || isMissing || clip.privacy === 'private'}
                        className="px-[10px] py-[6px] rounded-[8px] bg-[#D97706]/15 border border-[#D97706]/40 text-[#D97706] text-[12px] font-semibold hover:bg-[#D97706]/25 transition-colors cursor-pointer"
                      >
                        Make Private
                      </button>

                      <button
                        onClick={() => {
                          setDeleteModalClip(clip);
                          setDeleteModalConfirm('');
                        }}
                        disabled={actionLoading}
                        className="px-[10px] py-[6px] rounded-[8px] bg-[#D4412F] text-white text-[12px] font-semibold hover:bg-[#b83424] transition-colors cursor-pointer"
                      >
                        {isMissing ? 'Remove Record' : 'Delete'}
                      </button>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* SECTION 4: Claims History & Learned Blocklist */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-[24px] items-start">
        {/* Left: Claims History */}
        <div className="bg-white rounded-[16px] p-[20px] lg:p-[28px] border border-[#D5DBD5]/40 flex flex-col gap-[16px] shadow-sm">
          <div className="flex items-center justify-between">
            <h3 className="font-heading font-semibold text-[20px] text-[#1D3B33]">
              Claims History ({claims.length})
            </h3>
          </div>
          <p className="font-ui text-[13px] text-[#6F7A73]">
            Content ID claims logged via Gmail sync and Studio.
          </p>

          <div className="flex flex-col divide-y divide-[#D5DBD5] max-h-[520px] overflow-y-auto">
            {claims.length === 0 ? (
              <div className="py-[24px] text-center text-[#6F7A73] text-[13px]">
                No claims recorded yet.
              </div>
            ) : (
              claims.map((c) => (
                <div key={c.id} className="py-[12px] flex flex-col gap-[6px]">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-[8px]">
                      <span className="font-ui font-semibold text-[14px] text-[#1D3B33]">
                        {c.claimant}
                      </span>
                      <ClaimChip status={c.state} size="sm" />
                      <span className="px-[6px] py-[1px] rounded text-[10px] font-bold uppercase bg-gray-100 text-gray-700">
                        {c.claim_type}
                      </span>
                      <span className="px-[6px] py-[1px] rounded text-[10px] font-medium bg-blue-50 text-blue-700">
                        {c.source || 'manual'}
                      </span>
                    </div>

                    <div className="flex items-center gap-[6px]">
                      {c.state === 'active' && (
                        <button
                          onClick={() => handleMarkClaimReleased(c.id, c.post_id)}
                          className="text-[11px] font-semibold text-[#2E7D4F] hover:underline cursor-pointer"
                        >
                          Mark Released
                        </button>
                      )}
                    </div>
                  </div>

                  <span className="font-ui text-[13px] text-[#1D3B33] font-medium">
                    {c.asset_title || c.track_title || 'Unnamed Asset'}
                    {c.track_artist && <span className="text-[#6F7A73] font-normal"> — {c.track_artist}</span>}
                  </span>

                  <div className="flex items-center gap-[10px] font-ui text-[11px] text-[#6F7A73]">
                    <span>Post: {c.post_id}</span>
                    <span>·</span>
                    <span>Detected: {c.detected_at ? formatTimeIST(c.detected_at) : 'N/A'}</span>
                    <span>·</span>
                    <span>Reach: {c.affects_reach ? 'Blocked' : 'No Impact'}</span>
                  </div>

                  {/* 7-DAY NUDGE: Verify in Studio */}
                  {c.verify_in_studio_nudge && (
                    <div className="p-[8px] rounded-[6px] bg-amber-50 border border-amber-300 text-[11px] text-amber-900 flex items-center justify-between">
                      <span><strong>Verify in Studio:</strong> Manual claim is older than 7 days. Confirm if still active.</span>
                      <button
                        onClick={() => handleMarkClaimReleased(c.id, c.post_id)}
                        className="font-bold underline cursor-pointer"
                      >
                        Studio shows clear
                      </button>
                    </div>
                  )}

                  {c.notes && (
                    <p className="font-ui text-[12px] text-[#D97706] italic">Note: {c.notes}</p>
                  )}
                </div>
              ))
            )}
          </div>

          {/* Collapsible Action Audit Log */}
          {claimActions.length > 0 && (
            <div className="pt-[12px] border-t border-[#D5DBD5]">
              <span className="font-ui text-[12px] font-bold text-[#6F7A73] uppercase tracking-wider">
                Recent Auto-Action Audit Log ({claimActions.length})
              </span>
              <div className="flex flex-col divide-y divide-gray-100 max-h-[160px] overflow-y-auto mt-[6px]">
                {claimActions.slice(0, 10).map((act) => (
                  <div key={act.id} className="py-[6px] flex items-center justify-between text-[11px] font-ui">
                    <div className="flex items-center gap-[6px]">
                      <span className={`px-[5px] py-[1px] rounded text-[9px] font-bold uppercase ${act.dry_run ? 'bg-amber-100 text-amber-800' : 'bg-red-100 text-red-800'}`}>
                        {act.dry_run ? 'Simulated' : 'Live'}
                      </span>
                      <span className="font-semibold text-[#1D3B33]">{act.action_type}</span>
                      <span className="text-[#6F7A73]">({act.post_id})</span>
                    </div>
                    <span className="text-[#6F7A73] font-mono">{act.created_at.split('T')[0]}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right: Learned Rights Blocklist */}
        <div className="bg-white rounded-[16px] p-[20px] lg:p-[28px] border border-[#D5DBD5]/40 flex flex-col gap-[16px] shadow-sm">
          <div>
            <h3 className="font-heading font-semibold text-[20px] text-[#1D3B33]">
              Learned Rights Blocklist ({blocklist.active.length} active)
            </h3>
            <p className="font-ui text-[13px] text-[#6F7A73]">
              Keywords, claimants, tracks, uploaders, and subreddits learned from past claims.
            </p>
          </div>

          {/* Pending confirmation suggestions banner */}
          {blocklist.pending.length > 0 && (
            <div className="p-[14px] rounded-[12px] bg-[#D97706]/10 border border-[#D97706]/30 flex flex-col gap-[10px]">
              <span className="font-ui text-[12px] font-bold text-[#D97706] uppercase tracking-wider">
                Pending Confirmation ({blocklist.pending.length})
              </span>
              <div className="flex flex-col gap-[8px]">
                {blocklist.pending.map((p) => (
                  <div key={p.id} className="flex items-center justify-between bg-white p-[8px] rounded-[8px] border border-gray-200">
                    <div className="flex items-center gap-[8px]">
                      <span className="px-[6px] py-[1px] rounded bg-gray-100 text-[10px] font-bold uppercase text-[#6F7A73]">
                        {p.type}
                      </span>
                      <span className="font-ui font-medium text-[13px] text-[#1D3B33]">
                        {p.value}
                      </span>
                    </div>
                    <div className="flex items-center gap-[6px]">
                      <button
                        onClick={() => handleBlocklistApprove(p.id)}
                        className="px-[8px] py-[3px] rounded bg-[#2E7D4F] text-white text-[11px] font-bold cursor-pointer"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => handleBlocklistDismiss(p.id)}
                        className="px-[8px] py-[3px] rounded bg-gray-200 text-[#6F7A73] text-[11px] font-bold cursor-pointer"
                      >
                        Dismiss
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Add Manual Entry */}
          <form onSubmit={handleAddManualBlocklist} className="flex gap-[8px] items-center pt-[4px]">
            <select
              value={newBlockType}
              onChange={(e) => setNewBlockType(e.target.value)}
              className="px-[10px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[13px] bg-white text-[#1D3B33]"
            >
              <option value="text">Agency / Text</option>
              <option value="uploader">Uploader</option>
              <option value="subreddit">Subreddit</option>
              <option value="claimant">Claimant</option>
              <option value="track">Commercial Track</option>
            </select>
            <input
              type="text"
              placeholder="e.g. ViralHog, BadUser, survivor"
              value={newBlockValue}
              onChange={(e) => setNewBlockValue(e.target.value)}
              className="flex-1 px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[13px] text-[#1D3B33]"
            />
            <button
              type="submit"
              className="px-[12px] py-[8px] rounded-[8px] bg-[#1D3B33] text-white font-ui text-[13px] font-semibold flex items-center gap-[4px] cursor-pointer"
            >
              <Plus size={16} /> Add
            </button>
          </form>

          {/* Active List */}
          <div className="flex flex-col divide-y divide-[#D5DBD5] max-h-[300px] overflow-y-auto pt-[8px]">
            {blocklist.active.map((item) => (
              <div key={item.id} className="py-[8px] flex items-center justify-between">
                <div className="flex items-center gap-[8px]">
                  <span className="px-[6px] py-[1px] rounded bg-gray-100 text-[10px] font-bold uppercase text-[#6F7A73]">
                    {item.type}
                  </span>
                  <span className="font-ui text-[13px] font-medium text-[#1D3B33]">
                    {item.value}
                  </span>
                </div>
                <button
                  onClick={() => handleBlocklistToggle(item.id)}
                  className="text-[12px] font-semibold text-[#D4412F] hover:underline cursor-pointer"
                >
                  Disable
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* MODAL: Paste Claim from YouTube Studio */}
      {studioPasteModalOpen && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-[16px] backdrop-blur-xs">
          <div className="bg-white rounded-[16px] max-w-[560px] w-full p-[24px] lg:p-[32px] flex flex-col gap-[16px] shadow-xl">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-[8px]">
                <FileText className="text-[#1D3B33]" size={20} />
                <h3 className="font-heading font-semibold text-[22px] text-[#1D3B33]">
                  Paste Claim from YouTube Studio
                </h3>
              </div>
              <button onClick={() => setStudioPasteModalOpen(false)} className="cursor-pointer text-[#6F7A73]">
                <X size={20} />
              </button>
            </div>

            <p className="font-ui text-[13px] text-[#6F7A73]">
              Copy text out of the YouTube Studio claim details dialog or table, paste it here, and the parser will automatically extract track, artist, claimant, and reach impact.
            </p>

            <div className="flex flex-col gap-[10px]">
              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Target Pipeline Clip</label>
                <select
                  value={studioTargetPostId}
                  onChange={(e) => setStudioTargetPostId(e.target.value)}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[13px]"
                >
                  <option value="">-- Select Clip --</option>
                  {reviewClips.map((c) => (
                    <option key={c.post_id} value={c.post_id}>
                      [Scheduled] {c.title} ({c.post_id})
                    </option>
                  ))}
                  {publishedClips.map((c) => (
                    <option key={c.post_id} value={c.post_id}>
                      [Published] {c.title} ({c.post_id})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Copied Studio Text</label>
                <textarea
                  rows={4}
                  placeholder="e.g. Fortunate Son - Creedence Clearwater Revival&#10;Content used: Audio&#10;Impact on video: No impact&#10;Claimant: UMG"
                  value={studioPasteText}
                  onChange={(e) => setStudioPasteText(e.target.value)}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[13px] font-mono"
                />
              </div>

              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={handleParseStudioPaste}
                  disabled={parsingStudio || !studioPasteText.trim()}
                  className="px-[12px] py-[6px] rounded-[8px] bg-gray-100 hover:bg-gray-200 text-[#1D3B33] text-[12px] font-semibold cursor-pointer"
                >
                  {parsingStudio ? 'Parsing...' : 'Parse Text'}
                </button>
              </div>

              {/* Parsed Preview Confirmation */}
              {studioParsed && (
                <div className="p-[12px] rounded-[10px] bg-[#EDF0EC] border border-[#D5DBD5] flex flex-col gap-[6px] text-[12px]">
                  <span className="font-bold text-[#1D3B33]">Parsed Claim Preview:</span>
                  <div className="grid grid-cols-2 gap-[6px]">
                    <div>Asset / Song: <strong>{studioParsed.asset_title || 'Unknown'}</strong></div>
                    <div>Artist: <strong>{studioParsed.track_artist || 'Unknown'}</strong></div>
                    <div>Claimant: <strong>{studioParsed.claimant || 'Unknown'}</strong></div>
                    <div>Type: <strong>{studioParsed.claim_type}</strong></div>
                  </div>
                  <div className="mt-1">
                    Affects Reach: <strong>{studioParsed.affects_reach ? 'Blocked / Restricted' : 'No Impact'}</strong>
                  </div>
                </div>
              )}
            </div>

            <div className="flex items-center justify-end gap-[10px] pt-[8px]">
              <button
                type="button"
                onClick={() => setStudioPasteModalOpen(false)}
                className="px-[14px] py-[8px] rounded-[8px] border border-[#D5DBD5] text-[#1D3B33] text-[13px] font-semibold cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={actionLoading || !studioParsed}
                onClick={handleSaveStudioClaim}
                className="px-[16px] py-[8px] rounded-[8px] bg-[#1D3B33] text-white text-[13px] font-semibold hover:bg-[#132722] disabled:opacity-50 cursor-pointer"
              >
                Save Claim (source=manual)
              </button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL: Unmatched Emails List */}
      {unmatchedModalOpen && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-[16px] backdrop-blur-xs">
          <div className="bg-white rounded-[16px] max-w-[640px] w-full p-[24px] lg:p-[32px] flex flex-col gap-[16px] shadow-xl">
            <div className="flex items-center justify-between">
              <h3 className="font-heading font-semibold text-[22px] text-[#1D3B33]">
                Unmatched Claim Emails ({unmatchedList.length})
              </h3>
              <button onClick={() => setUnmatchedModalOpen(false)} className="cursor-pointer text-[#6F7A73]">
                <X size={20} />
              </button>
            </div>
            <p className="font-ui text-[13px] text-[#6F7A73]">
              YouTube copyright messages received in Gmail that did not match any active video in the uploads table (e.g. older legacy channel uploads).
            </p>

            <div className="flex flex-col divide-y divide-gray-200 max-h-[400px] overflow-y-auto">
              {unmatchedList.length === 0 ? (
                <div className="py-[20px] text-center text-[#6F7A73] text-[13px]">No unmatched emails.</div>
              ) : (
                unmatchedList.map((item) => (
                  <div key={item.message_id} className="py-[10px] flex flex-col gap-[3px]">
                    <span className="font-semibold text-[13px] text-[#1D3B33]">{item.subject}</span>
                    <span className="text-[11px] text-[#6F7A73]">{item.received_at} · from: {item.sender}</span>
                    {item.raw_snippet && (
                      <p className="text-[12px] text-gray-600 bg-gray-50 p-1.5 rounded">{item.raw_snippet}</p>
                    )}
                  </div>
                ))
              )}
            </div>

            <div className="flex justify-end pt-[8px]">
              <button
                type="button"
                onClick={() => setUnmatchedModalOpen(false)}
                className="px-[14px] py-[8px] rounded-[8px] bg-[#1D3B33] text-white text-[13px] font-semibold cursor-pointer"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL 1: Mark Claimed Modal */}
      {claimModalClip && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-[16px] backdrop-blur-xs">
          <div className="bg-white rounded-[16px] max-w-[500px] w-full p-[24px] lg:p-[32px] flex flex-col gap-[20px] shadow-xl">
            <div className="flex items-center justify-between">
              <h3 className="font-heading font-semibold text-[22px] text-[#1D3B33]">
                Register Claim & Learn
              </h3>
              <button onClick={() => setClaimModalClip(null)} className="cursor-pointer text-[#6F7A73]">
                <X size={20} />
              </button>
            </div>

            <p className="font-ui text-[14px] text-[#6F7A73]">
              Record Content ID claim facts for <strong>{claimModalClip.title}</strong> to block future re-use.
            </p>

            <form onSubmit={handleSaveClaim} className="flex flex-col gap-[14px]">
              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Claim Type</label>
                <select
                  value={claimForm.claim_type}
                  onChange={(e) => setClaimForm({ ...claimForm, claim_type: e.target.value })}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[14px]"
                >
                  <option value="audio">Audio (Commercial music / sound recording)</option>
                  <option value="visual">Visual (Agency footage / video clip)</option>
                  <option value="both">Audiovisual (Both audio & visual)</option>
                </select>
              </div>

              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Claimant / Agency Name</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. ViralHog, SME, CollabDRM, Warner Music"
                  value={claimForm.claimant}
                  onChange={(e) => setClaimForm({ ...claimForm, claimant: e.target.value })}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[14px]"
                />
              </div>

              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Asset / Song Title</label>
                <input
                  type="text"
                  placeholder="e.g. Eye of the Tiger, Female Pheasant Video"
                  value={claimForm.asset_title}
                  onChange={(e) => setClaimForm({ ...claimForm, asset_title: e.target.value })}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[14px]"
                />
              </div>

              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Found in (Timestamp range)</label>
                <input
                  type="text"
                  placeholder="e.g. 0:02 - 0:19"
                  value={claimForm.found_in}
                  onChange={(e) => setClaimForm({ ...claimForm, found_in: e.target.value })}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[14px]"
                />
              </div>

              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Action Taken</label>
                <select
                  value={claimForm.action}
                  onChange={(e) => setClaimForm({ ...claimForm, action: e.target.value })}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[14px]"
                >
                  <option value="held">Hold (Set private on YouTube)</option>
                  <option value="kept">Kept (Leave published / revenue shared)</option>
                  <option value="pulled">Pulled (Deleted from YouTube)</option>
                  <option value="disputed">Disputed (Formal YouTube dispute)</option>
                </select>
              </div>

              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Notes</label>
                <input
                  type="text"
                  placeholder="Optional notes or details"
                  value={claimForm.notes}
                  onChange={(e) => setClaimForm({ ...claimForm, notes: e.target.value })}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[14px]"
                />
              </div>

              <div className="flex items-center justify-end gap-[10px] pt-[8px]">
                <button
                  type="button"
                  onClick={() => setClaimModalClip(null)}
                  className="px-[14px] py-[8px] rounded-[8px] border border-[#D5DBD5] text-[#1D3B33] text-[13px] font-semibold cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={actionLoading}
                  className="px-[16px] py-[8px] rounded-[8px] bg-[#D4412F] text-white text-[13px] font-semibold hover:bg-[#b83424] cursor-pointer"
                >
                  Save Claim & Learn
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* MODAL 2: Replace from Reserve Modal */}
      {replaceModalClip && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-[16px] backdrop-blur-xs">
          <div className="bg-white rounded-[16px] max-w-[500px] w-full p-[24px] lg:p-[32px] flex flex-col gap-[20px] shadow-xl">
            <div className="flex items-center justify-between">
              <h3 className="font-heading font-semibold text-[22px] text-[#1D3B33]">
                Replace from Reserve
              </h3>
              <button onClick={() => setReplaceModalClip(null)} className="cursor-pointer text-[#6F7A73]">
                <X size={20} />
              </button>
            </div>

            <p className="font-ui text-[14px] text-[#6F7A73]">
              The system will select the highest-scoring reserve pool clip that has cleared the rights check, schedule it
              into slot <strong>{replaceModalClip.slot_time}</strong>, and update the claimed video.
            </p>

            <form onSubmit={handleReplaceFromReserve} className="flex flex-col gap-[14px]">
              <div>
                <label className="font-ui text-[12px] font-semibold text-[#6F7A73]">Action on Claimed Video</label>
                <select
                  value={replaceAction}
                  onChange={(e) => setReplaceAction(e.target.value as any)}
                  className="w-full mt-[4px] px-[12px] py-[8px] rounded-[8px] border border-[#D5DBD5] font-ui text-[14px]"
                >
                  <option value="held">Hold (Set private on YouTube — Recommended)</option>
                  <option value="delete">Delete from YouTube (Permanent)</option>
                </select>
              </div>

              {replaceAction === 'delete' && (
                <div className="flex flex-col gap-[6px]">
                  <label className="font-ui text-[12px] font-semibold text-[#D4412F]">
                    Type &quot;DELETE&quot; to confirm permanent deletion:
                  </label>
                  <input
                    type="text"
                    required
                    placeholder="DELETE"
                    value={deleteConfirmationText}
                    onChange={(e) => setDeleteConfirmationText(e.target.value)}
                    className="w-full px-[12px] py-[8px] rounded-[8px] border border-[#D4412F] font-mono text-[14px] text-[#D4412F]"
                  />
                </div>
              )}

              <div className="flex items-center justify-end gap-[10px] pt-[8px]">
                <button
                  type="button"
                  onClick={() => setReplaceModalClip(null)}
                  className="px-[14px] py-[8px] rounded-[8px] border border-[#D5DBD5] text-[#1D3B33] text-[13px] font-semibold cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={actionLoading}
                  className="px-[16px] py-[8px] rounded-[8px] bg-[#1D3B33] text-white text-[13px] font-semibold hover:bg-[#132722] cursor-pointer"
                >
                  Confirm Replacement
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* MODAL 3: Delete Published Video Modal */}
      {deleteModalClip && (() => {
        const isModalClipMissing = Boolean(
          deleteModalClip.is_missing ||
          deleteModalClip.privacy === 'missing' ||
          deleteModalClip.live_privacy === 'missing' ||
          deleteModalClip.live_status === 'missing'
        );
        return (
          <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-[16px] backdrop-blur-xs">
            <div className="bg-white rounded-[16px] max-w-[460px] w-full p-[24px] lg:p-[32px] flex flex-col gap-[16px] shadow-xl">
              <h3 className="font-heading font-semibold text-[20px] text-[#D4412F]">
                {isModalClipMissing ? 'Remove Missing Clip Record' : 'Confirm Video Deletion'}
              </h3>
              <p className="font-ui text-[13px] text-[#6F7A73]">
                {isModalClipMissing ? (
                  <>This video is already missing/deleted from YouTube. Type <strong>DELETE</strong> to remove this record from the database.</>
                ) : (
                  <>Are you sure you want to permanently delete <strong>{deleteModalClip.title}</strong> from YouTube? This cannot be undone.</>
                )}
              </p>
              <div className="flex flex-col gap-[6px]">
                <label className="font-ui text-[12px] font-semibold text-[#D4412F]">
                  Type &quot;DELETE&quot; to proceed:
                </label>
                <input
                  type="text"
                  placeholder="DELETE"
                  value={deleteModalConfirm}
                  onChange={(e) => setDeleteModalConfirm(e.target.value)}
                  className="w-full px-[12px] py-[8px] rounded-[8px] border border-[#D4412F] font-mono text-[14px] text-[#D4412F]"
                />
              </div>
              <div className="flex items-center justify-end gap-[10px] pt-[8px]">
                <button
                  type="button"
                  onClick={() => setDeleteModalClip(null)}
                  className="px-[14px] py-[8px] rounded-[8px] border border-[#D5DBD5] text-[#1D3B33] text-[13px] font-semibold cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={deleteModalConfirm !== 'DELETE' || actionLoading}
                  onClick={() => handlePublishedAction(deleteModalClip.post_id, deleteModalClip.video_id, isModalClipMissing ? 'remove_record' : 'delete')}
                  className="px-[16px] py-[8px] rounded-[8px] bg-[#D4412F] text-white text-[13px] font-semibold hover:bg-[#b83424] disabled:opacity-50 cursor-pointer"
                >
                  {isModalClipMissing ? 'Remove Record' : 'Delete Video'}
                </button>
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
};
