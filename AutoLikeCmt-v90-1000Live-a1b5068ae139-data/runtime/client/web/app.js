let captchaBalanceLabel = 'Captcha còn: đang tải…';
/* UI logic gọi Python Bridge qua pywebview API.

Quy ước: mọi call trả Promise (pywebview.api.<method>(...)). JS await.
State: logCursor (index log đã render), poll mỗi 250ms.
*/
let logCursor = 0;
let pollTimer = null;
let pollBusy = false;
let updateInfo = null;
let resultPendingBlock = null;
let resultRenderQueue = [];
let resultRenderTimer = null;
let resultFilter = 'all';
let resultAutoFollow = true;
let resultUnseen = 0;
let resultCounts = { total: 0, ok: 0, attention: 0 };
// Giữ toàn bộ kết quả trong lượt chạy; không cắt ở 600 account.

const THEME_KEY = 'alc-theme';

function syncThemeControls() {
  const isLight = document.documentElement.dataset.theme === 'light';
  document.querySelectorAll('.theme-toggle').forEach(button => {
    button.textContent = isLight ? 'Tối' : 'Sáng';
    button.setAttribute('aria-pressed', String(isLight));
    button.title = isLight ? 'Chuyển sang giao diện tối' : 'Chuyển sang giao diện sáng';
  });
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
  syncThemeControls();
}

// pywebview sẵn sàng mới gọi được api. Đợi event.
// Core controls remain bound even if an optional panel fails during setup.
document.addEventListener('click', event => {
  const control = event.target.closest && event.target.closest('.tabtab, #startBtn');
  if (!control || control.disabled) return;
  if (control.id === 'startBtn') { doStart(); return; }
  document.querySelectorAll('.tabtab').forEach(tab => tab.classList.toggle('active', tab === control));
  ['buff', 'accs', 'cmt', 'cmtacc', 'proxy', 'log'].forEach(name => {
    const panel = document.getElementById('tab-' + name);
    if (panel) panel.classList.toggle('hidden', name !== control.dataset.tab);
  });
  const load = {accs: loadAccList, cmt: loadCmtConfig, cmtacc: loadCmtAccList, proxy: loadProxysachStatus}[control.dataset.tab];
  if (load) Promise.resolve(load()).catch(error => console.error('Không tải được tab:', error));
});
window.addEventListener('pywebviewready', init);

// Gắn sự kiện cho các nút (không dùng inline onclick để tránh lệ thuộc
// global scope). Chạy khi DOM sẵn sàng, độc lập với pywebviewready.
function bindEvents() {
  document.querySelectorAll('.theme-toggle').forEach(button => button.addEventListener('click', toggleTheme));
  syncThemeControls();
  document.getElementById('loginBtn').addEventListener('click', doLogin);

  document.getElementById('apiCaptureBtn').addEventListener('click', doApiCapture);
  document.getElementById('pauseBtn').addEventListener('click', doPause);
  document.getElementById('stopBtn').addEventListener('click', doStop);
  document.getElementById('refreshPoolBtn').addEventListener('click', doRefreshPool);
  document.getElementById('onTopBtn').addEventListener('click', async () => {
    try { await pywebview.api.toggle_on_top(); } catch (e) { /* ignore */ }
  });
  document.getElementById('liveCheckBtn').addEventListener('click', doLiveCheck);

  document.getElementById('accListRefresh').addEventListener('click', loadAccList);
  document.getElementById('livePauseBtn').addEventListener('click', doPauseLiveCheck);
  document.getElementById('liveStopBtn').addEventListener('click', doStopLiveCheck);
  const delBan = document.getElementById('deleteBannedBtn');
  if (delBan) delBan.addEventListener('click', doDeleteBanned);
  const delDie = document.getElementById('deleteDeadBtn');
  if (delDie) delDie.addEventListener('click', doDeleteDead);
  const delUnknown = document.getElementById('deleteUnknownBtn');
  if (delUnknown) delUnknown.addEventListener('click', doDeleteUnknown);
  const delSel = document.getElementById('deleteSelectedBtn');
  if (delSel) delSel.addEventListener('click', doDeleteSelected);
  const importAccSend = document.getElementById('importAccSend');
  if (importAccSend) importAccSend.addEventListener('click', doImportAcc);
  const cmtImp = document.getElementById('cmtImportBtn');
  if (cmtImp) cmtImp.addEventListener('click', doCmtImport);
  const cmtRf = document.getElementById('cmtRefreshBtn');
  if (cmtRf) cmtRf.addEventListener('click', loadCmtAccList);
  const cmtDel = document.getElementById('cmtDeleteBtn');
  if (cmtDel) cmtDel.addEventListener('click', doCmtDelete);
  const cmtGo = document.getElementById('cmtStartBtn');
  if (cmtGo) cmtGo.addEventListener('click', doCmtStart);
  const cmtSave = document.getElementById('cmtSaveCfgBtn');
  if (cmtSave) cmtSave.addEventListener('click', doSaveCmtCfg);
  const cmtPause = document.getElementById('cmtPauseBtn');
  if (cmtPause) cmtPause.addEventListener('click', doPause);
  const cmtStop = document.getElementById('cmtStopBtn');
  if (cmtStop) cmtStop.addEventListener('click', () => doStopFrom('cmtStopBtn'));
  const cmtLp = document.getElementById('cmtLivePauseBtn');
  if (cmtLp) cmtLp.addEventListener('click', doCmtLivePause);
  const cmtLs = document.getElementById('cmtLiveStopBtn');
  if (cmtLs) cmtLs.addEventListener('click', doCmtLiveStop);
  const cmtLive = document.getElementById('cmtLiveBtn');
  if (cmtLive) cmtLive.addEventListener('click', doCmtLiveCheck);
  const cmtPrivate = document.getElementById('cmtPrivateBtn');
  if (cmtPrivate) cmtPrivate.addEventListener('click', () => doCmtProfileAction('private'));
  const cmtProfileStop = document.getElementById('cmtProfileStopBtn');
  if (cmtProfileStop) cmtProfileStop.addEventListener('click', doCmtProfileStop);
  const cmtAvatar = document.getElementById('cmtAvatarBtn');
  if (cmtAvatar) cmtAvatar.addEventListener('click', () => doCmtProfileAction('avatar'));
  const cacheLoad = document.getElementById('autoCmtCacheLoadBtn');
  if (cacheLoad) cacheLoad.addEventListener('click', () => loadAutoCommentHandoff(''));
  const cacheFile = document.getElementById('autoCmtCacheFile');
  if (cacheFile) cacheFile.addEventListener('change', importAutoCommentFile);
  const missionCmtFile = document.getElementById('missionCmtImportFile');
  if (missionCmtFile) missionCmtFile.addEventListener('change', importMissionAutoCommentFile);
  const cacheExport = document.getElementById('cmtExportTargetsBtn');
  if (cacheExport) cacheExport.addEventListener('click', exportAutoCommentHandoff);
  const addTemplate = document.getElementById('addCmtTemplateBtn');
  if (addTemplate) addTemplate.addEventListener('click', () =>
    renderCommentTemplates([...commentTemplateValues(true), '']));
  if (document.getElementById('cmtTemplateList')) renderCommentTemplates(['']);
  const bioToggle = document.getElementById('cmtBioToggle');
  if (bioToggle) bioToggle.addEventListener('click', () => {
    const fields = document.getElementById('cmtBioFields');
    const expanded = bioToggle.getAttribute('aria-expanded') !== 'true';
    bioToggle.setAttribute('aria-expanded', String(expanded));
    bioToggle.textContent = expanded ? 'Ẩn dữ liệu bio' : 'Thêm dữ liệu bio';
    if (fields) fields.classList.toggle('hidden', !expanded);
  });
  const bindChip = (id, mode) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', () => cmtSelectFilter(mode));
  };
  bindChip('cmtSelAll', 'all');
  bindChip('cmtSelLive', 'live');
  bindChip('cmtSelDie', 'die');
  bindChip('cmtSelUnknown', 'unknown');
  bindChip('cmtSelNone', 'none');
  if (delDie) delDie.addEventListener('click', doDeleteDead);
  const proxyCheck = document.getElementById('proxyCheckBtn');
  if (proxyCheck) proxyCheck.addEventListener('click', testAllProxies);
  const proxyAdd = document.getElementById('proxyAddBtn');
  if (proxyAdd) proxyAdd.addEventListener('click', addManualProxies);
  const proxyRefresh = document.getElementById('proxyRefreshBtn');
  if (proxyRefresh) proxyRefresh.addEventListener('click', loadProxysachStatus);
  const proxyDelDead = document.getElementById('proxyDelDeadBtn');
  if (proxyDelDead) proxyDelDead.addEventListener('click', doDeleteDeadProxies);
  const proxyDelAll = document.getElementById('proxyDelAllBtn');
  if (proxyDelAll) proxyDelAll.addEventListener('click', doDeleteAllProxies);
  // Enter trong ô key = bấm Kích hoạt.
  document.getElementById('keyInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doLogin();
  });
  // Debug luôn hiện browser: bật Debug → tắt + khóa "Chạy nền".
  const dbg = document.getElementById('debugInput');
  const hl = document.getElementById('headlessInput');
  const threads = document.getElementById('threadsInput');
  dbg.addEventListener('change', () => {
    if (dbg.checked) {
      hl.checked = false; hl.disabled = true;
      threads.value = 1; threads.disabled = true;   // Debug ép 1 luồng
    } else {
      hl.disabled = false;
      threads.disabled = false;
    }
  });
  const cmtDbg = document.getElementById('cmtDebugInput');
  const cmtHl = document.getElementById('cmtHeadlessInput');
  const cmtThreads = document.getElementById('cmtThreadsInput');
  cmtDbg.addEventListener('change', () => {
    if (cmtDbg.checked) {
      cmtHl.checked = false; cmtHl.disabled = true;
      cmtThreads.value = 1; cmtThreads.disabled = true;
    } else {
      cmtHl.disabled = false;
      cmtThreads.disabled = false;
    }
  });
  // Loại tim = "video" → ẩn ô nhập @user + chế độ tim (chỉ dùng cho comment).
  const likeType = document.getElementById('likeTypeInput');
  const targetBlock = document.getElementById('targetBlock');
  const findModeBlock = document.getElementById('findModeBlock');
  const apiLikeBlock = document.getElementById('apiLikeBlock');
  likeType.addEventListener('change', () => {
    const isVideoOnly = likeType.value === 'video';
    targetBlock.style.display = isVideoOnly ? 'none' : '';
    findModeBlock.style.display = isVideoOnly ? 'none' : '';
    if (apiLikeBlock) apiLikeBlock.style.display = isVideoOnly ? 'none' : '';
  });
  const logSearch = document.getElementById('logSearch');
  if (logSearch) logSearch.addEventListener('input', applyLogSearch);
  const accSearch = document.getElementById('accSearch');
  if (accSearch) accSearch.addEventListener('input', applyAccSearch);
  document.querySelectorAll('[data-result-filter]').forEach(button => {
    button.addEventListener('click', () => {
      resultFilter = button.dataset.resultFilter || 'all';
      document.querySelectorAll('[data-result-filter]').forEach(candidate =>
        candidate.classList.toggle('active', candidate === button));
      applyResultFilter();
    });
  });
  const resultFollow = document.getElementById('resultFollowBtn');
  if (resultFollow) resultFollow.addEventListener('click', resumeResultFollow);
  const resultArea = document.getElementById('resultArea');
  if (resultArea) resultArea.addEventListener('scroll', () => {
    const distance = resultArea.scrollHeight
      - resultArea.scrollTop - resultArea.clientHeight;
    if (distance <= 24) {
      resultAutoFollow = true;
      resultUnseen = 0;
    } else if (resultAutoFollow) {
      resultAutoFollow = false;
    }
    updateResultFollowState();
  }, { passive: true });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bindEvents);
} else {
  bindEvents();
}

