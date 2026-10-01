"""GitHub public commit and user profile email discovery."""
from __future__ import annotations

import os
import requests
from ..validation.syntax import is_valid_syntax, is_role_email

GITHUB_API_URL = "https://api.github.com"


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.cloak-preview+json,application/vnd.github.v3+json",
        "User-Agent": "agent-outreach-prospecting/1.0"
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def search_github_domain_emails(domain: str, limit: int = 5) -> dict[str, str]:
    """Search public GitHub commits for exposed author emails on the company domain.
    
    Returns:
        dict of {email: commit_or_profile_url}
    """
    if not domain:
        return {}

    dom_clean = domain.strip().lower().removeprefix("www.")
    found: dict[str, str] = {}

    try:
        url = f"{GITHUB_API_URL}/search/commits?q={dom_clean}&sort=author-date&order=desc&per_page={limit}"
        resp = requests.get(url, headers=_headers(), timeout=6.0)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("items", []):
                commit = item.get("commit", {})
                html_url = item.get("html_url") or ""
                for person_key in ("author", "committer"):
                    person = commit.get(person_key) or {}
                    email = (person.get("email") or "").lower().strip()
                    if is_valid_syntax(email) and email.endswith(f"@{dom_clean}"):
                        if not is_role_email(email) and email not in found:
                            found[email] = html_url
    except Exception:
        pass

    return found


def search_github_user_email(full_name: str, domain: str) -> tuple[str | None, str | None]:
    """Search GitHub for a user by name to check if their public profile lists an email on the domain."""
    if not full_name or not domain:
        return None, None

    dom_clean = domain.strip().lower().removeprefix("www.")
    try:
        url = f"{GITHUB_API_URL}/search/users?q={full_name}+in:name&per_page=3"
        resp = requests.get(url, headers=_headers(), timeout=6.0)
        if resp.status_code == 200:
            data = resp.json()
            for user in data.get("items", []):
                user_api = user.get("url")
                if user_api:
                    u_resp = requests.get(user_api, headers=_headers(), timeout=4.0)
                    if u_resp.status_code == 200:
                        u_data = u_resp.json()
                        email = (u_data.get("email") or "").lower().strip()
                        if is_valid_syntax(email) and email.endswith(f"@{dom_clean}"):
                            return email, user.get("html_url")
    except Exception:
        pass

    return None, None
