import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Rail } from '../components/Rail';
import { AgentView } from '../views/AgentView';

// Mock global fetch for AgentView
beforeEach(() => {
  globalThis.fetch = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/api/agent/days')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([
          { date: '2026-11-23', is_today: true, found: 20, approved: 6, rejected: 14 }
        ]),
      });
    }
    if (url.includes('/api/agent/day/')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          totals: { found: 20, rejected: 14, accepted: 6 },
          funnel: { dropped_before_judging: 5, rejected_by_judge: 9, stored_in_reserve: 0, selected_for_tomorrow: 6 },
          lineup: [],
          reserve: [],
          reasons: { not_funny: 9 },
          sources: { 'r/funny': { judged: 10, accepted: 4 } },
        }),
      });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Rail Component Status Card Tests', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it('renders "Waiting to retry" status with blue indicator and next attempt time when status is waiting', async () => {
    const pipelineStatus: any = {
      last_run: {
        status: 'waiting',
        started_at: '2026-11-23T07:00:00Z',
        finished_at: '2026-11-23T07:04:00Z',
        uploaded: 0,
        scheduled: 0,
        failed: 0,
        next_attempt_at: '10:00 AM IST',
      },
      next_run_time: '2026-11-23T10:00:00+05:30',
    };

    await act(async () => {
      root.render(
        <Rail activeTab="agent" onTabChange={() => {}} pipelineStatus={pipelineStatus} />
      );
    });

    const text = container.textContent || '';
    expect(text).toContain('Waiting to retry');
    expect(text).toContain('Waiting (10:00 AM IST)');
    expect(text).toContain('Next attempt 10:00 AM IST');

    // Blue indicator dot
    const blueDot = container.querySelector('.bg-\\[\\#4B88D6\\]');
    expect(blueDot).not.toBeNull();
  });

  it('renders "Curation found 0/6 clips" when curation failed with 0 approved clips', async () => {
    const pipelineStatus: any = {
      last_run: {
        status: 'failed',
        started_at: '2026-11-23T07:00:00Z',
        finished_at: '2026-11-23T07:04:00Z',
        uploaded: 0,
        scheduled: 0,
        failed: 6,
        message: 'Daily pipeline FAILED for date 2026-11-23: Curation produced 0/6 approved clips.',
      },
    };

    await act(async () => {
      root.render(
        <Rail activeTab="agent" onTabChange={() => {}} pipelineStatus={pipelineStatus} />
      );
    });

    const text = container.textContent || '';
    expect(text).toContain('Last run failed');
    expect(text).toContain('Curation found 0/6 clips');
  });

  it('renders failed upload counts when uploads failed', async () => {
    const pipelineStatus: any = {
      last_run: {
        status: 'failed',
        started_at: '2026-11-23T07:00:00Z',
        finished_at: '2026-11-23T07:08:00Z',
        uploaded: 4,
        scheduled: 0,
        failed: 2,
      },
      next_run_time: '2026-11-24T07:00:00+05:30',
    };

    await act(async () => {
      root.render(
        <Rail activeTab="agent" onTabChange={() => {}} pipelineStatus={pipelineStatus} />
      );
    });

    const text = container.textContent || '';
    expect(text).toContain('Last run failed');
    expect(text).toContain('4 of 6 uploaded · 2 failed');

    // Red indicator dot
    const redDot = container.querySelector('.bg-\\[\\#D4412F\\]');
    expect(redDot).not.toBeNull();
  });

  it('renders last_task_error banner when task error is present', async () => {
    const pipelineStatus: any = {
      last_run: {
        status: 'failed',
        started_at: '2026-11-23T07:00:00Z',
        uploaded: 0,
        scheduled: 0,
        failed: 6,
      },
      last_task_error: 'Directory not found: C:\\Drive-D\\python tut\\100B',
    };

    await act(async () => {
      root.render(
        <Rail activeTab="agent" onTabChange={() => {}} pipelineStatus={pipelineStatus} />
      );
    });

    const text = container.textContent || '';
    expect(text).toContain('Directory not found: C:\\Drive-D\\python tut\\100B');
  });

  it('renders "Waiting for YouTube login" status with amber indicator when status is auth_required', async () => {
    const pipelineStatus: any = {
      last_run: {
        status: 'auth_required',
        started_at: '2026-11-23T13:00:00Z',
        finished_at: '2026-11-23T13:00:05Z',
        uploaded: 0,
        scheduled: 0,
        failed: 0,
      },
      next_run_time: '2026-11-24T07:00:00+05:30',
    };

    await act(async () => {
      root.render(
        <Rail activeTab="agent" onTabChange={() => {}} pipelineStatus={pipelineStatus} />
      );
    });

    const text = container.textContent || '';
    expect(text).toContain('Waiting for YouTube login');

    // Amber indicator dot
    const amberDot = container.querySelector('.bg-\\[\\#D99A1E\\]');
    expect(amberDot).not.toBeNull();
  });
});

describe('AgentView Reddit Scraper Status Chip Tests', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it('renders Blocked chip with red indicator and reason when reddit is blocked', async () => {
    const redditStatus: any = {
      status: 'blocked',
      requests_today: 4,
      max_requests_per_day: 80,
      blocked_until: 'blocked until 10:00 AM IST',
    };

    await act(async () => {
      root.render(<AgentView redditStatus={redditStatus} />);
    });

    const text = container.textContent || '';
    expect(text).toContain('Reddit:');
    expect(text).toContain('Blocked (blocked until 10:00 AM IST)');

    // Red styling
    const redChip = container.querySelector('.text-\\[\\#D4412F\\]');
    expect(redChip).not.toBeNull();
  });

  it('renders Throttled chip with amber indicator when reddit is throttled', async () => {
    const redditStatus: any = {
      status: 'throttled',
      requests_today: 75,
      max_requests_per_day: 80,
    };

    await act(async () => {
      root.render(<AgentView redditStatus={redditStatus} />);
    });

    const text = container.textContent || '';
    expect(text).toContain('Reddit:');
    expect(text).toContain('Throttled (75/80 reqs)');

    // Amber styling
    const amberChip = container.querySelector('.text-\\[\\#D99A1E\\]');
    expect(amberChip).not.toBeNull();
  });

  it('renders OK chip with green indicator when reddit is healthy', async () => {
    const redditStatus: any = {
      status: 'ok',
      requests_today: 12,
      max_requests_per_day: 80,
    };

    await act(async () => {
      root.render(<AgentView redditStatus={redditStatus} />);
    });

    const text = container.textContent || '';
    expect(text).toContain('Reddit:');
    expect(text).toContain('OK (12/80 reqs)');

    // Green styling
    const greenChip = container.querySelector('.text-\\[\\#2E7D4F\\]');
    expect(greenChip).not.toBeNull();
  });
});