async function init() {
  try {
    await loadMachineProxyAllocation();
    await loadPoolCaptchaSettings();
    let st = {};
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.get_status) {
        st = await pywebview.api.get_status();
      }
    } catch (e) {}
    document.getElementById('statusLine').textContent =
      `Phiên bản 94 · VIP`;
    const vb = document.getElementById('verBadge');
    if (vb) vb.textContent = `v95`;
    applyEdition(st);
    if (st && st.thread_hint) applyThreadHint(st.thread_hint);
    showLicenseEntry();
    const restored = await pywebview.api.resume_saved_activation();
    if (restored && restored.ok === true) {
      showMain(restored.pool_summary || st.pool_summary || {});
      await loadAccList();
      startPolling();
      pollSyncBadge();
    } else if (restored && !restored.needs_key) {
      const detail = String(restored.error || restored.message || '');
      document.getElementById('loginError').textContent = /1033|cloudflare tunnel/i.test(detail)
        ? 'Đã lưu key trên máy, nhưng server kích hoạt đang mất kết nối (1033). Không cần nhập lại key; thử lại khi server hoạt động.'
        : detail || 'Không khôi phục được phiên kích hoạt.';
    }
  } catch (e) {
    console.error('init note:', e);
    showLicenseEntry();
  }
}

function applyEdition(st) {
  const vip = true;
  const eb = document.getElementById('editionBadge');
  if (eb) eb.textContent = 'VIP';
  ['tabCommentBtn', 'tabCmtAccBtn'].forEach(id => {
    const tab = document.getElementById(id);
    if (tab) tab.classList.remove('hidden');
  });
  const proxyTab = document.getElementById('tabProxyBtn');
  if (proxyTab) proxyTab.classList.remove('hidden');
  const captureButton = document.getElementById('apiCaptureBtn');
  if (captureButton) captureButton.classList.remove('hidden');
  const importPanel = document.getElementById('importPanel');
  if (importPanel) importPanel.classList.remove('hidden');
}

function proxyDateLabel(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('vi-VN', { hour12: false });
}

const PROXY_TEST_LABELS = {
  untested: 'Chưa test',
  testing: 'Đang test',
  live: 'LIVE',
  blocked: 'TikTok 403',
  dead: 'DIE',
};

