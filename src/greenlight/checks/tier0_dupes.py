"""Tier 0 duplicate detection (03_CHECK_CATALOG.md, T0.dup_asset / T0.dup_story).

Perceptual image hashes (64-bit pHash) and fuzzy title matching against the
tenant's recent history in the Store. Comparison happens BEFORE this story's
own title/hashes are registered, and prior rows with this story_id are excluded
everywhere, so re-running an unchanged story never matches itself.

Requires ``ctx.store``; without one (some unit tests) the module returns [].
"""
from __future__ import annotations

from io import BytesIO

import imagehash
from PIL import Image
from rapidfuzz import fuzz

from ..models import Evidence, Finding, Story
from .base import CheckContext, make_finding

#: (page_index, asset_url, perceptual hash) for each decodable OK image asset.
PageHash = tuple[int, str, imagehash.ImageHash]


def run(story: Story, ctx: CheckContext) -> list[Finding]:
    """Run duplicate checks, then register this story into the dup memory."""
    if ctx.store is None:
        return []
    tenant_id = ctx.policy.tenant_id
    page_hashes = _hash_image_pages(story, ctx)
    findings, near_dup_story_ids = _dup_asset(page_hashes, story, ctx, tenant_id)
    findings.extend(_dup_story(story, ctx, tenant_id, near_dup_story_ids))
    _register(story, ctx, tenant_id, page_hashes)
    return findings


def _hash_image_pages(story: Story, ctx: CheckContext) -> list[PageHash]:
    """pHash every image page whose asset fetch is OK and decodable.

    Undecodable bytes are skipped silently: T0.image_decodes (media module)
    owns that finding, and a hash of garbage would only produce noise.
    """
    hashes: list[PageHash] = []
    for index, page in enumerate(story.pages):
        if page.type != "image" or not page.asset_url:
            continue
        result = ctx.fetch_bytes(page.asset_url)
        if result.state != "OK" or not result.content:
            continue
        try:
            img = Image.open(BytesIO(result.content))
            img.load()
            hashes.append((index, page.asset_url, imagehash.phash(img)))
        except Exception:  # noqa: BLE001 — decode failures are not this check's job
            continue
    return hashes


def _dup_asset(
    page_hashes: list[PageHash], story: Story, ctx: CheckContext, tenant_id: str
) -> tuple[list[Finding], set[str]]:
    """T0.dup_asset: pHash Hamming distance vs the tenant's recent assets.

    Compares against the trailing ``dup_asset_window`` (500) stored hashes.
    Distance <= ``phash_same_hamming`` (4) -> "duplicate media"; <=
    ``phash_near_hamming`` (8) -> "near-duplicate media"; both P2. WHY these
    bands: on a 64-bit pHash, <=4 bits is the standard same-image band (robust
    to re-encoding/resizing) and 5-8 bits catches light edits such as a new
    text overlay. WHY P2/advisory: asset reuse is often deliberate (club
    crests, sponsor frames), so this informs rather than alarms.

    Also returns the set of prior story_ids with at least one near-or-better
    match — T0.dup_story's second condition.
    """
    same_max = int(ctx.policy.threshold("phash_same_hamming"))
    near_max = int(ctx.policy.threshold("phash_near_hamming"))
    window = int(ctx.policy.threshold("dup_asset_window"))
    prior = [
        row
        for row in ctx.store.recent_asset_hashes(tenant_id, window)
        if row["story_id"] != story.story_id  # re-runs must not self-match
    ]
    findings: list[Finding] = []
    near_dup_story_ids: set[str] = set()
    for index, asset_url, page_hash in page_hashes:
        best: tuple[int, dict] | None = None
        for row in prior:
            try:
                distance = page_hash - imagehash.hex_to_hash(row["phash"])
            except ValueError:
                continue  # malformed stored hash; ignore the row
            if distance <= near_max:
                near_dup_story_ids.add(row["story_id"])
            if best is None or distance < best[0]:
                best = (distance, row)
        if best is None:
            continue
        distance, row = best
        if distance <= same_max:
            label = "duplicate media"
        elif distance <= near_max:
            label = "near-duplicate media"
        else:
            continue
        findings.append(
            make_finding(
                ctx,
                "T0.dup_asset",
                f"{label}: asset matches {row['url']} from story {row['story_id']}",
                evidence=Evidence(
                    json_path=f"pages[{index}].asset_url",
                    url=row["url"],
                    excerpt=f"matched story_id={row['story_id']}",
                    measured=f"hamming_distance={distance}",
                    threshold=f"same<={same_max}, near<={near_max}",
                ),
                suggested_fix="Confirm the reuse is intentional or swap in fresh art.",
            )
        )
    return findings, near_dup_story_ids


def _dup_story(
    story: Story, ctx: CheckContext, tenant_id: str, near_dup_story_ids: set[str]
) -> list[Finding]:
    """T0.dup_story: same title AND shared media within the recency window.

    Flags P1 only when BOTH hold against the SAME prior story (different
    story_id, within ``dup_window_days`` = 7): token_set_ratio of the titles
    >= ``title_similarity_min`` (90), and at least one of this story's assets
    is a near-duplicate (Hamming <= ``phash_near_hamming``) of that story's.
    WHY the dual condition: recurring editorial formats ("Matchday build-up")
    legitimately repeat near-identical titles every week; only title AND media
    matching together indicate an accidental republish.
    """
    title = (story.story_title or "").strip()
    if not title or not near_dup_story_ids:
        return []
    similarity_min = float(ctx.policy.threshold("title_similarity_min"))
    window_days = int(ctx.policy.threshold("dup_window_days"))
    best: tuple[float, dict] | None = None
    for row in ctx.store.recent_titles(tenant_id, window_days):
        if row["story_id"] == story.story_id:
            continue  # re-runs must not self-match
        if row["story_id"] not in near_dup_story_ids:
            continue  # title alone is not enough — see docstring
        ratio = fuzz.token_set_ratio(title, row["title"])
        if ratio >= similarity_min and (best is None or ratio > best[0]):
            best = (ratio, row)
    if best is None:
        return []
    ratio, row = best
    return [
        make_finding(
            ctx,
            "T0.dup_story",
            f"probable duplicate of story {row['story_id']}: matching title and near-duplicate media",
            evidence=Evidence(
                json_path="story_title",
                excerpt=row["title"][:200],
                measured=f"title_similarity={ratio:.0f}",
                threshold=f"similarity>={similarity_min:g} and >=1 near-dup asset",
            ),
            suggested_fix="Check whether this story was already published; unpublish or differentiate it.",
        )
    ]


def _register(
    story: Story, ctx: CheckContext, tenant_id: str, page_hashes: list[PageHash]
) -> None:
    """Record this story's title and asset hashes AFTER all comparisons.

    Ordering matters: registering first would make every story a duplicate of
    itself. Blank titles are not registered — they carry no dedup signal and
    are flagged by T0.schema_valid instead.
    """
    if (story.story_title or "").strip():
        ctx.store.add_title(tenant_id, story.story_id, story.story_title)
    for _index, asset_url, page_hash in page_hashes:
        ctx.store.add_asset_hash(tenant_id, asset_url, str(page_hash), story.story_id)
