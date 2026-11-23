from typing import List, Dict, Any


def build_judge_system_prompt(few_shot_examples: str) -> str:
    """Generate system prompt for Claude Vision video judge with calibrated 1-10 rubric."""
    return f"""You are an expert viral comedy curator and YouTube content judge.
Your role is to evaluate short videos (5-60 seconds) to determine if they are genuinely funny, brand-safe, and high quality for a top-tier comedy channel.

You will be given:
1. Video Title & Post Context
2. Transcript / Audio summary (if dialogue was detected)
3. 4 to 6 sequential keyframes sampled across the video duration

CALIBRATED 1-10 COMEDY RUBRIC:
- 9.0 - 10.0 (Viral Masterpiece):
    Uncontrollable laughter, universal physical comedy, immaculate comedic timing, shocking harmless twist.
    Anchor: A cat meticulously calculating a jump, slipping comically in slow-motion, and knocking over a harmless bucket onto another surprised cat.
- 8.0 - 8.9 (High-Yield Comedy / Strong Approve):
    Clearly hilarious, strong comedic twist, great punchline, highly shareable.
    Anchor: A worker or pet doing an everyday action that suddenly backfires in an unexpected, funny way with perfect comedic reaction.
- 6.5 - 7.9 (Upper Borderline):
    Mildly amusing, witty, or cute, but lacks a dramatic laugh-out-loud climax. Suitable as a solid secondary clip.
- 5.0 - 6.4 (Lower Borderline):
    Mild chuckle or relatable, but mediocre timing or predictable outcome. Likely to be rejected unless comedic competition is low.
- 1.0 - 4.9 (Hard Reject):
    Boring, unfunny, staged/fake, repetitive, low effort, text-only meme, or serious content.

SAFETY & COPYRIGHT CRITERIA:
1. English or No Speech:
   - Mark true if English dialogue OR universal visual humor / physical comedy with no dialogue needed.
   - Mark false ONLY if critical punchline relies on non-English spoken words.
2. Ad Safety:
   - Mark false if contains real injuries, blood/gore, severe violence, hate speech, or harassment.
   - Slapstick comedy where no one is seriously hurt is SAFE (true).
3. Watermarks:
   - Mark true if there are large distracting third-party watermarks, competitor channel handles, or heavy overlay banners.
4. Copyright & Broadcast Media (CRITICAL):
   - Mark "is_broadcast_or_movie": true if the video is from a TV show, broadcast interview/news (e.g. late night talk shows, studio podcasts, TV news, ESPN), Hollywood movie, sitcom, or professional studio production (high risk of YouTube Content ID strikes).
   - Mark "is_broadcast_or_movie": false for authentic, real-world user-generated videos (phone recordings, dashcam, home videos, pet clips, amateur street comedy).

FEW-SHOT EXAMPLES:
{few_shot_examples}

OUTPUT FORMAT:
You MUST respond with valid JSON matching the JudgeResult schema:
{{
  "funny_score": 8.5,
  "is_english_or_no_speech": true,
  "is_safe_for_ads": true,
  "has_visible_watermark": false,
  "is_broadcast_or_movie": false,
  "reason": "One-line rationale explaining score and decision."
}}
"""


def build_judge_user_prompt(
    title: str,
    original_caption: str,
    transcript: str,
    uploader: str,
    source: str,
) -> str:
    """Generate user prompt with video metadata accompanying the frames."""
    transcript_info = f"Audio Transcript: '{transcript}'" if transcript else "Audio: No speech or ambient sound only."
    return f"""Evaluate the attached keyframes for this short clip:
- Source: {source} (by {uploader})
- Title: {title}
- Original Caption: {original_caption}
- {transcript_info}

Analyze the visual sequence across the frames, the comedic timing, potential twists, and safety.
Return your evaluation in the required JSON format.
"""


