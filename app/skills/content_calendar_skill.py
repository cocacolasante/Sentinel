"""
ContentCalendarSkill — generate a structured content calendar.

Produces a posting schedule with topic ideas, formats, and platform assignments
based on the requested period, platforms, and strategic themes.

Intent: content_calendar
Params:
  period         — week | month | quarter (default: month)
  platforms      — comma-separated list (default: instagram,linkedin,twitter)
  themes         — comma-separated content themes or pillars (optional)
  posting_freq   — posts per platform per week (default: 3)
  goal           — awareness | leads | engagement | authority (default: awareness)
  content_mix    — e.g. "60% educational, 30% personal, 10% promotional" (optional)
"""

from __future__ import annotations

from app.skills.base import BaseSkill, SkillResult

_PERIOD_CONFIG: dict[str, dict] = {
    "week": {
        "label":      "1-Week Content Calendar",
        "slots":      "7 days",
        "output":     "Day-by-day schedule with 1-2 posts per day",
        "columns":    "Day | Platform | Format | Topic / Hook | Goal | Notes",
    },
    "month": {
        "label":      "Monthly Content Calendar",
        "slots":      "4 weeks",
        "output":     "Week-by-week schedule with daily slots per platform",
        "columns":    "Week | Day | Platform | Format | Topic / Hook | Pillar | Goal",
    },
    "quarter": {
        "label":      "Quarterly Content Calendar",
        "slots":      "13 weeks",
        "output":     "Monthly overview with weekly themes and key campaign dates",
        "columns":    "Month | Week | Platform | Theme | Content Type | Campaign Tie-in",
    },
}

_PLATFORM_BEST_TIMES: dict[str, str] = {
    "instagram": "Tue/Wed/Fri — 9am-11am or 5pm-7pm local",
    "linkedin":  "Tue/Wed/Thu — 8am-10am or 12pm local",
    "twitter":   "Mon-Fri — 9am, 12pm, 5pm local (3x/day)",
    "tiktok":    "Tue/Thu/Sat — 7pm-9pm local",
    "facebook":  "Wed — 11am-1pm; also Fri 9am",
    "youtube":   "Fri/Sat — 2pm-4pm local",
}

_DEFAULT_CONTENT_MIX = "40% educational/value, 30% personal/behind-the-scenes, 20% promotional, 10% engagement/questions"

_DEFAULT_THEMES = [
    "Industry insights and hot takes",
    "Personal story / founder journey",
    "Product or service spotlight",
    "Customer wins / social proof",
    "Educational how-to content",
]

_FORMAT_OPTIONS: dict[str, list[str]] = {
    "instagram": ["Reel", "Carousel", "Static post", "Stories", "Collab post"],
    "linkedin":  ["Text post", "Document carousel", "Article", "Poll", "Video"],
    "twitter":   ["Tweet", "Thread", "Poll", "Quote tweet"],
    "tiktok":    ["Original video", "Duet", "Stitch", "Trending audio"],
    "facebook":  ["Post", "Reel", "Live", "Story", "Event"],
    "youtube":   ["Long-form video", "Short", "Community post", "Premiere"],
}


class ContentCalendarSkill(BaseSkill):
    name = "content_calendar"
    description = "Generate a content calendar with topic ideas, formats, and posting schedule for specified platforms"
    trigger_intents = ["content_calendar"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        period       = params.get("period", "month").lower()
        platforms_raw = params.get("platforms", "instagram,linkedin,twitter")
        themes_raw   = params.get("themes", "")
        posting_freq = int(params.get("posting_freq", 3))
        goal         = params.get("goal", "awareness")
        content_mix  = params.get("content_mix", _DEFAULT_CONTENT_MIX)

        platforms = [p.strip().lower() for p in platforms_raw.split(",") if p.strip()]
        themes    = [t.strip() for t in themes_raw.split(",") if t.strip()] if themes_raw else _DEFAULT_THEMES

        config = _PERIOD_CONFIG.get(period, _PERIOD_CONFIG["month"])

        # Best posting times for requested platforms
        timing_lines = [
            f"  {p.title()}: {_PLATFORM_BEST_TIMES.get(p, 'research best time for your audience')}"
            for p in platforms
        ]

        # Available formats per platform
        format_lines = [
            f"  {p.title()}: {', '.join(_FORMAT_OPTIONS.get(p, ['Post', 'Video', 'Story']))}"
            for p in platforms
        ]

        total_posts = posting_freq * len(platforms) * (4 if period == "month" else 13 if period == "quarter" else 1)

        context = f"""Content calendar production brief:

Calendar type:    {config['label']}
Period:           {period.title()} ({config['slots']})
Platforms:        {', '.join(p.title() for p in platforms)}
Posts per platform per week: {posting_freq}
Estimated total posts: ~{total_posts}
Campaign goal:    {goal}

Content pillars / themes:
{chr(10).join(f"  {i+1}. {t}" for i, t in enumerate(themes))}

Content mix:
  {content_mix}

Best posting times (reference only):
{chr(10).join(timing_lines)}

Available formats per platform:
{chr(10).join(format_lines)}

Output format: {config['output']}
Columns: {config['columns']}

Instructions:
- Produce the complete calendar as a structured table or day-by-day list
- For each slot include: specific topic/hook idea (not just "educational post")
- Vary formats across the week — avoid repeating the same format on consecutive days
- Flag 2-3 "anchor" pieces (highest effort content to produce first and repurpose from)
- Add a "Repurpose opportunities" section at the end: list which anchor pieces to break into smaller posts
- Include a content production checklist at the end (what to batch-create per week)

Produce the full calendar now."""

        return SkillResult(context_data=context, skill_name=self.name)