function renderProxysachStatus(state) {
  const enabled = Boolean(state.enabled);
  const managedRows = Array.isArray(state.details) ? state.details : [];
  const manualRows = Array.isArray(state.manual_details) ? state.manual_details : [];
  const managed = Number(state.active_proxies ?? managedRows.length ?? 0);
  const manual = Number(state.manual_proxies ?? manualRows.length ?? 0);
  const total = Number(state.total_proxies ?? (managed + manual));
  const configured = Number(state.configured_keys || 0);
  const leased = Number(state.leased_count || 0);
  const available = Number(state.available_count ?? Math.max(0, configured - leased));
  document.getElementById('proxyActiveCount').textContent = String(total);
  document.getElementById('proxyConfiguredCount').textContent =
    enabled ? `${available}/${configured}` : '0';
  document.getElementById('proxyManualCount').textContent = String(manual);
  document.getElementById('proxyNextChange').textContent =
    enabled ? 'Sau mỗi batch 100 acc' : 'Chưa bật';

  const badge = document.getElementById('proxyVipState');
  badge.textContent = enabled
    ? `${available}/${configured} slot trống · ${leased} đang lease`
    : 'ProxySach đang tắt';
  badge.classList.toggle('proxy-ready', Boolean(enabled && available));
  badge.classList.toggle('proxy-offline', !enabled);

  const rotation = document.querySelector('.proxy-rotation');
  rotation.classList.toggle('proxy-disabled', !enabled);
  document.getElementById('proxyRotationEyebrow').textContent =
    enabled ? 'Batch lease' : 'Cần bật trên server';
  document.getElementById('proxyRotationTitle').textContent =
    enabled ? 'Mỗi client giữ IP riêng' : 'ProxySach đang tắt';
  document.getElementById('proxyRotationCopy').textContent = enabled
    ? 'Server cấp theo số proxy đã lưu cho máy này. Ba scout quét đủ comment một lượt, gộp CID rồi account sau chỉ gửi API; đủ 100 account mới trả lease và xoay batch kế tiếp.'
    : 'Proxy tự thêm vẫn dùng bình thường. Muốn dùng ProxySach, vào Admin → Pool → Proxy rồi bật dịch vụ.';

  const list = document.getElementById('proxyEndpointList');
  list.replaceChildren();
  const appendRow = (proxy, source, note) => {
    const row = document.createElement('article');
    row.className = 'proxy-endpoint';
    const slot = document.createElement('span');
    slot.className = 'proxy-slot';
    slot.textContent = source;
    const address = document.createElement('strong');
    address.className = 'mono';
    address.textContent = String(proxy.address || 'Đang cập nhật');
    const detail = document.createElement('small');
    detail.textContent = note;
    const test = proxy.test_status || {};
    const testState = Object.hasOwn(PROXY_TEST_LABELS, test.state)
      ? test.state
      : 'untested';
    const testBadge = document.createElement('span');
    testBadge.className = `proxy-test-state state-${testState}`;
    testBadge.textContent = PROXY_TEST_LABELS[testState];
    testBadge.title = String(test.note || PROXY_TEST_LABELS[testState]);

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'chip chip-danger proxy-del-btn';
    delBtn.style.padding = '2px 8px';
    delBtn.style.fontSize = '12px';
    delBtn.style.marginLeft = 'auto';
    delBtn.textContent = 'Xóa';
    delBtn.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Xóa proxy ${proxy.address}?`)) return;
      await doDeleteSingleProxy(proxy.id, proxy.address);
    };

    row.append(slot, address, detail, testBadge, delBtn);
    list.append(row);
  };
  managedRows.forEach((proxy) => appendRow(
    proxy,
    `ProxySach ${String(proxy.slot || 0).padStart(2, '0')}`,
    `IP ra: ${proxy.public_ipv4 || '—'}`,
  ));
  manualRows.forEach((proxy) => appendRow(
    proxy,
    'Pool chung',
    String(proxy.proxy_type || 'HTTP').toUpperCase(),
  ));
  if (!managedRows.length && !manualRows.length) {
    const empty = document.createElement('div');
    empty.className = 'proxy-empty';
    const title = document.createElement('strong');
    title.textContent = enabled
      ? 'Chưa có batch đang giữ proxy'
      : 'Chưa có proxy để chạy';
    const copy = document.createElement('span');
    copy.textContent = enabled
      ? `${available}/${configured} ProxySach đang chờ cấp riêng khi bot bắt đầu batch.`
      : 'Thêm proxy vào pool chung bên dưới, hoặc bật ProxySach trên dashboard Admin.';
    empty.append(title, copy);
    list.append(empty);
  }

  document.getElementById('proxyActionStatus').textContent = !enabled
    ? (manual
      ? `${manual} proxy tự thêm vẫn đang hoạt động.`
      : 'ProxySach đang tắt; proxy tự thêm vẫn có thể sử dụng.')
    : (state.last_error ||
      `${leased} slot đang được client sử dụng; ${available} slot còn trống.`);
}

async function loadProxysachStatus() {
  if (!window.pywebview?.api?.get_proxysach_status && renderLocalProxyView()) return;
  const status = document.getElementById('proxyActionStatus');
  if (status) status.textContent = 'Đang lấy trạng thái mới nhất...';
  try {
    let result = null;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.get_proxysach_status) {
        result = await pywebview.api.get_proxysach_status();
      }
    } catch (err) {}
    if (!result || !result.ok) {
      const resp = await fetch('http://127.0.0.1:19802/api/client/proxysach');
      result = await resp.json();
    }
    if (!result || !result.ok) throw new Error(result?.error || 'Không tải được proxy');
    if (!Array.isArray(result.manual_details) || (!result.manual_details.length && Number(result.manual_proxies || result.total_proxies || 0) > 0)) {
      try {
        const response = await fetch('http://127.0.0.1:19802/api/client/fetch-pool', {method: 'POST'});
        const pool = await response.json();
        const rows = pool.proxies || pool.pool?.proxies;
        if (Array.isArray(rows)) result = {...result, manual_details: rows, manual_proxies: rows.length,
          total_proxies: rows.length + (result.details?.length || 0)};
      } catch (_) {}
    }
    renderProxysachStatus(result);
    updatePoolLine({proxies: Number(result.total_proxies || 0)});
    if (window.__LOCAL_POOL_VIEW__) window.__LOCAL_POOL_VIEW__.proxies = result.manual_details || [];
  } catch (error) {
    if (status) status.textContent = `Không thể tải proxy: ${error}`;
  }
}

async function testAllProxies() {
  const button = document.getElementById('proxyCheckBtn');
  const status = document.getElementById('proxyTestStatus');
  if (button) {
    button.disabled = true;
    button.textContent = 'Đang test...';
  }
  if (status) status.textContent = 'Đang chuẩn bị test proxy trong pool chung...';
  try {
    let started = null;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.check_proxies) {
        started = await pywebview.api.check_proxies();
      }
    } catch (e) {}
    if (!started || !started.ok) {
      const resp = await fetch('http://127.0.0.1:19802/api/client/test-proxies', {method: 'POST'});
      started = await resp.json();
    }
    if (!started || !started.ok) throw new Error((started && started.error) || 'Không test được proxy.');
    for (let attempt = 0; attempt < 180; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      let state = null;
      try {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.get_proxy_test_status) {
          state = await pywebview.api.get_proxy_test_status();
        }
      } catch (e) {}
      if (!state || !state.ok) {
        const resp2 = await fetch('http://127.0.0.1:19802/api/client/get-proxy-test-status');
        state = await resp2.json();
      }
      if (!state || !state.ok) throw new Error((state && state.error) || 'Không đọc được trạng thái test.');
      renderProxysachStatus(state);
      const summary = state.test_summary || {};
      const testing = Number(summary.testing || 0);
      const live = Number(summary.live || 0);
      const blocked = Number(summary.blocked || 0);
      const dead = Number(summary.dead || 0);
      if (status) {
        status.textContent = testing
          ? `Đang test ${testing}/${started.count || 0} proxy · Live ${live} · 403 ${blocked} · Die ${dead}`
          : `Đã test ${started.count || 0} proxy · Live ${live} · TikTok 403 ${blocked} · Die ${dead}`;
      }
      if (!testing && !state.test_running) return;
    }
    if (status) status.textContent = 'Kiểm tra vẫn đang chạy; bấm Làm mới để xem kết quả.';
  } catch (error) {
    if (status) status.textContent = `Không thể test proxy: ${error}`;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = 'Test toàn bộ proxy';
    }
  }
}

async function addManualProxies() {
  const input = document.getElementById('proxyAddInput');
  const button = document.getElementById('proxyAddBtn');
  const status = document.getElementById('proxyTestStatus');
  const lines = input.value.trim();
  if (!lines) {
    if (status) status.textContent = 'Dán ít nhất một proxy, mỗi dòng một proxy.';
    input.focus();
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = 'Đang thêm...';
  }
  if (status) status.textContent = 'Đang thêm proxy vào pool chung.';
  try {
    let result = null;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.add_manual_proxies) {
        result = await pywebview.api.add_manual_proxies(lines);
      }
    } catch (err) {}
    if (!result || !result.ok) {
      const resp = await fetch('http://127.0.0.1:19802/api/client/proxies/bulk', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lines: lines})
      });
      result = await resp.json();
    }
    if (!result || !result.ok) throw new Error((result && result.error) || 'Không thêm được proxy.');
    input.value = '';
    await loadProxysachStatus();
    if (status) status.textContent = `Đã thêm ${result.added || 0} proxy vào pool chung.`;
  } catch (error) {
    if (status) status.textContent = `Không thể thêm proxy: ${error}`;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = 'Thêm vào pool';
    }
  }
}

async function doDeleteSingleProxy(id, address) {
  try {
    const res = await pywebview.api.local_delete_proxy(id, null);
    if (!res?.ok) throw new Error(res?.error || 'Không xóa được proxy');
    if (res && res.ok) {
      await loadProxysachStatus();
      const status = document.getElementById('proxyTestStatus');
      if (status) status.textContent = `Đã xóa proxy ${address}.`;
    }
  } catch (e) {
    alert('Lỗi xóa proxy: ' + e);
  }
}

async function doDeleteDeadProxies() {
  if (!confirm('Xóa vĩnh viễn proxy DIE khỏi pool? Proxy TikTok 403 giữ lại để có thể test lại.')) return;
  try {
    const res = await pywebview.api.local_delete_proxy(null, 'dead');
    if (!res?.ok) throw new Error(res?.error || 'Không xóa được proxy');
    if (res && res.ok) {
      await loadProxysachStatus();
      const status = document.getElementById('proxyTestStatus');
      if (status) status.textContent = `Đã xóa ${res.deleted || 0} proxy lỗi/chết.`;
    }
  } catch (e) {
    alert('Lỗi: ' + e);
  }
}

async function doDeleteAllProxies() {
  if (!confirm('CẢNH BÁO: Xóa TOÀN BỘ proxy trong pool?')) return;
  try {
    const res = await pywebview.api.local_delete_proxy(null, 'all');
    if (!res?.ok) throw new Error(res?.error || 'Không xóa được proxy');
    if (res && res.ok) {
      await loadProxysachStatus();
      const status = document.getElementById('proxyTestStatus');
      if (status) status.textContent = 'Đã xóa toàn bộ proxy trong pool.';
    }
  } catch (e) {
    alert('Lỗi: ' + e);
  }
}


// Sync badge: poll log sender state (đã đồng bộ / đang chờ).
function pollSyncBadge() {
  setInterval(async () => {
    try {
      const s = await pywebview.api.sync_state();
      const b = document.getElementById('syncBadge');
      if (!b) return;
      if (s.synced) { b.textContent = 'đã đồng bộ'; b.classList.add('ok'); }
      else { b.textContent = 'đang chờ'; b.classList.remove('ok'); }
    } catch (e) {}
  }, 1500);
}

// ── Login ──────────────────────────────────────────────────────────────
function showLicenseEntry() {
  stopPolling();
  const login = document.getElementById('loginView');
  login.classList.remove('hidden');
  login.style.display = 'grid';
  login.style.visibility = 'visible';
  const main = document.getElementById('mainView');
  main.classList.add('hidden');
  main.style.display = 'none';
  document.getElementById('keyInput').focus();
}
async function doLogin() {
  const button = document.getElementById('loginBtn');
  const error = document.getElementById('loginError');
  const key = document.getElementById('keyInput').value.trim();
  error.textContent = '';
  if (!key) { error.textContent = 'Nhập license key để kích hoạt máy này.'; return; }
  if (!window.pywebview?.api?.login) {
error.textContent = 'Ứng dụng chưa sẵn sàng. Thử lại sau vài giây.'; return;
  }
  button.disabled = true;
  button.textContent = 'Đang kích hoạt…';
  try {
const result = await pywebview.api.login(key);
if (!result || result.ok !== true) {
  const detail = String(result?.error || result?.message || '');
  error.textContent = /1033|cloudflare tunnel|status[^0-9]*530/i.test(detail)
    ? 'Máy chủ kích hoạt mất kết nối Cloudflare (1033). Chưa kiểm tra được key. Cần khôi phục server rồi thử lại.'
    : detail || 'Kích hoạt không thành công. Kiểm tra key và kết nối mạng.';
  return;
}
if (result.remember_error) alert(result.remember_error);
document.getElementById('keyInput').value = '';
showMain(result.pool_summary || {});
await loadAccList();
startPolling();
  } catch (exception) {
error.textContent = 'Không thể kích hoạt: ' + (exception.message || String(exception));
  } finally {
button.disabled = false;
button.textContent = 'Kích hoạt';
  }
}

// Danh sách tài khoản pool, chọn để check live.
let accListCache = [];
async function loadAccList() {
  if (await refreshCheckedAccounts()) return;
  if (renderLocalPoolView()) return;
  try {
    let r = null;
    try {
      const resp = await fetch('http://127.0.0.1:19802/api/client/fetch-pool', {method: 'POST'});
      r = await resp.json();
    } catch (e) {}

    if (!r || !Array.isArray(r.accounts)) {
      try {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.get_accounts) {
          r = await pywebview.api.get_accounts();
        }
      } catch (err) {}
    }

    if (!r || !Array.isArray(r.accounts)) throw new Error('Không tải được pool đã lưu.');
    accListCache = r.accounts;
    renderAccList();
    renderAccStats();
    updatePoolLine({accounts: accListCache.length, ...(Array.isArray(r.proxies) ? {proxies: r.proxies.length} : {})});

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.refresh_pool) {
        pywebview.api.refresh_pool().catch(() => {});
      }
    } catch (e) {}
  } catch (e) { /* ignore */ }
}

function renderAccStats() {
  const c = { live: 0, die: 0, banned: 0, unknown: 0 };
  for (const a of accListCache) c[a.status] = (c[a.status] || 0) + 1;
  document.getElementById('stTotal').textContent = accListCache.length;
  document.getElementById('stLive').textContent = c.live || 0;
  document.getElementById('stDie').textContent = c.die || 0;
  const bn = document.getElementById('stBanned');
  if (bn) bn.textContent = c.banned || 0;
  document.getElementById('stUnknown').textContent =
    accListCache.length - (c.live || 0) - (c.die || 0) - (c.banned || 0);
}

function applyAccPatch(a) {
  if (!a || a.id == null) return;
  const saved = window.__LOCAL_POOL_VIEW__?.accounts?.find(x => String(x.id) === String(a.id));
  if (saved) { if (a.status) saved.status = a.status; if (a.username) saved.username = a.username; }
  const aId = String(a.id);
  const i = accListCache.findIndex(x => String(x.id) === aId);
  if (i >= 0) {
    if (a.username) accListCache[i].username = a.username;
    if (a.status) accListCache[i].status = a.status;
  }
  const row = document.querySelector(`#accListBody .acc-chk[data-id='${aId}']`);
  if (!row) {
    renderAccList();
    renderAccStats();
    return;
  }
  const wrap = row.closest('.acc-row');
  if (!wrap) return;
  const name = wrap.querySelector('.acc-name');
  const st = wrap.querySelector('.acc-st');
  const uname = (i >= 0 ? accListCache[i].username : a.username) || '';
  if (name) name.textContent = uname
    ? '@' + String(uname).replace(/^@+/, '') : ('acc' + aId);
  if (st) {
    const s = (i >= 0 ? accListCache[i].status : a.status) || 'unknown';
    st.className = 'acc-st ' + s;
    st.textContent = s === 'banned' ? 'BANNED' : s;
  }
  renderAccStats();
}

function accSearchQuery() {
  return (document.getElementById('accSearch')?.value || '').trim().toLowerCase();
}

function accMatchesSearch(a, q) {
  if (!q) return true;
  const uname = (a.username || '').replace(/^@+/, '').toLowerCase();
  const id = String(a.id || '');
  const st = (a.status || '').toLowerCase();
  return uname.includes(q) || ('@' + uname).includes(q)
    || id.includes(q) || ('acc' + id).includes(q) || st.includes(q);
}

function applyAccSearch() {
  const q = accSearchQuery();
  let vis = 0;
  const byId=new Map(accListCache.map(a=>[String(a.id),a]));
  document.querySelectorAll('#accListBody .acc-row').forEach(row => {
    const id = parseInt(row.querySelector('.acc-chk')?.dataset.id);
    const a = byId.get(String(id));
    const show = !a || accMatchesSearch(a, q);
    row.style.display = show ? '' : 'none';
    if (show) vis++;
  });
  const el = document.getElementById('accListCount');
  if (el) el.textContent = q ? vis + '/' + accListCache.length : accListCache.length;
}

function renderAccList() {
  const body = document.getElementById('accListBody');
  const checkedIds = new Set(selectedAccIds());
  body.innerHTML = accListCache.length ? accListCache.map(a => `
    <div class="acc-row" onclick="toggleAccRow(event, ${a.id})">
      <input type="checkbox" class="acc-chk" data-id="${a.id}"${checkedIds.has(a.id) ? ' checked' : ''}>
      <span class="acc-name">${escapeHtml(a.username ? '@' + a.username.replace(/^@+/, '') : 'acc' + a.id)}</span>
      <span class="acc-st ${a.status}">${a.status === 'banned' ? 'BANNED' : a.status}</span>
    </div>`).join('') : '<div class="empty-state">Chưa có tài khoản trong pool.</div>';
  applyAccSearch();
  updateSelectedCount();
}

window.accSelectFilter = (mode) => {
  document.querySelectorAll('#accListBody .acc-chk').forEach(cb => {
    const row = cb.closest('.acc-row');
    if (row && row.style.display === 'none') { cb.checked = false; return; }
    const a = accListCache.find(x => x.id === parseInt(cb.dataset.id));
    if (!a) return;
    cb.checked = mode === 'all'
      || (mode === 'live' && a.status === 'live')
      || (mode === 'die' && a.status === 'die')
      || (mode === 'unknown'
        && !['live', 'die', 'banned'].includes(a.status))
      || (mode === 'banned' && a.status === 'banned');
  });
  updateSelectedCount();
};

// Đếm số acc đang tick, hiện cạnh tiêu đề để biết trước khi bấm Check.
function updateSelectedCount() {
  const el = document.getElementById('selectedCount');
  if (el) el.textContent = selectedAccIds().length;
}

// Click cả hàng = toggle checkbox (click trực tiếp checkbox không toggle đôi).
window.toggleAccRow = (event, id) => {
  if (event.target.tagName === 'INPUT') { updateSelectedCount(); return; }
  const cb = document.querySelector(`#accListBody .acc-chk[data-id='${id}']`);
  if (cb) cb.checked = !cb.checked;
  updateSelectedCount();
};


function selectedAccIds() {
  return [...document.querySelectorAll('#accListBody .acc-chk:checked')]
    .map(cb => parseInt(cb.dataset.id));
}

async function doDeleteSelected() {
  const ids = selectedAccIds();
  if (!ids.length) { alert('Chưa chọn tài khoản nào để xóa.'); return; }
  if (!confirm(`Xóa ${ids.length} tài khoản đã chọn khỏi pool? Không hoàn tác.`)) return;
  try {
    const idSet = new Set(ids);
    accListCache = accListCache.filter(a => !idSet.has(a.id));
    renderAccList();
    renderAccStats();

    try {
      await fetch('http://127.0.0.1:19802/api/client/delete-accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: ids})
      });
    } catch (netErr) {}

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.refresh_pool) {
        await pywebview.api.refresh_pool();
      }
    } catch (e) {}

    await loadAccList();
  } catch (err) {
    alert('Lỗi xóa: ' + err);
  }
}

