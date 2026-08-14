"""Tier 0 deterministic text and schema checks.

Pure-code checks over story structure and copy: schema completeness,
placeholder text, PII, profanity, shouting caps, emoji overload, stale
fixture dates, and missing CTAs. No network, no models: confidence is
always 1.0 and cost is zero, so these run on every story every cycle.

Check ids implemented here (03_CHECK_CATALOG.md): T0.schema_valid,
T0.placeholder_text, T0.pii_leak, T0.profanity_lexicon, T0.text_style_caps,
T0.text_style_emoji, T0.date_logic, T0.missing_action.

json_path convention is story-relative: "story_title", "pages[1].action.cta".
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ..models import Evidence, Finding, Story
from .base import CheckContext, make_finding

#: Page types the brief defines; anything else is a schema finding.
_KNOWN_PAGE_TYPES = frozenset({"image", "video"})

#: 03_CHECK_CATALOG T0.placeholder_text regex, verbatim. Word-bounded so real
#: words containing a token ("drafted", "Maxxx") never fire.
_PLACEHOLDER_RE = re.compile(
    r"\b(TBD|TKTK|TODO|FIXME|lorem ipsum|FPO|placeholder|draft|xxx+|asdf)\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

#: E.164-ish phone candidates: optional +, digits with spaces/dashes/parens
#: between them. The lookarounds refuse candidates glued to words, slashes, or
#: dots, which keeps slash-dates (14/02/26) and decimals from ever matching.
#: Real filtering (digit count, date shapes) happens in _looks_like_phone().
_PHONE_RE = re.compile(r"(?<![\w./+-])\+?\(?\d[\d\s()-]*\d(?![\w./-])")

#: Numeric patterns that survive the candidate regex but are not phones:
#: ISO dates (2026-08-14), separator dates (14-02-26), season ranges (2025-2026).
_NOT_A_PHONE_RE = re.compile(
    r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-\d{2,4}|\d{4}\s*-\s*\d{4})$"
)

#: Catalog rule: fewer than 8 digits is a scoreline/shirt number/date fragment,
#: not a phone. 15 is the E.164 maximum. Neither exists in settings.yaml
#: thresholds, so they live here as named constants.
_PHONE_MIN_DIGITS = 8
_PHONE_MAX_DIGITS = 15

#: Explicit emoji code-point blocks (03_CHECK_CATALOG T0.text_style_emoji):
#: emoticons, symbols & pictographs, transport, supplemental, flags. No emoji
#: library: an explicit list is deterministic and dependency-free. A flag is
#: two regional indicators and therefore counts as 2.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F300, 0x1F5FF),  # Miscellaneous Symbols and Pictographs
    (0x1F680, 0x1F6FF),  # Transport and Map Symbols
    (0x1F900, 0x1F9FF),  # Supplemental Symbols and Pictographs
    (0x1F1E6, 0x1F1FF),  # Regional Indicator Symbols (flags)
)

#: Fixture-date suffix in a category, e.g. "PFC v SU - 14/02/26".
#: Accepts en-dash (–) or ASCII hyphen before the date; two-digit year.
_FIXTURE_DATE_RE = re.compile(r"[–-]\s*(\d{1,2})/(\d{1,2})/(\d{2})(?!\d)")

#: Pre-event language in a title (03_CHECK_CATALOG T0.date_logic), tolerant of
#: a curly apostrophe (’) in "don't".
_PRE_EVENT_RE = re.compile(
    r"\b(?:build-up|preview|tonight|don['’]t miss|matchday build)\b",
    re.IGNORECASE,
)

_PROFANITY_PATH = Path(__file__).resolve().parent.parent / "data" / "profanity_en.txt"

#: Module-level cache: the lexicon is loaded and compiled once per process.
_profanity_re: re.Pattern[str] | None = None


def _profanity_pattern() -> re.Pattern[str]:
    """Compile data/profanity_en.txt into one word-bounded regex (cached)."""
    global _profanity_re
    if _profanity_re is None:
        terms: list[str] = []
        for line in _PROFANITY_PATH.read_text(encoding="utf-8").splitlines():
            term = line.strip().lower()
            if term and not term.startswith("#"):
                terms.append(re.escape(term))
        if terms:
            _profanity_re = re.compile(
                r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE
            )
        else:
            _profanity_re = re.compile(r"(?!x)x")  # empty lexicon: never match
    return _profanity_re


def _excerpt(text: str) -> str:
    """Clip quoted evidence to the 200-char Evidence contract."""
    return text if len(text) <= 200 else text[:197] + "..."


def _copy_fields(story: Story) -> list[tuple[str, str]]:
    """(json_path, text) for every copy field the text checks scan:
    the story title plus each page's CTA text. Empty fields are skipped;
    their absence is T0.schema_valid / T0.missing_action territory."""
    fields: list[tuple[str, str]] = []
    if story.story_title:
        fields.append(("story_title", story.story_title))
    for i, page in enumerate(story.pages):
        if page.action is not None and page.action.cta:
            fields.append((f"pages[{i}].action.cta", page.action.cta))
    return fields


# -- checks -----------------------------------------------------------------


def check_schema_valid(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.schema_valid: required fields present and sane.

    P1 (the check's default) for fields the pipeline cannot work without:
    story_id, story_title, a non-empty pages[], per-page asset_url, and the
    context block. P3 for defects that only degrade later checks while the
    story stays checkable: missing/duplicate page_id, missing or unknown page
    type, empty categories. Unknown extra fields are deliberately fine (feed
    models are extra="allow"). One finding per issue, multiple allowed.
    """
    cid = "T0.schema_valid"
    findings: list[Finding] = []
    if not story.story_id:
        findings.append(make_finding(
            ctx, cid, "story_id is missing or empty",
            evidence=Evidence(json_path="story_id"),
            suggested_fix="Populate story_id in the feed export.",
        ))
    if not story.story_title:
        findings.append(make_finding(
            ctx, cid, "story_title is missing or empty",
            evidence=Evidence(json_path="story_title"),
            suggested_fix="Add a title before publishing.",
        ))
    if not story.pages:
        findings.append(make_finding(
            ctx, cid, "story has no pages",
            evidence=Evidence(json_path="pages"),
            suggested_fix="Add at least one page or unpublish the story.",
        ))
    seen_page_ids: set[str] = set()
    for i, page in enumerate(story.pages):
        if not page.asset_url:
            findings.append(make_finding(
                ctx, cid, f"page {i} is missing its asset_url",
                evidence=Evidence(json_path=f"pages[{i}].asset_url"),
                suggested_fix="Attach the media asset to the page.",
            ))
        if not page.page_id:
            findings.append(make_finding(
                ctx, cid, f"page {i} is missing its page_id",
                severity="P3",
                evidence=Evidence(json_path=f"pages[{i}].page_id"),
                suggested_fix="Assign a unique page_id.",
            ))
        elif page.page_id in seen_page_ids:
            findings.append(make_finding(
                ctx, cid, f"duplicate page_id '{page.page_id}' at page {i}",
                severity="P3",
                evidence=Evidence(json_path=f"pages[{i}].page_id",
                                  excerpt=_excerpt(page.page_id)),
                suggested_fix="Make page_ids unique within the story.",
            ))
        else:
            seen_page_ids.add(page.page_id)
        if not page.type:
            findings.append(make_finding(
                ctx, cid, f"page {i} is missing its type",
                severity="P3",
                evidence=Evidence(json_path=f"pages[{i}].type"),
                suggested_fix="Set the page type to 'image' or 'video'.",
            ))
        elif page.type not in _KNOWN_PAGE_TYPES:
            findings.append(make_finding(
                ctx, cid, f"page {i} has unknown type '{page.type}'",
                severity="P3",
                evidence=Evidence(json_path=f"pages[{i}].type",
                                  excerpt=_excerpt(page.type)),
                suggested_fix="Use a supported page type ('image' or 'video').",
            ))
    if story.context is None:
        findings.append(make_finding(
            ctx, cid, "context block is missing",
            evidence=Evidence(json_path="context"),
            suggested_fix="Include the context block (categories, tenant, publish_date).",
        ))
    elif not story.context.categories:
        findings.append(make_finding(
            ctx, cid, "context.categories is empty",
            severity="P3",
            evidence=Evidence(json_path="context.categories"),
            suggested_fix="Tag the story with at least one category.",
        ))
    return findings


