from typing import List, Dict, Any, Optional
from typing_extensions import TypedDict


class CurationState(TypedDict, total=False):
    """
    Central typed state passed between nodes in the LangGraph curation workflow.
    """
    target_count: int                   # Goal number of videos (e.g. 24)
    target_date: str                    # Target publishing date (YYYY-MM-DD)
    loop_count: int                     # Active loop count (starts at 1, max 3)
    max_loops: int                      # Max allowed loops
    spare_buffer: int                   # Buffer to cover potential failures (default 4)

    # Candidate tracking across pipeline
    candidates: List[Dict[str, Any]]    # Candidates currently in processing
    approved: List[Dict[str, Any]]      # Verified and approved videos
    borderline: List[Dict[str, Any]]    # Borderline clips needing human decision
    rejected: List[Dict[str, Any]]      # Dropped clips with granular failure reasons
    judge_failed: List[Dict[str, Any]]  # Candidates where judge call failed (skipped from decisions & registry, pending retry)
    sources_tried: List[str]            # List of subreddits & X accounts queried so far

    # Strategy & financial metrics
    plan: Dict[str, Any]                # Active search plan for current loop
    cost_so_far: float                  # USD accumulated from Claude API
    llm_calls_count: int                # Number of LLM calls made

    # Control flags
    backfill_performed: bool            # True if reserve pool was tapped
    trigger_extra_loop: bool            # True if backfill was short and needs 1 extra plan loop
    human_interrupted: bool             # True if human review exited early to resume later
    dry_run: bool                       # True if running in dry-run mode
    no_review: bool                     # True to bypass interactive human review (unattended mode)
    borderline_policy: str              # Policy for borderline clips in unattended mode ('reject' or 'approve_top')
    scraper_blocked: bool               # True if circuit breaker halted scraping
    scraper_error: bool                 # True if scraper process crashed (non-zero exit that isn't blocked)

    # Phase 3.5 Discovery & Two-step pipeline fields
    discovered: bool                    # True if daily discovery LLM node has run
    selected_sources: List[Dict[str, Any]] # Selected sources from registry (subreddits/search queries with modes/cursors)
    raw_candidates: List[Dict[str, Any]]   # Candidates fetched from scraper --list-only
    vetted_candidates: List[Dict[str, Any]]# Surviving candidates after title pre-screen
    dropped_titles: List[Dict[str, Any]]   # Candidates dropped during title pre-screen

    # Final outputs
    edited_videos: List[Dict[str, Any]] # Branded output video paths
    rights_evaluations: List[Dict[str, Any]] # Phase 6 Rights check evaluations
    final_schedule: List[Dict[str, Any]]# 24-slot hourly schedule

