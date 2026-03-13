"""
ContentDraftSkill — long-form content drafting.

Produces structured briefs for the marketing agent to fill:
blog posts, newsletters, YouTube scripts, Twitter/X threads, email sequences.

Intent: content_draft
Params:
  type           — blog | newsletter | youtube_script | thread | email_sequence
  topic          — the subject matter
  target_audience — who this is written for
  goal           — awareness | engagement | conversion | authority
  length         — short | medium | long (word count hints per type)
  tone           — professional | casual | bold | educational
  keywords       — comma-separated SEO keywords (optional)
"""

from __future__ import annotations

from app.skills.base import BaseSkill, SkillResult

# Word count targets per type × length
_LENGTH_MAP: dict[str, dict[str, str]] = {
    "blog": {
        "short": "600-800 words",
        "medium": "1,200-1,500 words",
        "long": "2,000-2,500 words",
    },
    "newsletter": {
        "short": "250-350 words",
        "medium": "500-700 words",
        "long": "900-1,200 words",
    },
    "youtube_script": {
        "short": "3-5 min (~500-700 words spoken)",
        "medium": "8-12 min (~1,200-1,800 words spoken)",
        "long": "15-20 min (~2,200-3,000 words spoken)",
    },
    "thread": {
        "short": "5-7 tweets",
        "medium": "10-12 tweets",
        "long": "15-20 tweets",
    },
    "email_sequence": {
        "short": "3-email sequence",
        "medium": "5-email sequence",
        "long": "7-email sequence",
    },
}

_FORMAT_SPECS: dict[str, str] = {
    "blog": """Structure: H1 title → hook paragraph (no 'I will...' openers) → 3-5 H2 sections with subpoints → takeaway section → CTA.
SEO: include primary keyword in H1, first paragraph, and at least 2 H2s. Use secondary keywords naturally.
Formatting: use bullet points for lists of 3+ items. Bold key phrases. Add a meta description under 160 chars at the top.""",
    "newsletter": """Structure: subject line + preview text → personal opener (1-2 sentences) → main value section → 1 key insight or story → CTA.
Subject line: under 50 chars, no clickbait. Preview text: 85-100 chars that complement the subject.
Tone: conversational, like writing to one person. Sign off with a first name.""",
    "youtube_script": """Structure: Hook (first 30 sec, no intro) → Problem/Context → Main content (3-5 sections) → Summary → CTA → Outro (keep under 15 sec).
Pacing: write for spoken delivery. Short sentences. Use ellipses for natural pauses...
Include: [B-ROLL] and [CUT TO] markers. Add timestamps for sections. Hook must answer "why should I watch this?".""",
    "thread": """Structure: Tweet 1 = hook (the entire value prop in one line). Tweet 2-N = numbered points or story beats. Last tweet = summary + CTA + follow prompt.
Each tweet: max 280 chars. No filler. No 'a thread 🧵' openers — start with the hook.
Format each tweet on a new numbered line. Emoji: 1 per tweet max.""",
    "email_sequence": """For each email include: Subject line | Preview text | Body | CTA.
Sequence arc: Email 1 = welcome/problem agitation. Email 2 = solution introduction. Email 3 = social proof. Email 4+ = objection handling → conversion.
Subject lines: A/B variant for each. Keep under 50 chars.""",
}


class ContentDraftSkill(BaseSkill):
    name = "content_draft"
    description = "Draft and write long-form content: blog posts, articles, technical documentation, emails, reports, essays, proposals. Use when Anthony says 'write a blog post', 'draft an article about', 'write content for', 'create a report on', 'draft a technical doc', or 'write a long-form piece about'. NOT for: social media captions (use social_caption), ad copy (use ad_copy), or repurposing existing content (use content_repurpose)."
    trigger_intents = ["content_draft"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        content_type = params.get("type", "blog").lower()
        topic = params.get("topic", original_message)
        target_audience = params.get("target_audience", "founders and operators")
        goal = params.get("goal", "awareness")
        length = params.get("length", "medium").lower()
        tone = params.get("tone", "direct")
        keywords = params.get("keywords", "")

        length_target = _LENGTH_MAP.get(content_type, _LENGTH_MAP["blog"]).get(length, "medium length")
        format_spec = _FORMAT_SPECS.get(content_type, _FORMAT_SPECS["blog"])

        keyword_block = f"\nSEO Keywords to incorporate: {keywords}" if keywords else ""

        context = f"""Content production brief:

Type:             {content_type.replace("_", " ").title()}
Topic:            {topic}
Target audience:  {target_audience}
Goal:             {goal}
Length target:    {length_target}
Tone:             {tone}
{keyword_block}

Format requirements:
{format_spec}

Produce the complete {content_type.replace("_", " ")} now. Do not include a preamble — output the content directly, ready to publish."""

        return SkillResult(context_data=context, skill_name=self.name)
