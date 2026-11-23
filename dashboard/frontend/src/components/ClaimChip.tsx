import React from 'react';

interface ClaimChipProps {
  status?: string; // 'claimed', 'active', 'released', 'pending_confirmation', 'unknown_pending_confirmation', 'possible', 'needs_check', 'none'
  size?: 'sm' | 'md';
}

export const ClaimChip: React.FC<ClaimChipProps> = ({ status, size = 'md' }) => {
  if (!status || status === 'none') return null;

  const sizeClasses = size === 'sm' ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs font-medium';

  if (status === 'claimed' || status === 'active') {
    return (
      <span className={`inline-flex items-center gap-1 rounded bg-red-500/20 text-red-300 border border-red-500/30 ${sizeClasses}`}>
        <span className="w-1.5 h-1.5 rounded-full bg-red-400"></span>
        Claimed
      </span>
    );
  }

  if (status === 'released') {
    return (
      <span className={`inline-flex items-center gap-1 rounded bg-gray-500/20 text-gray-300 border border-gray-500/30 ${sizeClasses}`}>
        <span className="w-1.5 h-1.5 rounded-full bg-gray-400"></span>
        Claim released
      </span>
    );
  }

  if (status === 'pending_confirmation' || status === 'unknown_pending_confirmation') {
    return (
      <span className={`inline-flex items-center gap-1 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 ${sizeClasses}`}>
        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse"></span>
        Possible claim
      </span>
    );
  }

  if (status === 'possible' || status === 'needs_check') {
    return (
      <span className={`inline-flex items-center gap-1 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 ${sizeClasses}`}>
        <span className="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
        Possible claim
      </span>
    );
  }

  return null;
};
