"""Single prospect enrichment processor executing the 10-step pipeline in order."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import (
    ProspectInput,
    ProspectResult,
    CandidateEmail,
    Evidence
)
from ..config import (
    CONFIDENCE_TO_SKIP_PROVIDERS
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
                    if c_row:
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
                        catch_all=1 if is_catch_all else 0
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
            else:
                # Targeted check for top candidate
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

        # Step 9: Use external free-tier APIs only when confidence is insufficient
        if self.allow_providers and res.confidence_score < CONFIDENCE_TO_SKIP_PROVIDERS:
            provider_res = query_fallback_providers(first, last, company, domain)
            if provider_res and provider_res.email:
                # If provider returned email with higher confidence, upgrade result
                if provider_res.confidence > res.confidence_score:
                    res.final_email = provider_res.email
                    res.confidence_score = provider_res.confidence
                    res.verification_provider = provider_res.provider
                    res.verification_result = provider_res.status
                    res.source = f"provider_{provider_res.provider}"
                    res.evidence.append(Evidence(
                        type="fallback_provider",
                        detail=f"Resolved via {provider_res.provider} fallback with score {provider_res.confidence}",
                        weight=provider_res.confidence - top.score
                    ))

        # Step 10: Classify final status and save results
        conf_level, status = classify_confidence(res.confidence_score)
        res.confidence_level = conf_level
        res.email_status = status

        return self._save_and_return(res)

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
        except Exception:
            pass
        return res


def enrich_prospect(input_data: ProspectInput | dict, use_cache: bool = True, allow_providers: bool = True) -> ProspectResult:
    """Convenience function to enrich a single prospect from model or dict."""
    if isinstance(input_data, dict):
        input_data = ProspectInput(**input_data)
    return ProspectProcessor(use_cache=use_cache, allow_providers=allow_providers).process(input_data)
