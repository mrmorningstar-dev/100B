import React from 'react';
import { Bot, Calendar, PlaySquare, ShieldAlert } from 'lucide-react';
import { formatTimeIST, formatWeekdayIST } from '../utils/timeFormat';

export type NavTab = 'agent' | 'schedule' | 'youtube' | 'claims';

interface PipelineStatusData {
  last_run?: {
    status?: string;
    finished_at?: string;
    uploaded?: number;
    scheduled?: number;
    failed?: number;
    message?: string;
    next_attempt_at?: string;
  } | null;
  next_run_time?: string;
  last_task_result?: number | null;
  last_task_error?: string | null;
}

interface RailProps {
  activeTab: NavTab;
  onTabChange: (tab: NavTab) => void;
  pipelineStatus?: PipelineStatusData | null;
}

export const Rail: React.FC<RailProps> = ({
  activeTab,
  onTabChange,
  pipelineStatus,
}) => {
  const lastRun = pipelineStatus?.last_run;
  const isLastRunPassed = lastRun?.status === 'success';
  const isLastRunPartial = lastRun?.status === 'partial';
  const isLastRunWaiting = lastRun?.status === 'waiting';
  const isLastRunAuthRequired = lastRun?.status === 'auth_required';

  let statusLabel = 'Last run failed';
  if (isLastRunPassed) statusLabel = 'Last run passed';
  else if (isLastRunAuthRequired) statusLabel = 'Waiting for YouTube login';
  else if (isLastRunPartial) statusLabel = 'Last run partial';
  else if (isLastRunWaiting) statusLabel = 'Waiting to retry';

  const dotColor = isLastRunPassed
    ? 'bg-[#2E7D4F]'
    : isLastRunWaiting
    ? 'bg-[#4B88D6]'
    : (isLastRunPartial || isLastRunAuthRequired)
    ? 'bg-[#D99A1E]'
    : 'bg-[#D4412F]';

  // Format last run time (Channel IST)
  let lastRunTimeStr = 'Sat 4:32 AM IST · 6 of 6 uploaded';
  if (isLastRunAuthRequired) {
    lastRunTimeStr = 'Waiting for YouTube login';
  }
  if (lastRun?.finished_at) {
    try {
      const dayName = formatWeekdayIST(lastRun.finished_at);
      const timeStr = formatTimeIST(lastRun.finished_at);
      const up = lastRun.uploaded || 0;
      const sched = lastRun.scheduled || 0;
      const failedCount = lastRun.failed || 0;
      const okCount = up + sched;
      const total = okCount + failedCount || 6;

      if (isLastRunAuthRequired) {
        lastRunTimeStr = `${dayName} ${timeStr} · Waiting for YouTube login`;
      } else if (isLastRunWaiting) {
        lastRunTimeStr = `${dayName} ${timeStr} · Waiting (${lastRun.next_attempt_at || 'retry soon'})`;
      } else if (lastRun.status === 'failed' && (lastRun.message || '').includes('Curation produced 0')) {
        lastRunTimeStr = `${dayName} ${timeStr} · Curation found 0/6 clips`;
      } else {
        lastRunTimeStr = `${dayName} ${timeStr} · ${okCount} of ${total} uploaded${failedCount > 0 ? ` · ${failedCount} failed` : ''}`;
      }
    } catch {
      // fallback
    }
  }

  // Format next run time (Channel IST)
  let nextRunTimeStr = 'Next run Mon 4:30 AM IST';
  if (isLastRunWaiting && lastRun?.next_attempt_at) {
    nextRunTimeStr = `Next attempt ${lastRun.next_attempt_at}`;
  } else if (pipelineStatus?.next_run_time) {
    try {
      const dayName = formatWeekdayIST(pipelineStatus.next_run_time);
      const timeStr = formatTimeIST(pipelineStatus.next_run_time);
      nextRunTimeStr = `Next run ${dayName} ${timeStr}`;
    } catch {
      // fallback
    }
  }

  const navItems = [
    { id: 'agent' as NavTab, label: 'Agent', icon: Bot },
    { id: 'schedule' as NavTab, label: 'Schedule', icon: Calendar },
    { id: 'youtube' as NavTab, label: 'YouTube', icon: PlaySquare },
    { id: 'claims' as NavTab, label: 'Claims', icon: ShieldAlert },
  ];

  return (
    <>
      {/* Desktop & Tablet Sidebar Rail */}
      <aside
        className="hidden md:flex flex-col shrink-0 bg-[#1D3B33] self-stretch min-h-screen text-white transition-all duration-200 z-30
                   w-[72px] xl:w-[248px] px-[12px] xl:px-[20px] pt-[28px] pb-[24px]"
      >
        {/* Brand header */}
        <div className="flex items-center gap-[12px] overflow-hidden w-full">
          <div className="size-[44px] rounded-[22px] bg-[#D4412F] flex items-center justify-center shrink-0 shadow-sm">
            <span className="font-heading font-extrabold text-[22px] text-white leading-none">
              B
            </span>
          </div>
          <div className="hidden xl:flex flex-col gap-px leading-tight overflow-hidden whitespace-nowrap">
            <span className="font-heading font-semibold text-[18px] text-white">
              Betterverse
            </span>
            <span className="font-ui text-[13px] text-[#B0C7BD]">
              @betterverse-f2
            </span>
          </div>
        </div>

        {/* Spacing gap */}
        <div className="h-[36px] shrink-0" />

        {/* Navigation links */}
        <nav className="flex flex-col gap-[4px] w-full" aria-label="Main Navigation">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = activeTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onTabChange(item.id)}
                className={`w-full flex items-center gap-[12px] px-[12px] py-[11px] rounded-[10px] cursor-pointer transition-colors text-left
                  ${isActive ? 'bg-[#2A4F43] text-white shadow-sm' : 'text-[#B0C7BD] hover:bg-[#2A4F43]/40 hover:text-white'}
                `}
                aria-current={isActive ? 'page' : undefined}
              >
                <Icon className={`size-[20px] shrink-0 ${isActive ? 'text-white' : 'text-[#B0C7BD]'}`} />
                <span className="hidden xl:inline flex-1 min-w-0 font-ui font-medium text-[15px] truncate">
                  {item.label}
                </span>
                {isActive && (
                  <span className="hidden xl:block size-[7px] rounded-full bg-[#2E7D4F] shrink-0" />
                )}
              </button>
            );
          })}
        </nav>

        {/* Spacer to push status card to bottom */}
        <div className="flex-1 min-h-[40px]" />

        {/* Pipeline status card */}
        <div className="bg-[#2A4F43] rounded-[12px] p-[10px] xl:p-[14px] flex flex-col gap-[6px] shrink-0 w-full overflow-hidden border border-[#D5DBD5]/10">
          <div className="flex items-center gap-[8px]">
            <span className={`size-[8px] rounded-full shrink-0 ${dotColor}`} />
            <span className="hidden xl:inline font-ui font-semibold text-[13px] text-white whitespace-nowrap">
              {statusLabel}
            </span>
          </div>
          <p className="hidden xl:block font-ui text-[12px] text-[#B0C7BD] truncate">
            {lastRunTimeStr}
          </p>
          {pipelineStatus?.last_task_error ? (
            <div className="hidden xl:flex items-start gap-[6px] p-[6px] rounded-[6px] bg-[#D4412F]/25 border border-[#D4412F]/50 text-[#FF9B8A] text-[11px] leading-tight">
              <span className="font-ui font-semibold">{pipelineStatus.last_task_error}</span>
            </div>
          ) : (
            <p className="hidden xl:block font-ui text-[12px] text-[#B0C7BD] truncate">
              {nextRunTimeStr}
            </p>
          )}
        </div>
      </aside>

      {/* Mobile Bottom Tab Bar (<768px) */}
      <nav
        className="md:hidden fixed bottom-0 left-0 right-0 h-[64px] bg-[#1D3B33] border-t border-[#2A4F43] flex items-center justify-around px-[16px] z-50 shadow-lg"
        aria-label="Mobile Navigation"
      >
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = activeTab === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onTabChange(item.id)}
              className={`flex flex-col items-center justify-center gap-[4px] px-[16px] py-[8px] rounded-[8px] transition-colors
                ${isActive ? 'text-white' : 'text-[#B0C7BD]'}`}
            >
              <Icon className="size-[20px]" />
              <span className="font-ui text-[11px] font-medium leading-none">
                {item.label}
              </span>
            </button>
          );
        })}
      </nav>
    </>
  );
};
