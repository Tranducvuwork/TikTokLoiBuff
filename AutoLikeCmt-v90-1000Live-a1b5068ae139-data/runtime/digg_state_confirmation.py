"""1-Shot Fast Verify: confirm real comment digg state (user_digged=1) in a single parallel pass."""
import json

FETCH_SCRIPT = r"""
const [awemeId, targets, cb] = arguments;
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const states = {};
  const wantedCids = new Set((targets || []).map(x => String(x.cid || '')).filter(Boolean));
  const hasRootTargets = (targets || []).some(x => {
    const p = String(x.parent_cid || '');
    return !p || p === '0' || p === String(x.cid || '');
  });
  const parents = [...new Set((targets || []).map(x => {
    const p = String(x.parent_cid || '');
    const c = String(x.cid || '');
    return (p && p !== '0' && p !== c) ? p : '';
  }).filter(Boolean))];

  if (!window.__alc_cid_cursors) window.__alc_cid_cursors = Object.create(null);
  if (!window.__alc_cid_cursors[awemeId]) window.__alc_cid_cursors[awemeId] = Object.create(null);
  const cidCursorMap = window.__alc_cid_cursors[awemeId];

  let http = window.__alc_http_client || null;
  try {
    if (!http) {
      const chunks = window.__LOADABLE_LOADED_CHUNKS__;
      let req = null;
      if (chunks && typeof chunks.push === 'function') {
        chunks.push([['alc_ver_' + Date.now()], {}, function(r) { req = r; }]);
      }
      const m345 = req && req.m && req.m[345146] ? req(345146) : null;
      if (m345 && m345.h) {
        http = m345.h;
        window.__alc_http_client = http;
      }
    }
    if (http) {
      if (http.appContext && typeof http.appContext === 'object' && !http.appContext.wid) {
        http.appContext = undefined;
      }
      if (!http.appContext && typeof http.handleAppContext === 'function') {
        try { await http.handleAppContext(); } catch (_) {}
      }
      const ctx = http.appContext;
      const id = window.__alc_identity || {};
      if (ctx && typeof ctx === 'object') {
        if (id.csrf) ctx.csrfToken = String(id.csrf);
        if (id.wid) ctx.wid = String(id.wid);
        if (id.uidStr) ctx.odinId = String(id.uidStr);
        else if (id.wid) ctx.odinId = String(id.wid);
        if (id.region) {
          ctx.region = String(id.region);
          ctx.priorityRegion = String(id.region);
        }
        if (!ctx.user || typeof ctx.user !== 'object') {
          ctx.user = {region: id.region || ctx.region || 'VN', ftcUser: false};
        } else if (id.region) {
          ctx.user.region = String(id.region);
        }
        if (id.uidStr) ctx.user.uid = String(id.uidStr);
        if (id.uname) ctx.user.uniqueId = String(id.uname).replace(/^@/, '');
      }
    }
  } catch (_) {}

  const fetchOnce = async (path, query) => {
    const q = Object.assign({}, query, {_t: Date.now() + Math.floor(Math.random() * 1000)});
    if (http && typeof http.get === 'function') {
      let timer = null;
      try {
        const p = Promise.resolve(
          http.get(path, {
            query: q,
            headers: {'cache-control': 'no-cache', 'pragma': 'no-cache'},
            baseUrlType: 2
          })
        );
        const raw = await Promise.race([
          p,
          new Promise((_, rej) => { timer = setTimeout(() => rej(new Error('timeout')), 2500); })
        ]);
        if (raw && typeof raw === 'object') {
          const d = (raw.data && typeof raw.data === 'object' && Array.isArray(raw.data.comments)) ? raw.data : raw;
          if (Array.isArray(d.comments)) return d;
        }
      } catch (_) {
      } finally {
        if (timer) clearTimeout(timer);
      }
    }
    let timer2 = null;
    try {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(q)) {
        if (v !== undefined && v !== null) qs.set(k, String(v));
      }
      const ctrl = new AbortController();
      timer2 = setTimeout(() => ctrl.abort(), 2500);
      const r = await fetch(path + '?' + qs.toString(), {
        credentials: 'include',
        cache: 'no-store',
        signal: ctrl.signal,
        headers: {accept: 'application/json, text/plain, */*'}
      });
      const raw = JSON.parse(await r.text());
      if (!raw || typeof raw !== 'object') return null;
      const d = (raw.data && typeof raw.data === 'object' && Array.isArray(raw.data.comments)) ? raw.data : raw;
      return Array.isArray(d.comments) ? d : null;
    } catch (_) {
      return null;
    } finally {
      if (timer2) clearTimeout(timer2);
    }
  };

  const fetchPage = async (path, query) => {
    let d = await fetchOnce(path, query);
    if (!d) {
      await sleep(140);
      d = await fetchOnce(path, query);
    }
    return d;
  };

  const harvest = (data, isReply, cursorUsed) => {
    let count = 0;
    for (const c of (data && data.comments) || []) {
      count++;
      const cid = String(c.cid || '');
      if (cid) {
        const val = Number(c.user_digged || 0);
        if (states[cid] === undefined || val === 1) states[cid] = val;
        if (!isReply && cursorUsed !== undefined) cidCursorMap[cid] = cursorUsed;
      }
      if (!isReply) {
        for (const r of c.reply_comment || []) {
          const rcid = String(r.cid || '');
          const rval = Number(r.user_digged || 0);
          if (rcid && (states[rcid] === undefined || rval === 1)) {
            states[rcid] = rval;
            if (cursorUsed !== undefined) cidCursorMap[rcid] = cursorUsed;
          }
        }
      }
    }
    return count;
  };

  const checkRoot = async () => {
    if (!hasRootTargets && parents.length > 0) return;
    const fetchedCursors = new Set();
    const cachedCursors = [...new Set(
      [...wantedCids]
        .map(cid => cidCursorMap[cid])
        .filter(cur => cur !== undefined && cur !== null && Number.isInteger(Number(cur)) && Number(cur) > 0)
        .map(cur => Number(cur))
    )].slice(0, 3);
    for (const cCur of cachedCursors) {
      fetchedCursors.add(cCur);
      const d = await fetchPage('/api/comment/list/', {aweme_id: awemeId, cursor: cCur, count: 20, aid: 1988});
      if (d) harvest(d, false, cCur);
      if ([...wantedCids].every(c => states[c] !== undefined)) return;
    }
    let cursor = 0;
    let seenCount = 0;
    for (let page = 0; page < 8; page++) {
      if (!fetchedCursors.has(cursor)) {
        fetchedCursors.add(cursor);
        const d = await fetchPage('/api/comment/list/', {aweme_id: awemeId, cursor: cursor, count: 20, aid: 1988});
        if (!d) break;
        seenCount += harvest(d, false, cursor);
        const allFound = [...wantedCids].every(c => states[c] !== undefined);
        if (allFound) break;
        const nextCur = Number(d.cursor);
        const total = Number(d.total || 0);
        if (d.has_more && !Number.isNaN(nextCur) && nextCur !== cursor) {
          cursor = nextCur;
        } else if (!d.has_more && !Number.isNaN(nextCur) && nextCur > cursor && seenCount < total) {
          cursor = nextCur;
        } else {
          break;
        }
      } else {
        cursor += 20;
      }
    }
  };

  const checkReply = async (parentId) => {
    let cursor = 0;
    let seenCount = 0;
    for (let page = 0; page < 8; page++) {
      const d = await fetchPage('/api/comment/list/reply/', {
        item_id: awemeId,
        aweme_id: awemeId,
        comment_id: parentId,
        cursor: cursor,
        count: 20,
        aid: 1988
      });
      if (!d) break;
      seenCount += harvest(d, true, cursor);
      const parentTargets = (targets || []).filter(x => String(x.parent_cid || '') === parentId);
      if (parentTargets.every(x => states[String(x.cid || '')] !== undefined)) break;
      const nextCur = Number(d.cursor);
      const total = Number(d.total || 0);
      if (d.has_more && !Number.isNaN(nextCur) && nextCur !== cursor) {
        cursor = nextCur;
      } else if (!d.has_more && !Number.isNaN(nextCur) && nextCur > cursor && seenCount < total) {
        cursor = nextCur;
      } else {
        break;
      }
    }
  };

  try {
    await sleep(85);
    await Promise.all([checkRoot(), ...parents.map(p => checkReply(p))]);
    const zeroTargets = (targets || []).filter(x => states[String(x.cid || '')] === 0);
    if (zeroTargets.length > 0) {
      await sleep(200);
      const retryRoot = zeroTargets.some(x => {
        const p = String(x.parent_cid || '');
        return !p || p === '0' || p === String(x.cid || '');
      });
      const retryParents = [...new Set(zeroTargets.map(x => String(x.parent_cid || '')).filter(p => p && p !== '0'))];
      await Promise.all([
        retryRoot ? checkRoot() : Promise.resolve(),
        ...retryParents.map(p => checkReply(p))
      ]);
    }
  } catch (_) {}
  cb(JSON.stringify(states));
})();
"""


