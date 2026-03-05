"""
Sentry Ingestion Tasks

Tasks for ingesting and analyzing Sentry errors.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="ingest_top_10_sentry_errors")
def ingest_top_10_sentry_errors() -> dict:
    """
    Ingest and collect the top 10 most frequent errors from Sentry.
    
    Returns:
        dict with top_10_errors list containing rank, error title, and frequency
    """
    from app.integrations.sentry_client import SentryClient
    import json
    
    logger.info("Starting ingestion of top 10 Sentry errors")
    
    client = SentryClient()
    if not client.is_configured():
        logger.warning("Sentry client not configured")
        return {"error": "Sentry not configured", "top_10_errors": []}
    
    try:
        # Fetch unresolved issues with high limit to get frequency data
        issues = client._list_issues_sync(query="is:unresolved", limit=100)
        
        # Count error frequencies
        error_counts = Counter()
        for issue in issues:
            error_title = issue.get('title', 'Unknown')
            count = issue.get('count', 1)
            if isinstance(count, str):
                count = int(count)
            error_counts[error_title] += count
        
        # Get top 10
        top_10 = error_counts.most_common(10)
        result = [
            {
                'rank': i + 1,
                'error': title,
                'frequency': count,
                'timestamp': datetime.utcnow().isoformat()
            }
            for i, (title, count) in enumerate(top_10)
        ]
        
        logger.info(f"Successfully ingested top 10 Sentry errors. Top error: {result[0]['error']} with {result[0]['frequency']} occurrences")
        
        return {
            'success': True,
            'top_10_errors': result,
            'total_issues': len(issues),
            'timestamp': datetime.utcnow().isoformat()
        }
    
    except Exception as e:
        logger.error(f"Error ingesting Sentry errors: {e}", exc_info=True)
        return {'error': str(e), 'top_10_errors': [], 'success': False}
