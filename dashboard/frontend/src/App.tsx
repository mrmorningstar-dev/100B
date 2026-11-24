import React, { useState, useEffect, useCallback } from 'react';
import { Rail, type NavTab } from './components/Rail';
import { ViewErrorBoundary } from './components/ViewErrorBoundary';
import { UnlockModal } from './components/UnlockModal';
import { apiFetch, getAuthToken, refreshMediaToken } from './utils/api';
import { AgentView } from './views/AgentView';
import { ScheduleView } from './views/ScheduleView';
import { YouTubeView } from './views/YouTubeView';
import { ClaimsView } from './views/ClaimsView';
import { PrivacyPolicyView } from './views/PrivacyPolicyView';
import { TermsOfServiceView } from './views/TermsOfServiceView';

const getInitialPage = (): 'dashboard' | 'privacy' | 'terms' => {
  if (typeof window !== 'undefined') {
    const p = window.location.pathname.toLowerCase();
    if (p.includes('privacy')) return 'privacy';
    if (p.includes('terms')) return 'terms';
  }
  return 'dashboard';
};

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
  last_task_error?: string;
  reddit?: {
    status: 'ok' | 'throttled' | 'blocked';
    requests_today: number;
    max_requests_per_day: number;
    blocked_until?: string | null;
  };
  youtube_auth?: {
    status: 'ok' | 'expired' | 'missing' | 'error';
    account?: string | null;
    expires_at?: string | null;
    error?: string | null;
  };
}

export const App: React.FC = () => {
  const [currentPage, setCurrentPage] = useState<'dashboard' | 'privacy' | 'terms'>(getInitialPage);
  const [activeTab, setActiveTab] = useState<NavTab>('agent');
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatusData | null>(null);
  const [targetClipForYouTube, setTargetClipForYouTube] = useState<string | null>(null);
  const [isUnlockOpen, setIsUnlockOpen] = useState(false);

  useEffect(() => {
    const handlePopState = () => {
      setCurrentPage(getInitialPage());
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  const navigateTo = (page: 'dashboard' | 'privacy' | 'terms') => {
    setCurrentPage(page);
    const path = page === 'privacy' ? '/privacy' : page === 'terms' ? '/terms' : '/';
    if (typeof window !== 'undefined' && window.location.pathname !== path) {
      window.history.pushState({}, '', path);
    }
  };

  useEffect(() => {
    const handleUnauthorized = () => {
      // Never block public privacy and terms pages with unlock modal
      if (currentPage === 'dashboard') {
        setIsUnlockOpen(true);
      }
    };
    window.addEventListener('dashboard:unauthorized', handleUnauthorized);
    return () => window.removeEventListener('dashboard:unauthorized', handleUnauthorized);
  }, [currentPage]);

  // Renew signed media token on page load if key is stored, and every ~8 minutes
  useEffect(() => {
    if (getAuthToken()) {
      refreshMediaToken();
    }
    const mediaInterval = setInterval(() => {
      if (getAuthToken()) {
        refreshMediaToken();
      }
    }, 8 * 60 * 1000); // 8 minutes
    return () => clearInterval(mediaInterval);
  }, []);

  const fetchPipelineStatus = useCallback(async () => {
    try {
      const resp = await apiFetch('/api/pipeline/status');
      if (resp.ok) {
        const data = await resp.json();
        setPipelineStatus(data);
      }
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    fetchPipelineStatus();
    const interval = setInterval(() => {
      if (document.visibilityState === 'visible') {
        fetchPipelineStatus();
      }
    }, 30000);
    return () => clearInterval(interval);
  }, [fetchPipelineStatus]);

  const handleSelectClipForYouTube = (postId: string) => {
    setTargetClipForYouTube(postId);
    setActiveTab('youtube');
  };

  const isAuthExpired =
    pipelineStatus?.youtube_auth?.status === 'expired' ||
    pipelineStatus?.last_run?.status === 'auth_required';

  if (currentPage === 'privacy') {
    return <PrivacyPolicyView onBackToDashboard={() => navigateTo('dashboard')} />;
  }

  if (currentPage === 'terms') {
    return <TermsOfServiceView onBackToDashboard={() => navigateTo('dashboard')} />;
  }

  return (
    <div className="min-h-screen bg-[#EDF0EC] text-[#1D3B33] flex flex-row font-ui antialiased">
      {/* Navigation rail (sidebar on desktop/tablet, bottom bar on mobile) */}
      <Rail
        activeTab={activeTab}
        onTabChange={setActiveTab}
        pipelineStatus={pipelineStatus}
        onNavigatePage={(page) => navigateTo(page)}
      />

      {/* Main content view */}
      <main className="flex-1 flex flex-col min-h-screen overflow-x-hidden overflow-y-auto">
        {isAuthExpired && (
          <div className="bg-[#D4412F] text-white px-4 py-3 flex items-center gap-2 shrink-0 shadow-sm font-ui text-[14px]">
            <span className="font-semibold">YouTube token expired. Re-run upload.py auth in terminal.</span>
          </div>
        )}
        {activeTab === 'agent' && (
          <ViewErrorBoundary key="agent" viewName="Agent">
            <AgentView
              onSelectClipForYouTube={handleSelectClipForYouTube}
              redditStatus={pipelineStatus?.reddit}
            />
          </ViewErrorBoundary>
        )}
        {activeTab === 'schedule' && (
          <ViewErrorBoundary key="schedule" viewName="Schedule">
            <ScheduleView />
          </ViewErrorBoundary>
        )}
        {activeTab === 'youtube' && (
          <ViewErrorBoundary key="youtube" viewName="YouTube">
            <YouTubeView
              initialPostId={targetClipForYouTube}
              onClearInitialPostId={() => setTargetClipForYouTube(null)}
            />
          </ViewErrorBoundary>
        )}
        {activeTab === 'claims' && (
          <ViewErrorBoundary key="claims" viewName="Claims">
            <ClaimsView />
          </ViewErrorBoundary>
        )}
      </main>

      {/* Unlock screen modal when DASHBOARD_API_KEY authentication is required */}
      <UnlockModal
        isOpen={isUnlockOpen}
        onUnlocked={() => {
          setIsUnlockOpen(false);
          fetchPipelineStatus();
        }}
      />
    </div>
  );
};

export default App;
