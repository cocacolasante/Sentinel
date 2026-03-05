"""
ContentRepurposeSkill — convert existing content to another format.

Extracts key ideas from the source and restructures them for the target format
using that format's specific rules and structure.

Intent: content_repurpose
Params:
  source_type    — blog | youtube_script | podcast_transcript | thread | newsletter | linkedin_post
  target_type    — any of the above, or: instagram_caption | tiktok_script | email | ad_copy | slides
  source_content — the raw content to repurpose (pasted in message or param)
  preserve       — comma-separated elements to keep: hook | stats | examples | cta
  audience_shift — if the target audience differs from source (optional)
"""

from __future__ import annotations

from app.skills.base import BaseSkill, SkillResult

_EXTRACTION_RULES: dict[str, str] = {
    "blog": "Extract: main thesis, 3-5 key points, any statistics or examples, conclusion/CTA",
    "youtube_script": "Extract: hook, section headings, key examples or stories, CTA, memorable one-liners",
    "podcast_transcript": "Extract: key insights (ignore filler words), memorable quotes, actionable takeaways, any stats mentioned",
    "thread": "Extract: hook tweet, each numbered point, final CTA tweet",
    "newsletter": "Extract: subject line idea, main insight, supporting points, CTA",
    "linkedin_post": "Extract: hook line, core lesson or story, engagement question",
}

_FORMAT_RULES: dict[str, str] = {
    "blog": """Target: Blog Post
Structure: SEO title → hook intro → 3-5 H2 sections with subpoints → conclusion → CTA
Length: 800-1,500 words. Include a meta description (under 160 chars) at the top.""",
    "youtube_script": """Target: YouTube Script
Structure: Hook (first 30 sec) → Problem/Context → Main content with section labels → Summary → CTA → Outro
Add [B-ROLL] and [PAUSE] markers. Write for spoken delivery — short sentences, natural rhythm.""",
    "podcast_transcript": """Target: Podcast Talking Points
Structure: Intro hook → Agenda (3-5 topics) → Talking points per topic (not a script — conversational bullets) → Outro CTA
Include natural segue lines between sections.""",
    "thread": """Target: Twitter/X Thread
Tweet 1: hook — the entire value in one line (max 280 chars)
Tweets 2-N: numbered points, one idea per tweet, max 280 chars each
Final tweet: summary + CTA + follow prompt
Label each tweet: [1/N], [2/N], etc.""",
    "newsletter": """Target: Email Newsletter
Components: Subject line (under 50 chars) | Preview text (85-100 chars) | Opener (personal, 1-2 sentences) | Main value section | One insight or story | CTA
Tone: like writing to one person. Sign off with a first name.""",
    "linkedin_post": """Target: LinkedIn Post
Structure: Bold hook line → blank line → 3-5 short paragraphs or bullet points → blank line → reflection question or CTA → 3-5 hashtags
Line breaks after every 1-2 sentences. Max 3,000 chars.""",
    "instagram_caption": """Target: Instagram Caption
First 125 chars must work as a standalone hook (the only visible text without tapping 'more')
Structure: Hook → Value/Story → CTA → [blank lines] → 5-10 hashtags
Include 2-4 emoji. Keep body under 2,200 chars.""",
    "tiktok_script": """Target: TikTok Video Script
Structure: Hook (first 3 sec, bold visual/statement) → Setup (5-10 sec) → Content (30-45 sec, 3-5 points) → CTA (final 5 sec)
Write the hook, voiceover text, and on-screen text separately. Total: 45-60 seconds.""",
    "email": """Target: Email (single send)
Components: Subject (under 50 chars, A/B variant) | Preview text (85-100 chars) | Body (problem → solution → proof → CTA) | P.S. line
Under 300 words. One CTA only.""",
    "ad_copy": """Target: Ad Copy (Meta/Social)
Components: Primary text (first 125 chars are critical) | Headline (40 chars) | Description (30 chars)
Framework: lead with pain point or outcome. 3 headline variants for A/B testing.""",
    "slides": """Target: Presentation Slides
Structure: Title slide → Agenda (3-5 bullets) → One idea per slide (title + 3 bullets max) → Key stat slides → CTA/Next steps slide
Each slide: title (under 8 words) + 3 bullet points max. Include speaker notes under each slide.""",
}


class ContentRepurposeSkill(BaseSkill):
    name = "content_repurpose"
    description = "Repurpose existing content to another format: blog → thread, video script → newsletter, podcast → LinkedIn, etc."
    trigger_intents = ["content_repurpose"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        source_type = params.get("source_type", "blog").lower()
        target_type = params.get("target_type", "thread").lower()
        source_content = params.get("source_content", "").strip()
        preserve = params.get("preserve", "")
        audience_shift = params.get("audience_shift", "")

        # If source content isn't in params, the user likely pasted it in their message
        if not source_content:
            source_content = "[Source content is in the user's message above]"

        extraction = _EXTRACTION_RULES.get(source_type, f"Extract the key ideas and structure from this {source_type}")
        target_fmt = _FORMAT_RULES.get(target_type, f"Repurpose into: {target_type}")

        preserve_block = f"\nElements to preserve from source: {preserve}" if preserve else ""
        audience_block = (
            f"\nAudience shift: the target format is for {audience_shift} — adjust tone and examples accordingly."
            if audience_shift
            else ""
        )

        context = f"""Content repurposing brief:

Source format:  {source_type.replace("_", " ").title()}
Target format:  {target_type.replace("_", " ").title()}
{preserve_block}
{audience_block}

Step 1 — Extract from source ({source_type.replace("_", " ")}):
{extraction}

Source content:
---
{source_content}
---

Step 2 — Produce the repurposed output:
{target_fmt}

Do not summarize — fully produce the complete {target_type.replace("_", " ")} output, ready to use.
No preamble or explanation. Output the content directly."""

        return SkillResult(context_data=context, skill_name=self.name)