async function doDeleteBanned() {
  if (!confirm('Xóa mọi acc BANNED khỏi pool? Không hoàn tác.')) return;
  try {
    accListCache = accListCache.filter(a => a.status !== 'banned');
    renderAccList();
    renderAccStats();

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.delete_banned) {
        await pywebview.api.delete_banned();
      }
    } catch (e) {}

    try {
      await fetch('http://127.0.0.1:19802/api/client/delete-banned', {method: 'POST'});
    } catch (netErr) {}

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.refresh_pool) {
        await pywebview.api.refresh_pool();
      }
    } catch (e) {}

    await loadAccList();
  } catch (e) { alert('Lỗi: ' + e); }
}

async function doDeleteDead() {
  if (!confirm('Xóa mọi acc DIE khỏi pool? Banned giữ nguyên. Không hoàn tác.')) return;
  try {
    accListCache = accListCache.filter(a => a.status !== 'die');
    renderAccList();
    renderAccStats();

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.delete_dead) {
        await pywebview.api.delete_dead();
      }
    } catch (e) {}

    try {
      await fetch('http://127.0.0.1:19802/api/client/delete-dead', {method: 'POST'});
    } catch (netErr) {}

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.refresh_pool) {
        await pywebview.api.refresh_pool();
      }
    } catch (e) {}

    await loadAccList();
  } catch (e) { alert('Lỗi: ' + e); }
}

async function doDeleteUnknown() {
  if (!confirm('Xóa mọi acc UNKNOWN khỏi pool? Không hoàn tác.')) return;
  try {
    accListCache = accListCache.filter(a => a.status === 'live' || a.status === 'die' || a.status === 'banned');
    renderAccList();
    renderAccStats();

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.delete_unknown) {
        await pywebview.api.delete_unknown();
      }
    } catch (e) {}

    try {
      await fetch('http://127.0.0.1:19802/api/client/delete-unknown', {method: 'POST'});
    } catch (netErr) {}

    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.refresh_pool) {
        await pywebview.api.refresh_pool();
      }
    } catch (e) {}

    await loadAccList();
  } catch (error) {
    alert('Lỗi: ' + error);
  }
}
async function doImportAcc() {
  const st = document.getElementById('importAccStatus');
  const text = document.getElementById('importAccText').value.trim();
  if (!text) { st.textContent = 'Chưa nhập gì.'; return; }
  const btn = document.getElementById('importAccSend');
  btn.disabled = true; st.textContent = '⏳ Đang nạp...';
  try {
    const accTypeEl = document.getElementById('importAccType');
    const accountType = (accTypeEl && accTypeEl.value) ? accTypeEl.value : 'totp';
    let r = null;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.import_accounts) {
        r = await pywebview.api.import_accounts(text, accountType);
      }
    } catch (apiErr) {}
    if (!r || !r.ok) {
      try {
        const resp = await fetch('http://127.0.0.1:19802/api/client/import-accounts', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({lines: text, account_type: accountType})
        });
        r = await resp.json();
      } catch (netErr) {}
    }
    const count = (r && typeof r.added !== 'undefined') ? r.added : (r && r.count ? r.count : 0);
    st.textContent = (r && r.ok)
      ? `✅ Đã nạp ${count} tài khoản vào pool.`
      : `❌ ${(r && r.error) || 'Lỗi nạp tài khoản.'}`;
    if (r && r.ok) {
      document.getElementById('importAccText').value = '';
      loadAccList();
    }
  } catch (e) {
    st.textContent = 'Lỗi: ' + e;
  } finally {
    btn.disabled = false;
  }
}

// Check live toàn bộ acc pool trên máy khách; kết quả báo thẳng lên server.
// Check live: ưu tiên acc đang chọn trong tab Tài khoản; không chọn = tất cả.
async function doLiveCheck() {
  const btn = document.getElementById('liveCheckBtn');
  const ids = selectedAccIds();
  if (!ids.length && accListCache.length
      && !confirm(`Chưa chọn tài khoản nào. Check TOÀN BỘ (${accListCache.length} acc)?`)) return;
  const threads = parseInt(document.getElementById('checkThreadsInput').value) || 8;
  const headless = !document.getElementById('checkVisibleInput').checked;
  try {
    const r = await pywebview.api.start_live_check(ids.length ? ids : null, threads, headless);
    if (!r.ok) { alert(r.error || 'Không check được.'); return; }
    btn.disabled = true; btn.textContent = 'Đang check...';
    startLiveCheckPolling();
  } catch (e) { alert('Lỗi: ' + e); }
}

function startLiveCheckPolling() {
  let polling = false;
  const btn = document.getElementById('liveCheckBtn');
  const timer = setInterval(async () => {
    try {
      if (polling) return;
      polling = true;
      await refreshCheckedAccounts();
      const st = await pywebview.api.is_live_check_running();
      if (!st || !st.running) {
        clearInterval(timer);
        if (btn) { btn.disabled = false; btn.textContent = 'Check'; }
        loadAccList();
      }
    } catch (e) { /* retry on next poll */ }
    finally { polling = false; }
  }, 800);
}

// Pause/Resume/Stop check live.
async function doPauseLiveCheck() {
  const btn = document.getElementById('livePauseBtn');
  try {
    if (btn.dataset.state === 'paused') {
      await pywebview.api.resume_live_check();
      btn.textContent = 'Tạm dừng'; btn.dataset.state = '';
    } else {
      await pywebview.api.pause_live_check();
      btn.textContent = 'Tiếp tục'; btn.dataset.state = 'paused';
    }
  } catch (e) { alert('Lỗi: ' + e); }
}

async function doStopLiveCheck() {
  if (!confirm('DỪNG HẲN check live? Acc đang check bị đóng browser, acc chưa check bị bỏ qua (không đánh dấu DIE).')) return;
  try { await pywebview.api.stop_live_check(); } catch (e) { alert('Lỗi: ' + e); }
}

async function doRefreshPool() {
  const btn = document.getElementById('refreshPoolBtn');
  btn.disabled = true; btn.textContent = '↻ Đang cập nhật...';
  try {
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.refresh_pool) {
        const r = await pywebview.api.refresh_pool();
        if (r && r.ok) updatePoolLine(r.pool_summary);
      }
    } catch (e) {}
    await loadAccList();
  } catch (e) { /* log panel se hien loi */ }
  finally { btn.disabled = false; btn.textContent = '↻ Cập nhật pool'; }
}


async function doLogout() {
  try { await pywebview.api.logout(); }
  finally { showLicenseEntry(); }
}

async function doApiCapture() {
  const opts = {
    links: document.getElementById('linksInput').value.split('\n'),
    autocap: document.getElementById('autocapInput').checked,
    like_type: 'comment',
    headless: false,
    debug: true,
    api_capture: true,
  };
  const r = await pywebview.api.start_run(opts);
  if (!r.ok) {
    alert(r.error || 'Không thể mở listener API.');
    return;
  }
  setRunning(true);
}
async function loadAutoCommentHandoff(rawJson = '') {
  const status = document.getElementById('autoCmtCacheStatus');
  if (status) status.textContent = 'Đang nạp...';
  try {
    const r = await pywebview.api.import_auto_comment_handoff(rawJson);
    if (!r.ok) {
      if (status) status.textContent = r.error || 'Không nạp được cache.';
      return;
    }
    document.getElementById('linksInput').value = (r.links || []).join('\n');
    document.getElementById('targetInput').value = '';
    document.getElementById('likeTypeInput').value = 'comment';
    document.getElementById('likeTypeInput').dispatchEvent(new Event('change'));
    if (status) status.textContent = `Đã nạp ${r.count} câu từ Auto Cmt.`;
  } catch (e) {
    if (status) status.textContent = 'Lỗi: ' + e;
  }
}

async function importAutoCommentFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  try {
    await loadAutoCommentHandoff(await file.text());
  } finally {
    event.target.value = '';
  }
}

async function importMissionAutoCommentFile(event) {
  const file = event.target.files && event.target.files[0];
  const status = document.getElementById('missionCmtImportStatus');
  if (!file) return;
  try {
    const payload = JSON.parse(await file.text());
    if (payload.kind !== 'alc-mission-auto-cmt' || !Array.isArray(payload.targets)) {
      throw new Error('File không phải dữ liệu yêu cầu Auto Cmt.');
    }
    const targets = payload.targets.map((target) => {
      const link = String(target.video_url || target.link || '').trim();
      const codes = Array.isArray(target.codes)
        ? target.codes.map((code) => String(code || '').trim()).filter(Boolean)
        : String(target.codes || '').split(',').map((code) => code.trim()).filter(Boolean);
      return {
        link,
        codes,
        bios: Array.isArray(target.bios) ? target.bios : [],
      };
    }).filter((target) => target.link);
    if (!targets.length) throw new Error('File không có link video hợp lệ.');

    document.getElementById('cmtLinksInput').value =
      targets.map((target) => target.link).join('\n');
    document.getElementById('shopCodesInput').value =
      targets.map((target) => target.codes.join(', ')).join('\n');

    const bioUsers = [...new Set(targets.flatMap((target) =>
      target.bios.map((bio) => String(bio.username || '').replace(/^@+/, '').trim())
        .filter(Boolean)))];
    const bioToggle = document.getElementById('cmtBioToggle');
    const bioFields = document.getElementById('cmtBioFields');
    if (bioUsers.length === 1) {
      document.getElementById('cmtBioUsername').value = bioUsers[0];
      document.getElementById('shopNumbersInput').value = targets.map((target) =>
        target.bios.flatMap((bio) => Array.isArray(bio.numbers) ? bio.numbers : [])
          .join(', ')).join('\n');
      bioToggle.setAttribute('aria-expanded', 'true');
      bioToggle.textContent = 'Ẩn dữ liệu bio';
      bioFields.classList.remove('hidden');
    } else {
      document.getElementById('cmtBioUsername').value = '';
      document.getElementById('shopNumbersInput').value = '';
      bioToggle.setAttribute('aria-expanded', 'false');
      bioToggle.textContent = 'Thêm dữ liệu bio';
      bioFields.classList.add('hidden');
    }
    if (status) {
      status.textContent = bioUsers.length > 1
        ? `Đã import ${targets.length} video và mã. Có nhiều username bio, cần kiểm tra thủ công.`
        : `Đã import ${targets.length} video và mã, sẵn sàng chạy.`;
    }
  } catch (error) {
    if (status) status.textContent = 'Lỗi: ' + error.message;
  } finally {
    event.target.value = '';
  }
}

