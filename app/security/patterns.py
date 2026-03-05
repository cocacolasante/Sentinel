"""
Injection pattern blocklist for SecurityHook.

Patterns target common prompt injection, jailbreak, and role-confusion attacks.
"""

import re

INJECTION_PATTERNS: list[re.Pattern] = [
    # Classic instruction injection
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"<<SYS>>", re.IGNORECASE),
    # Role/identity manipulation
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|context)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)\s+(instructions?|prompts?)", re.IGNORECASE),
    re.compile(r"forget\s+everything\s+you\s+(were|have\s+been)\s+told", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?!brain|an?\s+AI)", re.IGNORECASE),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+", re.IGNORECASE),
    re.compile(r"act\s+as\s+if\s+you\s+(have\s+no|don't\s+have)\s+(restrictions?|limits?|rules?)", re.IGNORECASE),
    # DAN / jailbreak phrases
    re.compile(r"\bDAN\b"),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"developer\s+mode", re.IGNORECASE),
    re.compile(r"do\s+anything\s+now", re.IGNORECASE),
    # System prompt exfiltration
    re.compile(r"(print|output|repeat|show|reveal|tell\s+me)\s+(your|the)\s+system\s+prompt", re.IGNORECASE),
    re.compile(r"what\s+(are|were)\s+your\s+(original\s+)?(instructions?|rules?|guidelines?)", re.IGNORECASE),
]
