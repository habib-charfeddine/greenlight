"""Tier-0 link health and CTA domain policy checks.

Catalog rows covered: ``T0.asset_reachable``, ``T0.asset_type_match``,
``T0.cta_link_health``, ``T0.cta_domain_policy``.

Three-state HTTP rationale (why UNVERIFIABLE exists and never alerts):
CDNs and bot-protection layers answer automated probes with 401/403/407/429/999
while serving the same URL to real users just fine. Treating those responses as
"broken" would page a human for links that work in production, and that alert
fatigue is how a review gate gets ignored. Reachability is therefore
three-state: OK (2xx/3xx) emits nothing, BROKEN (404/410/DNS failure/timeout/
persistent 5xx) is a genuinely dead link and alerts at P0, and UNVERIFIABLE
(bot-blocked, or an external host in offline replay mode) leaves only a P3
informational trail. ``ctx.probe()`` implements the states, retries, and the
offline short-circuit; this module only maps states to findings.
"""
from __future__ import annotations

import ipaddress

import httpx
from rapidfuzz.distance import Levenshtein

from ..models import Evidence, Finding, Page, Story
from .base import CheckContext, FetchResult, make_finding

#: Declared page type -> required Content-Type prefix (T0.asset_type_match).
_EXPECTED_CONTENT_PREFIX: dict[str, str] = {"image": "image/", "video": "video/"}

#: Known URL-shortener domains (matched exactly or as a parent domain).
#: Shorteners hide the destination, so domain policy cannot be applied to it.
_SHORTENER_DOMAINS: frozenset[str] = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly",
    "is.gd", "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at",
})


def run(story: Story, ctx: CheckContext) -> list[Finding]:
    """Run all four link checks over every page of one story."""
    findings: list[Finding] = []
    for index, page in enumerate(story.pages):
        findings.extend(_asset_checks(page, index, ctx))
        cta_url = (page.action.url if page.action else "").strip()
        if cta_url:
            findings.extend(_cta_link_health(cta_url, index, ctx))
            domain_finding = _cta_domain_policy(cta_url, index, ctx)
            if domain_finding is not None:
                findings.append(domain_finding)
    return findings


def _asset_checks(page: Page, index: int, ctx: CheckContext) -> list[Finding]:
    """T0.asset_reachable (P0 broken / P3 unverifiable) + T0.asset_type_match.

    One cached ``ctx.probe()`` serves both checks. BROKEN alerts at P0 because
    a story whose media does not load is unpublishable; UNVERIFIABLE is P3
    info only (see module docstring). An empty ``asset_url`` is skipped here:
    a missing asset URL is a schema defect owned by ``T0.schema_valid``, and
    this check judges URLs that exist.
    """
    url = page.asset_url.strip()
    if not url:
        return []
    result = ctx.probe(url)
    json_path = f"pages[{index}].asset_url"
    if result.state == "BROKEN":
        return [make_finding(
            ctx, "T0.asset_reachable",
            f"Asset URL is broken: {result.error}.",
            evidence=Evidence(json_path=json_path, url=url,
                              http_status=result.status_code,
                              measured=result.error),
            suggested_fix="Re-upload the asset or correct asset_url.",
        )]
    if result.state == "UNVERIFIABLE":
        return [make_finding(
            ctx, "T0.asset_reachable",
            f"Asset URL unverifiable ({result.error}); "
            "informational only, this state never alerts.",
            severity="P3",
            evidence=Evidence(json_path=json_path, url=url,
                              http_status=result.status_code,
                              measured=result.error),
        )]
    return _asset_type_match(page, url, result, index, ctx)