async function exportAutoCommentHandoff() {
  const status = document.getElementById('cmtExportTargetsStatus');
  if (status) status.textContent = 'Đang chuẩn bị...';
  try {
    const r = await pywebview.api.get_auto_comment_handoff();
    if (!r.ok) {
      if (status) status.textContent = r.error || 'Chưa có dữ liệu.';
      return;
    }
    const blob = new Blob([r.json], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = r.filename || 'auto_comment_targets.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (status) status.textContent = `${r.count} câu, đã lưu cache tại ${r.path}`;
  } catch (e) {
    if (status) status.textContent = 'Lỗi: ' + e;
  }
}

let startPending = false;
async function doStart() {
  if (startPending) return;
  const button = document.getElementById('startBtn');
  const label = button.textContent;
  startPending = true;
  button.disabled = true;
  button.textContent = 'Đang bắt đầu...';
  let started = false;
  try {
  if (!window.pywebview?.api?.start_run) {
    throw new Error('Ứng dụng chưa kết nối xong. Vui lòng đợi vài giây rồi bấm lại.');
  }
  if (!(await saveMachineProxyAllocation())) throw new Error('Chưa lưu được số proxy cho máy này.');
  await savePoolCaptchaSettings();
  const targetValue = document.getElementById('targetInput').value;
  const opts = {
    random_accounts: document.getElementById('randomAccountsInput').checked,
    account_ids: selectedAccIds(),
    links: document.getElementById('linksInput').value.split('\n'),
    target_text: targetValue,
    target_texts: targetValue.split('\n'),
    threads: Math.max(1, Math.min(30, parseInt(document.getElementById('threadsInput').value, 10) || 10)),
    target_count: parseInt(document.getElementById('targetCountInput').value) || 0,
    autocap: document.getElementById('autocapInput').checked,
    find_mode: document.getElementById('findModeInput').value,
    like_type: document.getElementById('likeTypeInput').value,
    api_like: document.getElementById('apiLikeInput').checked,
    headless: document.getElementById('headlessInput').checked,
    debug: document.getElementById('debugInput').checked,
  };
  const r = await pywebview.api.start_run(opts);
  if (!r || !r.ok) {
    alert(r?.error || 'Không thể bắt đầu.');
    return;
  }
  logCursor = 0;
  started = true;
  setRunning(true);
  document.getElementById('progressBox').classList.remove('hidden');
  } catch (error) {
    alert('Không thể bắt đầu: ' + (error.message || error));
  } finally {
    startPending = false;
    button.textContent = label;
    button.disabled = started;
  }
}

async function loadCmtAccList() {
  const box = document.getElementById('cmtAccList');
  if (!box) return;
  try {
    const r = await pywebview.api.get_comment_accounts();
    const accs = (r && r.accounts) || [];
    const checked = new Set([...document.querySelectorAll('.cmtAccCb:checked')].map(x => x.value));
    const live = accs.filter(a => a.status === 'live').length;
    const die = accs.filter(a => a.status === 'die').length;
    const unk = accs.length - live - die;
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    set('cmtStTotal', accs.length);
    set('cmtStLive', live);
    set('cmtStDie', die);
    set('cmtStUnknown', unk);
    set('cmtListCount', accs.length);
    box.innerHTML = accs.map(a => {
      const name = a.username ? '@' + String(a.username).replace(/^@+/, '') : (a.login_user || ('acc' + a.id));
      const st = a.status || 'unknown';
      const on = checked.has(String(a.id)) ? ' checked' : '';
      return `<div class="acc-row" onclick="toggleCmtRow(event, ${a.id})">
        <input type="checkbox" class="cmtAccCb acc-chk" value="${a.id}" data-id="${a.id}"${on}>
        <span class="acc-name">${escapeHtml(name)}</span>
        <span class="acc-st ${st}">${st === 'banned' ? 'BANNED' : st}</span>
      </div>`;
    }).join('') || '<div class="empty-state">Chưa có account comment.</div>';
    updateCmtSelected();
    const st = document.getElementById('cmtAccStatus');
    if (st) st.textContent = accs.length + ' acc local';
  } catch (e) {
    box.textContent = String(e);
  }
}

function commentTemplateValues(includeEmpty = false) {
  return [...document.querySelectorAll('#cmtTemplateList .cmt-template')].map(
    (textarea) => textarea.value.trim()).filter(
    (value) => includeEmpty || Boolean(value));
}

function syncTemplateRows() {
  const rows = [...document.querySelectorAll('#cmtTemplateList .template-row')];
  rows.forEach((row, index) => {
    const label = row.querySelector('.template-index');
    const remove = row.querySelector('.template-remove');
    const textarea = row.querySelector('.cmt-template');
    if (label) label.textContent = `Mẫu ${String(index + 1).padStart(2, '0')}`;
    if (remove) remove.disabled = rows.length === 1;
    if (textarea) textarea.setAttribute('aria-label', `Mẫu đăng ${index + 1}`);
  });
}

function renderCommentTemplates(templates) {
  const list = document.getElementById('cmtTemplateList');
  if (!list) return;
  const values = Array.isArray(templates) && templates.length ? templates : [''];
  list.replaceChildren();
  values.forEach((value) => {
    const row = document.createElement('article');
    row.className = 'template-row';
    const head = document.createElement('div');
    head.className = 'template-row-head';
    const label = document.createElement('span');
    label.className = 'template-index';
    const remove = document.createElement('button');
    remove.className = 'chip template-remove';
    remove.type = 'button';
    remove.textContent = 'Xóa';
    const textarea = document.createElement('textarea');
    textarea.className = 'input cmt-template';
    textarea.rows = 3;
    textarea.placeholder = 'Nhập trọn một comment. Hỗ trợ {code}, {number}, {username}.';
    textarea.value = String(value || '');
    remove.addEventListener('click', () => {
      row.remove();
      if (!list.children.length) renderCommentTemplates(['']);
      syncTemplateRows();
    });
    head.append(label, remove);
    row.append(head, textarea);
    list.append(row);
  });
  syncTemplateRows();
}

async function loadCmtConfig() {
  try {
    const r = await pywebview.api.get_comment_config();
    if (!r || !r.ok) return;
    const fp = document.getElementById('cmtExtraPhrases');
    if (fp && !(fp.value || '').trim()) {
      fp.value = (r.find_phrases || []).join('\n');
    }
    if (!commentTemplateValues().length) {
      renderCommentTemplates(r.templates || []);
    }
  } catch (e) {}
}
async function doSaveCmtCfg() {
  const fp = (document.getElementById('cmtExtraPhrases') || {}).value || '';
  const templates = commentTemplateValues();
  const r = await pywebview.api.save_comment_config(fp, templates);
  if (!r.ok) { alert(r.error || 'Lưu fail'); return; }
  renderCommentTemplates(r.templates || templates);
  const st = document.getElementById('cmtAccStatus');
  if (st) st.textContent = 'Đã lưu cụm + mẫu.';
}
window.toggleCmtRow = (event, id) => {
  if (event.target.tagName === 'INPUT') { updateCmtSelected(); return; }
  const cb = document.querySelector(`#cmtAccList .cmtAccCb[value='${id}']`);
  if (cb) cb.checked = !cb.checked;
  updateCmtSelected();
};
function updateCmtSelected() {
  const el = document.getElementById('cmtSelectedCount');
  if (el) el.textContent = document.querySelectorAll('.cmtAccCb:checked').length;
}
function cmtSelectFilter(mode) {
  document.querySelectorAll('#cmtAccList .cmtAccCb').forEach(cb => {
    const st = (cb.closest('.acc-row')?.querySelector('.acc-st')?.textContent || '').toLowerCase();
    cb.checked = mode === 'all'
      || (mode === 'live' && st === 'live')
      || (mode === 'die' && st === 'die')
      || (mode === 'unknown'
        && !['live', 'die', 'banned'].includes(st))
      || mode === 'none' && false;
  });
  updateCmtSelected();
}

async function doCmtImport() {
  const ta = document.getElementById('cmtImportText');
  const accountType = document.getElementById('cmtImportType').value;
  const r = await pywebview.api.import_comment_accounts(
    ta.value, accountType);
  if (!r.ok) { alert(r.error || 'Nạp fail'); return; }
  ta.value = '';
  loadCmtAccList();
}

async function doCmtDelete() {
  const ids = [...document.querySelectorAll('.cmtAccCb:checked')].map(x => parseInt(x.value, 10));
  if (!ids.length) { alert('Chọn acc cần xóa.'); return; }
  await pywebview.api.delete_comment_accounts(ids);
  loadCmtAccList();
}

async function doCmtStart() {
  const bioEnabled =
    document.getElementById('cmtBioToggle').getAttribute('aria-expanded') === 'true';
  const ids = [...document.querySelectorAll('.cmtAccCb:checked')].map(x => parseInt(x.value, 10));
  const opts = {
    links: document.getElementById('cmtLinksInput').value.split('\n'),
    shop_codes: document.getElementById('shopCodesInput').value.split('\n'),
    shop_numbers: bioEnabled
      ? document.getElementById('shopNumbersInput').value.split('\n') : [],
    bio_username: bioEnabled
      ? document.getElementById('cmtBioUsername').value.trim() : '',
    bio_enabled: bioEnabled,
    replies_per_root_min: parseInt(document.getElementById('repliesPerRootMin').value) || 3,
    replies_per_root_max: parseInt(document.getElementById('repliesPerRootMax').value) || 4,
    threads: parseInt(document.getElementById('cmtThreadsInput').value) || 1,
    headless: document.getElementById('cmtHeadlessInput').checked,
    debug: document.getElementById('cmtDebugInput').checked,
    autocap: document.getElementById('autocapInput') ? document.getElementById('autocapInput').checked : true,
    extra_phrases: (document.getElementById('cmtExtraPhrases') || {}).value || '',
    templates: commentTemplateValues(),
    account_ids: ids,
  };
  const r = await pywebview.api.start_comment_run(opts);
  if (!r.ok) { alert(r.error || 'Không chạy comment được.'); return; }
  setRunning(true);
}
async function doCmtProfileAction(action) {
  const ids = [...document.querySelectorAll('.cmtAccCb:checked')]
    .map(x => parseInt(x.value, 10));
  if (!ids.length) { alert('Chọn acc CMT cần xử lý.'); return; }
  let avatarPath = '';
  if (action === 'private' &&
      !confirm(`Bật tài khoản riêng tư cho ${ids.length} acc đã chọn?`)) {
    return;
  }
  const debug = action === 'avatar'
    && Boolean((document.getElementById('cmtAvatarDebug') || {}).checked);
  // Auto avatar luôn dùng Chrome hiện như luồng Debug đã xác nhận ổn định.
  // Checkbox này chỉ còn điều khiển thao tác Khóa acc.
  const headless = action === 'avatar'
    ? false : !(document.getElementById('cmtProfileVisible') || {}).checked;
  const r = await pywebview.api.start_comment_account_action(
    ids, action, avatarPath, headless, debug);
  if (!r.ok) { alert(r.error || 'Không bắt đầu được.'); return; }
  const buttons = ['cmtPrivateBtn', 'cmtAvatarBtn']
    .map(id => document.getElementById(id)).filter(Boolean);
  const stopButton = document.getElementById('cmtProfileStopBtn');
  buttons.forEach(b => { b.disabled = true; });
  if (stopButton) stopButton.disabled = false;
  const timer = setInterval(async () => {
    try {
      const p = await pywebview.api.get_progress();
      if (!p.profile_action_running) {
        clearInterval(timer);
        buttons.forEach(b => { b.disabled = false; });
        if (stopButton) stopButton.disabled = true;
        loadCmtAccList();
      }
    } catch (e) {
      clearInterval(timer);
      buttons.forEach(b => { b.disabled = false; });
      if (stopButton) stopButton.disabled = true;
    }
  }, 800);
}

async function doCmtProfileStop() {
  const button = document.getElementById('cmtProfileStopBtn');
  const actionButtons = ['cmtPrivateBtn', 'cmtAvatarBtn']
    .map(id => document.getElementById(id)).filter(Boolean);
  if (button) button.disabled = true;
  actionButtons.forEach(b => { b.disabled = false; });
  try {
    await pywebview.api.stop_comment_account_action();
  } catch (e) {
    if (button) button.disabled = false;
    alert('Lỗi dừng thao tác hồ sơ: ' + e);
  }
}



async function doCmtLiveCheck() {
  const btn = document.getElementById('cmtLiveBtn');
  const ids = [...document.querySelectorAll('.cmtAccCb:checked')].map(x => parseInt(x.value, 10));
  const threads = parseInt((document.getElementById('cmtCheckThreads') || {}).value) || 8;
  const headless = !(document.getElementById('cmtCheckVisible') || {}).checked;
  try {
    const r = await pywebview.api.start_comment_live_check(ids.length ? ids : null, threads, headless);
    if (!r.ok) { alert(r.error || 'Không check được.'); return; }
    setCmtLiveChecking(true);
    const t = setInterval(async () => {
      try {
        const s = await pywebview.api.is_live_check_running();
        loadCmtAccList();
        if (!s.running) { clearInterval(t); setCmtLiveChecking(false); }
      } catch (e) { clearInterval(t); setCmtLiveChecking(false); }
    }, 800);
  } catch (e) { alert('Lỗi: ' + e); }
}
function setCmtLiveChecking(on) {
  const btn = document.getElementById('cmtLiveBtn');
  if (btn) { btn.disabled = on; btn.textContent = on ? 'Đang check...' : 'Check'; }
  const p = document.getElementById('cmtLivePauseBtn');
  const s = document.getElementById('cmtLiveStopBtn');
  if (p) { p.disabled = !on; p.dataset.state = ''; p.textContent = 'Tạm dừng'; }
  if (s) s.disabled = !on;
}
async function doCmtLivePause() {
  const btn = document.getElementById('cmtLivePauseBtn');
  try {
    if (btn && btn.dataset.state === 'paused') {
      await pywebview.api.resume_live_check();
      btn.textContent = 'Tạm dừng'; btn.dataset.state = '';
    } else {
      await pywebview.api.pause_live_check();
      if (btn) { btn.textContent = 'Tiếp tục'; btn.dataset.state = 'paused'; }
    }
  } catch (e) { alert('Lỗi: ' + e); }
}
async function doCmtLiveStop() {
  if (!confirm('Dừng check live acc comment?')) return;
  setCmtLiveChecking(false);
  try {
    await pywebview.api.stop_live_check();
  } catch (e) {
    setCmtLiveChecking(true);
    alert('Lỗi: ' + e);
  }
}



async function doStop() {
  return doStopFrom('stopBtn');
}
async function doStopFrom(btnId) {
  const btn = document.getElementById(btnId) || document.getElementById('stopBtn');
  const force = btn && btn.dataset.state === 'stopping';
  const r = await pywebview.api.stop_run(force);
  if (!r || r.ok === false) { alert(r?.error || 'Chưa dừng được lượt cũ.'); return; }
  if (r && r.cleanup_pending) {
    showRunCleanup();
    return;
  }
  if (r && r.forced) {
    if (btn) { btn.dataset.state = ''; }
    setRunning(false);
    return;
  }
  ['stopBtn', 'cmtStopBtn'].forEach(id => {
    const b = document.getElementById(id);
    if (!b) return;
    b.dataset.state = 'stopping';
    b.textContent = 'Đang dừng... (bấm nữa để ép)';
  });
}
async function doPause() {
  const btn = document.getElementById('pauseBtn');
  if (btn.dataset.state === 'paused') {
    await pywebview.api.resume_run();
    btn.textContent = 'Tạm dừng'; btn.dataset.state = '';
  } else {
    await pywebview.api.pause_run();
    btn.textContent = 'Tiếp tục'; btn.dataset.state = 'paused';
  }
}

// ── Poll: log + progress + sync badge ──────────────────────────────────
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(poll, 250);
}
function stopPolling() {
  clearInterval(pollTimer); pollTimer = null;
}

