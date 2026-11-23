import React, { useState, useEffect, useCallback } from 'react';
import { Rail, type NavTab } from './components/Rail';
import { ViewErrorBoundary } from './components/ViewErrorBoundary';
import { AgentView } from './views/AgentView';
import { ScheduleView } from './views/ScheduleView';
import { YouTubeView } from './views/YouTubeView';
import { ClaimsView } from './views/ClaimsView';

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
}

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<NavTab>('agent');
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatusData | null>(null);
  const [targetClipForYouTube, setTargetClipForYouTube] = useState<string | null>(null);

  const fetchPipelineStatus = useCallback(async () => {
    try {
      const resp = await fetch('/api/pipeline/status');
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

  return (
    <div className="min-h-screen bg-[#EDF0EC] text-[#1D3B33] flex flex-row font-ui antialiased">
      {/* Navigation rail (sidebar on desktop/tablet, bottom bar on mobile) */}
      <Rail
        activeTab={activeTab}
        onTabChange={setActiveTab}
        pipelineStatus={pipelineStatus}
      />

      {/* Main content view */}
      <main className="flex-1 flex flex-col min-h-screen overflow-x-hidden overflow-y-auto">
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
    </div>
  );
};

export default App;
