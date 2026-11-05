"""Classify the current document without treating comment text as an HTTP error."""
import re

FIX_VERSION = 'v92-nav-scout-20261022.1'
_ERROR_TITLE = re.compile(r'^(?:(?:error|http(?: error)?)\s*[: -]?\s*)?(?:403(?:\s*[-: ]\s*(?:forbidden|access denied))?|forbidden|access denied|truy cập bị từ chối)\s*[.!]?$', re.I)


def classify_navigation(snapshot):
    try:
        status = int(snapshot.get('status') or 0)
    except (TypeError, ValueError):
        status = 0
    title = ' '.join(str(snapshot.get('title') or '').split())
    body = ' '.join(str(snapshot.get('body') or '').split())
    explicit_error = bool(_ERROR_TITLE.fullmatch(title))
    # Older browsers may not expose responseStatus. Only accept a small,
    # standalone error document in that case, never text inside a loaded post.
    fallback_error = (not status and not snapshot.get('app_content') and len(body) < 180
                      and bool(_ERROR_TITLE.fullmatch(body)))
    blocked = status == 403 or explicit_error or fallback_error
    reason = ('document_http_403' if status == 403 else
              'explicit_error_document' if blocked else 'no_access_denial_evidence')
    return {'status': status, 'blocked': blocked, 'reason': reason}


def inspect_navigation(driver):
    snapshot = driver.execute_script("""
        const navigation = performance.getEntriesByType('navigation')[0];
        return {
            status: Number(navigation && navigation.responseStatus) || 0,
            title: (document.title || '').slice(0, 180),
            body: ((document.body && document.body.innerText) || '').slice(0, 1200),
            app_content: !!document.querySelector(
                'video,[data-e2e="browse-video"],[data-e2e="comment-list"],[data-e2e="comment-level-1"]')
        };
    """) or {}
    return classify_navigation(snapshot)


def write_runtime_receipt(scheduler_path):
    """Record which installed sources the frozen app actually imported."""
    import hashlib
    import json
    import os
    from datetime import datetime
    from pathlib import Path
    import sys
    main_module = sys.modules.get('__main__')
    if not (getattr(sys, 'frozen', False) or hasattr(main_module, '__compiled__')):
        return
    root = Path(scheduler_path).resolve().parent
    receipt = {'fix_version': FIX_VERSION, 'loaded_at': datetime.now().astimezone().isoformat(),
               'python_version': sys.version.split()[0], 'executable': sys.executable,
               'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                 for name in ('pool_scheduler.py', 'tiktok_reply.py', 'navigation_health.py')}}
    destination = root / 'logs/v92-nav-scout-loaded.json'
    destination.parent.mkdir(exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    os.replace(temporary, destination)