async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    let lr = await pywebview.api.get_logs(logCursor);
    if (lr.next < logCursor) {
      logCursor = 0;
      lr = await pywebview.api.get_logs(0);
    }
    renderLogs(lr.logs);
    logCursor = lr.next;
    const p = await pywebview.api.get_progress();
    renderProgress(p);
    const sb = document.getElementById('solverBadge');
    if (sb && typeof p.solver_ready !== 'undefined') {
      sb.textContent = p.solver_ready
        ? 'Captcha: sẵn sàng' : 'Captcha: chưa có API key';
      sb.className = 'solver-badge ' + (p.solver_ready ? 'ok' : 'miss');
    }
    // Check live chạy nền, mở/khóa nút theo trạng thái pause/stop.
    const lcBtn = document.getElementById('liveCheckBtn');
    const lpBtn = document.getElementById('livePauseBtn');
    const lsBtn = document.getElementById('liveStopBtn');
    if (lcBtn && lcBtn.disabled && !p.live_checking) {
      lcBtn.disabled = false; lcBtn.textContent = 'Check';
      if (lpBtn) { lpBtn.disabled = true; lpBtn.textContent = 'Tạm dừng'; lpBtn.dataset.state = ''; }
      if (lsBtn) lsBtn.disabled = true;
    } else if (lcBtn && p.live_checking) {
      lcBtn.disabled = true; lcBtn.textContent =
        p.live_paused ? 'Tạm dừng' : 'Đang check...';
      if (lpBtn) lpBtn.disabled = false;
      if (lsBtn) lsBtn.disabled = false;
    }
    if (p.pool_dirty && p.pool_summary) {
      updatePoolLine(p.pool_summary);
      applyCaptchaVisibility(p.pool_summary);
      loadAccList();
    }
    if (p.acc_updates && p.acc_updates.length) {
      for (const a of p.acc_updates) applyAccPatch(a);
      renderAccStats();
    }
  } catch (e) {
    // Lỗi poll tạm thời không được làm dừng các lượt poll sau.
  } finally {
    pollBusy = false;
  }
}

function applyLogSearch() {
  const q = (document.getElementById('logSearch')?.value || '').trim().toLowerCase();
  document.querySelectorAll('#logArea .log-line').forEach(el => {
    const show = !q || (el.textContent || '').toLowerCase().includes(q);
    el.style.display = show ? '' : 'none';
  });
}

function classifyResult(text, groupType) {
  if (groupType === 'summary') return 'neutral';
  const value = String(text || '').toLowerCase();
  if (/(❌|thất bại|\bdie\b|cần xem|lỗi:)/i.test(value)) return 'attention';
  const ratios = [...value.matchAll(/(\d+)\s*\/\s*(\d+)/g)];
  if (ratios.length) {
    const last = ratios[ratios.length - 1];
    const completed = Number(last[1]), total = Number(last[2]);
    if (total > 0) {
      return completed === total ? 'ok' : 'attention';
    }
  }
  if (/(✅|\blive\b|thành công|hoàn tất)/i.test(value)) return 'ok';
  return 'neutral';
}

function cleanResultLine(message) {
  return String(message || '').replace(/^[┌│└]\s?/, '').trim();
}

function queueResultGroup(entries, groupType = 'compact') {
  const valid = entries.filter(entry => cleanResultLine(entry.msg));
  if (!valid.length) return;
  resultRenderQueue.push({ entries: valid, groupType });
  if (!resultRenderTimer) {
    resultRenderTimer = setTimeout(flushResultQueue, 120);
  }
}

function ingestResultEntry(entry) {
  const message = String(entry.msg || '');
  if (/^┌/.test(message)) {
    if (resultPendingBlock) {
      queueResultGroup(
        resultPendingBlock.entries, resultPendingBlock.groupType);
    }
    resultPendingBlock = { entries: [entry], groupType: 'account' };
    return;
  }
  if (/^━/.test(message)) {
    if (resultPendingBlock?.groupType === 'summary') {
      resultPendingBlock.entries.push(entry);
      queueResultGroup(resultPendingBlock.entries, 'summary');
      resultPendingBlock = null;
    } else {
      if (resultPendingBlock) {
        queueResultGroup(
          resultPendingBlock.entries, resultPendingBlock.groupType);
      }
      resultPendingBlock = { entries: [entry], groupType: 'summary' };
    }
    return;
  }
  if (resultPendingBlock) {
    resultPendingBlock.entries.push(entry);
    if (resultPendingBlock.groupType === 'account' && /^└/.test(message)) {
      queueResultGroup(resultPendingBlock.entries, 'account');
      resultPendingBlock = null;
    }
    return;
  }
  queueResultGroup([entry]);
}

function resultDetailMarkup(message) {
  const clean = cleanResultLine(message);
  const separator = clean.indexOf(':');
  if (separator <= 0) {
    return `<div class="result-detail"><span class="result-detail-value">${escapeHtml(clean)}</span></div>`;
  }
  const label = clean.slice(0, separator);
  const value = clean.slice(separator + 1).trim();
  const mono = /^link$/i.test(label) ? ' mono' : '';
  return `<div class="result-detail">`
    + `<span class="result-detail-label">${escapeHtml(label)}</span>`
    + `<span class="result-detail-value${mono}">${escapeHtml(value)}</span>`
    + `</div>`;
}

function createResultCard(group) {
  const lines = group.entries.map(entry => cleanResultLine(entry.msg))
    .filter(Boolean);
  const joined = lines.join('\n');
  const kind = classifyResult(joined, group.groupType);
  const card = document.createElement('article');
  const compact = group.groupType === 'compact';
  card.className = `result-card result-${kind}${compact ? ' compact' : ''}`;
  card.dataset.kind = kind;
  const firstEntry = group.entries[0] || {};
  const time = String(firstEntry.ts || '').slice(11, 19);
  const title = group.groupType === 'summary'
    ? 'Tổng kết lượt chạy' : (lines.shift() || 'Cập nhật');
  card.dataset.copyText = group.entries.map(entry => {
    const entryTime = String(entry.ts || '').slice(11, 19);
    return `${entryTime} ${String(entry.msg || '')}`.trim();
  }).join('\n');
  const status = group.groupType === 'summary' ? 'Tổng kết'
    : kind === 'ok' ? 'Hoàn tất'
    : kind === 'attention' ? 'Cần xem' : 'Thông tin';
  const details = group.groupType === 'summary'
    ? lines.filter(line => !/^━/.test(line))
    : lines;
  card.innerHTML = `
    <div class="result-card-head">
      <div class="result-card-title">${escapeHtml(title)}</div>
      <div class="result-card-meta">
        <span>${escapeHtml(time)}</span>
        <span class="result-status">${status}</span>
      </div>
    </div>
    <div class="result-card-body">${details.map(resultDetailMarkup).join('')}</div>`;
  return card;
}

