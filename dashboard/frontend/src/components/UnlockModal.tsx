import React, { useState } from 'react';
import { Lock, KeyRound, AlertCircle, Loader2 } from 'lucide-react';
import { setAuthToken, refreshMediaToken, getApiUrl } from '../utils/api';

interface UnlockModalProps {
  isOpen: boolean;
  onUnlocked: () => void;
}

export const UnlockModal: React.FC<UnlockModalProps> = ({ isOpen, onUnlocked }) => {
  const [key, setKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!key.trim()) {
      setError('Please enter your access key.');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const verifyUrl = getApiUrl('/api/auth/verify');
      const resp = await fetch(verifyUrl, {
        headers: {
          'X-Dashboard-Token': key.trim(),
        },
      });

      if (resp.ok) {
        setAuthToken(key.trim());
        await refreshMediaToken();
        setLoading(false);
        onUnlocked();
      } else if (resp.status === 401) {
        setError('Invalid access key. Please check DASHBOARD_API_KEY.');
        setLoading(false);
      } else if (resp.status === 429) {
        setError('Too many failed attempts. Please wait 60 seconds.');
        setLoading(false);
      } else {
        // If 403 or host blocked
        const errJson = await resp.json().catch(() => ({}));
        setError(errJson.error || `Authentication failed (HTTP ${resp.status})`);
        setLoading(false);
      }
    } catch (err: any) {
      setError(`Cannot reach backend: ${err.message || 'Network error'}. Check VITE_API_BASE_URL.`);
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 font-ui">
      <div className="w-full max-w-md bg-[#1D3B33] text-white rounded-[16px] border border-[#D5DBD5]/20 shadow-2xl overflow-hidden p-6">
        <div className="flex items-center gap-3 mb-4">
          <div className="size-10 rounded-full bg-[#2A4F43] flex items-center justify-center text-[#B0C7BD]">
            <Lock className="size-5 text-[#2E7D4F]" />
          </div>
          <div>
            <h2 className="font-heading font-semibold text-lg text-white">Dashboard Protected</h2>
            <p className="text-xs text-[#B0C7BD]">Enter access key to connect to 100B backend</p>
          </div>
        </div>

        {error && (
          <div className="mb-4 p-3 rounded-lg bg-[#D4412F]/20 border border-[#D4412F]/50 flex items-start gap-2 text-xs text-[#FF9B8A]">
            <AlertCircle className="size-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div>
            <label className="block text-xs font-medium text-[#B0C7BD] mb-1.5" htmlFor="access-key">
              DASHBOARD_API_KEY
            </label>
            <div className="relative">
              <KeyRound className="size-4 absolute left-3 top-3 text-[#B0C7BD]" />
              <input
                id="access-key"
                type="password"
                autoFocus
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="Paste your secret access key..."
                className="w-full bg-[#17302B] border border-[#D5DBD5]/20 rounded-lg pl-9 pr-3 py-2 text-sm text-white placeholder-white/30 focus:outline-none focus:border-[#2E7D4F] transition-colors"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 px-4 rounded-lg bg-[#2E7D4F] hover:bg-[#256640] text-white font-medium text-sm transition-colors flex items-center justify-center gap-2 cursor-pointer disabled:opacity-50"
          >
            {loading ? (
              <>
                <Loader2 className="size-4 animate-spin" />
                <span>Verifying...</span>
              </>
            ) : (
              <span>Unlock Dashboard</span>
            )}
          </button>
        </form>
      </div>
    </div>
  );
};
