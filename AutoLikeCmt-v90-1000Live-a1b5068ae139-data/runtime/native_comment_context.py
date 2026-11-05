"""Prepare native comment context once per loaded page, then send and verify all CIDs in one pass."""
import time
from digg_state_confirmation import reconcile

READY_SCRIPT = r"""
if (window.__alc_comment_mutations && window.__alc_comment_mutations.root && window.__alc_comment_mutations.reply) {
  return true;
}
try {
  const chunks = window.__LOADABLE_LOADED_CHUNKS__;
  if (chunks && typeof chunks.push === 'function') {
    let requireFn = null;
    chunks.push([['alc_probe_' + Date.now()], {}, function(r) { requireFn = r; }]);
    if (requireFn && requireFn.m) {
      for (const [moduleId, factory] of Object.entries(requireFn.m)) {
        const source = Function.prototype.toString.call(factory);
        if (!source.includes('/api/comment/digg/') || !source.includes('postCommentLike') || !source.includes('postReplyLike')) continue;
        const exports = requireFn(moduleId);
        let root = null, reply = null;
        for (const value of Object.values(exports || {})) {
          if (typeof value !== 'function' || value.length !== 0) continue;
          let configs;
          try { configs = value(); } catch (_) { continue; }
          if (!Array.isArray(configs)) continue;
          for (const config of configs) {
            const mutation = config && config.mutation;
            if (mutation && mutation.postCommentLike && typeof mutation.postCommentLike.mutationFn === 'function') root = mutation.postCommentLike.mutationFn;
            if (mutation && mutation.postReplyLike && typeof mutation.postReplyLike.mutationFn === 'function') reply = mutation.postReplyLike.mutationFn;
          }
        }
        if (root && reply) {
          window.__alc_comment_mutations = { root, reply, moduleId };
          return true;
        }
      }
    }
  }
} catch (_) {}
const roots = document.querySelectorAll('[data-e2e="comment-level-1"],[data-e2e="comment-username-1"],[data-e2e="comment-level-2"],[data-e2e="comment-username-2"],[data-e2e="comment-item"],[class*="DivCommentItemContainer"],[class*="CommentItem"]');
return [...roots].some(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&(e.textContent||'').trim().length>0;});
"""

QUICK_CLICK_SCRIPT = r"""
const sels = [
  "[data-e2e='comment-icon']",
  "[data-e2e='browse-comment-icon']",
  "[data-e2e='play-side-comment']",
  "[data-e2e='video-comment']",
  "[data-e2e='comments']",
  "[class*='SectionActionBarContainer'] button[aria-label*='omment']",
  "[class*='SectionActionBarContainer'] button[aria-label*='luận']",
  "button[aria-label*='omment']"
];
for (const s of sels) {
  const el = document.querySelector(s);
  if (el) {
    try { el.click(); return true; } catch (_) {}
  }
}
return false;
"""


def send(bot, original, aweme_id, comments, digg_type=1, concurrency=8):
    if not comments:
        return []
    bot._check_stop()
    started = time.monotonic()
    try:
        ready = bool(bot.driver.execute_script(READY_SCRIPT))
        if not ready:
            try:
                if bot.driver.execute_script(QUICK_CLICK_SCRIPT):
                    q_deadline = time.monotonic() + 1.8
                    while time.monotonic() < q_deadline:
                        bot._check_stop()
                        time.sleep(0.06)
                        if bot.driver.execute_script(READY_SCRIPT):
                            ready = True
                            break
            except Exception:
                pass
        if not ready:
            opened = bool(bot.open_comment_panel())
            if opened:
                deadline = time.monotonic() + 3.5
                while True:
                    opened = bool(bot.driver.execute_script(READY_SCRIPT))
                    if opened or time.monotonic() >= deadline:
                        break
                    bot._check_stop()
                    time.sleep(0.08)
            if opened or bool(bot.driver.execute_script(READY_SCRIPT)):
                bot.driver.execute_script('window.__alc_native_comment_ready=true;')
                bot.log(f'[context] Mở comment gốc: {time.monotonic()-started:.2f}s; giữ cache CID.')
            else:
                bot.log('[context] Comment chưa render; chưa gửi tim, giữ phiên để kiểm tra lại.')
                return reconcile(bot, aweme_id, comments, [(False, -1, 'Chưa gửi: comment context chưa sẵn sàng') for _ in comments])
    except Exception as error:
        if getattr(bot, 'stop_event', None) is not None and bot.stop_event.is_set():
            raise
        bot.log('[context] Chuẩn bị comment: ' + type(error).__name__)
        return [(False, -1, 'Chưa gửi: ' + type(error).__name__) for _ in comments]
    try:
        from cookie_api_session import _sync_page_identity
        _sync_page_identity(bot)
    except Exception:
        pass
    limit = min(8, max(1, int(concurrency or 8)))
    rows = original(bot, aweme_id, comments, digg_type=digg_type, concurrency=limit)
    return reconcile(bot, aweme_id, comments, rows, retry_digg_fn=original if digg_type == 1 else None)


def install():
    import tiktok_reply as tt
    cls = tt.TikTokLikeBot
    original = cls.api_digg_comments
    cls.api_digg_comments = lambda self, *a, **k: send(self, original, *a, **k)