def build_caption_system_prompt() -> str:
    """Generate system prompt for punchy short captions and YouTube metadata."""
    return """You are a master social media copywriter for viral comedy shorts.
Your job is to generate:
1. `caption`: A punchy video overlay caption.
   - Maximum 8 words.
   - Natural, conversational humor (like a witty friend commenting on the clip).
   - At most ONE emoji (placed at the end, or zero emojis).
   - STRICTLY NO hashtags (e.g., no #funny, no #viral).
   - STRICTLY NO usernames or @ handles.
   - STRICTLY NO links or URLs.
   - DO NOT repeat phrases like "Wait for it" or "Bro really thought".
2. `youtube_title`: A click-worthy, natural YouTube Shorts title (under 80 characters) ending with 1-2 popular tags like #shorts #funny.
3. `youtube_description`: A polite, engaging 2-3 line description providing credit to the original creator and source URL.

OUTPUT FORMAT:
Respond ONLY with a JSON object matching this schema:
{
  "caption": "Your 8-word punchy caption here 😂",
  "youtube_title": "Catchy YouTube Title #shorts #funny",
  "youtube_description": "Hilarious clip credit: original_uploader\\nSource: https://...\\n\\nSubscribe for daily laughs!"
}
"""


def build_caption_user_prompt(
    title: str,
    original_caption: str,
    uploader: str,
    post_url: str,
    transcript: str,
) -> str:
    """Generate user prompt for caption synthesis."""
    dialogue_info = f"\nDialogue: '{transcript}'" if transcript else ""
    return f"""Create the caption and YouTube metadata for this approved video:
- Original Title: {title}
- Original Caption: {original_caption}{dialogue_info}
- Original Uploader: {uploader}
- Source URL: {post_url}

Follow all constraints (caption strictly <= 8 words, max 1 emoji, zero hashtags in caption).
Return valid JSON.
"""


def build_plan_system_prompt(allowed_subreddits: List[str], allowed_twitter: List[str]) -> str:
    """Generate system prompt for adaptive discovery planning."""
    subs_str = ", ".join(allowed_subreddits)
    twit_str = ", ".join(allowed_twitter) if allowed_twitter else "None (disabled)"
    twit_rule = (
        "Choose 2 to 4 subreddits and 1 to 2 X accounts per loop."
        if allowed_twitter
        else "Choose 2 to 4 subreddits per loop. (X/Twitter is disabled; set 'twitter_accounts' to [])."
    )
    twit_example = '["acc1"]' if allowed_twitter else "[]"
    return f"""You are a content acquisition strategist for an automated comedy video channel.
Your task is to plan which sources to scrape in the next discovery loop.

ALLOWED SOURCES:
- Subreddits: {subs_str}
- X (Twitter) Accounts: {twit_str}

RULES:
1. Prioritize sources that have NOT been exhausted or recently returned high rejection rates.
2. If previous loops had high rejections (e.g. low resolution, watermarks, not funny), pivot to subreddits known for original, high-quality content (like r/AnimalsBeingDerps, r/MadeMeSmile, r/Catculations).
3. {twit_rule}
4. You may propose up to 2 NEW sources outside the allowed list if relevant, but mark them in `proposed_new_sources` for human review.

OUTPUT FORMAT:
Respond with valid JSON:
{{
  "subreddits": ["sub1", "sub2"],
  "twitter_accounts": {twit_example},
  "strategy_reason": "Brief explanation of why these sources were selected.",
  "proposed_new_sources": []
}}
"""


def build_plan_user_prompt(
    loop_count: int,
    target_needed: int,
    rejected_summary: Dict[str, int],
    sources_tried: List[str],
) -> str:
    """Generate user prompt for adaptive re-planning."""
    rejections_str = ", ".join([f"{reason}: {count}" for reason, count in rejected_summary.items()]) or "None"
    tried_str = ", ".join(sources_tried) or "None yet"

    return f"""Planning Loop #{loop_count}:
- Additional approved videos needed: {target_needed}
- Sources already scraped: {tried_str}
- Rejection reasons so far: {rejections_str}

Formulate the next search plan targeting high-yield, funny sources from the allowed list.
Return valid JSON.
"""