def _asset_type_match(page: Page, url: str, result: FetchResult,
                      index: int, ctx: CheckContext) -> list[Finding]:
    """T0.asset_type_match (P1): served Content-Type must match the page type.

    Rule: declared ``image`` requires ``image/*``, declared ``video`` requires
    ``video/*``. A mismatch (the classic seed: type "video" pointing at a
    .jpg) renders wrong or not at all in the player, hence P1. Runs only on an
    OK probe with a known Content-Type: on BROKEN there is nothing to compare,
    and guessing from a bot-blocked response would be noise. Declared types
    outside the image/video vocabulary are a schema defect, not a media one,
    so they are skipped here.
    """
    declared = page.type.strip().lower()
    expected = _EXPECTED_CONTENT_PREFIX.get(declared)
    if expected is None or not result.content_type:
        return []
    if result.content_type.lower().startswith(expected):
        return []
    return [make_finding(
        ctx, "T0.asset_type_match",
        f'Page is declared "{declared}" but the asset serves '
        f'content-type "{result.content_type}".',
        evidence=Evidence(json_path=f"pages[{index}].asset_url", url=url,
                          measured=f"content-type={result.content_type}",
                          threshold=f"expected {expected}*"),
        suggested_fix=f"Point asset_url at a real {declared} asset "
                      "or correct the page type.",
    )]


def _cta_link_health(url: str, index: int, ctx: CheckContext) -> list[Finding]:
    """T0.cta_link_health (P0 broken / P3 unverifiable): probe the action URL.

    Same three-state logic and rationale as ``T0.asset_reachable``: a dead
    call-to-action is a P0 because it wastes the one tap the story earns,
    while a bot-blocked or offline-unverifiable link must never page a human.
    Empty URLs are skipped by the caller (``T0.missing_action`` owns absence).
    """
    result = ctx.probe(url)
    if result.state == "OK":
        return []
    json_path = f"pages[{index}].action.url"
    if result.state == "BROKEN":
        return [make_finding(
            ctx, "T0.cta_link_health",
            f"CTA link is broken: {result.error}.",
            evidence=Evidence(json_path=json_path, url=url,
                              http_status=result.status_code,
                              measured=result.error),
            suggested_fix="Correct the action URL to a live destination.",
        )]
    return [make_finding(
        ctx, "T0.cta_link_health",
        f"CTA link unverifiable ({result.error}); "
        "informational only, this state never alerts.",
        severity="P3",
        evidence=Evidence(json_path=json_path, url=url,
                          http_status=result.status_code,
                          measured=result.error),
    )]


