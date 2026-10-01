"""Discovery package for company website, public search and GitHub."""
from .company_site import discover_emails_from_site
from .search import (
    search_company_emails,
    search_exact_candidate_email,
    search_name_with_domain
)
from .github import search_github_domain_emails, search_github_user_email

__all__ = [
    "discover_emails_from_site",
    "search_company_emails",
    "search_exact_candidate_email",
    "search_name_with_domain",
    "search_github_domain_emails",
    "search_github_user_email"
]