def reconcile(bot, aweme_id, comments, results, retry_digg_fn=None):
    rows = list(results)
    candidates = [i for i, r in enumerate(rows) if i < len(comments) and r[0]]
    if not candidates or not getattr(bot, '_api_identity_confirmed', False):
        return rows
    targets = [
        {'cid': str(comments[i].get('cid') or ''), 'parent_cid': str(comments[i].get('parent_cid') or '')}
        for i in candidates if comments[i].get('cid')
    ]
    if not targets:
        return rows
    try:
        bot.driver.set_script_timeout(8)
        raw = bot.driver.execute_async_script(FETCH_SCRIPT, str(aweme_id), targets)
        states = json.loads(raw or '{}') if isinstance(raw, str) else {}
    except Exception:
        return rows
    if not isinstance(states, dict) or not states:
        return rows
    unconfirmed_indices = [
        i for i in candidates
        if rows[i][0] and states.get(str(comments[i].get('cid') or '')) == 0
    ]
    if (
        unconfirmed_indices
        and callable(retry_digg_fn)
        and not getattr(bot, 'last_session_invalid', False)
        and not (getattr(bot, 'stop_event', None) is not None and bot.stop_event.is_set())
    ):
        try:
            bot._wid_salt = int(getattr(bot, '_wid_salt', 0) or 0) + 1
            from cookie_api_session import _sync_page_identity
            _sync_page_identity(bot)
            retry_comments = [comments[i] for i in unconfirmed_indices]
            retry_digg_fn(bot, aweme_id, retry_comments, digg_type=1, concurrency=3)
            retry_targets = [
                {'cid': str(comments[i].get('cid') or ''), 'parent_cid': str(comments[i].get('parent_cid') or '')}
                for i in unconfirmed_indices if comments[i].get('cid')
            ]
            if retry_targets:
                bot.driver.set_script_timeout(8)
                raw2 = bot.driver.execute_async_script(FETCH_SCRIPT, str(aweme_id), retry_targets)
                states2 = json.loads(raw2 or '{}') if isinstance(raw2, str) else {}
                if isinstance(states2, dict):
                    for k, v in states2.items():
                        if v == 1 or k not in states:
                            states[k] = v
        except Exception:
            pass
    confirmed = 0
    rejected = 0
    has_any_zero = any(states.get(str(comments[i].get('cid') or '')) == 0 for i in candidates)
    has_any_one = any(states.get(str(comments[i].get('cid') or '')) == 1 for i in candidates)
    for i in candidates:
        cid = str(comments[i].get('cid') or '')
        st = states.get(cid)
        if st == 1:
            rows[i] = (True, 0, 'confirmed_user_digged=1')
            confirmed += 1
        elif st == 0 and rows[i][0]:
            rows[i] = (False, -3, 'unconfirmed_user_digged=0')
            rejected += 1
        elif rows[i][0] and has_any_zero and not has_any_one:
            rows[i] = (False, -3, 'unconfirmed_user_digged=0')
            rejected += 1
    if rejected:
        bot.log(f'[confirm] Thực tế lên tim: {confirmed}/{len(candidates)} CID (loại {rejected} tim ảo user_digged=0 để chạy bù acc khác).')
    elif confirmed:
        bot.log(f'[confirm] Xác nhận {confirmed}/{len(candidates)} CID đã ăn tim thật 100% (user_digged=1).')
    return rows
