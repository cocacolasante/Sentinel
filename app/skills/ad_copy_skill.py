"""
AdCopySkill — paid advertising copy across platforms.

Returns character limits, component structure, and copywriting framework
so every ad produced is spec-compliant and conversion-focused.

Intent: ad_copy
Params:
  platform        — meta | google | linkedin | tiktok | youtube
  product         — what is being advertised
  target_audience — who sees this ad
  goal            — conversions | leads | awareness | traffic | installs
  offer           — the specific offer, discount, or hook (optional)
  objection       — the main objection to overcome (optional)
  framework       — aida | pas | bab | fab (default: aida)
"""

from __future__ import annotations

from app.skills.base import BaseSkill, SkillResult

# Platform-specific ad component specs
_PLATFORM_SPECS: dict[str, dict] = {
    "meta": {
        "display_name": "Meta (Facebook / Instagram) Ads",
        "components": {
            "primary_text":  "125 chars (125 visible before 'See more'); max 2,200",
            "headline":      "40 chars — appears below the creative",
            "description":   "30 chars — appears under headline in some placements",
            "link_description": "optional; 30 chars",
        },
        "notes": (
            "Primary text: lead with the pain point or outcome, not the product. "
            "Headline: benefit-first, specific number or claim. "
            "Produce 3 headline variants for A/B testing. "
            "First 125 chars of primary text must work standalone."
        ),
        "formats": ["Single image", "Carousel", "Video (hook in first 3 sec)", "Stories (9:16, 15 sec)"],
    },
    "google": {
        "display_name": "Google Search Ads",
        "components": {
            "headline":     "30 chars each — up to 15 headlines (min 3 required)",
            "description":  "90 chars each — up to 4 descriptions (min 2 required)",
            "display_url":  "15 chars per path field",
        },
        "notes": (
            "Write 5 headlines + 2 descriptions minimum. Include primary keyword in at least 2 headlines. "
            "One headline must state the clear benefit. One description must include a CTA. "
            "Google auto-combines these — each must make sense independently."
        ),
        "formats": ["Responsive Search Ad (RSA)"],
    },
    "linkedin": {
        "display_name": "LinkedIn Ads",
        "components": {
            "introductory_text": "150 chars (600 max)",
            "headline":          "70 chars",
            "description":       "100 chars (Sponsored Content); 300 chars (Message Ad body)",
            "cta_button":        "choose: Learn More | Sign Up | Download | Get Quote | Apply | Subscribe",
        },
        "notes": (
            "LinkedIn audience is professional — lead with business outcome or ROI, not features. "
            "Introductory text: state the problem or result in the first line. "
            "Headline: action-oriented. Always include a specific CTA button."
        ),
        "formats": ["Single Image", "Document Ad", "Message Ad", "Lead Gen Form"],
    },
    "tiktok": {
        "display_name": "TikTok Ads",
        "components": {
            "ad_text":       "100 chars — shown below video",
            "display_name":  "brand name (20 chars)",
            "cta_button":    "choose: Shop Now | Learn More | Sign Up | Download | Book Now | Contact Us",
        },
        "notes": (
            "TikTok ads must feel native — scripted, polished ads underperform. "
            "Write a video script hook (first 3 seconds) + voiceover outline. "
            "Hook must be a bold statement, question, or visual surprise. "
            "Keep total script under 60 seconds."
        ),
        "formats": ["TopView", "In-Feed Ad", "Spark Ad (boosted organic)"],
    },
    "youtube": {
        "display_name": "YouTube Ads",
        "components": {
            "skippable_hook":  "first 5 sec (unskippable) — must hook before skip button appears",
            "non_skippable":   "15-20 sec total script",
            "bumper":          "6 sec — one punchy message only",
            "display_headline": "25 chars",
            "display_description": "35 chars",
            "cta_overlay":     "10 chars",
        },
        "notes": (
            "Hook = first 5 seconds. State the payoff immediately — 'By the end of this, you'll know X.' "
            "Produce both a 6-sec bumper and a 30-sec skippable script. "
            "End with a direct verbal CTA matching the overlay button."
        ),
        "formats": ["Skippable In-Stream", "Non-Skippable", "Bumper (6 sec)"],
    },
}

_FRAMEWORKS: dict[str, str] = {
    "aida": """Framework: AIDA
A — Attention:  grab with a bold hook (stat, question, or outcome)
I — Interest:   explain the problem or context briefly
D — Desire:     show the transformation / benefit / result
A — Action:     clear, specific CTA""",

    "pas": """Framework: PAS
P — Problem:    name the pain clearly (mirror the reader's internal monologue)
A — Agitate:    make the problem feel urgent and costly
S — Solution:   introduce the product/offer as the obvious fix""",

    "bab": """Framework: BAB
B — Before:     describe their current state (the painful status quo)
A — After:      paint the transformed future state
B — Bridge:     show how your product is the path between the two""",

    "fab": """Framework: FAB
F — Feature:    what it is
A — Advantage:  what that feature does
B — Benefit:    why that matters to the customer (lead with this)""",
}


class AdCopySkill(BaseSkill):
    name = "ad_copy"
    description = "Write paid advertising copy for Meta, Google, LinkedIn, TikTok, and YouTube — character-limit accurate"
    trigger_intents = ["ad_copy"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        platform   = params.get("platform", "meta").lower()
        product    = params.get("product", original_message)
        audience   = params.get("target_audience", "small business owners")
        goal       = params.get("goal", "conversions")
        offer      = params.get("offer", "")
        objection  = params.get("objection", "")
        framework  = params.get("framework", "aida").lower()

        spec           = _PLATFORM_SPECS.get(platform, _PLATFORM_SPECS["meta"])
        framework_text = _FRAMEWORKS.get(framework, _FRAMEWORKS["aida"])

        offer_block     = f"\nOffer / hook:     {offer}" if offer else ""
        objection_block = f"\nKey objection to overcome: {objection}" if objection else ""

        components_text = "\n".join(
            f"  {k}: {v}" for k, v in spec["components"].items()
        )

        context = f"""Ad copy production brief:

Platform:         {spec['display_name']}
Product:          {product}
Target audience:  {audience}
Campaign goal:    {goal}
{offer_block}
{objection_block}

{framework_text}

Character limits (hard limits — do not exceed):
{components_text}

Platform notes:
{spec['notes']}

Available formats: {", ".join(spec['formats'])}

Produce complete ad copy with every required component labeled and character counts shown next to each line.
Format: Component Name (X/Y chars): [copy]
Include at least 2 headline variants for A/B testing where applicable."""

        return SkillResult(context_data=context, skill_name=self.name)
