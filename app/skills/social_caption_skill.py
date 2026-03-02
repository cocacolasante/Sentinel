"""
SocialCaptionSkill — platform-specific social media captions.

Returns platform specs (character limits, hashtag rules, structure) as context
so the marketing agent produces captions that are immediately post-ready.

Intent: social_caption
Params:
  platform        — instagram | linkedin | twitter | tiktok | facebook
  topic           — what the post is about
  goal            — engagement | awareness | conversion | authority
  include_hashtags — true | false (default true)
  include_cta      — true | false (default true)
  variations       — number of caption variants to produce (1-3, default 3)
  hook            — optional first line / hook text to start from
"""

from __future__ import annotations

from app.skills.base import BaseSkill, SkillResult

_PLATFORM_SPECS: dict[str, dict] = {
    "instagram": {
        "char_limit":    2200,
        "visible_chars": 125,
        "hashtag_count": "5-10 hashtags",
        "hashtag_placement": "after 2 blank lines at the end of caption",
        "structure": "Hook (line 1, max 125 chars, the only thing visible without 'more') → Value or story → CTA → [blank line] → [blank line] → Hashtags",
        "emoji_guidance": "2-4 emoji in body, 1 per hashtag line optional",
        "notes": "Line 1 is everything. It must stop the scroll. Avoid starting with your name or brand.",
    },
    "linkedin": {
        "char_limit":    3000,
        "visible_chars": 210,
        "hashtag_count": "3-5 hashtags",
        "hashtag_placement": "end of post",
        "structure": "Hook line (bold idea or contrarian take) → blank line → 3-5 short paragraphs or numbered points → blank line → CTA or question → Hashtags",
        "emoji_guidance": "minimal — 0-2 per post, only if natural",
        "notes": "LinkedIn rewards personal stories + professional lessons. Lead with insight, not announcements. Line breaks every 1-2 sentences.",
    },
    "twitter": {
        "char_limit":    280,
        "visible_chars": 280,
        "hashtag_count": "1-2 hashtags max",
        "hashtag_placement": "end of tweet or woven into text",
        "structure": "One punchy idea per tweet. If thread: hook tweet → numbered insights → summary + CTA",
        "emoji_guidance": "1 emoji max, only if it adds meaning",
        "notes": "No padding. Every word earns its place. Avoid 'Just posted:', 'Excited to share:', or any filler opener.",
    },
    "tiktok": {
        "char_limit":    2200,
        "visible_chars": 100,
        "hashtag_count": "3-6 hashtags, mix niche + trending",
        "hashtag_placement": "end of caption",
        "structure": "1-line hook (what the video is about) → 1-2 lines supporting detail → CTA (comment/follow/link in bio) → Hashtags",
        "emoji_guidance": "2-5 emoji, placed naturally",
        "notes": "Caption supports the video — it's not standalone. Hook must complement the video's first second. Include a question to drive comments.",
    },
    "facebook": {
        "char_limit":    63206,
        "visible_chars": 477,
        "hashtag_count": "1-3 hashtags",
        "hashtag_placement": "end of post",
        "structure": "Hook (question or bold statement) → Story or context → Value → CTA → Hashtags",
        "emoji_guidance": "2-4 emoji, natural placement",
        "notes": "Facebook rewards conversational tone and longer stories. Ask a question to drive comments. Tag relevant pages if applicable.",
    },
}

_GOAL_CTAS: dict[str, dict[str, str]] = {
    "engagement": {
        "instagram": "Drop a 🔥 if you agree — or tell me what I'm missing.",
        "linkedin":  "What's your take? Drop it in the comments.",
        "twitter":   "Hot take or agree? Reply below.",
        "tiktok":    "Comment your answer below 👇",
        "facebook":  "Do you agree? Let me know in the comments.",
    },
    "awareness": {
        "instagram": "Save this for later + share with someone who needs it.",
        "linkedin":  "Repost if this resonates with someone in your network.",
        "twitter":   "RT if this hits.",
        "tiktok":    "Share this if it helped you 🙏",
        "facebook":  "Tag someone who needs to see this.",
    },
    "conversion": {
        "instagram": "Link in bio → [what they get].",
        "linkedin":  "DM me '[keyword]' and I'll send you the details.",
        "twitter":   "Link below. [what they get] →",
        "tiktok":    "Link in bio for [what they get] 👆",
        "facebook":  "Click the link to [specific action].",
    },
    "authority": {
        "instagram": "Follow for more on [topic].",
        "linkedin":  "Follow me for weekly insights on [topic].",
        "twitter":   "Follow for more like this.",
        "tiktok":    "Follow for more [topic] content 📲",
        "facebook":  "Like the page for more [topic] content.",
    },
}


class SocialCaptionSkill(BaseSkill):
    name = "social_caption"
    description = "Generate platform-specific social media captions for Instagram, LinkedIn, Twitter, TikTok, Facebook"
    trigger_intents = ["social_caption"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        platform         = params.get("platform", "instagram").lower()
        topic            = params.get("topic", original_message)
        goal             = params.get("goal", "engagement").lower()
        include_hashtags = str(params.get("include_hashtags", "true")).lower() != "false"
        include_cta      = str(params.get("include_cta", "true")).lower() != "false"
        variations       = min(int(params.get("variations", 3)), 3)
        hook_seed        = params.get("hook", "")

        spec = _PLATFORM_SPECS.get(platform, _PLATFORM_SPECS["instagram"])
        cta  = _GOAL_CTAS.get(goal, _GOAL_CTAS["engagement"]).get(platform, "")

        hook_block = f"\nHook seed (use or improve): {hook_seed}" if hook_seed else ""
        cta_block  = f"\nCTA to use (adapt as needed): {cta}" if include_cta and cta else ""
        hashtag_block = (
            f"\nHashtags: include {spec['hashtag_count']} placed {spec['hashtag_placement']}"
            if include_hashtags else "\nHashtags: omit"
        )

        context = f"""Platform caption brief:

Platform:         {platform.title()}
Topic:            {topic}
Goal:             {goal}
Character limit:  {spec['char_limit']} total ({spec['visible_chars']} visible before 'more')
Variations:       {variations}
{hook_block}
Structure:        {spec['structure']}
Emoji guidance:   {spec['emoji_guidance']}
{hashtag_block}
{cta_block}
Platform notes:   {spec['notes']}

Produce exactly {variations} caption variation{"s" if variations > 1 else ""}, labeled:

Variation A:
[caption]

{"Variation B:" + chr(10) + "[caption]" + chr(10) + chr(10) if variations >= 2 else ""}{"Variation C:" + chr(10) + "[caption]" if variations >= 3 else ""}

Each variation must be complete and ready to copy-paste. No preamble, no explanation after — just the captions."""

        return SkillResult(context_data=context, skill_name=self.name)
