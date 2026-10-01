"""Single prospect enrichment processor executing the 10-step pipeline in order."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..models import (
    ProspectInput,
    ProspectResult,
    CandidateEmail,
    Evidence
)
from ..config import (
    CONFIDENCE_TO_SKIP_PROVIDERS,
    THRESHOLD_HIGH,
)
from ..domain import resolve_domain
from ..discovery import (
    discover_emails_from_site,
    search_company_emails,
    search_exact_candidate_email,
    search_name_with_domain,
    search_github_domain_emails,
    search_github_user_email
)
from ..email import (
    detect_company_pattern,
    generate_candidates,
    score_candidate,
    classify_confidence
)
from ..validation import (
    is_role_email,
    check_mx,
    check_catch_all
)
from ..providers import query_fallback_providers
from ... import db


EXACT_SEARCHES = 3   # web searches for guessed addresses, per person
CACHE_DAYS = 90      # a company's learned address pattern is re-checked after this


def provider_skip_reason(allowed: bool, res: ProspectResult, first: str, last: str, catch_all: bool) -> str:
    """Why the paid-tier providers are NOT asked about this person ('' = ask). Credits are scarce, so they
    are only used when they can change the answer."""
    if not allowed:
        return "providers turned off for this run"
    if res.confidence_score >= CONFIDENCE_TO_SKIP_PROVIDERS:
        return f"already {res.confidence_score}% confident without them"
    if any(ev.type == "exact_public_match" for ev in res.evidence):
        return "the address is published publicly"
    if not (first and last):
        return "first and last name are both needed for a reliable lookup"
    if catch_all:
        return "the domain accepts every address, so providers can't verify it either"
    if res.role_email:
        return "generic role address"
    return ""


class ProspectProcessor:
    """Enriches prospects by discovering professional emails with full provenance."""

    def __init__(self, use_cache: bool = True, allow_providers: bool = True):
        self.use_cache = use_cache
        self.allow_providers = allow_providers

    def process(self, input_data: ProspectInput) -> ProspectResult:
        """Run the complete 10-step pipeline for a single prospect."""
        first = input_data.first_name.strip()
        last = input_data.last_name.strip()
        company = input_data.company.strip()
        full_name = input_data.full_name or f"{first} {last}".strip()
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Result object being constructed
        res = ProspectResult(
            first_name=first,
            last_name=last,
            full_name=full_name,
            company=company,
            title=input_data.title or "",
            linkedin_url=input_data.linkedin_url or "",
            created_at=now_iso,
            updated_at=now_iso,
            last_checked_at=now_iso,
        )

        # Step 2: Determine or normalize the company's domain
        domain = resolve_domain(company, input_data.domain)
        res.domain = domain
        if not domain:
            res.email_status = "invalid"
            res.confidence_level = "unresolved"
            res.evidence.append(Evidence(
                type="domain_resolution_failed",
                detail="Could not determine or normalize an official company domain",
                weight=0
            ))
            return self._save_and_return(res)

        # Step 6 preview: Validate MX and domain configuration
        has_mx, mx_hosts, provider_name = check_mx(domain)
        res.mx_valid = has_mx
        if not has_mx:
            res.email_status = "invalid"
            res.confidence_level = "unresolved"
            res.evidence.append(Evidence(
                type="missing_mx",
                detail=f"Domain '{domain}' has no valid MX records to receive email",
                weight=0
            ))
            return self._save_and_return(res)

        is_catch_all, catch_all_method = check_catch_all(domain, mx_hosts)
        res.catch_all = is_catch_all

        # Step 3: Search publicly available company info for known employee email addresses
        known_emails: list[str] = []
        sources_map: dict[str, str] = {}
        cached_pattern: str | None = None
        cached_conf: float = 0.0

        if self.use_cache:
            try:
                with db.connect() as conn:
                    c_row = db.get_prospect_domain_cache(conn, domain)
                    fresh = (c_row or {}).get("updated_at", "") >= (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
                    if c_row and fresh:
                        cached_pattern = c_row.get("detected_pattern")
                        cached_conf = float(c_row.get("pattern_confidence") or 0.0)
                        try:
                            known_emails = json.loads(c_row.get("known_emails") or "[]")
                        except Exception:
                            known_emails = []
            except Exception:
                pass

        if not known_emails and not cached_pattern:
            # 3a. Scrape company website
            site_data = discover_emails_from_site(domain)
            for e in site_data.get("employee_emails", []):
                if e not in known_emails:
                    known_emails.append(e)
            sources_map.update(site_data.get("sources", {}))

            # 3b. Search public indexed search results
            search_emails = search_company_emails(domain)
            for e, url in search_emails.items():
                if e not in known_emails:
                    known_emails.append(e)
                    sources_map[e] = url

            # 3c. Search public GitHub commits
            gh_emails = search_github_domain_emails(domain)
            for e, url in gh_emails.items():
                if e not in known_emails:
                    known_emails.append(e)
                    sources_map[e] = url

        # Step 4: Detect the company's likely email naming convention
        pattern_data = detect_company_pattern(known_emails, domain)
        dominant_pattern = cached_pattern or pattern_data.get("dominant_pattern")
        pattern_confidence = cached_conf or pattern_data.get("pattern_confidence", 0.0)
        match_count = pattern_data.get("match_count", 0)

        # Cache domain findings in database
        if self.use_cache and dominant_pattern:
            try:
                with db.connect() as conn:
                    db.set_prospect_domain_cache(
                        conn,
                        domain=domain,
                        detected_pattern=dominant_pattern,
                        pattern_confidence=pattern_confidence,
                        known_emails=json.dumps(known_emails),
                        mx_valid=1 if has_mx else 0,
                        is_catch_all=1 if is_catch_all else 0
                    )
            except Exception:
                pass

        # Step 5: Generate likely email permutations for the target person
        candidates = generate_candidates(first, last, domain, preferred_pattern=dominant_pattern)
        if not candidates:
            res.email_status = "not_found"
            res.confidence_level = "unresolved"
            res.evidence.append(Evidence(
                type="no_candidates",
                detail="Could not generate email candidates from provided name and domain",
                weight=0
            ))
            return self._save_and_return(res)

        # Step 7: Look for public evidence supporting generated emails
        # First check if search of full name + domain finds a specific email
        found_name_email, found_name_url = search_name_with_domain(full_name, domain)
        github_user_email, github_user_url = search_github_user_email(full_name, domain)

        # Step 8: Calculate confidence score for each candidate
        scored_candidates: list[CandidateEmail] = []
        conflicting_patterns = (len(pattern_data.get("counts", {})) > 1 and pattern_confidence < 0.70)

        # Web-searching each guessed address is slow (one search each): only the top few, and none when the
        # company's pattern is already well established.
        strong_pattern = bool(dominant_pattern and pattern_confidence >= 0.8 and (match_count >= 2 or cached_pattern))
        searchable = set() if strong_pattern else {c.email for c in candidates[:EXACT_SEARCHES]}
        for cand in candidates:
            exact_public = False
            public_url = None

            # Did we find this exact email in step 3 site/search/github scrape?
            if cand.email in known_emails:
                exact_public = True
                public_url = sources_map.get(cand.email)
            elif found_name_email and cand.email == found_name_email:
                exact_public = True
                public_url = found_name_url
            elif github_user_email and cand.email == github_user_email:
                exact_public = True
                public_url = github_user_url
            elif cand.email in searchable:
                has_exact, exact_url = search_exact_candidate_email(cand.email)
                if has_exact:
                    exact_public = True
                    public_url = exact_url

            pattern_matched = bool(dominant_pattern and cand.pattern == dominant_pattern)
            is_role = is_role_email(cand.email)

            scored = score_candidate(
                candidate=cand,
                exact_public_match=exact_public,
                pattern_match=pattern_matched,
                multiple_employees=(match_count >= 2),
                valid_mx=has_mx,
                name_identity_match=True,
                is_catch_all=is_catch_all,
                has_conflicting_patterns=conflicting_patterns,
                is_role=is_role,
                public_url=public_url
            )
            scored_candidates.append(scored)

        # Pick top candidate
        scored_candidates.sort(key=lambda c: c.score, reverse=True)
        top = scored_candidates[0]

        res.candidate_email = top.email
        res.final_email = top.email
        res.email_pattern = top.pattern
        res.confidence_score = top.score
        res.role_email = is_role_email(top.email)
        res.evidence = list(top.evidence)

        # Find matching public URL if present
        for ev in res.evidence:
            if ev.url:
                res.source_url = ev.url
                break
        res.source = "company_pattern" if (dominant_pattern and top.pattern == dominant_pattern) else "pattern_inference"
        if any(ev.type == "exact_public_match" for ev in res.evidence):
            res.source = "public_match"

        # A pattern a provider verified at this company (see _learn_pattern) is strong evidence on its own.
        if (cached_pattern and cached_conf >= 0.9 and top.pattern == cached_pattern and not is_catch_all
                and res.confidence_score < 80):
            res.evidence.append(Evidence(type="verified_company_pattern", weight=80 - res.confidence_score,
                                         detail=f"Matches the '{cached_pattern}' pattern a provider verified at {domain}"))
            res.confidence_score = 80

        # Step 9: paid-tier providers (free plans, tightly budgeted), only when they can change the answer
        skip = provider_skip_reason(self.allow_providers, res, first, last, is_catch_all)
        if skip:
            res.evidence.append(Evidence(type="providers_not_used", detail=skip, weight=0))
        else:
            provider_res = query_fallback_providers(first, last, company, domain)
            if provider_res and provider_res.email:
                if provider_res.email == res.final_email:      # it confirms our own best guess
                    new_score = max(res.confidence_score, provider_res.confidence, THRESHOLD_HIGH)
                    detail = f"{provider_res.provider} confirmed this address (score {provider_res.confidence})"
                elif provider_res.status in ("verified", "remembered") or provider_res.confidence > res.confidence_score:
                    new_score = provider_res.confidence
                    detail = f"Found by {provider_res.provider} (score {provider_res.confidence})"
                    res.final_email = provider_res.email
                    res.source = f"provider_{provider_res.provider}"
                else:
                    new_score, detail = None, ""
                if new_score is not None:
                    res.evidence.append(Evidence(type="fallback_provider", detail=detail,
                                                 weight=new_score - res.confidence_score))
                    res.confidence_score = new_score
                    res.verification_provider = provider_res.provider
                    res.verification_result = provider_res.status
                    self._learn_pattern(first, last, domain, provider_res.email, known_emails, has_mx, is_catch_all)

        # Step 10: Classify final status and save results
        conf_level, status = classify_confidence(res.confidence_score)
        res.confidence_level = conf_level
        res.email_status = status

        return self._save_and_return(res)

    def _learn_pattern(self, first, last, domain, email, known_emails, has_mx, is_catch_all) -> None:
        """One provider hit teaches the company's address pattern, so the next person there costs nothing."""
        match = next((c for c in generate_candidates(first, last, domain) if c.email == email), None)
        if not match or not self.use_cache:
            return
        try:
            with db.connect() as conn:
                db.set_prospect_domain_cache(conn, domain=domain, detected_pattern=match.pattern, pattern_confidence=0.9,
                                             known_emails=json.dumps(sorted(set(known_emails) | {email})),
                                             mx_valid=1 if has_mx else 0, is_catch_all=1 if is_catch_all else 0)
        except Exception:
            pass

    def _save_and_return(self, res: ProspectResult) -> ProspectResult:
        """Persist result to database and attach inserted ID."""
        try:
            with db.connect() as conn:
                # Check for existing duplicate record to update
                existing = db.find_prospect_duplicate(
                    conn,
                    email=res.final_email,
                    linkedin_url=res.linkedin_url,
                    full_name=res.full_name,
                    company=res.company,
                    domain=res.domain
                )
                fields = {
                    "first_name": res.first_name,
                    "last_name": res.last_name,
                    "full_name": res.full_name,
                    "company": res.company,
                    "title": res.title,
                    "linkedin_url": res.linkedin_url,
                    "domain": res.domain,
                    "candidate_email": res.candidate_email or "",
                    "final_email": res.final_email or "",
                    "email_pattern": res.email_pattern or "",
                    "confidence_score": res.confidence_score,
                    "confidence_level": res.confidence_level,
                    "email_status": res.email_status,
                    "source": res.source,
                    "source_url": res.source_url or "",
                    "mx_valid": 1 if res.mx_valid else 0,
                    "catch_all": 1 if res.catch_all else 0,
                    "role_email": 1 if res.role_email else 0,
                    "verification_provider": res.verification_provider or "",
                    "verification_result": res.verification_result or "",
                    "provenance": json.dumps([e.model_dump() for e in res.evidence]),
                    "last_checked_at": res.last_checked_at,
                }
                if existing:
                    db.update_prospect(conn, existing["id"], **fields)
                    res.id = existing["id"]
                else:
                    new_id = db.add_prospect(conn, **fields)
                    res.id = new_id
        except Exception as e:  # keep the result for the caller, but say why it wasn't stored
            print(f"  ! could not save prospect {res.full_name} @ {res.domain}: {type(e).__name__}: {e}")
        return res


def enrich_prospect(input_data: ProspectInput | dict, use_cache: bool = True, allow_providers: bool = True) -> ProspectResult:
    """Convenience function to enrich a single prospect from model or dict."""
    if isinstance(input_data, dict):
        input_data = ProspectInput(**input_data)
    return ProspectProcessor(use_cache=use_cache, allow_providers=allow_providers).process(input_data)