function updateResultOverview() {
  const total = document.getElementById('resultTotal');
  const ok = document.getElementById('resultOk');
  const attention = document.getElementById('resultAttention');
  if (total) total.textContent = String(resultCounts.total);
  if (ok) ok.textContent = String(resultCounts.ok);
  if (attention) attention.textContent = String(resultCounts.attention);
}

function applyResultFilter() {
  document.querySelectorAll('#resultArea .result-card').forEach(card => {
    card.classList.toggle(
      'filtered', resultFilter !== 'all' && card.dataset.kind !== resultFilter);
  });
}

function updateResultFollowState() {
  const button = document.getElementById('resultFollowBtn');
  const label = document.getElementById('resultFollowLabel');
  if (!button || !label) return;
  button.classList.toggle('active', resultAutoFollow);
  button.classList.toggle('has-new', !resultAutoFollow && resultUnseen > 0);
  label.textContent = resultAutoFollow
    ? 'Đang theo dõi'
    : resultUnseen > 0 ? `${resultUnseen} kết quả mới` : 'Theo dõi mới nhất';
}

function resumeResultFollow() {
  const area = document.getElementById('resultArea');
  resultAutoFollow = true;
  resultUnseen = 0;
  updateResultFollowState();
  if (area) area.scrollTop = area.scrollHeight;
}

function flushResultQueue() {
  resultRenderTimer = null;
  const area = document.getElementById('resultArea');
  if (!area || !resultRenderQueue.length) return;
  const groups = resultRenderQueue.splice(0);
  area.querySelector('.result-empty')?.remove();
  const fragment = document.createDocumentFragment();
  for (const group of groups) {
    const card = createResultCard(group);
    resultCounts.total += 1;
    if (card.dataset.kind === 'ok') resultCounts.ok += 1;
    if (card.dataset.kind === 'attention') resultCounts.attention += 1;
    card.classList.toggle(
      'filtered', resultFilter !== 'all' && card.dataset.kind !== resultFilter);
    fragment.appendChild(card);
  }
  area.appendChild(fragment);
  updateResultOverview();
  if (resultAutoFollow) {
    requestAnimationFrame(() => { area.scrollTop = area.scrollHeight; });
  } else {
    resultUnseen += groups.length;
  }
  updateResultFollowState();
}

function renderLogs(logs) {
  if (!logs.length) return;
  const logArea = document.getElementById('logArea');
  let touchedLog = false;
  const q = (document.getElementById('logSearch')?.value || '').trim().toLowerCase();
  for (const e of logs) {
    if (e.level === 'result') {
      ingestResultEntry(e);
      continue;
    }
    const line = document.createElement('div');
    const cls = e.level === 'error' ? 'error'
              : e.level === 'warn' ? 'warn'
              : e.msg.includes('✅') ? 'ok'
              : e.msg.includes('❌') ? 'error' : '';
    line.className = 'log-line ' + cls;
    const t = (e.ts || '').slice(11, 19);
    line.innerHTML = `<span class="ts">${t}</span>${escapeHtml(e.msg)}`;
    if (q && !(e.msg || '').toLowerCase().includes(q)
        && !t.toLowerCase().includes(q)) {
      line.style.display = 'none';
    }
    logArea.appendChild(line);
    touchedLog = true;
  }
  while (logArea.childElementCount > 3000) {
    logArea.firstElementChild.remove();
  }
  if (touchedLog) logArea.scrollTop = logArea.scrollHeight;
}

function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = String(Math.floor(total / 3600)).padStart(2, '0');
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const s = String(total % 60).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

function renderPhaseTiming(timing) {
  if (!timing || typeof timing !== 'object') return '';
  const parts = [];
  if (timing.scout_elapsed > 0 || timing.phase === 'scout') {
    parts.push(`Scout ${formatElapsed(timing.scout_elapsed)}`);
  }
  if (timing.like_elapsed > 0 || timing.phase === 'like') {
    parts.push(`Sau scout ${formatElapsed(timing.like_elapsed)}`);
  }
  if (timing.paused) parts.push('Đang tạm dừng');
  return parts.length ? ` · ${parts.join(' · ')}` : '';
}

function renderMeasuredPerformance(p) {
  const m = p.performance;
  if (!m) return p.api_like ? ' · Chưa xác nhận số tim tăng thực tế' : '';
  const first = m.first_60s_new_likes || 0;
  const windowCount = m.new_likes_last_60s || 0;
  const elapsed = Math.floor(Number(m.elapsed_seconds) || 0);
  return ` · 60 giây gần nhất: <b>${windowCount}</b> tim mới`
    + ` · Phút đầu: <b>${first}/150</b> (${Math.min(60, elapsed)}/60s)`
    + ` · Đã có: ${m.already_present || 0} · Chưa đọc được: ${m.unread || 0}`
    + (m.target_150_first_60s_met ? ' · Đạt 150 tim mới/60s với 10 tab' : '');
}

function renderProgress(p) {
  const ok = p.performance ? (p.performance.new_likes || 0) : (p.ok || 0), done = p.done || 0, accOk = p.acc_ok || 0,
        target = p.target || 0;
  // API status_code=0 chỉ xác nhận TikTok nhận request. Không suy diễn thành
  // reply/lượt tim đã vượt kiểm duyệt và hiển thị công khai.
  const isCommentJob = p.job === 'comment';
  const subjectLabel = isCommentJob ? 'Lượt acc-link đã xử lý' : 'Acc đã chạy';
  const successLabel = isCommentJob ? 'Có API nhận reply'
    : p.api_like ? 'Acc hoàn tất' : 'Thành công';
  const actionLabel = isCommentJob ? 'API nhận reply'
    : p.api_like ? (p.performance ? 'Tim mới xác nhận' : 'API mã 0 (tổng CID)') : 'Đã tim';
  const txt = target > 0
    ? `${subjectLabel} <b>${done}</b> · ${successLabel} <b>${accOk}</b> · `
      + `${actionLabel} <b>${ok}</b>/${target}`
    : `${subjectLabel} <b>${done}</b> · ${successLabel} <b>${accOk}</b> · `
      + `${actionLabel} <b>${ok}</b>`;
  document.getElementById('progressText').innerHTML =
    txt + renderPhaseTiming(p.timing) + renderMeasuredPerformance(p);
  const pct = target > 0 ? Math.min(100, (ok / target) * 100) : 0;
  const timingActive = p.timing && p.timing.phase && p.timing.phase !== 'idle';
  if (done > 0 || timingActive) {
    document.getElementById('progressBox').classList.remove('hidden');
  }
  if (p.cleanup_pending) { showRunCleanup(p.cleanup_error); return; }
  if (!p.running) setRunning(false);
}

function setRunning(running) {
  const start = document.getElementById('startBtn');
  if (start) start.disabled = running;
  const apiCapture = document.getElementById('apiCaptureBtn');
  if (apiCapture) apiCapture.disabled = running;
  const cmtStart = document.getElementById('cmtStartBtn');
  if (cmtStart) cmtStart.disabled = running;
  ['pauseBtn', 'cmtPauseBtn'].forEach(id => {
    const b = document.getElementById(id);
    if (!b) return;
    b.disabled = !running;
    if (!running) { b.dataset.state = ''; b.textContent = 'Tạm dừng'; }
  });
  ['stopBtn', 'cmtStopBtn'].forEach(id => {
    const b = document.getElementById(id);
    if (!b) return;
    b.disabled = !running;
    b.dataset.state = '';
    b.textContent = 'Dừng';
  });
}

// ── Update ─────────────────────────────────────────────────────────────
async function checkUpdate() {
  try {
    const u = await pywebview.api.check_update();
    if (u.has_update) {
      updateInfo = u;
      document.getElementById('updateTitle').textContent =
        `Có bản cập nhật ${u.latest} (đang dùng ${u.current})`;
      document.getElementById('updateNotes').textContent = u.release_notes || '';
      document.getElementById('updateNotice').classList.remove('hidden');
    }
  } catch (e) { /* silent */ }
}

async function doUpdate() {
  if (!updateInfo) return;
  if (!confirm(`Cập nhật lên ${updateInfo.latest}? Tool sẽ tự khởi động lại.`)) return;
  // Download + swap + relaunch thực hiện ở Python (updater.py).
  // JS chỉ trigger; nếu fail sẽ throw lên log.
  alert('Đang tải bản cập nhật... tool sẽ đóng và mở lại tự động.');
  await pywebview.api.install_update();  // bridge method nếu có; else fallback
}

// ── Helpers ────────────────────────────────────────────────────────────
let lastPoolSummary = {accounts: 0, proxies: 0};
function updatePoolLine(poolSummary) {
  if (!poolSummary) return;
  lastPoolSummary = {...lastPoolSummary, ...poolSummary};
  poolSummary = lastPoolSummary;
  document.getElementById('poolLine').textContent =
    `Pool: ${poolSummary.accounts} tài khoản · ${poolSummary.proxies} proxy · Solver: ${poolSummary.solver || 'chưa cấu hình'} · ${captchaBalanceLabel}`;
}

// Captcha preference belongs to the pool and survives application restarts.
function applyCaptchaVisibility(poolSummary) {
  const input = document.getElementById('autocapInput');
  const capRow = input && input.closest('.opt-row');
  if (capRow) capRow.style.display = '';
}
async function loadPoolCaptchaSettings() {
  const result = await pywebview.api.get_pool_captcha_settings();
  if (!result.ok) throw new Error(result.error || 'Không nạp được cấu hình captcha.');
  document.getElementById('autocapInput').checked = result.enabled;
  document.getElementById('solverBadge').textContent = result.solver_ready ? 'Captcha: sẵn sàng' : 'Captcha: chưa có API key';
}
async function savePoolCaptchaSettings() {
  const result = await pywebview.api.set_pool_captcha_settings(document.getElementById('autocapInput').checked);
  if (!result.ok) throw new Error(result.error || 'Không lưu được cấu hình captcha.');
}
function showMain(poolSummary) {
  const lv = document.getElementById('loginView');
  if (lv) {
    lv.classList.add('hidden');
    lv.style.display = 'none';
  }
  const mv = document.getElementById('mainView');
  if (mv) {
    mv.classList.remove('hidden');
    mv.style.display = 'block';
  }
  updatePoolLine(poolSummary);
  applyCaptchaVisibility(poolSummary);
  applyThreadHint({});
  if (window.pywebview && window.pywebview.api && window.pywebview.api.get_thread_hint) {
    pywebview.api.get_thread_hint().then(applyThreadHint).catch(() => {});
  }
}