def check_placeholder_text(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.placeholder_text: unfinished copy shipped to production.

    Uses the catalog regex verbatim (TBD, TKTK, TODO, FIXME, lorem ipsum,
    FPO, placeholder, draft, xxx+, asdf), case-insensitive and word-bounded
    so ordinary words containing a token never fire. Any hit means the copy
    was published unfinished, which is reader-visible: P1 (default).
    """
    findings: list[Finding] = []
    for json_path, text in _copy_fields(story):
        m = _PLACEHOLDER_RE.search(text)
        if m:
            findings.append(make_finding(
                ctx, "T0.placeholder_text",
                f"placeholder text '{m.group(0)}' in {json_path}",
                evidence=Evidence(json_path=json_path, excerpt=_excerpt(text)),
                suggested_fix="Replace the placeholder with final copy.",
            ))
    return findings


def _looks_like_phone(candidate: str) -> bool:
    """The two catalog false-positive guards: a phone has 8-15 digits
    (fewer is a scoreline/date fragment, more is not E.164) and is not
    shaped like a date or a season range."""
    digits = re.sub(r"\D", "", candidate)
    if not _PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS:
        return False
    return _NOT_A_PHONE_RE.match(candidate) is None


def check_pii_leak(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.pii_leak: email addresses or phone numbers in public story copy.

    Email is a standard user@domain.tld regex. Phone is E.164-ish: 8-15
    digits with spaces/dashes/parens tolerated; the floor of 8 digits plus
    an explicit date/season-range rejection keeps scorelines ("3-2") and
    dates out. Personal contact details in a public story are P1 (default)
    regardless of which kind leaked.
    """
    findings: list[Finding] = []
    for json_path, text in _copy_fields(story):
        for m in _EMAIL_RE.finditer(text):
            findings.append(make_finding(
                ctx, "T0.pii_leak",
                f"email address '{m.group(0)}' in {json_path}",
                evidence=Evidence(json_path=json_path, excerpt=_excerpt(text)),
                suggested_fix="Remove the address; route contact via an official page.",
            ))
        for m in _PHONE_RE.finditer(text):
            candidate = m.group(0)
            if _looks_like_phone(candidate):
                findings.append(make_finding(
                    ctx, "T0.pii_leak",
                    f"phone number '{candidate}' in {json_path}",
                    evidence=Evidence(json_path=json_path, excerpt=_excerpt(text)),
                    suggested_fix="Remove the number; route contact via an official page.",
                ))
    return findings


def check_profanity_lexicon(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.profanity_lexicon: bundled-wordlist profanity pre-filter.

    Word-bounded, case-insensitive match against data/profanity_en.txt
    (compiled once per process). The word boundaries are the Scunthorpe
    guard: a listed term inside a longer clean word never matches. Any hit
    on brand-facing copy is P0 (default): the gate errs loud and the Tier-1
    judge confirms borderline context.
    """
    pattern = _profanity_pattern()
    findings: list[Finding] = []
    for json_path, text in _copy_fields(story):
        m = pattern.search(text)
        if m:
            findings.append(make_finding(
                ctx, "T0.profanity_lexicon",
                f"profanity '{m.group(0)}' in {json_path}",
                evidence=Evidence(json_path=json_path, excerpt=_excerpt(text)),
                suggested_fix="Remove or rephrase the flagged language.",
            ))
    return findings


def check_text_style_caps(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.text_style_caps: SHOUTING titles.

    Fires when uppercase alpha ratio exceeds threshold caps_ratio (0.5) and
    the title has more than caps_min_len (12) alpha characters. The ratio
    catches shouting; the length floor keeps short acronym-heavy titles
    ("PFC V SU") from firing. P2 (default): a style problem, not a blocker.
    """
    title = story.story_title
    alpha = [ch for ch in title if ch.isalpha()]
    caps_min_len = int(ctx.policy.threshold("caps_min_len"))
    if len(alpha) <= caps_min_len:
        return []
    ratio = sum(1 for ch in alpha if ch.isupper()) / len(alpha)
    caps_ratio = float(ctx.policy.threshold("caps_ratio"))
    if ratio <= caps_ratio:
        return []
    return [make_finding(
        ctx, "T0.text_style_caps",
        f"title is {ratio:.0%} uppercase (shouting)",
        evidence=Evidence(
            json_path="story_title",
            excerpt=_excerpt(title),
            measured=f"caps_ratio={ratio:.2f}",
            threshold=f"max={caps_ratio}",
        ),
        suggested_fix="Rewrite the title in sentence case.",
    )]


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def check_text_style_emoji(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.text_style_emoji: emoji overload in the title.

    Counts code points in the explicit emoji blocks listed in _EMOJI_RANGES;
    the threshold is the tenant's max_emoji (policy pack, default 3) because
    emoji tolerance is a brand-voice decision, not a global constant. P2
    (default) style finding.
    """
    title = story.story_title
    count = sum(1 for ch in title if _is_emoji(ch))
    max_emoji = int(ctx.policy.tenant.max_emoji)
    if count <= max_emoji:
        return []
    return [make_finding(
        ctx, "T0.text_style_emoji",
        f"title contains {count} emoji (tenant allows {max_emoji})",
        evidence=Evidence(
            json_path="story_title",
            excerpt=_excerpt(title),
            measured=f"emoji={count}",
            threshold=f"max={max_emoji}",
        ),
        suggested_fix="Trim the title to the tenant's emoji budget.",
    )]


def _parse_fixture_date(m: re.Match[str], locale: str) -> date | None:
    """Fixture dates are DD/MM/YY except under en-US locale (MM/DD/YY).
    Returns None when the numbers make no real date (e.g. month 14)."""
    first, second, yy = (int(g) for g in m.groups())
    month, day = (first, second) if locale.lower().startswith("en-us") else (second, first)
    try:
        return date(2000 + yy, month, day)
    except ValueError:
        return None


def check_date_logic(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.date_logic: pre-event copy published after the event date.

    Fixture categories embed the event date ("PFC v SU – 14/02/26",
    en-dash or hyphen tolerated); day/month order follows the tenant locale.
    If ISO publish_date is after the event date AND the title uses pre-event
    language (build-up|preview|tonight|don't miss|matchday build), the story
    is stale: P1 (default), because "tonight's build-up" after the final
    whistle is a reader-visible embarrassment. First stale fixture wins;
    unparseable dates are skipped rather than guessed.
    """
    story_ctx = story.context
    if story_ctx is None or not story_ctx.publish_date:
        return []
    try:
        publish = date.fromisoformat(story_ctx.publish_date)
    except ValueError:
        return []  # unparseable publish_date is a feed bug, not a stale story
    if not _PRE_EVENT_RE.search(story.story_title):
        return []
    for i, category in enumerate(story_ctx.categories):
        m = _FIXTURE_DATE_RE.search(category)
        if m is None:
            continue
        event = _parse_fixture_date(m, ctx.policy.tenant.locale)
        if event is None:
            continue
        if publish > event:
            return [make_finding(
                ctx, "T0.date_logic",
                "pre-event title published after the event date",
                evidence=Evidence(
                    json_path=f"context.categories[{i}]",
                    excerpt=_excerpt(category),
                    measured=f"publish={publish.isoformat()}, event={event.isoformat()}",
                ),
                suggested_fix="Unpublish, or reframe as post-event coverage.",
            )]
    return []


def check_missing_action(story: Story, ctx: CheckContext) -> list[Finding]:
    """T0.missing_action: page with no usable CTA.

    Fires only when the tenant policy sets require_action: true, because
    whether every page must carry a CTA is a tenant product decision, not a
    global rule. An absent action block (or one with neither cta text nor
    url) is P3 (default): an informational nudge, never a gate.
    """
    if not ctx.policy.tenant.require_action:
        return []
    findings: list[Finding] = []
    for i, page in enumerate(story.pages):
        action = page.action
        if action is None or (not action.cta and not action.url):
            findings.append(make_finding(
                ctx, "T0.missing_action",
                f"page {i} has no action (tenant requires a CTA)",
                evidence=Evidence(json_path=f"pages[{i}].action"),
                suggested_fix="Add a CTA action to the page.",
            ))
    return findings


def run(story: Story, ctx: CheckContext) -> list[Finding]:
    """All Tier 0 text/schema checks for one story, in catalog order."""
    findings: list[Finding] = []
    findings += check_schema_valid(story, ctx)
    findings += check_placeholder_text(story, ctx)
    findings += check_pii_leak(story, ctx)
    findings += check_profanity_lexicon(story, ctx)
    findings += check_text_style_caps(story, ctx)
    findings += check_text_style_emoji(story, ctx)
    findings += check_date_logic(story, ctx)
    findings += check_missing_action(story, ctx)
    return findings