def _cta_domain_policy(url: str, index: int,
                       ctx: CheckContext) -> Finding | None:
    """T0.cta_domain_policy: ordered rules, first hit wins per URL.

    1. Non-web scheme (javascript:, mailto:, ftp:, or none) -> P1: the CTA
       cannot open a web destination, and scheme tricks are a phishing vector.
    2. Raw IP host (IPv4 or IPv6), if tenant ``deny_raw_ip_urls`` -> P1:
       legitimate campaigns link to domains; bare IPs evade domain policy.
    3. Known URL shortener, if tenant ``deny_url_shorteners`` -> P1: the real
       destination is hidden, so rules 4-5 cannot be applied to it.
    4. Typosquat -> P0: host is not allowlisted but its registrable name is
       within ``typosquat_max_levenshtein`` (settings.yaml, default 2) edits
       of an allowlisted registrable name. One or two edits cover the classic
       squats (l->1 homoglyphs, dropped doubled letters) while unrelated real
       domains sit much further apart, so a hit means a near-identical
       lookalike is stealing the tenant's traffic -- exactly what P0 is for.
    5. Host not on the allowlist (exact match or subdomain) -> P1: off-brand
       but honestly named, so a human decides.

    Hosts are compared lowercased; ``httpx.URL(...).host`` already excludes
    the port. An empty tenant allowlist skips rules 4-5: with no approved
    domains there is no basis to judge. An unparseable URL is skipped here
    because ``T0.cta_link_health`` already reports it as BROKEN.
    """
    try:
        parsed = httpx.URL(url)
    except Exception:
        return None
    json_path = f"pages[{index}].action.url"
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return make_finding(
            ctx, "T0.cta_domain_policy",
            f'CTA uses a non-web URL scheme "{scheme or "(none)"}".',
            evidence=Evidence(json_path=json_path, url=url,
                              measured=f"scheme={scheme or '(none)'}"),
            suggested_fix="Use an absolute https:// link.",
        )
    host = (parsed.host or "").lower()
    if not host:
        return None
    tenant = ctx.policy.tenant
    if tenant.deny_raw_ip_urls and _is_raw_ip(host):
        return make_finding(
            ctx, "T0.cta_domain_policy",
            f"CTA links to a raw IP address ({host}) instead of a domain.",
            evidence=Evidence(json_path=json_path, url=url,
                              measured=f"host={host}"),
            suggested_fix="Link via a domain on the tenant allowlist.",
        )
    if tenant.deny_url_shorteners and _is_shortener(host):
        return make_finding(
            ctx, "T0.cta_domain_policy",
            f'CTA uses a URL shortener ("{host}"), '
            "hiding the real destination.",
            evidence=Evidence(json_path=json_path, url=url,
                              measured=f"host={host}"),
            suggested_fix="Replace the short link with the direct "
                          "destination URL.",
        )
    allowlist = [d.strip().lower() for d in tenant.allowed_domains if d.strip()]
    if not allowlist:
        return None
    if _on_allowlist(host, allowlist):
        return None
    closest = _closest_allowlisted(host, allowlist)
    max_dist = int(ctx.policy.threshold("typosquat_max_levenshtein"))
    if closest is not None and closest[2] <= max_dist:
        host_reg, domain_reg, dist = closest
        return make_finding(
            ctx, "T0.cta_domain_policy",
            f'CTA host "{host}" looks like a typosquat of allowlisted '
            f'domain "{domain_reg}".',
            severity="P0",
            evidence=Evidence(json_path=json_path, url=url,
                              measured=f"levenshtein({host_reg}, {domain_reg})={dist}",
                              threshold=f"max={max_dist}"),
            suggested_fix="Correct the domain spelling; if the domain is "
                          "genuinely new, add it to allowed_domains.",
        )
    return make_finding(
        ctx, "T0.cta_domain_policy",
        f'CTA domain "{host}" is not on the tenant allowlist.',
        evidence=Evidence(json_path=json_path, url=url,
                          measured=f"host={host}"),
        suggested_fix="Link to an allowlisted domain or add this domain to "
                      "allowed_domains in the tenant policy.",
    )


def _is_raw_ip(host: str) -> bool:
    """True for IPv4 dotted-quad or IPv6 hosts (brackets already stripped)."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _is_shortener(host: str) -> bool:
    """True when host is, or is a subdomain of, a known shortener domain."""
    return any(host == s or host.endswith("." + s) for s in _SHORTENER_DOMAINS)


def _on_allowlist(host: str, allowlist: list[str]) -> bool:
    """Exact match or subdomain: host == d or host ends with '.' + d."""
    return any(host == d or host.endswith("." + d) for d in allowlist)


def _registrable(host: str) -> str:
    """Naive registrable name: the last two dot-labels of the host.

    Good enough for tenant allowlists, which use conventional second-level
    domains (a full public-suffix list is deliberately out of scope for v1);
    single-label hosts are returned unchanged.
    """
    labels = [part for part in host.split(".") if part]
    if len(labels) < 2:
        return host
    return ".".join(labels[-2:])


def _closest_allowlisted(host: str,
                         allowlist: list[str]) -> tuple[str, str, int] | None:
    """Closest allowlisted registrable name by Levenshtein distance.

    Returns ``(host_registrable, closest_registrable, distance)`` or None when
    no allowlist entry qualifies. Registrable names are compared so that an
    allowlisted "tickets.antarcticfootballleague.com" still guards the bare
    "antarcticfootbal1league.com" lookalike. Single-label entries such as
    "localhost" are skipped: they have no registrable name and short strings
    are trivially within a couple of edits of each other.
    """
    host_reg = _registrable(host)
    candidates = sorted({_registrable(d) for d in allowlist if "." in d})
    best: tuple[str, str, int] | None = None
    for candidate in candidates:
        dist = Levenshtein.distance(host_reg, candidate)
        if best is None or dist < best[2]:
            best = (host_reg, candidate, dist)
    return best