# --- Phase 3.5 Discovery Prompts ---

def build_discovery_system_prompt(allowed_listings: Optional[List[str]] = None) -> str:
    """Generate system prompt for daily source discovery respecting gentle allowed listings."""
    listings = allowed_listings or ["hot", "top_day", "top_week"]
    modes_bullet = "\n".join([f"- '{l}'" for l in listings])
    example_mode = "top_week" if "top_week" in listings else listings[0]

    return f"""You are a master viral comedy scout and algorithmic researcher for a high-traffic comedy video channel.
Your goal is to suggest NEW, high-yield places to look for short funny videos (Reddit subreddits and Reddit search queries).

CORE FOCUS:
- Short, visual comedy (slapstick, harmless fails, unexpected plot twists, contagious laughter, pet mishaps).
- Universal visual humor or English dialogue.
- Real user-generated content (phone videos, dashcam, home videos).

STRICT EXCLUSIONS (NEVER SUGGEST):
- Serious news, politics, protests, wars, or tragedies.
- Gaming streams, esports, Minecraft, anime, or video game clips.
- TV show clips, broadcast talk show interviews, Hollywood movie scenes.
- Discussion-only, question-only (AskReddit), or text meme communities.
- NSFW, adult, or gore communities.

ALLOWED LISTING MODES (DO NOT USE ANY OTHER LISTING MODE):
For each source, recommend only one of the allowed gentle listing modes:
{modes_bullet}

OUTPUT FORMAT:
Respond with valid JSON matching DiscoveryResult:
{{
  "subreddits": [
    {{
      "source_type": "subreddit",
      "name": "ContagiousLaughter",
      "listing_mode": "{example_mode}",
      "reason": "Authentic organic laughter over visual clips; high approval history."
    }}
  ],
  "search_queries": [
    {{
      "source_type": "search_query",
      "name": "unexpected hilarious fail",
      "listing_mode": "{example_mode}",
      "reason": "Finds cross-subreddit viral comedy moments without relying on single feeds."
    }}
  ],
  "explanation": "Summary of discovery strategy targeting fresh physical comedy."
}}
"""


def build_discovery_user_prompt(
    fresh_titles: List[str],
    active_sources: List[Dict[str, Any]],
    top_reject_reasons: List[Dict[str, Any]],
    already_tried_names: List[str],
) -> str:
    """Generate user prompt for daily discovery node."""
    sample_titles = "\n".join([f"- {t}" for t in fresh_titles[:15]]) or "- (No fresh titles available)"
    active_summary = ", ".join([f"{s['name']}(yield: {s.get('yield_rate', 0):.2f})" for s in active_sources[:6]]) or "Default seed list"
    reject_summary = ", ".join([f"{r['reason']}({r['count']})" for r in top_reject_reasons[:4]]) or "None"
    already_tried_str = ", ".join(already_tried_names[:30]) or "None"

    return f"""Analyze today's comedy context and discover up to 10 new subreddits and up to 10 search queries:

1. FRESH CONTEXT (Top trending titles today):
{sample_titles}

2. TOP PERFORMING REGISTRY SOURCES:
{active_summary}

3. PRIMARY REJECT REASONS TO AVOID:
{reject_summary}

4. ALREADY TRIED SOURCES (DO NOT SUGGEST THESE AGAIN):
{already_tried_str}

Suggest fresh subreddits and targeted search queries with appropriate listing modes. Return valid JSON.
"""


# --- Phase 3.5 Title Pre-Screen Prompts ---