function applyThreadHint(h) {
  const row = document.getElementById('threadHintRow');
  const help = document.getElementById('threadHintHelp');
  const inp = document.getElementById('threadsInput');
  if (!row || !inp) return;
  inp.min = '1'; inp.max = '30';
  if (!inp.dataset.threadBound) {
    let saved = null;
    try { saved = localStorage.getItem('alc-browser-count-v95'); } catch (_) {}
    if (saved !== null) {
      inp.value = Math.max(1, Math.min(30, parseInt(saved, 10) || 10));
    } else {
      inp.value = '10';
    }
    inp.dataset.threadBound = '1';
    inp.addEventListener('input', () => {
      inp.dataset.userSet = '1';
      const n = Number(inp.value);
      if (Number.isInteger(n) && n >= 1 && n <= 30) {
        try { localStorage.setItem('alc-browser-count-v95', String(n)); } catch (_) {}
      }
      syncThreadChoice();
    });
  }
  row.innerHTML = [10, 15, 20, 25, 30].map(n => `<button type="button" class="chip" data-th="${n}">${n} trình duyệt</button>`).join('');
  row.querySelectorAll('[data-th]').forEach(b => b.addEventListener('click', () => {
    inp.value = b.dataset.th;
    inp.dispatchEvent(new Event('input', {bubbles:true}));
  }));
  syncThreadChoice();
  if (help) help.textContent = 'Mặc định 10 trình duyệt. Tốc độ thực tế được đo theo tim mới đã xác nhận.';
}
function syncThreadChoice() {
  const value = Number(document.getElementById('threadsInput').value);
  document.querySelectorAll('#threadHintRow [data-th]').forEach(b => {
    const active = Number(b.dataset.th) === value;
    b.classList.toggle('chip-accent', active);
    b.setAttribute('aria-pressed', String(active));
  });
}

function clearLog() {
  document.getElementById('logArea').innerHTML = '';
}

// Copy toàn bộ nội dung panel log/kết quả (bôi đen Ctrl+C khó dùng trong webview).
function copyLog(areaId) {
  const area = document.getElementById(areaId);
  const resultCards = areaId === 'resultArea'
    ? [...area.querySelectorAll('.result-card')] : [];
  const text = resultCards.length
    ? resultCards.map(card => card.dataset.copyText || '').join('\n')
    : area.innerText;
  navigator.clipboard.writeText(text)
    .then(() => toastCopy(areaId, 'Đã copy!'))
    .catch(() => {
      // Fallback: chọn text để bấm Ctrl+C tay (clipboard API bị chặn).
      const range = document.createRange();
      range.selectNodeContents(document.getElementById(areaId));
      const sel = window.getSelection();
      sel.removeAllRanges(); sel.addRange(range);
      toastCopy(areaId, 'Đã bôi đen. Bấm Ctrl+C');
    });
}
function toastCopy(areaId, msg) {
  const area = document.getElementById(areaId);
  const btn = area?.closest('.log-panel')
    ?.querySelector('.log-head button');
  if (!btn) return;
  const old = btn.textContent;
  btn.textContent = msg;
  setTimeout(() => { btn.textContent = old; }, 1200);
}

function clearResult() {
  clearTimeout(resultRenderTimer);
  resultRenderTimer = null;
  resultRenderQueue = [];
  resultPendingBlock = null;
  resultCounts = { total: 0, ok: 0, attention: 0 };
  resultUnseen = 0;
  resultAutoFollow = true;
  const area = document.getElementById('resultArea');
  area.innerHTML = `
    <div class="result-empty">
      <strong>Chưa có kết quả</strong>
      <span>Mỗi tài khoản sẽ xuất hiện thành một mục riêng khi hoàn tất.</span>
    </div>`;
  updateResultOverview();
  updateResultFollowState();
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Same-origin view generated from saved disk data; no localhost fetch needed.
function renderLocalPoolView() {
  const view = window.__LOCAL_POOL_VIEW__;
  if (!view || !Array.isArray(view.accounts)) return false;
  accListCache = view.accounts.map(a => ({...a, status:String(a.status || 'unknown'),
                                        username:String(a.username || '')}));
  try {
    renderAccStats();
    renderAccList();
    updatePoolLine({accounts:accListCache.length, proxies:(view.proxies || []).length});
  } catch (error) {
    const box = document.getElementById('accListBody');
    if (box) box.textContent = 'Lỗi hiển thị: ' + (error.message || error);
    console.error(error);
  }
  return true;
}
function renderLocalProxyView() {
  const view = window.__LOCAL_POOL_VIEW__;
  if (!view || !Array.isArray(view.proxies)) return false;
  renderProxysachStatus({ok:true, enabled:false, details:[], manual_details:view.proxies,
    manual_proxies:view.proxies.length, total_proxies:view.proxies.length,
    active_proxies:0, configured_keys:0, available_count:0, leased_count:0});
  return true;
}
let accountRefreshBusy=false;
async function refreshCheckedAccounts() {
 const api=window.pywebview?.api;
 if(!api?.get_accounts)return false;
 if(accountRefreshBusy)return true;
 accountRefreshBusy=true;
 try {
  const result=await api.get_accounts();
  const accounts=Array.isArray(result)?result:result?.accounts;
  if(!Array.isArray(accounts))return false;
  const changed=accounts.length!==accListCache.length || accounts.some((a,i)=>{
   const old=accListCache[i];
   return !old || String(old.id)!==String(a.id) || old.status!==a.status || old.username!==a.username;
  });
  if(!window.__LOCAL_POOL_VIEW__)window.__LOCAL_POOL_VIEW__={};
  window.__LOCAL_POOL_VIEW__.accounts=accounts.map(a=>({...a}));
  if(changed){accListCache=accounts.map(a=>({...a}));renderAccList();renderAccStats();updatePoolLine({accounts:accListCache.length});}
  return true;
 }catch(_){return false;}finally{accountRefreshBusy=false;}
}

async function refreshLocalPoolView() {
  await refreshCheckedAccounts();
  await loadProxysachStatus();
}
setTimeout(() => { renderLocalPoolView(); renderLocalProxyView(); }, 0);
setInterval(refreshLocalPoolView, 10000);

function renderMachineProxyAllocation(config) {
  if (!Number.isInteger(config.count) || !config.machine) throw new Error('Server chưa hỗ trợ lưu số proxy; mở bản Server cập nhật.');
  const input = document.getElementById('machineProxyCount');
  input.value = config.count;
  input.removeAttribute('max');
  document.getElementById('machineProxyStatus').textContent =
    `Máy ${config.machine}: đã chọn ${config.count} proxy. Giữ proxy trong phiên, không cần chọn lại khi Bắt đầu.`;
}
async function loadMachineProxyAllocation() {
  try {
    const config = await pywebview.api.get_machine_proxy_allocation();
    if (config.ok) renderMachineProxyAllocation(config);
  } catch (error) {
    document.getElementById('machineProxyStatus').textContent = 'Chưa nạp được cấu hình proxy máy này.';
  }
}
async function saveMachineProxyAllocation() {
  try {
    const count = document.getElementById('machineProxyCount').value;
    const config = await pywebview.api.set_machine_proxy_allocation(count);
    if (!config.ok) throw new Error(config.error);
    renderMachineProxyAllocation(config);
    return true;
  } catch (error) {
    document.getElementById('machineProxyStatus').textContent = error.message || String(error);
    return false;
  }
}
function bindMachineProxyAllocation() {
  document.getElementById('saveMachineProxyBtn').addEventListener('click', saveMachineProxyAllocation);
  document.getElementById('machineProxyCount').addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); saveMachineProxyAllocation(); }
  });
  document.querySelectorAll('[data-machine-proxies]').forEach(button => {
    button.addEventListener('click', () => {
      document.getElementById('machineProxyCount').value = button.dataset.machineProxies;
      saveMachineProxyAllocation();
    });
  });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bindMachineProxyAllocation);
} else {
  bindMachineProxyAllocation();
}

let captchaBalanceBusy = false;
async function refreshCaptchaBalance() {
  if (captchaBalanceBusy || !window.pywebview?.api?.get_captcha_balance) return;
  captchaBalanceBusy = true;
  try {
    const r = await pywebview.api.get_captcha_balance();
    if (r.ok) {
      const badge = document.getElementById('solverBadge');
      if (badge) badge.textContent = 'Captcha: sẵn sàng';
    }
    captchaBalanceLabel = r.ok ? `Captcha còn: ${r.estimated ? '≈' : ''}${Number(r.remaining).toLocaleString('vi-VN')} lượt` : 'Captcha còn: chưa đọc được';
  } catch (_) { captchaBalanceLabel = 'Captcha còn: chưa đọc được'; }
  finally { captchaBalanceBusy = false; updatePoolLine({}); }
}
setInterval(refreshCaptchaBalance, 60000);
window.addEventListener('pywebviewready', refreshCaptchaBalance);
setTimeout(refreshCaptchaBalance, 1500);

window.addEventListener('beforeunload', () => { window.pywebview?.api?.clear_machine_proxy_session?.(); });

// v90: account management is authoritative in the current local pool.
loadAccList = async function() {
  try {
    const r=await pywebview.api.local_pool_snapshot();
    if(!r.ok||!Array.isArray(r.accounts)) throw new Error('Không đọc được pool trên máy.');
    window.__LOCAL_POOL_VIEW__=r;
    accListCache=r.accounts;
    renderAccList();renderAccStats();updateSelectedCount();
    updatePoolLine({accounts:r.accounts.length,proxies:r.proxies.length});
  } catch(e) { alert('Lỗi tải pool: '+e); }
};
async function deleteLocalPool(ids,status) {
  const r=await pywebview.api.local_delete_accounts(ids,status);
  if(!r.ok) throw new Error(r.error||'Chưa xóa được tài khoản.');
  await loadAccList();
}
doDeleteSelected=async function(){const ids=selectedAccIds();if(!ids.length)return alert('Chưa chọn tài khoản.');if(!confirm(`Xóa ${ids.length} tài khoản đã chọn khỏi pool?`))return;try{await deleteLocalPool(ids,null);}catch(e){alert('Lỗi xóa: '+e);}};
doDeleteBanned=async function(){if(!confirm('Xóa acc BANNED khỏi pool?'))return;try{await deleteLocalPool(null,'banned');}catch(e){alert('Lỗi xóa: '+e);}};
doDeleteDead=async function(){if(!confirm('Xóa acc DIE khỏi pool?'))return;try{await deleteLocalPool(null,'die');}catch(e){alert('Lỗi xóa: '+e);}};
doDeleteUnknown=async function(){if(!confirm('Xóa acc UNKNOWN khỏi pool?'))return;try{await deleteLocalPool(null,'unknown');}catch(e){alert('Lỗi xóa: '+e);}};


// Keep the pool window open while only the current run is being stopped.
function showRunCleanup(error) {
  ['startBtn','apiCaptureBtn','cmtStartBtn','pauseBtn','cmtPauseBtn'].forEach(id => {
    const element = document.getElementById(id);
    if (element) element.disabled = true;
  });
  ['stopBtn','cmtStopBtn'].forEach(id => {
    const element = document.getElementById(id);
    if (!element) return;
    element.dataset.state = 'stopping';
    element.disabled = true;
    element.textContent = error || 'Đang ép dừng lượt cũ…';
  });
}
