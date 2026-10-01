"""Bulk prospect processing with controlled concurrency, rate limiting, deduplication, and daily target cap."""
from __future__ import annotations

import csv
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from ..models import ProspectInput, ProspectResult
from ..config import daily_verified_target
from .processor import ProspectProcessor

CSV_COLUMN_ALIASES = {
    "first_name": ["first_name", "first name", "firstname", "first"],
    "last_name": ["last_name", "last name", "lastname", "last"],
    "company": ["company", "company name", "organization", "agency", "business"],
    "domain": ["domain", "website", "company website", "url", "site"],
    "title": ["title", "job title", "role", "designation", "position"],
    "linkedin_url": ["linkedin", "linkedin url", "linkedin_url", "person linkedin"],
}


def _extract_column(row: dict[str, str], field: str) -> str:
    lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    for alias in CSV_COLUMN_ALIASES[field]:
        if alias in lowered and lowered[alias]:
            return lowered[alias]
    return ""


def load_prospects_from_csv(file_path: Path | str) -> list[ProspectInput]:
    """Parse a CSV file of prospects with flexible column matching."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    prospects: list[ProspectInput] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            first = _extract_column(row, "first_name")
            last = _extract_column(row, "last_name")
            comp = _extract_column(row, "company")
            dom = _extract_column(row, "domain")
            title = _extract_column(row, "title")
            li = _extract_column(row, "linkedin_url")

            # Try splitting full name if first_name was empty but a 'name' column exists
            if not first and not last:
                full = row.get("name") or row.get("full name") or row.get("Full Name") or ""
                parts = full.strip().split(maxsplit=1)
                if len(parts) == 2:
                    first, last = parts
                elif len(parts) == 1:
                    first = parts[0]

            if (first or last) and (comp or dom):
                prospects.append(ProspectInput(
                    first_name=first,
                    last_name=last,
                    company=comp,
                    domain=dom or None,
                    title=title or None,
                    linkedin_url=li or None
                ))
    return prospects


class BulkProcessor:
    """Processes bulk prospect batches with controlled concurrency, deduplication and daily target cap."""

    def __init__(
        self,
        max_workers: int = 3,
        rate_limit_delay: float = 0.5,
        target_verified: int | None = None
    ):
        self.max_workers = max_workers
        self.rate_limit_delay = rate_limit_delay
        self.target_verified = target_verified or daily_verified_target()
        self.processor = ProspectProcessor()

    def process_batch(
        self,
        inputs: list[ProspectInput],
        on_progress: Callable[[ProspectResult, int, int], None] | None = None
    ) -> list[ProspectResult]:
        """Process a list of prospects concurrently, stopping when target_verified is reached."""
        results: list[ProspectResult] = []
        verified_count = 0
        total = len(inputs)

        # In-memory batch deduplication tracker
        seen_keys: set[str] = set()
        deduped_inputs: list[ProspectInput] = []

        for p in inputs:
            k1 = f"li:{p.linkedin_url.strip().lower().rstrip('/')}" if p.linkedin_url else None
            k2 = f"name_dom:{p.full_name.lower()}:{p.domain.lower()}" if p.domain else None
            k3 = f"name_comp:{p.full_name.lower()}:{p.company.lower()}" if p.company else None

            if (k1 and k1 in seen_keys) or (k2 and k2 in seen_keys) or (k3 and k3 in seen_keys):
                continue
            if k1:
                seen_keys.add(k1)
            if k2:
                seen_keys.add(k2)
            if k3:
                seen_keys.add(k3)
            deduped_inputs.append(p)

        def _worker(item: ProspectInput) -> ProspectResult:
            time.sleep(self.rate_limit_delay)
            return self.processor.process(item)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_item = {executor.submit(_worker, item): item for item in deduped_inputs}
            for i, future in enumerate(as_completed(future_to_item), 1):
                try:
                    res = future.result()
                    results.append(res)
                    if res.confidence_level in ("verified", "high_confidence") and res.final_email:
                        verified_count += 1

                    if on_progress:
                        on_progress(res, i, total)

                    # Check daily verified cap
                    if self.target_verified and verified_count >= self.target_verified:
                        # Cancel pending futures
                        for f in future_to_item:
                            f.cancel()
                        break
                except Exception:
                    pass

        return results

    def export_to_csv(self, results: list[ProspectResult], output_path: Path | str) -> Path:
        """Export prospect results to CSV."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "id", "name", "company", "title", "domain", "email",
            "confidence", "status", "source", "source_url",
            "provider", "pattern", "linkedin_url"
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                writer.writerow({
                    "id": r.id or "",
                    "name": r.full_name,
                    "company": r.company,
                    "title": r.title,
                    "domain": r.domain,
                    "email": r.final_email or "",
                    "confidence": r.confidence_score,
                    "status": r.email_status,
                    "source": r.source,
                    "source_url": r.source_url or "",
                    "provider": r.verification_provider or "",
                    "pattern": r.email_pattern or "",
                    "linkedin_url": r.linkedin_url,
                })
        return path