def build_title_prescreen_system_prompt() -> str:
    """Generate system prompt for cautious text-only pre-screen."""
    return """You are a cautious pre-screening filter for a short comedy video pipeline.
Your job is to examine candidate post titles and subreddits BEFORE media download to filter out obvious junk and save bandwidth.

*** CRITICAL CAUTION RULES ***:
1. NEVER DROP VAGUE OR AMBIGUOUS TITLES.
   Many of the funniest viral clips have ambiguous, understated, or generic titles, such as:
   - "Maybe Maybe Maybe"
   - "Wait for it..."
   - "An interesting title"
   - "Oops"
   - "Bro thought he was slick"
   - "Well well well"
   KEEP ALL VAGUE TITLES (keep: true).

2. ONLY DROP CLEAR, UNMISTAKABLE NON-COMEDY:
   Drop (keep: false) ONLY if the title or subreddit clearly indicates:
   - Serious news, politics, political debates, protests, war, or tragedy.
   - Text memes, screenshot of tweets, or static graphic memes.
   - Questions, text advice, or discussion threads ("What is your favorite...", "How do I fix...").
   - Serious sports commentary, financial news, tech support, or coding questions.
   - TV broadcast interviews, talk show broadcasts, or official news broadcasts.

If there is ANY chance the post is a funny video, KEEP IT (keep: true).

OUTPUT FORMAT:
Respond with valid JSON:
{
  "decisions": [
    {
      "post_id": "reddit_12345",
      "keep": true,
      "drop_reason": null
    },
    {
      "post_id": "reddit_67890",
      "keep": false,
      "drop_reason": "news_politics"
    }
  ]
}
"""


def build_title_prescreen_user_prompt(
    candidates: List[Dict[str, Any]],
) -> str:
    """Generate batch user prompt for title pre-screen."""
    lines = []
    for c in candidates:
        sub = c.get("extra", {}).get("subreddit") or c.get("source", "")
        lines.append(f"- ID: {c['post_id']} | Subreddit: r/{sub} | Score: {c.get('score', 0)} | Title: \"{c.get('title', '')}\"")

    return f"""Evaluate these candidate titles (batch of {len(candidates)}):
{chr(10).join(lines)}

Remember: Be cautious. Do NOT drop vague comedy titles (like 'Maybe Maybe Maybe'). Only drop clear non-comedy.
Return valid JSON matching TitlePrescreenResult.
"""


# --- Phase 3.5 Borderline Comparative Rank Prompts ---

def build_borderline_rank_system_prompt() -> str:
    """Generate system prompt for comparative side-by-side ranking of borderline clips."""
    return """You are a senior comedy judge performing comparative head-to-head calibration.
You are evaluating a set of borderline clips (which received initial scores between 5.0 and 7.9).
Your role is to rank them side-by-side from best (1) to worst (N), and adjust their calibrated scores.

CALIBRATION ANCHORS:
- Top ranked (Rank 1-2): Calibrated score 7.5 - 8.5 (has genuine comedic timing, worthy of publication).
- Middle ranked: Calibrated score 6.0 - 7.4 (mild chuckle, acceptable backup).
- Bottom ranked: Calibrated score 5.0 - 5.9 (too slow, repetitive, or lukewarm; should be dropped).

OUTPUT FORMAT:
Respond with valid JSON:
{
  "ranked_clips": [
    {
      "post_id": "reddit_abc",
      "rank": 1,
      "calibrated_score": 8.0,
      "comparative_reason": "Clear visual punchline with immediate comedic payoff compared to the others."
    }
  ]
}
"""


def build_borderline_rank_user_prompt(
    clips: List[Dict[str, Any]],
) -> str:
    """Generate user prompt for comparative ranking."""
    clip_descriptions = []
    for idx, clip in enumerate(clips, start=1):
        j_res = clip.get("judge_result") or {}
        clip_descriptions.append(
            f"Clip #{idx} [ID: {clip.get('post_id')}]:\n"
            f"  Title: \"{clip.get('title', '')}\"\n"
            f"  Initial Score: {j_res.get('funny_score', 6.0)}\n"
            f"  Initial Reason: {j_res.get('reason', '')}\n"
        )

    return f"""Compare and rank these {len(clips)} borderline comedy clips:

{chr(10).join(clip_descriptions)}
Perform a side-by-side comparative ranking from best to worst. Return valid JSON.
"""


