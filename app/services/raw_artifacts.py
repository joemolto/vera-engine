"""Persistent corpus of raw scraped payloads for re-extraction.

Every Jina / Apollo / PredictLeads / threat-feed network response is
written to Supabase Storage and indexed in `raw_artifacts`. When a prompt
improves or a new extractor is added, the stored corpus can be re-processed
without re-fetching every payload.

Key shape:   `<source>/<domain>/<sha256[:16]>.json`
Dedup:       (source, content_hash) is unique — if the same payload
             arrives twice for the same source we return the existing id
             without writing storage twice.

`persist_raw` is fire-and-forget from the caller's perspective: any
exception is logged and swallowed so a storage hiccup never breaks an
enrichment run.
"""

import hashlib
import json
import logging
from typing import Any, Optional

from supabase import Client

logger = logging.getLogger(__name__)

BUCKET = "raw-artifacts"


def _to_bytes(payload: Any) -> tuple[bytes, str]:
    """Coerce a payload to bytes + content_type. Tolerates dicts, lists,
    strings, and pre-encoded bytes."""
    if isinstance(payload, bytes):
        return payload, "application/octet-stream"
    if isinstance(payload, str):
        return payload.encode("utf-8"), "text/plain"
    return json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"), "application/json"


def _ensure_bucket(db: Client) -> None:
    """Create the private bucket if it doesn't exist. Idempotent."""
    try:
        db.storage.get_bucket(BUCKET)
    except Exception:
        try:
            db.storage.create_bucket(BUCKET, options={"public": False})
            logger.info("Created Supabase Storage bucket %s", BUCKET)
        except Exception as e:
            # Bucket exists race or permission issue — don't crash callers
            logger.debug("create_bucket noop for %s: %s", BUCKET, e)


def persist_raw(
    db: Client,
    domain: str,
    source: str,
    payload: Any,
    *,
    source_endpoint: Optional[str] = None,
    org_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    company_id: Optional[str] = None,
) -> Optional[str]:
    """Persist a raw payload. Returns the artifact id, or None on failure.

    Idempotent: identical (source, content_hash) returns the existing id.
    Failures are logged + swallowed so callers stay simple.
    """
    if not domain or not source or payload is None:
        return None

    try:
        from app.services.domain_resolver import clean_domain
        cleaned_domain = clean_domain(domain) or domain.lower()

        body, content_type = _to_bytes(payload)
        if not body:
            return None
        digest = hashlib.sha256(body).hexdigest()
        short = digest[:16]

        # Dedup check before doing any storage I/O.
        existing = (
            db.table("raw_artifacts")
            .select("id, storage_path")
            .eq("source", source)
            .eq("content_hash", digest)
            .limit(1)
            .execute()
        )
        if existing.data:
            return existing.data[0]["id"]

        ext = "json" if content_type == "application/json" else (
            "txt" if content_type == "text/plain" else "bin"
        )
        path = f"{source}/{cleaned_domain}/{short}.{ext}"

        _ensure_bucket(db)
        try:
            db.storage.from_(BUCKET).upload(
                path=path,
                file=body,
                file_options={"content-type": content_type, "upsert": "true"},
            )
        except Exception as e:
            # Some supabase-py versions raise on duplicate; treat as benign.
            logger.debug("Storage upload noop for %s: %s", path, e)

        row = {
            "domain": cleaned_domain,
            "source": source,
            "source_endpoint": source_endpoint,
            "content_hash": digest,
            "storage_path": path,
            "size_bytes": len(body),
            "content_type": content_type,
            "org_id": org_id,
            "lead_id": lead_id,
            "company_id": company_id,
        }
        inserted = db.table("raw_artifacts").insert(row).execute()
        if inserted.data:
            return inserted.data[0]["id"]
        return None

    except Exception as e:
        logger.warning("persist_raw failed (%s/%s): %s", source, domain, e)
        return None


def fetch_raw(db: Client, artifact_id: str) -> Optional[bytes]:
    """Fetch the raw bytes for a given artifact id. Used by reprocess scripts."""
    res = db.table("raw_artifacts").select("storage_path").eq("id", artifact_id).execute()
    if not res.data:
        return None
    path = res.data[0]["storage_path"]
    try:
        return db.storage.from_(BUCKET).download(path)
    except Exception as e:
        logger.warning("fetch_raw failed for %s: %s", artifact_id, e)
        return None
