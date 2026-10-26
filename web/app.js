/* 热点资讯采集台 —— 前端逻辑 */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  view: 'news',
  q: '', status: '', category: '', sort: 'default',
  groupState: new Map(),   // date -> {page, items, total, expanded}
  cachePage: 1, cacheType: '', cacheQ: '',
  polling: new Set(),
  pollTimer: null,
  imageCount: 0,   // 0 = 用服务端默认配置
  videoCount: 0,
  autoRefresh: localStorage.getItem('autonews.autoRefresh') !== '0',
  autoTimer: null,
};

/* ---------------- utils ---------------- */
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function safeUrl(u) {
  const s = String(u || '');
  return /^(https?:)?\/\//i.test(s) ? s : '';
}
function fmtNum(n) {
  n = Number(n || 0);
  if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + '万';
  return n.toLocaleString('zh-CN');
}
function fmtSize(bytes) {
  const mb = bytes / 1024 / 1024;
  return mb >= 1 ? mb.toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
}
const STATUS_TEXT = { pending: '未抓取', queued: '排队中', fetching: '抓取中', success: '抓取成功', failed: '抓取失败' };
function statusTag(status, error) {
  const cls = status || 'pending';
  const title = error ? `title="${escapeHtml(error)}"` : '';
  return `<span class="tag ${cls}" ${title}>${STATUS_TEXT[cls] || cls}</span>`;
}
async function api(url, opts = {}) {
  const res = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

/* ---------------- 兜底：事件绑定 / 错误提示 ---------------- */
function on(sel, ev, fn) {
  const el = $(sel);
  if (!el) { console.warn('[init] 缺少元素，跳过绑定：' + sel); return; }
  el.addEventListener(ev, fn);
}
function toast(msg, type) {
  let t = $('#toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    document.body.appendChild(t);
  }
  t.className = 'toast ' + (type || 'error');
  t.textContent = msg;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add('hidden'), 5000);
}
function safe(fn) {
  return async (...args) => {
    try { await fn(...args); }
    catch (e) { console.error(e); toast('操作失败：' + (e.message || e)); }
  };
}

/* ---------------- 上传 ---------------- */
function initUpload() {
  const zone = $('#dropzone');
  const input = $('#file-input');
  if (!zone || !input) return;
  const inner = zone.querySelector('.upload-inner') || zone;
  inner.addEventListener('click', () => input.click());
  on('#file-input', 'change', () => { uploadFiles(input.files); input.value = ''; });
  ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('drag'); }));
  zone.addEventListener('drop', safe((e) => { if (e.dataTransfer?.files?.length) return uploadFiles(e.dataTransfer.files); }));
}

async function uploadFiles(fileList) {
  if (!fileList || !fileList.length) return;
  const form = new FormData();
  Array.from(fileList).forEach((f) => form.append('files', f));
  let data;
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: form });
    data = await res.json();
  } catch (e) {
    toast('上传失败：' + (e.message || e));
    return;
  }
  const ids = (data.batches || []).filter((b) => b.batch_id).map((b) => b.batch_id);
  (data.batches || []).forEach((b) => {
    if (b.error) renderBatches([{ id: 'x', filename: b.filename, status: 'error', message: b.error }]);
  });
  if (!ids.length) return;
  await watchBatches(ids);
}

async function watchBatches(ids) {
  renderBatches(ids.map((id) => ({ id, filename: '解析中…', status: 'parsing' })));
  let tries = 0;
  const tick = async () => {
    const list = await api('/api/batches');
    const mine = list.items.filter((b) => ids.includes(b.id));
    renderBatches(mine);
    if (mine.some((b) => b.status === 'parsing') && tries++ < 600) {  // 最多约 8 分钟，避免死循环
      setTimeout(tick, 800);
    } else {
      await Promise.all([refreshStats(), refreshGroups(), loadDicts(), refreshCache()]);
    }
  };
  await tick();
}

function renderBatches(items) {
  const box = $('#batch-list');
  const keep = items.slice(0, 8);
  box.innerHTML = keep.map((b) => {
    const pill = b.status === 'done'
      ? '<span class="tag success">完成</span>'
      : b.status === 'error' ? '<span class="tag failed">失败</span>' : '<span class="tag fetching">解析中</span>';
    const msg = b.status === 'parsing'
      ? `已读取 ${b.total || 0} 行，入库 ${b.inserted || 0} 条`
      : (b.message || '');
    return `<div class="batch-item"><span class="name">${escapeHtml(b.filename)}</span>
      <span class="msg">${escapeHtml(msg)}</span><span class="pill">${pill}</span></div>`;
  }).join('');
}

/* ---------------- 统计 ---------------- */
async function refreshStats() {
  const s = await api('/api/stats');
  const a = s.articles || {};
  const cards = [
    ['资讯总数', a.total || 0, ''],
    ['日期数', s.date_count || 0, ''],
    ['未抓取', a.pending || 0, ''],
    ['抓取成功', a.success || 0, 'green'],
    ['抓取失败', a.failed || 0, 'red'],
    ['缓存原文', (s.cache || {}).cached || 0, 'blue'],
    ['已导出', (s.media || {}).exported || 0, 'green'],
    ['素材文件', ((s.media || {}).assets || {}).c || 0, 'blue'],
  ];
  $('#stat-cards').innerHTML = cards.map(([label, value, cls]) =>
    `<div class="stat-card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`).join('');

  const c = s.cache || {};
  $('#cache-stats').innerHTML = [
    ['缓存条数', c.cached || 0, ''],
    ['文章', c.articles || 0, ''],
    ['图片', c.images || 0, ''],
    ['视频', c.videos || 0, ''],
    ['占用空间', fmtSize(s.cache_size || 0), 'blue'],
  ].map(([label, value, cls]) =>
    `<div class="stat-card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`).join('');
  $('#cache-badge').textContent = c.cached || 0;
  updateQueueInfo(s.queue);
}

function updateQueueInfo(q) {
  const info = $('#queue-info');
  if (!q || (!q.queued && !q.running)) { info.textContent = ''; return; }
  info.textContent = `队列：${q.queued} 待抓 · ${q.running} 进行中`;
}

/* ---------------- 字典 ---------------- */
async function loadDicts() {
  const d = await api('/api/categories');
  const catSel = $('#category');
  const current = catSel.value;
  catSel.innerHTML = '<option value="">全部分类</option>' + d.categories.map((c) =>
    `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}（${c.c}）</option>`).join('');
  catSel.value = current;
  $('#cat-list').innerHTML = d.categories.map((c) => `<option value="${escapeHtml(c.name)}"></option>`).join('');
}

/* ---------------- 资讯列表（按日期归类 + 组内分页） ---------------- */
const PAGE_SIZE = 8;   // 每个日期分组每页条数

/** 组内翻页请求参数；date 为空表示「未标注日期」分组，必须显式只要这组。 */
function pagerParams(date, page) {
  const params = new URLSearchParams({
    date: date || '', q: state.q, status: state.status,
    category: state.category, sort: state.sort,
    page: String(page), page_size: String(PAGE_SIZE),
  });
  if (!date) params.set('date_empty', '1');
  return params.toString();
}

async function refreshGroups() {
  const params = new URLSearchParams({ q: state.q, status: state.status, category: state.category, sort: state.sort, limit: PAGE_SIZE });
  const data = await api('/api/articles/grouped?' + params.toString());
  const groupsEl = $('#groups');
  $('#empty').classList.toggle('hidden', data.groups.length > 0);
  // 保留各组之前停留的页码（抓取/删除/自动刷新后不被打回第一页）
  const prev = state.groupState;
  const pageOf = (g) => {
    const st = prev.get(g.date);
    if (!st) return 1;
    const pages = Math.max(1, Math.ceil((g.total || 0) / PAGE_SIZE));
    return Math.min(Math.max(1, st.page || 1), pages);
  };
  const pages = data.groups.map(pageOf);
  const extra = await Promise.all(data.groups.map((g, i) => (
    pages[i] > 1
      ? api('/api/articles?' + pagerParams(g.date, pages[i])).catch(() => null)
      : null
  )));
  data.groups.forEach((g, i) => {
    if (extra[i]) { g.items = extra[i].items; g.total = extra[i].total; }
    g.page = pages[i];
    g.page_size = PAGE_SIZE;
  });
  groupsEl.innerHTML = data.groups.map((g, idx) => renderGroup(g, idx === 0)).join('');
  data.groups.forEach((g, idx) => {
    state.groupState.set(g.date, {
      page: g.page, total: g.total, items: g.items, page_size: PAGE_SIZE,
      expanded: prev.get(g.date)?.expanded ?? idx === 0,
    });
    const el = groupsEl.querySelector(`.group[data-date="${g.date}"]`);
    if (el && !state.groupState.get(g.date).expanded) el.classList.add('collapsed');
  });
}

function renderGroup(g, first) {
  const st = g.stats;
  return `<div class="group" data-date="${g.date}">
    <div class="group-head" data-act="toggle">
      <span class="caret">▾</span>
      <span class="group-date">${escapeHtml(g.date || '未标注日期')}</span>
      <span class="group-meta">
        <span>共 ${st.total} 条</span>
        <span>未抓取 ${st.pending}</span>
        <span class="muted">成功 ${st.success}</span>
        <span class="muted">失败 ${st.failed}</span>
        <span>阅读 ${fmtNum(st.reads)}</span>
      </span>
      <span class="group-actions">
        <button class="btn small" data-act="fetch-date">抓取本日原文</button>
        <button class="btn small" data-act="media-date">抓取本日素材</button>
        <button class="btn small ghost" data-act="del-date">删除本日</button>
      </span>
    </div>
    <div class="group-body">
      ${renderTable(g.items)}
      ${renderPager(g)}
    </div>
  </div>`;
}

function renderPager(g) {
  const total = Number(g.total) || 0;
  const size = Number(g.page_size) || PAGE_SIZE;
  const page = Math.min(Math.max(1, Number(g.page) || 1), Math.max(1, Math.ceil(total / size)));
  const pages = Math.max(1, Math.ceil(total / size));
  if (pages <= 1) return total ? '<div class="pager"><span class="pager-info">共 ' + total + ' 条</span></div>' : '';
  return `<div class="pager">
    <button class="btn small ghost" data-act="page-prev" ${page <= 1 ? 'disabled' : ''}>上一页</button>
    <span class="pager-info">第 ${page} / ${pages} 页 · 共 ${total} 条</span>
    <button class="btn small ghost" data-act="page-next" ${page >= pages ? 'disabled' : ''}>下一页</button>
  </div>`;
}

function renderTable(items) {
  if (!items.length) return '<div class="empty">无匹配数据</div>';
  return `<table class="grid">
    <thead><tr>
      <th class="col-title">标题</th><th>来源</th><th>原始分类</th><th>修正分类</th>
      <th class="num">阅读</th><th class="num">点赞</th><th>发布时间</th><th>抓取状态</th><th>操作</th>
    </tr></thead>
    <tbody>${items.map(rowHtml).join('')}</tbody>
  </table>`;
}

const MEDIA_TEXT = { pending: '素材', queued: '收集中…', running: '收集中…', success: '',
                    failed: '素材失败', empty: '无素材' };
// 素材已采过（成功/无素材）之后的「重采」入口：清空旧素材，按标题/关键词重新检索
const MEDIA_RETRY_BTN = '<button class="btn small ghost" data-act="media-retry"'
  + ' title="清空现有素材，按标题/关键词重新检索（不满意时用这个）">重采</button>';
// 已经采过（成功 / 无素材 / 失败）的状态：点主按钮进抽屉查看、上传、换词重采
const MEDIA_DONE_STATES = ['success', 'empty', 'failed'];
function mediaLabel(r) {
  const st = r.media_status || 'pending';
  if (st === 'success') return `素材${r.media_count ? `(${r.media_count})` : ''}`;
  return MEDIA_TEXT[st] || '素材';
}

const REWRITE_TEXT = { pending: '改写', queued: '改写排队', running: '改写中…', success: '重新改写', failed: '改写失败' };
function rewriteLabel(r) {
  return REWRITE_TEXT[r.rewrite_status || 'pending'] || '改写';
}

function rowHtml(r) {
  return `<tr data-id="${r.id}" data-cid="${r.content_id || ''}" data-fst="${r.fetch_status || ''}" data-mst="${r.media_status || 'pending'}" data-mc="${r.media_count || 0}" data-rst="${r.rewrite_status || ''}" data-dir="${escapeHtml(r.output_dir || '')}">
    <td class="col-title">
      <div class="title-cell">
        <a class="title-link" href="${escapeHtml(safeUrl(r.url))}" target="_blank" rel="noreferrer" title="${escapeHtml(r.title)}">${escapeHtml(r.title || '(无标题)')}</a>
      </div>
      ${r.fetch_status === 'failed' && r.fetch_error ? `<div class="row-msg">${escapeHtml(r.fetch_error)}</div>` : ''}
      ${r.media_status === 'failed' && r.media_error ? `<div class="row-msg">${escapeHtml(r.media_error)}</div>` : ''}
    </td>
    <td>${escapeHtml(r.source || '-')}</td>
    <td class="muted">${escapeHtml(r.orig_category || '-')}</td>
    <td><input class="cat-input" list="cat-list" data-act="cat" value="${escapeHtml(r.category || '')}" placeholder="修正分类" /></td>
    <td class="num">${fmtNum(r.read_count)}</td>
    <td class="num">${fmtNum(r.like_count)}</td>
    <td class="muted">${escapeHtml((r.publish_time || '').slice(11) || '-')}</td>
    <td data-role="status">${statusTag(r.fetch_status, r.fetch_error)}</td>
    <td>
      <div class="ops">
        <button class="btn small" data-act="fetch">${r.fetch_status === 'success' ? '重新抓取' : '抓取'}</button>
        <button class="btn small ghost" data-act="view" ${r.content_id ? '' : 'disabled'}>原文</button>
        <button class="btn small ghost" data-act="export" ${r.content_id ? '' : 'disabled'}>导出</button>
        <button class="btn small ${['queued', 'running'].includes(r.rewrite_status) ? '' : 'ghost'}" data-act="rewrite"
          ${r.content_id ? '' : 'disabled'}>${rewriteLabel(r)}</button>
        <button class="btn small ghost" data-act="rw-view" ${r.rewrite_id ? '' : 'hidden'}>对比</button>
        <button class="btn small ${r.media_status === 'success' ? 'accent' : 'ghost'}" data-act="media"
          ${r.content_id ? '' : 'disabled'}>${mediaLabel(r)}</button>
        ${MEDIA_DONE_STATES.includes(r.media_status || '') ? MEDIA_RETRY_BTN : ''}
        <button class="btn small ${r.rewrite_id ? '' : 'ghost'}" data-act="layout"
          ${r.rewrite_id ? '' : 'disabled'}>排版</button>
        <a class="btn small ghost" href="${escapeHtml(safeUrl(r.url))}" target="_blank" rel="noreferrer">链接</a>
        <button class="btn small ghost" data-act="del" title="删除该文章及其缓存、素材、导出文件">删除</button>
      </div>
    </td>
  </tr>`;
}

/* ---------------- 交互 ---------------- */
function initEvents() {
  $$('.tab').forEach((tab) => tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    state.view = tab.dataset.view;
    ['news', 'cache', 'settings', 'accounts', 'pipeline', 'publish'].forEach((v) => {
      const el = $(`#view-${v}`);
      if (el) el.classList.toggle('active', state.view === v);
    });
    if (state.view === 'cache') refreshCache();
    if (state.view === 'settings') initSettings();
    if (state.view === 'accounts') initAccounts();
    if (state.view === 'pipeline') refreshPipeline();
    if (state.view === 'publish') refreshPublishCenter();
  }));

  let timer = null;
  on('#q', 'input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(safe(() => { state.q = e.target.value.trim(); return refreshGroups(); }), 300);
  });
  on('#status', 'change', safe((e) => { state.status = e.target.value; return refreshGroups(); }));
  on('#only-cached', 'change', safe((e) => {
    state.status = e.target.checked ? 'success' : '';
    const sel = $('#status');
    if (sel) sel.value = state.status;
    return refreshGroups();
  }));
  on('#category', 'change', safe((e) => { state.category = e.target.value; return refreshGroups(); }));
  on('#sort', 'change', safe((e) => { state.sort = e.target.value; return refreshGroups(); }));

  on('#btn-fetch-all', 'click', safe(async () => {
    const btn = $('#btn-fetch-all');
    btn.disabled = true;
    try {
      const res = await api('/api/fetch', { method: 'POST', body: JSON.stringify({ q: state.q, category: state.category, status: state.status || null }) });
      if (!res.enqueued) { toast('没有需要抓取的记录', 'warn'); return; }
      await refreshGroups();
      startPolling();
    } finally { btn.disabled = false; }
  }));
  on('#btn-clear-queue', 'click', safe(async () => {
    await api('/api/fetch/queue', { method: 'DELETE' });
    await Promise.all([refreshStats(), refreshGroups()]);
  }));
  on('#btn-rewrite-all', 'click', safe(async () => {
    const btn = $('#btn-rewrite-all');
    btn.disabled = true;
    try {
      const res = await api('/api/rewrite', {
        method: 'POST',
        body: JSON.stringify({ q: state.q, category: state.category, status: state.status || 'success' }),
      });
      if (!res.enqueued) { toast('没有可改写的记录（需先抓取成功）', 'warn'); return; }
      toast(`已加入改写队列：${res.enqueued} 篇`, 'ok');
      await refreshGroups();
      startRewritePolling(res.ids || []);
    } finally { btn.disabled = false; }
  }));
  on('#btn-export-all', 'click', safe(async () => {
    const btn = $('#btn-export-all');
    btn.disabled = true;
    try {
      const res = await api('/api/export', {
        method: 'POST',
        body: JSON.stringify({ q: state.q, category: state.category, status: 'success' }),
      });
      toast(`已导出 ${res.exported || 0} 篇到 output/`, 'ok');
      await Promise.all([refreshStats(), refreshGroups()]);
    } finally { btn.disabled = false; }
  }));
  on('#btn-media-all', 'click', safe(async () => {
    const btn = $('#btn-media-all');
    btn.disabled = true;
    try {
      const res = await api('/api/media/collect', {
        method: 'POST',
        body: JSON.stringify({
          q: state.q, category: state.category,
          image_count: state.imageCount, video_count: state.videoCount,
        }),
      });
      if (!res.enqueued) { toast('没有可收集素材的记录（需先抓取成功）', 'warn'); return; }
      toast(`已加入素材队列：${res.enqueued} 篇`, 'ok');
      await refreshGroups();
      startMediaPolling(Array.from(document.querySelectorAll('tr[data-id]'))
        .filter((tr) => ['queued', 'running'].includes(tr.dataset.mst))
        .map((tr) => Number(tr.dataset.id)));
    } finally { btn.disabled = false; }
  }));

  on('#groups', 'click', safe(onGroupClick));
  on('#groups', 'change', safe(onCatChange));

  on('#cache-type', 'click', (e) => {
    const seg = e.target.closest('.seg');
    if (!seg) return;
    $$('#cache-type .seg').forEach((s) => s.classList.remove('active'));
    seg.classList.add('active');
    state.cacheType = seg.dataset.type;
    state.cachePage = 1;
    refreshCache();
  });
  let ct = null;
  on('#cache-q', 'input', (e) => {
    clearTimeout(ct);
    ct = setTimeout(safe(() => { state.cacheQ = e.target.value.trim(); state.cachePage = 1; return refreshCache(); }), 300);
  });
  on('#btn-cache-more', 'click', safe(() => { state.cachePage += 1; return refreshCache(true); }));
  on('#btn-clear-cache', 'click', safe(async () => {
    if (!confirm('确定清空临时缓存区？已抓取的原文内容将被删除（列表记录保留）。')) return;
    await api('/api/cache', { method: 'DELETE' });
    await Promise.all([refreshCache(), refreshStats(), refreshGroups()]);
  }));
  on('#cache-list', 'click', safe(onCacheClick));
  on('#drawer-body', 'click', safe(onMediaDrawerClick));
  on('#drawer-body', 'click', safe(onRewriteDrawerClick));
  on('#drawer-body', 'click', safe(onLayoutDrawerClick));
  on('#drawer-body', 'change', safe(onLayoutDrawerChange));
  on('#drawer-body', 'change', safe(onMediaUploadChange));
  on('#drawer-close', 'click', closeDrawer);
  on('#drawer-mask', 'click', closeDrawer);

  // ---------------- 系统设置 ----------------
  on('#settings-sub', 'click', (e) => {
    const seg = e.target.closest('.seg');
    if (!seg) return;
    $$('#settings-sub .seg').forEach((s) => s.classList.remove('active'));
    seg.classList.add('active');
    switchSettingsSub(seg.dataset.sub);
  });
  on('#btn-pipeline-refresh', 'click', safe(refreshPipeline));
  on('#btn-pipeline-clean', 'click', safe(cleanupDelivered));
  on('#pipeline-board', 'click', safe(onPipelineClick));
  on('#btn-pipeline-run', 'click', safe(runPipelineAll));
  on('#btn-pub-refresh', 'click', safe(refreshPublishCenter));
  on('#pub-filter-status', 'change', safe(refreshPublishCenter));
  on('#pub-filter-account', 'change', safe(refreshPublishCenter));
  on('#publish-center', 'click', safe(async (e) => {
    const del = e.target.closest('[data-pub-del]');
    if (del) {
      if (!confirm('删除这条发布记录？')) return;
      await api(`/api/publish/tasks/${del.dataset.pubDel}`, { method: 'DELETE' });
      return refreshPublishCenter();
    }
    const retry = e.target.closest('[data-pub-retry]');
    if (retry) {
      try {
        await api(`/api/publish/tasks/${retry.dataset.pubRetry}/retry`, { method: 'POST' });
        toast('已重新入队', 'ok');
      } catch (err) { toast(err.message || '重试失败', 'error'); }
      return refreshPublishCenter();
    }
    const cancel = e.target.closest('[data-pub-cancel]');
    if (cancel) {
      try {
        await api(`/api/publish/tasks/${cancel.dataset.pubCancel}/cancel`, { method: 'POST' });
        toast('已请求取消', 'ok');
      } catch (err) { toast(err.message || '取消失败', 'error'); }
      return refreshPublishCenter();
    }
  }));
  on('#btn-cleanup-scan', 'click', safe(scanCleanup));
  on('#btn-cleanup-vacuum', 'click', safe(vacuumDb));
  on('#cleanup-body', 'click', safe((e) => {
    const btn = e.target.closest('[data-clean]');
    if (btn) return runCleanup(btn.dataset.clean);
  }));
  on('#llm-cat', 'click', safe((e) => {
    const seg = e.target.closest('.seg');
    if (!seg) return;
    $$('#llm-cat .seg').forEach((s) => s.classList.remove('active'));
    seg.classList.add('active');
    cfgState.llmCat = seg.dataset.cat;
    return refreshLlm();
  }));
  on('#btn-llm-add', 'click', safe(() => openLlmForm(null)));
  on('#llm-list', 'click', safe(onLlmListClick));
  on('#f-provider', 'change', onProviderChange);
  on('#modal-save', 'click', safe(saveLlmForm));
  on('#modal-test', 'click', safe(testLlmForm));
  on('#modal-cancel', 'click', closeModal);
  on('#modal-close', 'click', closeModal);
  on('#modal-mask', 'click', closeModal);
  on('#btn-settings-save', 'click', safe(saveSettingsForm));
  on('#btn-settings-reset', 'click', safe(resetSettingsForm));
  // ---------------- 账号管理 ----------------
  on('#acc-platform', 'click', safe((e) => {
    const seg = e.target.closest('.seg');
    if (!seg) return;
    $$('#acc-platform .seg').forEach((s) => s.classList.remove('active'));
    seg.classList.add('active');
    accState.platform = seg.dataset.plat;
    return refreshAccounts();
  }));
  on('#btn-acc-add', 'click', safe(() => openAccountForm(null)));
  on('#acc-list', 'click', safe(onAccountClick));
  // 点空白处收起「更多」浮层：浮层是绝对定位的，不会再挤压卡片
  document.addEventListener('click', (e) => {
    $$('.acc-more[open]').forEach((d) => {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  on('#acc-modal-save', 'click', safe(saveAccountForm));
  on('#acc-modal-test', 'click', safe(accModalLogin));
  on('#acc-modal-cancel', 'click', closeAccModal);
  on('#acc-modal-close', 'click', closeAccModal);
  on('#acc-modal-mask', 'click', closeAccModal);

  on('#settings-groups', 'click', safe((e) => {
    // 提示词面板：切平台块 / 切换标题·正文·润色页签
    const blockBtn = e.target.closest('[data-pblock]');
    if (blockBtn) {
      stashSettingsInputs();
      cfgState.pblock = blockBtn.dataset.pblock;
      const g = (cfgState.promptGroups || []).find(
        (x) => `${x.platform}:${x.form}` === cfgState.pblock);
      cfgState.pstage = (g && g.stages[0] ? g.stages[0].stage : 'body');
      renderSettings();
      return;
    }
    const stageBtn = e.target.closest('[data-pstage]');
    if (stageBtn) {
      stashSettingsInputs();
      cfgState.pstage = stageBtn.dataset.pstage;
      renderSettings();
      return;
    }
    const probeKey = e.target.dataset.probe;
    if (probeKey) return probeSetting(probeKey, e.target.dataset.prober || 'bin');
    const resetKey = e.target.dataset.reset;
    if (resetKey) return resetSetting(resetKey);
  }));
}

async function onGroupClick(e) {
  const group = e.target.closest('.group');
  if (!group) return;
  const date = group.dataset.date;
  const act = e.target.dataset.act;

  if (act === 'toggle') { group.classList.toggle('collapsed'); return; }
  if (act === 'fetch-date') {
    const res = await api('/api/fetch', { method: 'POST', body: JSON.stringify({ date }) });
    if (!res.enqueued) { alert('该日期没有需要抓取的记录'); return; }
    await refreshGroups();
    startPolling();
    return;
  }
  if (act === 'media-date') {
    const res = await api('/api/media/collect', {
      method: 'POST',
      body: JSON.stringify({ date, image_count: state.imageCount, video_count: state.videoCount }),
    });
    if (!res.enqueued) { alert('该日期没有可收集素材的记录'); return; }
    toast(`已加入素材队列：${res.enqueued} 篇`, 'ok');
    await refreshGroups();
    startMediaPolling(Array.from(document.querySelectorAll('tr[data-id]'))
      .filter((tr) => ['queued', 'running'].includes(tr.dataset.mst))
      .map((tr) => Number(tr.dataset.id)));
    return;
  }
  if (act === 'del-date') {
    const st = state.groupState.get(date);
    const total = st ? st.total : (group.dataset.total || 0);
    if (!confirm(`确定删除 ${date || '未标注日期'} 的全部 ${total || ''} 条文章？\n缓存原文、素材文件、导出目录会一并删除，不可恢复。`)) return;
    const res = await api('/api/articles', { method: 'DELETE', body: JSON.stringify({ date }) });
    toast(`已删除 ${res.deleted} 条文章`, 'ok');
    await refreshGroups();
    refreshStats();
    return;
  }
  if (act === 'page-prev' || act === 'page-next') {
    const st = state.groupState.get(date);
    if (!st) return;
    const pages = Math.max(1, Math.ceil((st.total || 0) / PAGE_SIZE));
    const page = Math.min(pages, Math.max(1, (st.page || 1) + (act === 'page-next' ? 1 : -1)));
    if (page === st.page) return;
    const data = await api('/api/articles?' + pagerParams(date, page));
    st.page = page;
    st.items = data.items;
    st.total = data.total;
    st.page_size = PAGE_SIZE;
    state.groupState.set(date, st);
    if (group.classList.contains('collapsed')) group.classList.remove('collapsed');
    const body = group.querySelector('.group-body');
    if (body) body.innerHTML = renderTable(st.items) + renderPager(st);
    startPolling();
    return;
  }

  const tr = e.target.closest('tr[data-id]');
  if (!tr) return;
  const id = Number(tr.dataset.id);
  if (act === 'fetch') {
    await api('/api/fetch', { method: 'POST', body: JSON.stringify({ ids: [id] }) });
    tr.querySelector('[data-role=status]').innerHTML = statusTag('queued');
    startPolling();
  } else if (act === 'view') {
    let cid = tr.dataset.cid ? Number(tr.dataset.cid) : 0;
    if (!cid) {
      // 兜底：向服务端取一次最新 content_id，避免页面状态滞后
      const s = await api('/api/fetch/status', { method: 'POST', body: JSON.stringify({ ids: [id] }) });
      cid = (s.items[id] || {}).content_id || 0;
      if (cid) tr.dataset.cid = cid;
    }
    if (!cid) { toast('尚未抓取到原文，请先点击「抓取」', 'warn'); return; }
    openCache(cid);
  } else if (act === 'export') {
    const res = await api(`/api/export/${id}`, { method: 'POST' });
    if (!res.ok) { toast('导出失败：' + (res.error || '未知原因'), 'warn'); return; }
    tr.dataset.dir = res.dir || '';
    toast(`已导出到 output/${res.dir}`, 'ok');
    refreshStats();
  } else if (act === 'media') {
    // 采过（成功 / 无素材 / 失败）就进抽屉：查看、上传本地素材、换词重采都在里面
    if (MEDIA_DONE_STATES.includes(tr.dataset.mst || '')) { await openMedia(id); return; }
    const res = await api('/api/media/collect', {
      method: 'POST',
      body: JSON.stringify({ ids: [id], image_count: state.imageCount, video_count: state.videoCount }),
    });
    if (!res.enqueued) { toast('该记录无法收集素材，请先抓取原文', 'warn'); return; }
    tr.dataset.mst = 'queued';
    const btn = tr.querySelector('[data-act=media]');
    if (btn) btn.textContent = '收集中…';
    mediaAutoOpen.add(id);
    startMediaPolling([id]);
    toast('已加入素材队列，收集完成后自动打开', 'ok');
  } else if (act === 'media-retry') {
    if (!confirm('重新采集会清空这篇现有的素材，再按标题/关键词重新检索；' +
      '已交付的成稿会用新素材重建。继续？')) return;
    const res = await api('/api/media/collect', {
      method: 'POST',
      body: JSON.stringify({ ids: [id], image_count: state.imageCount, video_count: state.videoCount }),
    });
    if (!res.enqueued) { toast('该记录无法收集素材，请先抓取原文', 'warn'); return; }
    tr.dataset.mst = 'queued';
    const mbtn = tr.querySelector('[data-act=media]');
    if (mbtn) { mbtn.textContent = '收集中…'; mbtn.className = 'btn small ghost'; }
    const rbtn = tr.querySelector('[data-act=media-retry]');
    if (rbtn) rbtn.remove();
    mediaAutoOpen.add(id);
    startMediaPolling([id]);
    toast('已重新加入素材队列，完成后自动打开结果', 'ok');
  } else if (act === 'rewrite') {
    await startRewrite([id], tr);
  } else if (act === 'rw-view') {
    openRewriteCompare(id);
  } else if (act === 'layout') {
    await openLayout(id);
  } else if (act === 'del') {
    if (!confirm('确定删除该文章？\n缓存原文、素材文件、导出目录会一并删除，不可恢复。')) return;
    await api(`/api/articles/${id}`, { method: 'DELETE' });
    const group = tr.closest('.group');
    tr.remove();
    if (group) {
      const st = state.groupState.get(group.dataset.date);
      if (st) { st.total = Math.max(0, st.total - 1); st.items = st.items.filter((it) => it.id !== id); }
      const meta = group.querySelectorAll('.group-meta span');
      if (meta[0]) meta[0].textContent = `共 ${Math.max(0, (st ? st.total : 0))} 条`;
      const body = group.querySelector('.group-body');
      if (st && body) {
        body.innerHTML = renderTable(st.items) + renderPager(st);
        // 删掉最后一页最后一条时回退到上一页，避免停在空页
        if (!st.items.length && st.page > 1) { await refreshGroups(); return; }
      }
      if (!group.querySelector('tr[data-id]') && !(st && st.items.length)) group.remove();
    }
    refreshStats();
    toast('已删除', 'ok');
  }
}

function renderRow(r) {
  return rowHtml(r);
}

async function onCatChange(e) {
  if (e.target.dataset.act !== 'cat') return;
  const tr = e.target.closest('tr[data-id]');
  const id = Number(tr.dataset.id);
  const value = e.target.value.trim();
  await api(`/api/articles/${id}`, { method: 'PATCH', body: JSON.stringify({ category: value }) });
  e.target.classList.add('saved');
  loadDicts();
}

/* ---------------- 抓取状态轮询 ---------------- */
function startPolling() {
  // 收集当前页面里处于排队/抓取中的行
  $$('tr[data-id]').forEach((tr) => {
    const tag = tr.querySelector('[data-role=status] .tag');
    if (tag && ['queued', 'fetching'].includes(tag.className.replace('tag ', '').trim())) {
      state.polling.add(Number(tr.dataset.id));
    }
  });
  if (!state.pollTimer) state.pollTimer = setInterval(pollStatus, 1200);
}

async function pollStatus() {
  if (!state.polling.size) {
    clearInterval(state.pollTimer); state.pollTimer = null;
    await Promise.all([refreshStats(), updateGroupStats(), refreshCache()]);
    return;
  }
  const ids = Array.from(state.polling);
  const res = await api('/api/fetch/status', { method: 'POST', body: JSON.stringify({ ids }) });
  let finished = 0;
  ids.forEach((id) => {
    const item = res.items[id];
    if (!item) { state.polling.delete(id); return; }
    const tr = document.querySelector(`tr[data-id="${id}"]`);
    if (tr) {
      tr.querySelector('[data-role=status]').innerHTML = statusTag(item.fetch_status, item.fetch_error);
      // 关键：同步 content_id 与抓取状态，否则「原文」按钮拿到的还是旧值
      tr.dataset.cid = item.content_id || '';
      tr.dataset.fst = item.fetch_status || '';
      const btn = tr.querySelector('[data-act=fetch]');
      if (btn) btn.textContent = item.fetch_status === 'success' ? '重新抓取' : '抓取';
      const viewBtn = tr.querySelector('[data-act=view]');
      if (viewBtn) viewBtn.disabled = !item.content_id;
    }
    if (item.fetch_status === 'success' || item.fetch_status === 'failed') {
      state.polling.delete(id);
      finished += 1;
    }
  });
  updateQueueInfo(res.queue);
  // 有任务完成时立刻刷新统计与缓存区，无需手动刷新页面
  if (finished) {
    refreshStats();
    if (state.view === 'cache') refreshCache();
  }
}

/* ---------------- 自动刷新：把整个页面变成「活的」 ---------------- */
const autoState = { busy: false, idle: 0 };
const AUTO_MS = 4000;

function initAutoRefresh() {
  const btn = $('#auto-refresh');
  const paint = () => {
    if (!btn) return;
    btn.textContent = `自动刷新：${state.autoRefresh ? '开' : '关'}`;
    btn.classList.toggle('off', !state.autoRefresh);
  };
  paint();
  if (btn) {
    btn.addEventListener('click', () => {
      state.autoRefresh = !state.autoRefresh;
      localStorage.setItem('autonews.autoRefresh', state.autoRefresh ? '1' : '0');
      paint();
      toast(state.autoRefresh ? '已开启自动刷新（每 4 秒）' : '已关闭自动刷新', 'ok');
      if (state.autoRefresh) autoTick();
    });
  }
  if (state.autoTimer) clearInterval(state.autoTimer);
  state.autoTimer = setInterval(safe(autoTick), AUTO_MS);
  setTimeout(safe(autoTick), 500);
}

/* 从当前页面的行状态恢复轮询：切页签回来、上传完表格后都不会「停更」 */
function resumePollingFromDom() {
  const fetchIds = [];
  const mediaIds = [];
  const rewriteIds = [];
  $$('tr[data-id]').forEach((tr) => {
    const id = Number(tr.dataset.id);
    if (['queued', 'fetching'].includes(tr.dataset.fst || '')) fetchIds.push(id);
    if (['queued', 'running'].includes(tr.dataset.mst || '')) mediaIds.push(id);
    if (['queued', 'running'].includes(tr.dataset.rst || '')) rewriteIds.push(id);
  });
  if (fetchIds.length) {
    fetchIds.forEach((i) => state.polling.add(i));
    if (!state.pollTimer) state.pollTimer = setInterval(safe(pollStatus), 1500);
  }
  if (mediaIds.length) startMediaPolling(mediaIds);
  if (rewriteIds.length) startRewritePolling(rewriteIds);
  return fetchIds.length + mediaIds.length + rewriteIds.length;
}

function isEditingField() {
  const el = document.activeElement;
  return !!(el && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName));
}

async function autoTick() {
  if (!state.autoRefresh || document.hidden || autoState.busy) return;
  autoState.busy = true;
  try {
    if (state.view === 'news') {
      const active = resumePollingFromDom();
      if (active) {
        autoState.idle = 0;
      } else {
        autoState.idle += 1;
        // 没有进行中的任务时降频：每 3 个周期同步一次统计与分组头
        if (autoState.idle % 3 === 0) await Promise.all([refreshStats(), updateGroupStats()]);
      }
    } else if (state.view === 'pipeline') {
      if (!isEditingField()) await refreshPipeline();
    } else if (state.view === 'publish') {
      if (!isEditingField()) await refreshPublishCenter();
    } else if (state.view === 'cache') {
      await refreshStats();
    }
  } catch (e) {
    // 自动刷新失败不打扰用户（断网/服务重启时会连续失败）
  } finally {
    autoState.busy = false;
  }
}

async function updateGroupStats() {
  const params = new URLSearchParams({ q: state.q, status: state.status, category: state.category });
  const data = await api('/api/articles/dates?' + params.toString());
  data.items.forEach((d) => {
    const group = document.querySelector(`.group[data-date="${d.date}"]`);
    if (!group) return;
    group.querySelector('.group-meta').innerHTML =
      `<span>共 ${d.total} 条</span><span>未抓取 ${d.pending}</span>
       <span class="muted">成功 ${d.success}</span><span class="muted">失败 ${d.failed}</span>
       <span>阅读 ${fmtNum(d.reads)}</span>`;
  });
}

/* ---------------- 素材收集轮询 ---------------- */
const mediaPolling = new Set();
const mediaAutoOpen = new Set();   // 收集完成后自动打开抽屉
let mediaTimer = null;

function startMediaPolling(ids) {
  ids.forEach((i) => mediaPolling.add(Number(i)));
  if (!mediaTimer) mediaTimer = setInterval(pollMedia, 2500);
}

async function pollMedia() {
  if (!mediaPolling.size) {
    clearInterval(mediaTimer); mediaTimer = null;
    refreshStats();
    return;
  }
  const ids = Array.from(mediaPolling);
  const res = await api('/api/media/status', { method: 'POST', body: JSON.stringify({ ids }) });
  ids.forEach((id) => {
    const it = res.items[id];
    if (!it) { mediaPolling.delete(id); return; }
    const tr = document.querySelector(`tr[data-id="${id}"]`);
    if (tr) {
      tr.dataset.mst = it.media_status || 'pending';
      tr.dataset.mc = it.media_count || 0;
      tr.dataset.dir = it.output_dir || '';
      const btn = tr.querySelector('[data-act=media]');
      if (btn) {
        btn.textContent = it.media_status === 'success'
          ? `素材${it.media_count ? `(${it.media_count})` : ''}`
          : (MEDIA_TEXT[it.media_status] || '素材');
        btn.className = `btn small ${it.media_status === 'success' ? 'accent' : 'ghost'}`;
      }
      if (it.media_status === 'failed' && it.media_error) {
        const cell = tr.querySelector('.col-title');
        if (cell && !cell.querySelector('.row-msg')) {
          cell.insertAdjacentHTML('beforeend', `<div class="row-msg">${escapeHtml(it.media_error)}</div>`);
        }
      }
      // 采完/清空后把「重采」入口补回来，不用刷新页面
      const ops = tr.querySelector('.ops');
      if (ops && MEDIA_DONE_STATES.includes(it.media_status)
        && !ops.querySelector('[data-act=media-retry]')) {
        const mbtn = ops.querySelector('[data-act=media]');
        if (mbtn) mbtn.insertAdjacentHTML('afterend', MEDIA_RETRY_BTN);
      }
    }
    if (['success', 'failed', 'empty'].includes(it.media_status)) {
      mediaPolling.delete(id);
      if (mediaAutoOpen.has(id) && it.media_status === 'success') {
        mediaAutoOpen.delete(id);
        openMedia(id);
      } else if (isMediaDrawerOpen(id)) {
        // 在素材抽屉里点「重新收集 / 换关键词重采」，采完直接刷新抽屉
        openMedia(id);
      }
      mediaAutoOpen.delete(id);
    }
  });
}

/* ---------------- 素材抽屉 ---------------- */
function isMediaDrawerOpen(articleId) {
  const drawer = $('#drawer');
  if (!drawer || drawer.classList.contains('hidden')) return false;
  const title = $('#drawer-title');
  const body = $('#drawer-body');
  return !!title && title.textContent === '素材收集结果'
    && Number((body && body.dataset.article) || 0) === Number(articleId);
}

async function openMedia(articleId) {
  const d = await api(`/api/media/${articleId}`);
  const images = d.items.filter((x) => x.kind === 'image');
  const videos = d.items.filter((x) => x.kind === 'video');
  const frames = d.items.filter((x) => x.kind === 'frame');
  const imgTarget = Number(d.image_target || 0);
  const imgMin = Number(d.image_min || 0);
  const shortOfMin = imgMin > 0 && images.length < imgMin;
  const statusText = { pending: '未收集', queued: '排队中', running: '收集中', success: '已完成',
                       failed: '失败', empty: '无可用素材（正常）' };
  $('#drawer-title').textContent = '素材收集结果';
  $('#drawer-body').innerHTML = `
    <div class="meta-grid">
      <div>状态<b>${escapeHtml(statusText[d.media_status] || d.media_status || '未收集')}</b></div>
      <div>图片素材<b>${images.length} 张${
        imgTarget ? `（目标 ${imgTarget}${imgMin ? ` · 保底 ${imgMin}` : ''}）` : ''}</b>${
        shortOfMin ? ' <span class="tag failed">不足保底，建议重采</span>' : ''}</div>
      <div>原文平台<b>${escapeHtml(d.origin_platform || '-')}</b></div>
      <div>输出目录<b>${escapeHtml(d.dir ? 'output/' + d.dir : '-')}</b></div>
      <div style="grid-column:1/-1">标题检索词<b>${
        d.title_query ? `<span class="chip">${escapeHtml(d.title_query)}</span>` : '<span class="muted">-</span>'}</b></div>
      <div style="grid-column:1/-1">补充关键词<b>${
        (d.keywords || []).length
          ? d.keywords.map((k) => `<span class="chip">${escapeHtml(k)}</span>`).join('')
          : '<span class="muted">-</span>'}</b></div>
    </div>
    ${d.media_error ? `<div class="drawer-note">${escapeHtml(d.media_error)}</div>` : ''}
    <div class="drawer-actions">
      <button class="btn small primary" data-mact="upload"
        title="自动采的都不合用时，直接从电脑选图 / 选视频补进来">上传本地素材</button>
      <input type="file" id="media-upload-input" class="hidden" multiple accept="image/*,video/*" />
      <button class="btn small" data-mact="collect">重新收集</button>
      <button class="btn small accent" data-mact="kw-collect"
        title="换一批检索词重搜，适合当前素材和文章对不上时">换关键词重采</button>
      <button class="btn small ghost" data-mact="clear">清空素材</button>
      <button class="btn small ghost" data-mact="refresh">刷新</button>
    </div>
    <div class="section-title">图片素材（${images.length}）</div>
    ${images.length ? `<div class="media-grid">${images.map(mediaCard).join('')}</div>`
      : '<div class="empty">暂无图片素材，可点上方「上传本地素材」自己补</div>'}
    <div class="section-title">视频素材（${videos.length}）</div>
    ${videos.length ? videos.map(videoCard).join('') : '<div class="empty">暂无视频素材，可点上方「上传本地素材」自己补</div>'}
    ${frames.length ? `<div class="section-title">视频抽帧（${frames.length}）</div>
      <div class="drawer-note">抽帧会并入排版图片池，与正式图片一起按相关度排序；自动排版至少留 1 张名额给抽帧，同一视频最多取 2 张（都可在系统设置里改），排版页也能手动插入。</div>
      <div class="media-grid">${frames.map(mediaCard).join('')}</div>` : ''}
  `;
  $('#drawer-body').dataset.article = articleId;
  $('#drawer-body').dataset.keywords = JSON.stringify(d.keywords || []);
  openDrawer();
}

function matchTag(m) {
  const v = Number(m.match || 0);
  if (!v) return '';
  return ` <span class="tag ${v >= 0.3 ? 'success' : v >= 0.12 ? 'fetching' : 'failed'}">相关度 ${Math.round(v * 100)}%</span>`;
}
const PROVIDER_TEXT = { origin: '原文配图', news: '同源报道', baidu: '百度图片', bing: 'Bing 图片', pexels: 'Pexels', unsplash: 'Unsplash', bilibili: '哔哩哔哩', upload: '本地上传' };
// 被裁掉的边缘文字条（水印 / 台标 / 字幕），见 app/media/process.py::strip_text_bands
const BAND_TEXT = { top: '顶部', bottom: '底部', left: '左侧', right: '右侧' };
function bandTag(m) {
  const b = m.trimmed_bands || [];
  if (!b.length) return '';
  const names = b.map((x) => BAND_TEXT[x] || x).join('、');
  return ` <span class="tag success" title="已裁掉边缘文字条">已去字样(${escapeHtml(names)})</span>`;
}
function mediaCard(m) {
  const meta = [m.platform, m.width ? `${m.width}×${m.height}` : '', fmtSize(m.bytes || 0)]
    .filter(Boolean).map(escapeHtml).join(' · ') + matchTag(m) + bandTag(m);
  return `<div class="media-card">
    <img src="${escapeHtml(m.url)}" loading="lazy" referrerpolicy="no-referrer" alt="${escapeHtml(m.keyword || '')}" />
    <div class="m">${meta}</div>
    <div class="m muted">${escapeHtml(PROVIDER_TEXT[m.provider] || m.provider || '')} ${escapeHtml(m.keyword ? '· ' + m.keyword : '')}</div>
    <a class="btn small ghost" href="${escapeHtml(m.url)}" target="_blank" rel="noreferrer">查看</a>
    <a class="btn small ghost" href="${escapeHtml(m.url)}" download="${escapeHtml(m.name || '')}">下载</a>
    <button class="btn small ghost" data-mact="del-asset" data-asset="${m.id}" title="删除这张图">删除</button>
  </div>`;
}

function videoCard(v) {
  return `<div class="video-card">
    <video src="${escapeHtml(v.url)}" controls preload="metadata" playsinline></video>
    <div class="m">${escapeHtml(v.title || v.name || '')}</div>
    <div class="m muted">${escapeHtml(v.platform || '')} · ${v.duration ? v.duration + 's' : ''}${v.start ? `（从第 ${escapeHtml(String(v.start))}s 起）` : ''} · ${fmtSize(v.bytes || 0)}${matchTag(v)}</div>
    <a class="btn small ghost" href="${escapeHtml(v.url)}" target="_blank" rel="noreferrer">查看</a>
    <a class="btn small ghost" href="${escapeHtml(v.url)}" download="${escapeHtml(v.name || '')}">下载</a>
    <button class="btn small ghost" data-mact="del-asset" data-asset="${v.id}" title="删除这个视频">删除</button>
  </div>`;
}

async function onMediaDrawerClick(e) {
  const act = e.target.dataset.mact;
  if (!act) return;
  const articleId = Number($('#drawer-body').dataset.article || 0);
  if (!articleId) return;
  if (act === 'refresh') { await openMedia(articleId); return; }
  if (act === 'upload') {
    const input = $('#media-upload-input');
    if (input) input.click();
    return;
  }
  if (act === 'collect') {
    await api('/api/media/collect', { method: 'POST', body: JSON.stringify({ ids: [articleId] }) });
    toast('已重新加入素材队列', 'ok');
    startMediaPolling([articleId]);
    return;
  }
  if (act === 'kw-collect') {
    let cur = [];
    try { cur = JSON.parse($('#drawer-body').dataset.keywords || '[]') || []; } catch (e) { cur = []; }
    const input = prompt(
      '输入检索关键词（逗号分隔，会替代自动提取的关键词层，标题检索不变）\n留空＝恢复自动提取',
      cur.join('，'));
    if (input === null) return;
    const keywords = input.split(/[,，、\s]+/).map((s) => s.trim()).filter(Boolean);
    await api('/api/media/collect', {
      method: 'POST',
      body: JSON.stringify({ ids: [articleId], keywords }),
    });
    toast(keywords.length ? '已按新关键词重新采集' : '已恢复自动关键词并重新采集', 'ok');
    startMediaPolling([articleId]);
    return;
  }
  if (act === 'del-asset') {
    const assetId = Number(e.target.dataset.asset || 0);
    if (!assetId) return;
    if (!confirm('确定删除这张素材？文件会一并删除，不可恢复。')) return;
    await api(`/api/media/${articleId}/assets/${assetId}`, { method: 'DELETE' });
    await openMedia(articleId);
    await Promise.all([refreshStats(), refreshGroups()]);
    return;
  }
  if (act === 'clear') {
    if (!confirm('确定清空该文章的素材文件？')) return;
    await api(`/api/media/${articleId}`, { method: 'DELETE' });
    await openMedia(articleId);
    await Promise.all([refreshStats(), refreshGroups()]);
  }
}

/* ---------------- 本地上传素材 ---------------- */
async function onMediaUploadChange(e) {
  const input = e.target;
  if (!input || input.id !== 'media-upload-input' || !input.files || !input.files.length) return;
  const articleId = Number($('#drawer-body').dataset.article || 0);
  const files = Array.from(input.files);
  input.value = '';
  if (!articleId) return;
  const form = new FormData();
  files.forEach((f) => form.append('files', f));
  toast(`正在上传 ${files.length} 个素材…`);
  let data;
  try {
    const res = await fetch(`/api/media/${articleId}/upload`, { method: 'POST', body: form });
    data = await res.json();
    if (!res.ok) { toast('上传失败：' + (data.detail || res.status), 'warn'); return; }
  } catch (err) {
    toast('上传失败：' + (err.message || err), 'warn');
    return;
  }
  const skipped = data.skipped || [];
  const skipMsg = skipped.length
    ? `；跳过 ${skipped.length} 个（${skipped.map((s) => `${s.filename}：${s.reason}`).join('，')}）`
    : '';
  toast(`已添加 ${data.added} 个素材${skipMsg}`, data.added ? 'ok' : 'warn');
  await openMedia(articleId);
  await Promise.all([refreshGroups(), refreshStats()]);
}

/* ---------------- 缓存区 ---------------- */
async function refreshCache(append = false) {
  const params = new URLSearchParams({ q: state.cacheQ, content_type: state.cacheType, page: state.cachePage, page_size: 30 });
  const data = await api('/api/cache?' + params.toString());
  const list = $('#cache-list');
  const html = data.items.map((c) => `<div class="cache-card" data-id="${c.id}" data-aid="${c.article_id || ''}"${c.rid ? ` data-rid="${c.rid}"` : ''}>
      <div class="t">${escapeHtml(c.title || '(无标题)')}</div>
      <div class="s">${escapeHtml(c.source || '-')} · ${escapeHtml(c.category || '未分类')} · ${escapeHtml((c.publish_time || '').slice(0, 16) || '-')}</div>
      <span class="type-tag ${escapeHtml(c.content_type)}">${escapeHtml(c.content_type)}</span>
      <div class="summary">${escapeHtml(c.summary || '（无摘要）')}</div>
      <div class="s">正文 ${c.text_len || 0} 字 · ${escapeHtml(c.fetched_at)}</div>
      <div class="foot">
        <button class="btn small" data-act="view">查看原文</button>
        <button class="btn small accent" data-act="rewrite"${c.content_type === '文章' && c.article_id ? '' : ' disabled'}>改写</button>
        <button class="btn small ghost" data-act="rw-view"${c.rid ? '' : ' hidden'}>对比</button>
        <button class="btn small ${c.rid ? '' : 'ghost'}" data-act="layout"${c.rid ? '' : ' disabled'}>排版</button>
        <a class="btn small ghost" href="${escapeHtml(safeUrl(c.url))}" target="_blank" rel="noreferrer">原链接</a>
        <button class="btn small ghost" data-act="del">删除</button>
      </div>
    </div>`).join('');
  if (append) list.insertAdjacentHTML('beforeend', html);
  else list.innerHTML = html;
  $('#cache-empty').classList.toggle('hidden', (data.total > 0));
  $('#cache-more').classList.toggle('hidden', data.items.length === 0 || (state.cachePage * 30 >= data.total));
  $('#cache-badge').textContent = data.total;
}

async function onCacheClick(e) {
  const card = e.target.closest('.cache-card');
  if (!card) return;
  const id = Number(card.dataset.id);
  const act = e.target.dataset.act;
  if (act === 'view') { openCache(id); }
  if (act === 'rewrite') {
    const aid = Number(card.dataset.aid);
    if (!aid) { toast('这条缓存没有关联文章，无法改写', 'warn'); return; }
    await startRewrite([aid], card);
  }
  if (act === 'rw-view') {
    const aid = Number(card.dataset.aid);
    if (!aid) { toast('这条缓存没有关联文章', 'warn'); return; }
    await openRewriteCompare(aid);
  }
  if (act === 'layout') {
    const aid = Number(card.dataset.aid);
    if (!aid) { toast('这条缓存没有关联文章', 'warn'); return; }
    await openLayout(aid);
  }
  if (act === 'del') {
    await api(`/api/cache/${id}`, { method: 'DELETE' });
    await Promise.all([refreshCache(), refreshStats(), refreshGroups()]);
  }
}

async function openCacheByArticle(articleId) { /* 保留作兼容；现已用 tr.dataset.cid 跳转 */ }

async function openCache(cacheId) {
  const data = await api(`/api/cache/${cacheId}`);
  const d = data.detail || {};
  $('#drawer-title').textContent = d.title || data.title || '原文内容';
  $('#drawer-body').innerHTML = `
    <div class="meta-grid">
      <div>内容类型<b><span class="type-tag ${escapeHtml(data.content_type)}">${escapeHtml(data.content_type)}</span></b></div>
      <div>来源<b>${escapeHtml(data.source || '-')}</b></div>
      <div>分类<b>${escapeHtml(data.category || '-')}</b></div>
      <div>发布时间<b>${escapeHtml(data.publish_time || '-')}</b></div>
      <div>阅读 / 点赞<b>${fmtNum(data.read_count)} / ${fmtNum(data.like_count)}</b></div>
      <div>抓取时间<b>${escapeHtml(data.fetched_at)}</b></div>
      <div style="grid-column:1/-1">原文链接<b><a href="${escapeHtml(safeUrl(data.url))}" target="_blank" rel="noreferrer">${escapeHtml(data.url)}</a></b></div>
    </div>
    <div class="section-title">正文内容（${(d.text || '').length} 字）</div>
    <div class="article-text">${escapeHtml(d.text || '（未提取到正文）')}</div>
  `;
  openDrawer();
}

function openDrawer() {
  $('#drawer').classList.remove('hidden');
  $('#drawer-mask').classList.remove('hidden');
}
function closeDrawer() {
  $('#drawer').classList.add('hidden');
  $('#drawer-mask').classList.add('hidden');
  $('#drawer').classList.remove('wide');
}

/* ---------------- 文章改写 ---------------- */
const rwState = { timer: null, polling: new Set(), current: null, side: 'final', variants: [] };

async function startRewrite(ids, tr) {
  const res = await api('/api/rewrite', { method: 'POST', body: JSON.stringify({ ids }) });
  if (!res.enqueued) { toast('没有可改写的记录（需先抓取成功）', 'warn'); return; }
  (res.ids || ids).forEach((id) => {
    const row = document.querySelector(`tr[data-id="${id}"]`);
    if (row) { row.dataset.rst = 'queued'; const b = row.querySelector('[data-act=rewrite]'); if (b) b.textContent = '改写排队'; }
  });
  if (tr) { tr.dataset.rst = 'queued'; }
  toast(`已加入改写队列：${res.enqueued} 篇`, 'ok');
  startRewritePolling(res.ids || ids);
}

function startRewritePolling(ids) {
  ids.forEach((id) => rwState.polling.add(Number(id)));
  if (rwState.timer) return;
  const tick = async () => {
    if (!rwState.polling.size) { stopRewritePolling(); return; }
    const myIds = Array.from(rwState.polling);
    let data;
    try {
      data = await api('/api/rewrite/status', { method: 'POST', body: JSON.stringify({ ids: myIds }) });
    } catch (e) { return; }
    (myIds || []).forEach((id) => {
      const st = (data.items || {})[id];
      if (!st) return;
      updateRewriteRow(id, st);
      if (['success', 'failed'].includes(st.rewrite_status)) rwState.polling.delete(id);
    });
    if (!rwState.polling.size) {
      stopRewritePolling();
      refreshGroups();
      refreshStats();
      toast('改写队列已跑完', 'ok');
    }
  };
  rwState.timer = setInterval(safe(tick), 2000);
  tick();
}

function stopRewritePolling() {
  if (rwState.timer) clearInterval(rwState.timer);
  rwState.timer = null;
}

function updateRewriteRow(id, st) {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (tr) {
    tr.dataset.rst = st.rewrite_status || '';
    if (st.rewrite_id) tr.dataset.rid = st.rewrite_id;
    const btn = tr.querySelector('[data-act=rewrite]');
    if (btn) btn.textContent = REWRITE_TEXT[st.rewrite_status || 'pending'] || '改写';
    const view = tr.querySelector('[data-act=rw-view]');
    if (view) view.hidden = !st.rewrite_id;
  }
  const card = document.querySelector(`.cache-card[data-aid="${id}"]`);
  if (card) {
    const btn = card.querySelector('[data-act=rewrite]');
    if (btn) btn.textContent = REWRITE_TEXT[st.rewrite_status || 'pending'] || '改写';
    const view = card.querySelector('[data-act=rw-view]');
    if (view) view.hidden = !st.rewrite_id;
    if (st.rewrite_id) card.dataset.rid = st.rewrite_id;
  }
}

/* -------- 差异对比 -------- */
function splitSentences(t) {
  return String(t || '').split(/(?<=[。！？!?；;])|\n+/).map((s) => s.trim()).filter(Boolean);
}

function diffSegments(a, b) {
  const A = splitSentences(a);
  const B = splitSentences(b);
  const n = A.length;
  const m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0; let j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push({ t: 'same', x: A[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: 'del', x: A[i] }); i++; }
    else { out.push({ t: 'ins', x: B[j] }); j++; }
  }
  while (i < n) { out.push({ t: 'del', x: A[i] }); i++; }
  while (j < m) { out.push({ t: 'ins', x: B[j] }); j++; }
  return out;
}

function diffHtml(segments, side) {
  return segments.map((s) => {
    if (s.t === 'same') return `<span>${escapeHtml(s.x)}</span>`;
    if (side === 'old') {
      return s.t === 'del' ? `<span class="diff-del">${escapeHtml(s.x)}</span>` : '';
    }
    return s.t === 'ins' ? `<span class="diff-ins">${escapeHtml(s.x)}</span>` : '';
  }).join('');
}

async function openRewriteCompare(articleId, preferProblem = false) {
  const variants = await loadVariants(articleId);
  let it = null;
  if (variants.length) {
    const wanted = lyState.platform;
    const wantedForm = lyState.form;
    // 从看板异常列进来时优先展示「有问题」的那一版，别让人以为已经改好了
    it = (preferProblem
      ? variants.find((v) => v.sensitive_status === 'blocked')
      || variants.find((v) => v.status === 'failed')
      : null)
      || variants.find((v) => v.target_platform === wanted
        && (!wantedForm || variantForm(v) === wantedForm)) || variants[0];
  } else {
    const data = await api(`/api/rewrite/latest/${articleId}`);
    it = data.item;
  }
  if (!it) { toast('这篇还没有改写记录，先点「改写」', 'warn'); return; }
  rwState.variants = variants;
  rwState.current = it;
  rwState.side = 'final';
  renderRewriteCompare(it);
}

function renderRewriteCompare(it) {
  const draft = it.draft_text || '';
  const finalText = rwState.side === 'draft' ? draft : (it.final_text || draft);
  const origin = it.origin_text || '';
  const segs = diffSegments(origin, finalText);
  const del = segs.filter((s) => s.t === 'del').length;
  const ins = segs.filter((s) => s.t === 'ins').length;
  const same = segs.filter((s) => s.t === 'same').length;
  const titles = it.titles || [];
  const hasPolish = it.final_text && it.final_text !== draft;

  const variants = rwState.variants || [];
  const vtab = variants.length > 1
    ? `<div class="ly-platform-bar"><div class="segmented inline">${variants.map((v) =>
        `<button class="seg${String(v.id) === String(it.id) ? ' active' : ''}" data-rw-act="variant" data-rewrite="${v.id}">${
          escapeHtml(variantLabel(v))}</button>`).join('')}</div>
        <span class="muted">同一原文的各平台·形态版本：标题与正文都应当不同</span></div>`
    : '';

  const sensHits = it.sensitive_hits || [];
  const sensState = it.sensitive_status || '';
  const sensLabel = it.sensitive_label || '未校验';
  const sensCls = sensState === 'blocked' ? 'rd' : sensState ? 'gr' : '';
  const statusText = it.status === 'success'
    ? '完成'
    : (sensState === 'blocked' ? '敏感词未通过' : it.status);
  const sensTip = (h, extra) => (h.category ? h.category + '｜' : '')
    + (h.file || '') + ' · ' + extra;
  const hitChips = sensHits.map((h) => {
    const resolved = h.resolved === true;
    const cls = resolved ? 'sens-done'
      : (h.tier === 'case' ? 'sens-case' : h.tier === 'block' ? 'sens-block' : 'sens-warn');
    const tip = resolved
      ? sensTip(h, '已在修复轮清除')
      : (h.tier === 'block' || h.tier === 'case'
        ? sensTip(h, (h.snippets || [])[0] || '')
        : sensTip(h, '只提示不拦截'));
    const times = Number(h.count) > 1 ? `×${h.count}` : '';
    return `<span class="chip ${cls}" title="${escapeHtml(tip)}">`
      + `${escapeHtml(h.word)}${times}</span>`;
  }).join('');
  const needHuman = sensState === 'blocked' && sensHits.some((h) => h.resolved !== true
    && (h.tier === 'case' || h.category === '政治'));
  const sensPanel = sensHits.length ? `
    <div class="section-title">敏感词命中</div>
    <div class="sens-hits">${hitChips}</div>
    <div class="muted" style="font-size:12px;margin:-4px 0 10px">
      红色 = 硬敏感（不通过则不交付）；橙色 = 案件必要表述；黄色 = 提示级（不影响交付）；灰色 = 已在修复轮清除
    </div>
    ${needHuman ? `<div class="drawer-note">命中含案件必要表述或涉政内容：模型通常改不动（事实专名、案情必要词），
      建议人工确认后再决定是否发布。</div>` : ''}` : '';

  // 改写失败 / 敏感词未过：给出「重新改写」与「人工放行」两条出路，
  // 放行会保留命中词留痕（交付目录 meta.json 里也能查到）。
  const canAct = sensState === 'blocked' || it.status === 'failed';
  const opsPanel = canAct ? `
    <div class="sens-ops">
      <button class="btn small" data-rw-act="retry" data-aid="${it.article_id}">重新改写</button>
      ${sensState === 'blocked'
        ? `<button class="btn small accent" data-rw-act="waive" data-rid="${it.id}">人工放行</button>`
        : ''}
      <button class="btn small ghost" data-rw-act="hits">复制命中词</button>
      <span class="muted">放行后仍会记下命中词，交付目录 meta.json 可追溯</span>
    </div>` : '';

  $('#drawer-title').textContent = `改写对比 · ${it.origin_title || '(无标题)'}`;
  $('#drawer-body').innerHTML = `
    <div class="meta-grid">
      <div>模型<b>${escapeHtml(it.provider || '-')} / ${escapeHtml(it.model || '-')}</b></div>
      <div>状态<b>${escapeHtml(statusText)}</b></div>
      <div>敏感词<b class="${sensCls}">${escapeHtml(sensLabel)}${
        it.sensitive_rounds ? `（修复 ${it.sensitive_rounds} 轮）` : ''}</b></div>
      <div>字数<b>${it.words || 0}（原 ${Math.round((origin || '').replace(/\s/g, '').length)}）</b></div>
      <div>耗时<b>${escapeHtml(String(it.elapsed || 0))}s · 模型调用 ${it.attempts || 0} 次</b></div>
    </div>
    ${vtab}
    ${opsPanel}
    ${sensPanel}

    <div class="section-title">标题候选</div>
    <div class="title-orig"><span class="diff-del">${escapeHtml(it.origin_title || '')}</span><span class="muted">原标题</span></div>
    <div class="title-list">
      ${titles.length ? titles.map((t) => `
        <div class="title-row">
          <span class="chip">${escapeHtml(t.style)}</span>
          <span class="diff-ins">${escapeHtml(t.title)}</span>
          <div class="grow"></div>
          <button class="btn small ghost" data-rw-act="copy" data-target="title" data-text="${escapeHtml(t.title)}">复制</button>
          <button class="btn small ghost" data-rw-act="pick" data-title="${escapeHtml(t.title)}"
            ${it.used_title === t.title ? 'disabled' : ''}>${it.used_title === t.title ? '已采用' : '采用'}</button>
        </div>`).join('') : '<div class="muted">本次未生成标题候选（设置里开了「保留原标题」，或模型输出没能解析出标题）</div>'}
    </div>

    <div class="section-title bar">
      <span>正文对比</span>
      <span class="diff-stat">保留 ${same} 句 · 删除 <b class="rd">${del}</b> 句 · 新增 <b class="gr">${ins}</b> 句</span>
      ${hasPolish ? `<div class="segmented inline" id="rw-side">
        <button class="seg${rwState.side === 'final' ? ' active' : ''}" data-side="final">润色稿</button>
        <button class="seg${rwState.side === 'draft' ? ' active' : ''}" data-side="draft">初稿</button>
      </div>` : ''}
      <div class="diff-actions">
        <button class="btn small ghost" data-rw-act="copy" data-target="origin">复制原文</button>
        <button class="btn small ghost" data-rw-act="copy" data-target="draft"${draft ? '' : ' disabled'}>复制初稿</button>
        <button class="btn small ghost" data-rw-act="copy" data-target="final"${hasPolish ? '' : ' disabled'}>复制润色稿</button>
      </div>
    </div>
    <div class="diff-grid">
      <div class="diff-col"><div class="diff-head">原文</div><div class="diff-body">${diffHtml(segs, 'old')}</div></div>
      <div class="diff-col"><div class="diff-head">${rwState.side === 'draft' ? '初稿' : '润色稿'}</div><div class="diff-body">${diffHtml(segs, 'new')}</div></div>
    </div>
    ${it.error ? `<div class="drawer-note">${escapeHtml(it.error)}</div>` : ''}
  `;
  $('#drawer').classList.add('wide');
  openDrawer();
}

async function copyText(text, label) {
  if (!text) { toast(`${label} 内容为空`, 'warn'); return; }
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    // 非安全上下文（比如用局域网 IP 访问）下 clipboard API 不可用，退回 execCommand
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    ta.remove();
  }
  toast(ok ? `已复制${label}（${text.length} 字）` : '复制失败，请手动选中文本', ok ? 'ok' : 'error');
}

async function copyRewritePart(target, rawText) {
  const it = rwState.current;
  if (!it) return;
  const hitList = (it.sensitive_hits || []).map((h) =>
    `${h.word}\t${h.tier || ''}\t${h.category || ''}\t${h.file || ''}\t${h.field || ''}`);
  const table = {
    origin: [it.origin_text || '', '原文'],
    draft: [it.draft_text || '', '初稿'],
    final: [it.final_text || '', '润色稿'],
    title: [rawText || '', '标题'],
    hits: [hitList.join('\n'), '命中词'],
  };
  const hit = table[target];
  if (!hit) return;
  await copyText(hit[0], hit[1]);
}

async function retryRewrite(articleId) {
  const id = Number(articleId || 0);
  if (!id) return;
  if (!confirm('重新改写会覆盖这篇现有的改写稿，确定继续？')) return;
  await api(`/api/rewrite/${id}/retry`, { method: 'POST' });
  toast('已重新入队改写', 'ok');
  closeDrawer();
  if (state.view === 'pipeline') await refreshPipeline();
  else await refreshGroups();
}

async function waiveRewrite(rewriteId) {
  const note = prompt('放行备注（写清为什么放过这些词，可留空）：', '');
  if (note === null) return;                 // 点了取消
  const item = await api(`/api/rewrite/${rewriteId}/waive`,
    { method: 'POST', body: JSON.stringify({ note }) });
  rwState.current = item;
  renderRewriteCompare(item);
  toast('已人工放行，可到排版页继续成稿', 'ok');
  if (state.view === 'pipeline') await refreshPipeline();
}

async function pickRewriteTitle(title) {
  const it = rwState.current;
  if (!it) return;
  const res = await api(`/api/rewrite/${it.id}/title`, { method: 'PATCH', body: JSON.stringify({ title }) });
  toast('已采用该标题', 'ok');
  rwState.current = res;
  renderRewriteCompare(res);
}

async function onRewriteDrawerClick(e) {
  const side = e.target.closest('#rw-side .seg');
  if (side) {
    rwState.side = side.dataset.side;
    $$('#rw-side .seg').forEach((s) => s.classList.toggle('active', s === side));
    renderRewriteCompare(rwState.current);
    return;
  }
  const variant = e.target.closest('[data-rw-act="variant"]');
  if (variant) {
    const hit = (rwState.variants || []).find((v) => String(v.id) === String(variant.dataset.rewrite));
    if (hit) {
      rwState.current = hit;
      rwState.side = 'final';
      renderRewriteCompare(hit);
    }
    return;
  }
  const act = e.target.dataset.rwAct;
  if (act === 'pick') await pickRewriteTitle(e.target.dataset.title);
  if (act === 'copy') await copyRewritePart(e.target.dataset.target, e.target.dataset.text);
  if (act === 'hits') await copyRewritePart('hits');
  if (act === 'retry') await retryRewrite(e.target.dataset.aid);
  if (act === 'waive') await waiveRewrite(e.target.dataset.rid);
}

/* ---------------- 文章排版 ---------------- */
const lyState = {
  current: null, articleId: null, platform: '', form: '', variants: [], picking: null,
  editing: false, draftBlocks: null, hitWords: [], insertAt: null, copyHint: '',
};
const publishState = { accounts: null };

/* 一键复制：图片怎么带进剪贴板（与后端 layout.copy_image_mode 对应） */
const LY_COPY_MODES = [
  { value: 'inline', label: '图片内联' },
  { value: 'upload', label: '先传图床' },
  { value: 'text', label: '纯文字' },
];
const LY_COPY_HINT = {
  inline: '图片压缩后随剪贴板一起带走，粘贴时由平台编辑器自己上传（不依赖网络与账号）',
  upload: '先把图片传到该平台图床再复制，平台一定收；需要账号已登录，每张多等 1~2 秒',
  text: '只复制文字，图片位置留一行占位，图片自己拖进编辑器',
};
const LY_COPY_MODE_KEY = 'autonews.lyCopyMode';

function lyCopyMode(it) {
  const valid = (v) => LY_COPY_MODES.some((m) => m.value === v);
  const saved = localStorage.getItem(LY_COPY_MODE_KEY) || '';
  if (valid(saved)) return saved;
  const def = (it && it.copy && it.copy.mode) || 'inline';
  return valid(def) ? def : 'inline';
}
// 内容形态：头条 = 图文 / 微头条，百家 = 图文 / 动态（短稿，都是无标题）
// 短稿与图文互斥：原文 <400 字只产出微头条 + 动态，≥400 字只产出两组图文。
const FORM_BADGE_BY_PLATFORM = {
  toutiao: { article: '图文', micro: '微头条' },
  baijia: { article: '图文', micro: '动态' },
};
const FORM_BADGE = { article: '图文', micro: '微头条' };   // 无平台上下文时的叫法
const FORMS_BY_PLATFORM = { toutiao: ['article', 'micro'], baijia: ['article', 'micro'] };
// 有网页端自动发布入口的形态：头条图文/微头条、百家图文。
// 百家号动态先不自动发（平台没有可用的保存接口），成稿只交付到 output_finish，人工发。
const PUBLISHABLE_FORMS = { toutiao: ['article', 'micro'], baijia: ['article'] };
function formForPlatform(plat, form) {
  const forms = FORMS_BY_PLATFORM[plat] || ['article'];
  return forms.includes(form) ? form : 'article';
}
function formBadge(plat, form) {
  const f = form || 'article';
  return ((FORM_BADGE_BY_PLATFORM[plat] || {})[f]) || FORM_BADGE[f] || '图文';
}
function formPublishable(plat, form) {
  return (PUBLISHABLE_FORMS[plat] || ['article']).includes(form || 'article');
}
function variantForm(v) { return String((v && v.content_form) || 'article'); }
function variantKey(v) { return `${v && v.target_platform}:${variantForm(v)}`; }
function variantLabel(v) {
  const plat = (typeof PLATFORM_BADGE !== 'undefined' && PLATFORM_BADGE[v.target_platform])
    || v.target_platform || '';
  const form = formBadge(v.target_platform, variantForm(v));
  return `${plat}·${form}`;
}

// 极简 Markdown → HTML：只处理排版稿会用到的语法（标题 / 段落 / 图片 / video 标签 / 引用）
function mdToHtml(md) {
  const esc = (s) => escapeHtml(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  const out = [];
  let para = [];
  const flush = () => { if (para.length) { out.push(`<p>${para.join('<br/>')}</p>`); para = []; } };
  for (const raw of String(md || '').split('\n')) {
    const line = raw.trim();
    if (!line) { flush(); continue; }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) { flush(); out.push(`<h${Math.min(6, h[1].length + 2)}>${esc(h[2])}</h${Math.min(6, h[1].length + 2)}>`); continue; }
    if (/^<video\b/i.test(line)) { flush(); out.push(line); continue; }
    if (/^>/.test(line)) { flush(); out.push(`<blockquote>${esc(line.replace(/^>\s?/, ''))}</blockquote>`); continue; }
    const img = /^!\[(.*?)\]\((.*?)\)$/.exec(line);
    if (img) { flush(); out.push(`<figure><img src="${escapeHtml(img[2])}" alt="${escapeHtml(img[1])}" loading="lazy" />${img[1] ? `<figcaption>${escapeHtml(img[1])}</figcaption>` : ''}</figure>`); continue; }
    if (/^\*(.+)\*$/.test(line)) { flush(); out.push(`<p class="cap">${esc(line.replace(/^\*|\*$/g, ''))}</p>`); continue; }
    para.push(esc(line));
  }
  flush();
  return out.join('');
}

async function loadVariants(articleId) {
  try {
    const v = await api(`/api/rewrite/variants/${articleId}`);
    return (v.items || []).filter((x) => x.target_platform);
  } catch (e) { return []; }
}

async function openLayout(articleId, platform, form) {
  // 先看这篇有哪几个「平台 · 形态」版本：短稿（微头条 / 百家动态）与两组图文各自排版
  const variants = await loadVariants(articleId);
  lyState.variants = variants;
  const avail = variants.map(variantKey);
  let plat = platform || lyState.platform || '';
  let frm = form || lyState.form || '';
  let hit = variants.find((v) => variantKey(v) === `${plat}:${frm}`)
    || variants.find((v) => v.target_platform === plat);
  if (!hit || (plat && !avail.includes(`${plat}:${frm}`))) hit = variants[0] || null;
  if (hit) {
    plat = hit.target_platform;
    frm = variantForm(hit);
  }
  const params = new URLSearchParams();
  if (plat) params.set('platform', plat);
  if (frm) params.set('form', frm);
  const qs = params.toString() ? `?${params.toString()}` : '';
  let item = null;
  try {
    const data = await api(`/api/layout/${articleId}${qs}`);
    item = data.item;
  } catch (e) { item = null; }
  const wrong = !!(item && ((plat && item.platform && item.platform !== plat)
    || (frm && item.content_form && item.content_form !== frm)));
  if (!item || wrong) {
    try {
      const built = await api(`/api/layout/${articleId}${qs}`, { method: 'POST' });
      item = built.item;
    } catch (e) {
      if (!item) { toast(e.message || '排版失败', 'error'); return; }
      toast(e.message || '该平台版本还没有排版', 'warn');
    }
  }
  lyState.current = item;
  lyState.articleId = articleId;
  lyState.platform = plat;
  lyState.form = item && item.content_form ? item.content_form : frm;
  lyState.picking = null;
  // 切换平台版本 / 重新打开都从浏览态开始，别把上一份草稿带过来
  lyState.editing = false;
  lyState.draftBlocks = null;
  lyState.insertAt = null;
  lyState.hitWords = ((item && item.sensitive) || {}).words || [];
  const body = $('#drawer-body');
  if (body) body.scrollTop = 0;      // 打开排版页从顶部开始看
  renderLayout();
}

function layoutAsset(id) {
  return (lyState.current.assets || []).find((a) => a.id === Number(id)) || null;
}

function renderLayout() {
  const it = lyState.current;
  if (!it) return;
  // 每次微调都重渲染，滚动位置要留着，否则点一下按钮就跳回顶部
  const body = $('#drawer-body');
  const keepScroll = body ? body.scrollTop : 0;
  const editing = !!lyState.editing;
  const ps = it.placements || [];
  const on = ps.filter((p) => p.enabled);
  // 编辑态用草稿块（还没保存），其余时候都用已保存的块
  const blocks = editing && Array.isArray(lyState.draftBlocks)
    ? lyState.draftBlocks : (it.blocks || []);
  const heads = blocks.filter((b) => String(b.type).startsWith('h')).length;
  const paras = blocks.filter((b) => !String(b.type).startsWith('h')).length;
  const plan = it.plan || {};
  const unused = it.unused || [];
  const chars = editing
    ? blocks.reduce((n, b) => n + String(b.text || '').replace(/\s/g, '').length, 0)
    : ((it.plan && it.plan.chars) || (it.body_md || '').replace(/\s/g, '').length);
  const senBar = sensitiveBannerHtml(it.sensitive, lyState.hitWords);
  const targetImg = plan.target_images || 0;
  const targetVid = plan.target_videos || 0;
  const variants = lyState.variants || [];
  const plat = it.platform || lyState.platform || '';
  const form = it.content_form || lyState.form || 'article';
  const activeKey = `${plat}:${form}`;
  const entryLabel = variantLabel({ target_platform: plat, content_form: form });
  const platTabs = variants.length > 1
    ? `<div class="segmented inline" id="ly-platform">${variants.map((v) =>
        `<button class="seg${variantKey(v) === activeKey ? ' active' : ''}" data-ly-act="platform"
          data-platform="${escapeHtml(v.target_platform)}"
          data-form="${escapeHtml(variantForm(v))}">${escapeHtml(variantLabel(v))}</button>`).join('')}</div>`
    : `<span class="chip">${escapeHtml(entryLabel)}</span>`;

  const isShort = form === 'micro';
  const copyMode = lyCopyMode(it);
  const videoSlots = on.filter((p) => p.kind === 'video').length;
  const copyHint = `${LY_COPY_HINT[copyMode] || ''}${videoSlots
    ? `　·　正文里有 ${videoSlots} 个视频：粘贴带不过去，会用该视频的抽帧图占位，粘完请手动上传原片`
    : ''}`;
  lyState.copyHint = copyHint;
  const headText = isShort
    ? ((blocks.find((b) => !String(b.type).startsWith('h')) || {}).text || '').slice(0, 18)
    : '(无标题)';
  $('#drawer-title').textContent = `排版预览 · ${it.title || headText || '(无标题)'}`;
  body.innerHTML = `
    <div class="meta-grid">
      <div>发布入口<b>${escapeHtml(entryLabel)}${isShort
        ? (formPublishable(plat, form) ? '（无标题 · 只存草稿）' : '（无标题 · 后台人工发）')
        : '（需标题）'}</b></div>
      <div>正文<b>${chars} 字 / ${paras} 段（小标题 ${heads} 个）</b></div>
      <div>自动建议<b>${targetImg} 张图 · ${targetVid} 个视频</b></div>
      <div>已插入<b>${on.filter((p) => p.kind === 'image').length} 张图 · ${on.filter((p) => p.kind === 'video').length} 个视频</b></div>
      <div>可用素材<b>${(it.assets || []).length} 个（未使用 ${unused.length}）</b></div>
      <div>状态<b>${it.status === 'exported' ? '已导出' : '草稿'}</b><span id="ly-sync-hint" class="muted"></span></div>
    </div>
    ${platTabs ? `<div class="ly-platform-bar">${platTabs}<span class="muted">短稿（微头条 / 动态）与图文互斥，各自独立排版，标题与入口互不覆盖</span></div>` : ''}
    ${it.output_path ? `<div class="drawer-note">已导出：<a href="${escapeHtml('/output/' + it.output_path)}" target="_blank" rel="noreferrer">${escapeHtml(it.output_path)}</a></div>` : ''}
    ${editing ? '' : `<div class="drawer-note">素材已全部拷进交付目录（output_finish/…/素材/）。下面可以直接删除不合格的图、换一张、再加一张，改完会自动同步交付目录。</div>`}
    ${senBar}
    ${editing ? blocksEditorHtml(blocks, it) : `
    <div class="section-title bar"><span>复制到平台编辑器</span>
      <span class="diff-stat">手动发布：复制后到头条 / 百家发布页 Ctrl+V</span>
      <div class="diff-actions ly-copy-bar">
        <select class="ly-copy-mode" id="ly-copy-mode" data-ly-act="copy-mode"
          title="${escapeHtml(LY_COPY_HINT[copyMode] || '')}">
          ${LY_COPY_MODES.map((m) => `<option value="${m.value}"${m.value === copyMode ? ' selected' : ''}>${m.label}</option>`).join('')}
        </select>
        <button class="btn small accent" data-ly-act="copy-rich" title="复制带格式的正文与配图">一键复制</button>
        <button class="btn small ghost" data-ly-act="copy-title"${it.title ? '' : ' disabled'}
          title="只复制标题，粘到平台标题框">复制标题</button>
        <button class="btn small ghost" data-ly-act="copy" title="复制 Markdown 原文（含图片路径），用于存档或再加工">复制 MD</button>
      </div>
    </div>
    <div class="ly-copy-hint" id="ly-copy-hint">${escapeHtml(copyHint)}</div>

    <div class="section-title bar"><span>已插入素材</span>
      <span class="diff-stat">${on.length} 个位置</span>
      <div class="diff-actions">
        <button class="btn small" data-ly-act="edit" title="改正文：敏感词、错别字、增删段落，改完自动重跑校验">编辑正文</button>
        <button class="btn small ghost" data-ly-act="refresh-assets" title="素材重采后把新图拉进来">刷新素材</button>
        <button class="btn small ghost" data-ly-act="rebuild">重新生成</button>
        <button class="btn small ghost" data-ly-act="finish">重新交付</button>
        <button class="btn small accent" data-ly-act="publish">${isShort ? `发布${formBadge(plat, form)}到…` : '发布到…'}</button>
      </div>
    </div>
    <div class="place-list">
      ${ps.length ? ps.map((p) => placementRow(p, blocks)).join('') : '<div class="muted">当前没有插入任何素材，可从下面「可用素材」里点「插入」</div>'}
    </div>

    <div class="section-title bar"><span>可用素材（人工审核）</span>
      <span class="diff-stat">点「插入」加入正文；「删除」会连素材文件一起删掉</span>
    </div>
    ${unused.length ? `<div class="media-grid">
        ${unused.map((a) => unusedCard(a)).join('')}
      </div>` : '<div class="empty">没有未使用的素材了</div>'}

    <div class="section-title bar"><span>成稿预览</span></div>
    <div class="ly-preview">${previewHtml(it.body_md)}</div>

    <div class="ly-copy-fallback" id="ly-copy-fallback" hidden>
      <div class="section-title bar"><span>手动复制</span>
        <span class="diff-stat">浏览器不允许自动写剪贴板时用这里</span>
        <div class="diff-actions">
          <button class="btn small ghost" data-ly-act="copy-select">全选内容</button>
          <button class="btn small ghost" data-ly-act="copy-close">收起</button>
        </div>
      </div>
      <div class="ly-copy-body" id="ly-copy-body" contenteditable="true"></div>
    </div>`}
  `;
  $('#drawer').classList.add('wide');
  openDrawer();
  if (body) body.scrollTop = keepScroll;
}

/* 排版稿里的图片是本地绝对路径（发布时驱动要读文件），浏览器预览要换成 /output URL */
function previewHtml(md) {
  let html = mdToHtml(md);
  const map = {};
  (lyState.current && lyState.current.assets || []).forEach((a) => {
    if (a.path && a.url) map[a.path] = a.url;
  });
  return html.replace(/src="([^"]+)"/g, (full, src) => {
    const url = map[src];
    return url ? `src="${escapeHtml(url)}"` : full;
  });
}

/* ---------------- 正文编辑（敏感词 / 错别字 / 增删段落 / 指定位置插素材） ---------------- */
function sensitiveBannerHtml(sens, hitWords) {
  const words = (sens && sens.words) || [];
  const editing = !!lyState.editing;
  if (!words.length) {
    if (sens && sens.status === 'waived') {
      return `<div class="drawer-note">敏感词已人工放行${sens.note
        ? `（${escapeHtml(sens.note)}）` : ''}，仍建议在正文里改掉再发。</div>`;
    }
    return '';
  }
  return `<div class="ly-sensitive">敏感词未通过：<b>${words.map(escapeHtml).join('、')}</b>
    <span>${editing
      ? '命中处已标红，改完点「保存修改」会自动重跑校验。'
      : '点「编辑正文」直接改掉；带着这些词交付和发布都会被拦住。'}</span></div>`;
}

/* 编辑态的正文行：一段一个输入框，带类型切换 / 加段 / 删段 / 在此插素材 */
function blocksEditorHtml(blocks, it) {
  const hitWords = (lyState.hitWords || []).filter(Boolean);
  const rows = blocks.map((b, i) => {
    const isHead = String(b.type).startsWith('h');
    const text = String(b.text || '');
    const hits = hitWords.filter((w) => text.includes(w));
    return `<div class="ly-edit-row${hits.length ? ' hit' : ''}" data-idx="${i}">
      <div class="ly-edit-head">
        <span class="chip">${isHead ? '小标题' : '正文'}</span>
        <span class="muted">第 ${i + 1} 段 · ${text.replace(/\s/g, '').length} 字</span>
        <div class="grow"></div>
        <button class="btn small ghost" data-ly-act="edit-insert" data-idx="${i}"
          title="在这一段之后插入素材">插素材</button>
        <button class="btn small ghost" data-ly-act="edit-add" data-idx="${i}"
          title="在这一段后面加一段">＋段</button>
        <button class="btn small ghost" data-ly-act="edit-type" data-idx="${i}">${isHead ? '改正文' : '改标题'}</button>
        <button class="btn small danger" data-ly-act="edit-del" data-idx="${i}">删</button>
      </div>
      <textarea class="ly-edit-text" data-idx="${i}"
        rows="${Math.min(12, Math.max(2, Math.ceil(text.length / 38) + 1))}">${escapeHtml(text)}</textarea>
      ${hits.length ? `<div class="ly-edit-hit">敏感词：${hits.map(escapeHtml).join('、')}</div>` : ''}
    </div>`;
  }).join('');

  return `<div class="section-title bar"><span>正文编辑</span>
      <span class="diff-stat">共 ${blocks.length} 段 · 保存后重跑敏感词，并同步到交付目录与自动发布</span>
      <div class="diff-actions">
        <button class="btn small primary" data-ly-act="edit-save">保存修改</button>
        <button class="btn small ghost" data-ly-act="edit-cancel">取消</button>
      </div>
    </div>
    <div class="ly-edit-list" id="ly-edit-list">${rows || '<div class="muted">正文为空，先加一段</div>'}</div>
    <div class="ly-edit-foot">
      <button class="btn small ghost" data-ly-act="edit-append">＋ 末尾添加一段</button>
    </div>
    ${lyState.insertAt === null || lyState.insertAt === undefined ? '' : insertPickerHtml(blocks)}`;
}

/* 指定位置插素材：把素材卡直接列出来，点哪张就插到哪一段后面 */
function insertPickerHtml(blocks) {
  const idx = Number(lyState.insertAt);
  const assets = (lyState.current && lyState.current.assets) || [];
  const anchor = blocks[idx] ? `第 ${idx + 1} 段之后` : '正文末尾';
  return `<div class="ly-insert-picker">
    <div class="section-title bar"><span>插入素材到${escapeHtml(anchor)}</span>
      <span class="diff-stat">点一张图 / 视频即插入这个位置</span>
      <div class="diff-actions">
        <button class="btn small ghost" data-ly-act="edit-insert-close">收起</button>
      </div>
    </div>
    ${assets.length ? `<div class="media-grid">
      ${assets.map((a) => `<div class="media-card ly-insert-card" data-ly-act="edit-pick"
          data-asset="${a.id}" data-idx="${idx}" title="插到${escapeHtml(anchor)}">
          ${a.kind === 'video'
            ? `<video src="${escapeHtml(a.url || '')}" muted preload="metadata"></video>`
            : `<img src="${escapeHtml(a.url || '')}" alt="" loading="lazy" referrerpolicy="no-referrer" />`}
          <div class="m">${a.from_frame ? '抽帧 · ' : ''}${escapeHtml(a.title || a.keyword || (a.kind === 'video' ? '视频' : '图片'))}</div>
          <div class="m muted">${escapeHtml(assetMeta(a))}</div>
        </div>`).join('')}
    </div>` : '<div class="empty">没有可用素材，先回上一步点「刷新素材」</div>'}
  </div>`;
}

/* 把输入框里的最新文字收回草稿（任何重渲染之前都要先做这一步） */
function syncDraftBlocks() {
  if (!Array.isArray(lyState.draftBlocks)) return;
  $$('#ly-edit-list .ly-edit-text').forEach((el) => {
    const i = Number(el.dataset.idx);
    if (lyState.draftBlocks[i]) lyState.draftBlocks[i].text = el.value;
  });
}

/* 块增删后把插入点跟着挪：删第 idx 块 → 后面的插入点整体前移一格 */
function shiftPlacements(ps, idx, delta) {
  return (ps || []).map((p) => {
    let a = Number(p.after_block) || 0;
    if (delta < 0 && a >= idx) a = Math.max(0, a - 1);
    if (delta > 0 && a > idx) a += 1;
    return Object.assign({}, p, { after_block: a });
  });
}

function startEdit() {
  const it = lyState.current;
  if (!it) return;
  lyState.editing = true;
  lyState.draftBlocks = JSON.parse(JSON.stringify(it.blocks || []));
  lyState.insertAt = null;
  lyState.hitWords = ((it.sensitive || {}).words || []).slice();
  renderLayout();
}

function editInsertBlock(idx) {
  syncDraftBlocks();
  const list = lyState.draftBlocks || [];
  const at = Math.min(Math.max(0, Number(idx) + 1), list.length);
  list.splice(at, 0, { type: 'p', text: '' });
  if (Number(idx) >= 0) {
    lyState.current.placements = shiftPlacements(lyState.current.placements, Number(idx), 1);
  }
  renderLayout();
}

function editToggleType(idx) {
  syncDraftBlocks();
  const b = (lyState.draftBlocks || [])[idx];
  if (!b) return;
  b.type = String(b.type).startsWith('h') ? 'p' : 'h2';
  renderLayout();
}

function editDeleteBlock(idx) {
  syncDraftBlocks();
  const list = lyState.draftBlocks || [];
  if (list.length <= 1) { toast('至少要留一段正文', 'warn'); return; }
  list.splice(idx, 1);
  lyState.current.placements = shiftPlacements(lyState.current.placements, idx, -1);
  renderLayout();
}

/* 保存正文：顺带把 placements 一起提交（块增删后插入点已在前端改好） */
async function saveEdit() {
  const it = lyState.current;
  if (!it) return false;
  syncDraftBlocks();
  const blocks = (lyState.draftBlocks || [])
    .filter((b) => String(b.text || '').trim())
    .map((b) => ({ type: b.type || 'p', text: String(b.text).trim() }));
  if (!blocks.length) { toast('正文不能为空，至少留一段', 'warn'); return false; }
  try {
    const res = await api(`/api/layout/${it.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ blocks, placements: it.placements || [] }),
    });
    lyState.current = res;
    lyState.draftBlocks = JSON.parse(JSON.stringify(res.blocks || []));
    const rep = res.sensitive_edit || {};
    lyState.hitWords = (rep.blocked || []).map((h) => h.word).filter(Boolean);
    return true;
  } catch (e) {
    toast(e.message || '保存失败', 'error');
    return false;
  }
}

async function saveEditAndExit() {
  if (!(await saveEdit())) return;
  lyState.editing = false;
  lyState.insertAt = null;
  renderLayout();
  scheduleSyncFinish();
  const rep = lyState.current.sensitive_edit || {};
  if (rep.ok === false) {
    const words = (rep.blocked || []).map((h) => h.word).filter(Boolean);
    toast(`已保存，但还有敏感词：${words.slice(0, 5).join('、') || '见提示'}`, 'warn');
  } else {
    toast('已保存，正文已同步到交付稿', 'ok');
  }
}

async function editInsertAsset(assetId, idx) {
  const it = lyState.current;
  const a = (it.assets || []).find((x) => Number(x.id) === Number(assetId));
  if (!a) { toast('素材不存在，先刷新素材', 'warn'); return; }
  if ((it.placements || []).some((p) => Number(p.asset_id) === Number(a.id) && p.enabled)) {
    toast('这张素材已经在正文里了，要先移除再插', 'warn');
    return;
  }
  syncDraftBlocks();
  const blocks = (lyState.draftBlocks || [])
    .filter((b) => String(b.text || '').trim())
    .map((b) => ({ type: b.type || 'p', text: String(b.text).trim() }));
  if (!blocks.length) { toast('正文不能为空', 'warn'); return; }
  const ps = JSON.parse(JSON.stringify(it.placements || []));
  ps.push({
    id: `m${Date.now()}`,
    kind: a.kind === 'video' ? 'video' : 'image',
    asset_id: Number(a.id),
    after_block: Math.max(0, Math.min(Number(idx) || 0, blocks.length - 1)),
    reason: `手动插入到第 ${Number(idx) + 1} 段之后`,
    enabled: 1,
  });
  try {
    const res = await api(`/api/layout/${it.id}`, {
      method: 'PATCH', body: JSON.stringify({ blocks, placements: ps }),
    });
    lyState.current = res;
    lyState.draftBlocks = JSON.parse(JSON.stringify(res.blocks || []));
    lyState.hitWords = ((res.sensitive_edit || {}).blocked || []).map((h) => h.word);
    lyState.insertAt = null;
    renderLayout();
    toast('已插入素材', 'ok');
  } catch (e) { toast(e.message || '插入失败', 'error'); }
}

/* ---------------- 一键复制（富文本 + 配图） ---------------- */
async function copyRichLayout() {
  const it = lyState.current;
  if (!it) return;
  const btn = document.querySelector('[data-ly-act="copy-rich"]');
  const label = btn ? btn.textContent : '一键复制';
  const sel = $('#ly-copy-mode');
  const mode = sel ? sel.value : lyCopyMode(it);
  if (btn) { btn.disabled = true; btn.textContent = mode === 'upload' ? '传图上传中…' : '准备中…'; }
  const hint = $('#ly-copy-hint');
  if (hint) {
    hint.textContent = mode === 'upload'
      ? '正在把图片传到平台图床，每张要 1~2 秒，请稍等…'
      : '正在打包正文与图片…';
  }
  try {
    // upload 模式会真的去平台传图，所以是 POST；同步端点，不会卡住页面
    const data = await api(`/api/layout/${it.id}/copy`, {
      method: 'POST', body: JSON.stringify({ mode }),
    });
    const item = data.item || {};
    if (!item.html) throw new Error('没有可复制的正文');
    const ok = await writeRichClipboard(item.html, item.text || '');
    const st = item.stats || {};
    const bits = [`正文 ${st.chars || 0} 字`, `图 ${st.images || 0} 张`];
    if (st.video_slots) bits.push(`视频占位 ${st.video_slots} 处`);
    if ((item.warnings || []).length) toast(item.warnings[0], 'warn');
    if (ok) {
      const panel = $('#ly-copy-fallback');
      if (panel) panel.hidden = true;
      toast(`已复制（${bits.join(' / ')}）：到平台编辑器里 Ctrl+V`, 'ok');
    } else {
      showCopyFallback(item);
    }
  } catch (e) {
    toast(e.message || '复制失败', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    const h = $('#ly-copy-hint');
    if (h) h.textContent = lyState.copyHint || '';
  }
}

/* 富文本剪贴板：text/html 带格式与图片，text/plain 给只吃纯文本的编辑器 */
async function writeRichClipboard(html, text) {
  try {
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([text || ''], { type: 'text/plain' }),
      })]);
      return true;
    }
  } catch (e) { /* 非安全上下文（局域网 IP 访问）/ 权限被拒：落到 execCommand */ }
  return copyHtmlBySelection(html);
}

/* 把 HTML 放进离屏可编辑区，选中后 execCommand('copy')：
   剪贴板里同样是 text/html，图片（data URI）和格式都在。
   这里显式给容器一套干净的基础样式 —— execCommand 会把「计算后的样式」内联进剪贴板，
   不指定的话页面底色和字体也会被一起带进平台编辑器。 */
function copyHtmlBySelection(html) {
  const box = document.createElement('div');
  box.contentEditable = 'true';
  box.style.cssText = 'position:fixed;left:-9999px;top:0;width:600px;opacity:0;'
    + 'background:#fff;color:#111;font:16px/1.8 sans-serif;';
  box.innerHTML = html;
  document.body.appendChild(box);
  let ok = false;
  try {
    const range = document.createRange();
    range.selectNodeContents(box);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    ok = document.execCommand('copy');
    sel.removeAllRanges();
  } catch (e) { ok = false; }
  box.remove();
  return ok;
}

/* 自动写剪贴板失败时的兜底面板：内容已渲染在抽屉里，用户全选复制即可 */
function showCopyFallback(item) {
  const panel = $('#ly-copy-fallback');
  const body = $('#ly-copy-body');
  if (!panel || !body) { toast('复制失败，请手动选中成稿预览复制', 'error'); return; }
  body.innerHTML = item.html || '';
  panel.hidden = false;
  panel.scrollIntoView({ block: 'nearest' });
  toast('浏览器不允许自动写剪贴板：在下面区域全选后 Ctrl+C', 'warn');
}

function selectFallbackBody() {
  const body = $('#ly-copy-body');
  if (!body) return;
  body.focus();
  const range = document.createRange();
  range.selectNodeContents(body);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

function assetMeta(a) {
  const bits = [a.from_frame ? '视频抽帧' : '', PROVIDER_TEXT[a.provider] || a.platform || '',
    a.match ? `相关度 ${Math.round(a.match * 100)}%` : '']
    .filter(Boolean).join(' · ');
  return bits || a.keyword || '';
}

function unusedCard(a) {
  const isVid = a.kind === 'video';
  const thumb = isVid
    ? `<video src="${escapeHtml(a.url || '')}" muted preload="metadata"></video>`
    : `<img src="${escapeHtml(a.url || '')}" alt="" loading="lazy" referrerpolicy="no-referrer" />`;
  // 抽帧和正式图片同池排队，标一下来源方便人工判断（画面是视频截图，别当现场照片用）
  const frameTag = a.from_frame ? '<span class="tag fetching" title="来自视频抽帧">抽帧</span> ' : '';
  return `<div class="media-card">
    ${thumb}
    <div class="m">${frameTag}${escapeHtml(a.title || a.keyword || (isVid ? '视频' : '图片'))}</div>
    <div class="m muted">${escapeHtml(assetMeta(a))}</div>
    <button class="btn small accent" data-ly-act="add" data-asset="${a.id}">插入</button>
    <button class="btn small ghost" data-ly-act="del-asset" data-asset="${a.id}" title="删除这个素材文件">删除</button>
  </div>`;
}

async function publishLayout() {
  const it = lyState.current;
  if (!it) return;
  if (!publishState.accounts) {
    try {
      const data = await api('/api/accounts');
      publishState.accounts = data.items.filter((a) => a.enabled);
    } catch (e) { toast('读取账号失败', 'error'); return; }
  }
  const usable = publishState.accounts;
  if (!usable.length) { toast('还没有启用任何账号，先去「账号管理」添加', 'warn'); return; }
  const variants = await loadVariants(it.article_id);
  const form = it.content_form || 'article';
  const isMicro = form === 'micro';
  const shortName = formBadge(it.platform, form);
  const has = new Set(variants.map((v) => `${v.target_platform}:${v.content_form || 'article'}`));
  const canSend = (plat) => has.has(`${plat}:${formForPlatform(plat, form)}`)
    && formPublishable(plat, formForPlatform(plat, form));
  const entryLabel = variantLabel({ target_platform: it.platform, content_form: form });
  const versionLine = variants.length
    ? `这篇已产出：${variants.map((v) => `${variantLabel(v)}《${escapeHtml((v.used_title || '').slice(0, 22))}》`).join('；')}`
    : '这篇只有单一改写版本（老数据），两个平台会共用同一份稿子，建议先重新改写';
  const shortPublish = formPublishable(it.platform, form);
  $('#drawer-title').textContent = `发布 · ${it.title || (isMicro ? shortName : '(无标题)')}`;
  $('#drawer-body').innerHTML = `
    <div class="drawer-note"><b>本次入口：${escapeHtml(entryLabel)}</b>
      ${isMicro ? `（无标题${shortPublish ? ' · 只提交正文和配图到草稿箱' : ' · 网页端没有自动入口，人工发'}）`
                : (it.platform === 'baijia'
                  ? '（带标题的图文文章，图片先传图床、视频先传视频库，再一起写进草稿）'
                  : '（带标题的图文文章，存草稿）')}。下面每个账号会按自己支持的最高形态提交。</div>
    <div class="drawer-note">把排版稿发到下面的账号。默认只存草稿，最终由人工在平台后台审核发布。</div>
    <div class="drawer-note">${versionLine}</div>
    ${isMicro && shortPublish ? `<div class="drawer-note"><b>${escapeHtml(shortName)}没有标题</b>：`
      + '只提交正文与配图到草稿箱，不写标题。</div>' : ''}
    ${isMicro && !shortPublish ? `<div class="drawer-note"><b>${escapeHtml(shortName)}暂不支持网页端自动发布</b>：`
      + '成稿已按短稿交付到 output_finish，请到平台后台手动发；下面的账号只能发头条微头条。</div>' : ''}
    ${isMicro ? '' : `<div class="field">
      <label>发布标题（留空用各平台版本自己的标题）</label>
      <input id="pub-title" class="input" type="text" value="" placeholder="留空则按各平台标题分别发布" />
    </div>`}
    <div class="section-title bar"><span>选择账号</span>
      <span class="diff-stat">${usable.filter((a) => !has.size || canSend(a.platform)).length} 个可发 / ${usable.length} 个</span>
      <div class="diff-actions">
        <button class="btn small ghost" data-ly-act="pub-back">返回排版</button>
        <button class="btn small primary" data-ly-act="pub-submit">${isMicro ? `存${shortName}草稿` : '发布（存草稿）'}</button>
      </div>
    </div>
    <div class="place-list">
      ${usable.map((a) => {
        const ok = !has.size || canSend(a.platform);
        const targetForm = formForPlatform(a.platform, form);
        const target = variantLabel({ target_platform: a.platform, content_form: targetForm });
        const publishable = formPublishable(a.platform, targetForm);
        return `<label class="place-row${ok ? '' : ' off'}">
        <input type="checkbox" class="pub-pick" value="${a.id}"${ok ? '' : ' disabled'} />
        ${platChip(a.platform, a.platform_title || PLATFORM_BADGE[a.platform] || a.platform)}
        <span class="chip">${escapeHtml(formBadge(a.platform, targetForm))}</span>
        <div class="place-info">
          <div class="place-reason">${escapeHtml(a.name || '')}</div>
          <div class="muted">${ok
            ? `将按「${escapeHtml(target)}」提交 · ${escapeHtml(a.login_user || '未填登录名')} · cookie ${a.cookie_count || 0} 条 · ${escapeHtml(a.cookie_state || '')}`
            : !publishable
              ? `${escapeHtml(target)}网页端没有自动发布入口，请到平台后台手动发`
              : `这篇没有${escapeHtml(target)}版本，发不了`}</div>
        </div>
      </label>`;
      }).join('')}
    </div>
    <div class="section-title bar"><span>发布记录</span></div>
    <div id="pub-tasks">${await publishTasksHtml(it.article_id)}</div>
  `;
}

const PUB_STATUS_TEXT = {
  pending: '排队中', running: '发布中', success: '已存草稿',
  failed: '失败', cancelled: '已取消', interrupted: '中断',
};

function publishTasksHtmlFrom(items) {
  if (!(items || []).length) return '<div class="muted">还没发布过</div>';
  return items.map((t) => `<div class="place-row">
      ${platChip(t.account_platform || t.platform)}
      <span class="chip">${escapeHtml(formBadge(t.account_platform || t.platform, t.content_form || 'article'))}</span>
      <div class="place-info">
        <div class="place-reason">${escapeHtml(t.account_name || ('账号#' + (t.account_id || '-')))}
          · ${PUB_STATUS_TEXT[t.status] || t.status}</div>
        <div class="muted">${escapeHtml(String(t.created_at || '').slice(5, 16))}${t.error ? ' · ' + escapeHtml(t.error) : ''}</div>
      </div>
      <div class="grow"></div>
      ${t.post_url ? `<a class="btn small ghost" href="${escapeHtml(t.post_url)}" target="_blank" rel="noreferrer">打开</a>` : ''}
    </div>`).join('');
}

async function publishTasksHtml(articleId) {
  if (!articleId) return '<div class="muted">-</div>';
  try {
    const data = await api(`/api/publish/tasks?article_id=${articleId}`);
    return publishTasksHtmlFrom(data.items);
  } catch (e) { return '<div class="muted">读取失败</div>'; }
}

async function submitPublish() {
  const it = lyState.current;
  if (!it) return;
  const picked = $$('.pub-pick:checked').map((el) => Number(el.value));
  if (!picked.length) { toast('至少选一个账号', 'warn'); return; }
  const titleEl = $('#pub-title');
  const title = titleEl ? titleEl.value.trim() : '';   // 微头条没有标题输入框
  try {
    const res = await api('/api/publish', {
      method: 'POST',
      body: JSON.stringify({ layout_id: it.id, account_ids: picked, draft_only: 1, title }),
    });
    const tasks = res.tasks || [];
    const ok = tasks.filter((t) => t.success).length;
    const bad = tasks.filter((t) => !t.success);
    if (bad.length) toast(`${bad.length} 个账号没提交成功：${bad[0].error || ''}`, 'error');
    if (ok) {
      const kinds = [...new Set(tasks.filter((t) => t.success)
        .map((t) => formBadge(t.platform, t.content_form || 'article')))].join(' / ');
      toast(`已提交 ${ok} 个${kinds ? `（${kinds}）` : ''}发布任务，后台执行中`, 'ok');
    }
    it.title = title || it.title;
    await publishLayout();
    if (ok) startPublishPolling(it.article_id, it);
  } catch (e) { toast(e.message || '发布失败', 'error'); }
}

function startPublishPolling(articleId, it) {
  if (!articleId) return;
  const tick = async () => {
    let data;
    try { data = await api(`/api/publish/tasks?article_id=${articleId}`); }
    catch (e) { return; }
    const items = data.items || [];
    const box = $('#pub-tasks');
    if (box) box.innerHTML = publishTasksHtmlFrom(items);
    if (items.some((t) => t.status === 'pending' || t.status === 'running')) {
      setTimeout(safe(tick), 2500);
    }
  };
  tick();
}

function placementRow(p, blocks) {
  const a = layoutAsset(p.asset_id);
  const after = blocks[Number(p.after_block) || 0];
  const anchor = after ? (String(after.type).startsWith('h') ? after.text : `${after.text.slice(0, 18)}…`) : '-';
  const thumb = a && p.kind === 'image'
    ? `<img class="place-thumb" src="${escapeHtml(a.url || '')}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
    : (a && p.kind === 'video'
      ? `<video class="place-thumb" src="${escapeHtml(a.url || '')}" muted></video>` : '');
  // 同类型素材全列出来换一张（人工审核最主要的动作）。
  // 不用原生 select：选项里看不到画面，且 select 只能容纳一行字，素材一多就选不准。
  const same = (lyState.current.assets || []).filter((x) => (x.kind === 'video') === (p.kind === 'video'));
  const usedElsewhere = new Set((lyState.current.placements || [])
    .filter((q) => q.enabled && String(q.id) !== String(p.id))
    .map((q) => Number(q.asset_id)));
  const picking = String(lyState.picking || '') === String(p.id);
  const picker = picking ? `<div class="ly-picker">
      ${same.length ? same.map((x) => `<button class="ly-pick${Number(x.id) === Number(p.asset_id) ? ' on' : ''}"
          data-ly-act="use" data-asset="${x.id}" title="${escapeHtml(assetMeta(x))}">
          ${x.kind === 'video'
            ? `<video src="${escapeHtml(x.url || '')}" muted preload="metadata"></video>`
            : `<img src="${escapeHtml(x.url || '')}" alt="" loading="lazy" referrerpolicy="no-referrer" />`}
          <span>#${x.id}${x.from_frame ? ' 抽帧' : ''}${usedElsewhere.has(Number(x.id)) ? ' · 已用' : ''}</span>
        </button>`).join('') : '<span class="muted">没有同类素材可换，先点「刷新素材」</span>'}
    </div>` : '';
  return `<div class="place-row${p.enabled ? '' : ' off'}" data-pid="${escapeHtml(p.id)}">
    <span class="chip">${p.kind === 'video' ? '视频' : (a && a.from_frame ? '抽帧' : '图片')}</span>
    ${thumb}
    <div class="place-info">
      <div class="place-reason">${escapeHtml(p.reason || '')}</div>
      <div class="muted">插在「${escapeHtml(anchor)}」之后 · ${escapeHtml(a ? assetMeta(a) : '素材已删除')}</div>
    </div>
    <div class="grow"></div>
    <button class="btn small ${picking ? 'primary' : 'ghost'}" data-ly-act="pick"
      title="换一张同类型素材">${picking ? '收起' : '换图'}</button>
    <button class="btn small ghost" data-ly-act="prev" title="往前挪一段">↑</button>
    <button class="btn small ghost" data-ly-act="next" title="往后挪一段">↓</button>
    <button class="btn small ${p.enabled ? 'ghost' : 'accent'}" data-ly-act="toggle">${p.enabled ? '不插入' : '插入'}</button>
    <button class="btn small danger" data-ly-act="remove" title="从正文里移除（素材文件保留）">移除</button>
    ${picker}
  </div>`;
}

async function patchLayout(placements) {
  const it = await api(`/api/layout/${lyState.current.id}`, {
    method: 'PATCH', body: JSON.stringify({ placements }),
  });
  lyState.current = it;
  renderLayout();
}

/* 排版改动后自动同步交付目录：output_finish 里的文章与素材保持一致 */
let lySyncTimer = null;
function scheduleSyncFinish() {
  const it = lyState.current;
  if (!it) return;
  const hint = $('#ly-sync-hint');
  if (hint) hint.textContent = ' · 正在同步交付目录…';
  clearTimeout(lySyncTimer);
  lySyncTimer = setTimeout(safe(async () => {
    try {
      await api(`/api/finish/${it.article_id}`, { method: 'POST' });
      const h = $('#ly-sync-hint');
      if (h) h.textContent = ' · 已同步交付目录';
    } catch (e) {
      const h = $('#ly-sync-hint');
      if (h) h.textContent = ' · 同步失败：' + (e.message || '');
    }
  }), 900);
}

/* 新素材插在哪：挑离既有插入点最远的段落，避免堆在一起 */
function bestSlot(placements, blocks) {
  const slots = (blocks || []).map((b, i) => i).filter((i) => !String(blocks[i].type).startsWith('h'));
  if (!slots.length) return 0;
  const used = (placements || []).filter((p) => p.enabled).map((p) => Number(p.after_block));
  if (!used.length) return slots[0];
  let best = slots[0];
  let bestDist = -1;
  slots.forEach((i) => {
    const d = Math.min(...used.map((u) => Math.abs(u - i)));
    if (d > bestDist) { bestDist = d; best = i; }
  });
  return best;
}

async function onLayoutDrawerClick(e) {
  const it = lyState.current;
  if (!it) return;
  // 用 closest 取动作：按钮里含缩略图 / 文本时 e.target 不是按钮本身
  const actEl = e.target.closest('[data-ly-act]');
  const act = actEl ? actEl.dataset.lyAct : '';
  // 下拉框点击不在 click 里处理：这里一重渲染，展开的选项就被替换掉，根本选不了
  if (!act || act === 'replace' || e.target.closest('select')) return;

  if (act === 'copy-rich') { await copyRichLayout(); return; }
  if (act === 'copy-title') { await copyText(it.title || '', '标题'); return; }
  if (act === 'copy-select') { selectFallbackBody(); return; }
  if (act === 'copy-close') {
    const panel = $('#ly-copy-fallback');
    if (panel) panel.hidden = true;
    return;
  }
  if (act === 'copy') { await copyText(it.body_md || '', '排版稿 Markdown'); return; }

  // 正文编辑
  if (act === 'edit') { startEdit(); return; }
  if (act === 'edit-save') { await saveEditAndExit(); return; }
  if (act === 'edit-cancel') {
    lyState.editing = false;
    lyState.draftBlocks = null;
    lyState.insertAt = null;
    renderLayout();
    return;
  }
  if (act === 'edit-add') { editInsertBlock(Number(actEl.dataset.idx)); return; }
  if (act === 'edit-append') { editInsertBlock((lyState.draftBlocks || []).length - 1); return; }
  if (act === 'edit-type') { editToggleType(Number(actEl.dataset.idx)); return; }
  if (act === 'edit-del') { editDeleteBlock(Number(actEl.dataset.idx)); return; }
  if (act === 'edit-insert') {
    syncDraftBlocks();
    lyState.insertAt = Number(actEl.dataset.idx);
    renderLayout();
    return;
  }
  if (act === 'edit-insert-close') { lyState.insertAt = null; renderLayout(); return; }
  if (act === 'edit-pick') {
    await editInsertAsset(Number(actEl.dataset.asset), Number(actEl.dataset.idx));
    return;
  }
  if (act === 'refresh-assets') {
    const res = await api(`/api/layout/${it.id}/assets/refresh`, { method: 'POST' });
    lyState.current = res.item;
    lyState.picking = null;
    toast('已拉取最新素材', 'ok');
    renderLayout();
    return;
  }
  if (act === 'platform') {
    await openLayout(it.article_id, actEl.dataset.platform, actEl.dataset.form || '');
    return;
  }
  if (act === 'rebuild') {
    const qs = lyState.platform ? `?platform=${encodeURIComponent(lyState.platform)}` : '';
    const built = await api(`/api/layout/${lyState.articleId}${qs}`, { method: 'POST' });
    lyState.current = built.item;
    lyState.picking = null;
    toast('已按最新素材重新排版', 'ok');
    renderLayout();
    scheduleSyncFinish();
    return;
  }
  if (act === 'finish') {
    // 交付目录按平台分（output_finish/tt、bjh），正常由改写/采集自动触发，这里手动重建
    const res = await api(`/api/finish/${it.article_id}`, { method: 'POST' });
    const ok = (res.items || []).filter((x) => x.ok);
    toast(ok.length
      ? `已交付：${ok.map((x) => `${x.platform_title} → ${x.dir}`).join('；')}`
      : ((res.items || [])[0] || {}).error || '交付失败', ok.length ? 'ok' : 'error');
    return;
  }
  if (act === 'publish') { await publishLayout(); return; }
  if (act === 'pub-submit') { await submitPublish(); return; }
  if (act === 'pub-back') { publishState.accounts = null; renderLayout(); return; }

  // 展开 / 收起某个插入点的「换图」缩略图面板
  if (act === 'pick') {
    const rowEl = e.target.closest('.place-row');
    const pid = rowEl ? rowEl.dataset.pid : '';
    lyState.picking = String(lyState.picking || '') === String(pid) ? null : pid;
    renderLayout();
    return;
  }

  if (act === 'add') {
    const aid = Number(actEl.dataset.asset || 0);
    const a = (it.assets || []).find((x) => Number(x.id) === aid);
    if (!a) { toast('素材不存在，先刷新素材', 'warn'); return; }
    if ((it.placements || []).some((p) => Number(p.asset_id) === aid && p.enabled)) {
      toast('这张素材已经在正文里了，要挪位置请用插入行的 ↑↓', 'warn');
      return;
    }
    const ps = JSON.parse(JSON.stringify(it.placements || []));
    ps.push({
      id: `m${Date.now()}`, kind: a.kind === 'video' ? 'video' : 'image',
      asset_id: aid, after_block: bestSlot(ps, it.blocks || []),
      reason: '手动插入', enabled: 1,
    });
    await patchLayout(ps);
    scheduleSyncFinish();
    return;
  }
  if (act === 'del-asset') {
    const aid = Number(actEl.dataset.asset || 0);
    if (!aid) return;
    if (!confirm('确定删除这个素材？文件会一并删除，不可恢复。')) return;
    lyState.picking = null;
    await api(`/api/media/${it.article_id}/assets/${aid}`, { method: 'DELETE' });
    const res = await api(`/api/layout/${it.id}/assets/refresh`, { method: 'POST' });
    lyState.current = res.item;
    renderLayout();
    scheduleSyncFinish();
    toast('已删除该素材', 'ok');
    return;
  }

  const row = e.target.closest('.place-row');
  if (!row || !act) return;
  const pid = row.dataset.pid;
  const ps = JSON.parse(JSON.stringify(it.placements || []));
  const p = ps.find((x) => String(x.id) === String(pid));
  if (!p) return;
  const maxBlock = Math.max(0, (it.blocks || []).length - 1);
  if (act === 'use') {
    const aid = Number(actEl.dataset.asset || 0);
    const a = (it.assets || []).find((x) => Number(x.id) === aid);
    if (!a) return;
    p.asset_id = aid;
    p.kind = a.kind === 'video' ? 'video' : 'image';
    p.reason = `手动换成${p.kind === 'video' ? '视频' : (a.from_frame ? '抽帧' : '图片')} #${aid}`;
    lyState.picking = null;
    await patchLayout(ps);
    scheduleSyncFinish();
    return;
  }
  if (act === 'toggle') p.enabled = p.enabled ? 0 : 1;
  if (act === 'prev') p.after_block = Math.max(0, Number(p.after_block || 0) - 1);
  if (act === 'next') p.after_block = Math.min(maxBlock, Number(p.after_block || 0) + 1);
  if (act === 'remove') {
    const left = ps.filter((x) => String(x.id) !== String(pid));
    lyState.picking = null;
    await patchLayout(left);
    scheduleSyncFinish();
    return;
  }
  await patchLayout(ps);
  scheduleSyncFinish();
}

async function onLayoutDrawerChange(e) {
  const act = e.target.dataset.lyAct;
  // 复制方式只影响下一次复制，选完只更新说明文字，不必重渲染整页
  if (act === 'copy-mode') {
    localStorage.setItem(LY_COPY_MODE_KEY, e.target.value);
    const hint = $('#ly-copy-hint');
    if (hint) hint.textContent = LY_COPY_HINT[e.target.value] || '';
    return;
  }
  if (act !== 'replace') return;
  const it = lyState.current;
  if (!it) return;
  const row = e.target.closest('.place-row');
  if (!row) return;
  const pid = row.dataset.pid;
  const aid = Number(e.target.value || 0);
  if (!aid) return;
  const ps = JSON.parse(JSON.stringify(it.placements || []));
  const p = ps.find((x) => String(x.id) === String(pid));
  const a = (it.assets || []).find((x) => Number(x.id) === aid);
  if (!p || !a) return;
  p.asset_id = aid;
  p.kind = a.kind === 'video' ? 'video' : 'image';
  p.reason = `手动换成${p.kind === 'video' ? '视频' : '图片'} #${aid}`;
  await patchLayout(ps);
  scheduleSyncFinish();
}

/* ---------------- 系统设置 ---------------- */
const cfgState = {
  sub: 'llm',
  llmCat: 'article',
  meta: { categories: [], providers: [], api_styles: [] },
  settings: [],
  settingCats: [],
  promptGroups: [],
  pblock: '',        // 当前提示词平台块，形如 toutiao:micro
  pstage: '',        // 当前阶段页签：title / body / polish
  editingId: null,
  cleanup: null,
};

async function initSettings() {
  if (!cfgState.meta.providers.length) {
    cfgState.meta = await api('/api/llm-meta');
    fillLlmFormOptions();
  }
  await Promise.all([refreshLlm(), refreshSettings()]);
}

function fillLlmFormOptions() {
  const opt = (v, t) => `<option value="${escapeHtml(v)}">${escapeHtml(t)}</option>`;
  $('#f-category').innerHTML = cfgState.meta.categories.map((c) => opt(c.key, c.title)).join('');
  $('#f-provider').innerHTML = cfgState.meta.providers.map((p) => opt(p.key, p.label)).join('');
  $('#f-api_style').innerHTML = cfgState.meta.api_styles.map((s) => opt(s.key, s.label)).join('');
  $('#llm-cat').innerHTML = cfgState.meta.categories
    .map((c) => `<button class="seg${c.key === cfgState.llmCat ? ' active' : ''}" data-cat="${escapeHtml(c.key)}">${escapeHtml(c.title)}</button>`).join('');
}

/* -------- 大模型列表 -------- */
async function refreshLlm() {
  const data = await api('/api/llm-configs?category=' + encodeURIComponent(cfgState.llmCat));
  const items = data.items || [];
  $('#llm-list').innerHTML = items.map(llmCardHtml).join('');
  $('#llm-empty').classList.toggle('hidden', items.length > 0);
  const hit = cfgState.meta.categories.find((c) => c.key === cfgState.llmCat);
  $('#llm-cat-desc').textContent = hit ? hit.desc : '';
  const def = items.find((i) => i.is_default);
  $('#settings-hint').textContent = def
    ? `当前默认：${def.name}（${def.model || '未填模型名'}）`
    : '该分类尚未设置默认模型，将按优先级自动选择';
}

function llmCardHtml(c) {
  const style = (cfgState.meta.api_styles.find((s) => s.key === c.api_style) || {}).label || c.api_style;
  const badges = [
    `<span class="tag ${c.enabled ? 'success' : 'failed'}">${c.enabled ? '已启用' : '已停用'}</span>`,
    c.is_default ? '<span class="tag queued">默认</span>' : '',
    c.priority ? `<span class="chip">优先级 ${c.priority}</span>` : '',
  ].join('');
  return `<div class="cfg-card" data-id="${c.id}">
    <div class="cfg-head">
      <span class="cfg-name">${escapeHtml(c.name || '(未命名)')}</span>
      ${badges}
      <div class="grow"></div>
      <button class="btn small ghost" data-act="test">测试</button>
      <button class="btn small ghost" data-act="default"${c.is_default ? ' disabled' : ''}>设为默认</button>
      <button class="btn small" data-act="edit">编辑</button>
      <button class="btn small danger" data-act="del">删除</button>
    </div>
    <div class="cfg-meta">
      <span>服务商 ${escapeHtml(c.provider || 'custom')}</span>
      <span>协议 ${escapeHtml(style)}</span>
      <span>模型 ${escapeHtml(c.model || '未填')}</span>
      <span>端点 ${escapeHtml(c.base_url || '未填')}</span>
      <span>Key ${c.has_api_key ? escapeHtml(c.api_key_masked) : '<b class="muted">未配置</b>'}</span>
      <span>temp ${escapeHtml(String(c.temperature ?? 0))} / top_p ${escapeHtml(String(c.top_p ?? 1))}</span>
      <span>max_tokens ${escapeHtml(String(c.max_tokens ?? 0))}</span>
      <span>超时 ${escapeHtml(String(c.timeout ?? 0))}s</span>
    </div>
    ${c.system_prompt ? `<div class="cfg-note">提示词：${escapeHtml(c.system_prompt.slice(0, 120))}${c.system_prompt.length > 120 ? '…' : ''}</div>` : ''}
    ${c.note ? `<div class="cfg-note">备注：${escapeHtml(c.note)}</div>` : ''}
    <div class="cfg-test hidden"></div>
  </div>`;
}

async function onLlmListClick(e) {
  const card = e.target.closest('.cfg-card');
  if (!card) return;
  const id = Number(card.dataset.id);
  const act = e.target.dataset.act;
  const box = card.querySelector('.cfg-test');
  if (act === 'edit') { await openLlmForm(id); return; }
  if (act === 'default') {
    await api(`/api/llm-configs/${id}/default`, { method: 'POST' });
    toast('已设为默认模型', 'ok');
    await refreshLlm();
    return;
  }
  if (act === 'del') {
    if (!confirm('确定删除这条大模型配置？')) return;
    await api(`/api/llm-configs/${id}`, { method: 'DELETE' });
    toast('已删除', 'ok');
    await refreshLlm();
    return;
  }
  if (act === 'test') {
    e.target.disabled = true;
    box.classList.remove('hidden');
    box.innerHTML = '<span class="muted">测试中…</span>';
    try {
      const res = await api(`/api/llm-configs/${id}/test`, { method: 'POST' });
      box.innerHTML = res.ok
        ? `<span class="ok">✓ ${escapeHtml(res.message)}（${res.latency_ms}ms）${res.reply ? ' · ' + escapeHtml(res.reply) : ''}</span>`
        : `<span class="err">✗ ${escapeHtml(res.message)}</span>`;
    } finally { e.target.disabled = false; }
  }
}

/* -------- 大模型表单 -------- */
function openModal(title) {
  $('#modal-title').textContent = title;
  $('#modal-msg').classList.add('hidden');
  $('#modal').classList.remove('hidden');
  $('#modal-mask').classList.remove('hidden');
}
function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modal-mask').classList.add('hidden');
  cfgState.editingId = null;
}

async function openLlmForm(id) {
  cfgState.editingId = id || null;
  const p = cfgState.meta.providers;
  const fill = (v, dft) => { $('#' + v).value = dft ?? ''; };
  if (id) {
    const list = await api('/api/llm-configs');
    const c = (list.items || []).find((x) => x.id === id);
    if (!c) { toast('配置不存在', 'error'); return; }
    fill('f-name', c.name);
    $('#f-category').value = c.category;
    $('#f-provider').value = c.provider || 'custom';
    fill('f-base_url', c.base_url);
    $('#f-api_style').value = c.api_style || 'openai';
    fill('f-model', c.model);
    fill('f-api_key', '');
    $('#f-api_key').placeholder = c.has_api_key ? `已保存 ${c.api_key_masked}，留空不修改` : '粘贴 API Key';
    fill('f-temperature', c.temperature);
    fill('f-top_p', c.top_p);
    fill('f-max_tokens', c.max_tokens);
    fill('f-timeout', c.timeout);
    fill('f-priority', c.priority);
    fill('f-system_prompt', c.system_prompt);
    fill('f-note', c.note);
    $('#f-enabled').checked = !!c.enabled;
    $('#f-is_default').checked = !!c.is_default;
    onProviderChange();
    $('#f-api_style').value = c.api_style || 'openai';  // 允许自定义协议
    openModal('编辑大模型配置');
    return;
  } else {
    const first = p.find((x) => x.key === 'deepseek') || p[0];
    fill('f-name', '');
    $('#f-category').value = cfgState.llmCat;
    $('#f-provider').value = cfgState.meta.providers.length ? 'deepseek' : 'custom';
    fill('f-base_url', first.base_url);
    $('#f-api_style').value = first.api_style;
    fill('f-model', (first.models || [])[0] || '');
    fill('f-api_key', '');
    $('#f-api_key').placeholder = first.no_key ? '本地部署一般不需要 Key' : '粘贴 API Key';
    fill('f-temperature', 0.7);
    fill('f-top_p', 1);
    fill('f-max_tokens', 4096);
    fill('f-timeout', 90);
    fill('f-priority', 0);
    fill('f-system_prompt', '');
    fill('f-note', '');
    $('#f-enabled').checked = true;
    $('#f-is_default').checked = false;
    openModal('新增大模型配置');
    onProviderChange();
  }
}

function onProviderChange() {
  const p = cfgState.meta.providers.find((x) => x.key === $('#f-provider').value);
  if (!p) return;
  $('#f-api_style').value = p.api_style;
  if (!$('#f-base_url').value.trim()) $('#f-base_url').value = p.base_url;
  $('#f-model-list').innerHTML = (p.models || []).map((m) => `<option value="${escapeHtml(m)}"></option>`).join('');
  $('#f-api_key').placeholder = p.no_key ? '本地部署一般不需要 Key' : '粘贴 API Key';
}

function llmFormPayload() {
  const v = (id) => $(id).value.trim();
  return {
    name: v('#f-name'),
    category: v('#f-category'),
    provider: v('#f-provider'),
    api_style: v('#f-api_style'),
    base_url: v('#f-base_url'),
    api_key: v('#f-api_key'),
    model: v('#f-model'),
    temperature: Number(v('#f-temperature')) || 0,
    top_p: Number(v('#f-top_p')) || 1,
    max_tokens: Number(v('#f-max_tokens')) || 4096,
    timeout: Number(v('#f-timeout')) || 90,
    priority: Number(v('#f-priority')) || 0,
    enabled: $('#f-enabled').checked,
    is_default: $('#f-is_default').checked,
    system_prompt: v('#f-system_prompt'),
    note: v('#f-note'),
  };
}

function showModalMsg(text, bad) {
  const box = $('#modal-msg');
  box.textContent = text;
  box.classList.remove('hidden');
  box.style.background = bad ? '#fef2f2' : '#ecfdf5';
  box.style.borderColor = bad ? '#fecaca' : '#a7f3d0';
  box.style.color = bad ? '#b91c1c' : '#047857';
}

async function saveLlmForm() {
  const payload = llmFormPayload();
  if (!payload.name) { showModalMsg('请填写配置名称', true); return; }
  const btn = $('#modal-save');
  btn.disabled = true;
  try {
    if (cfgState.editingId) {
      await api(`/api/llm-configs/${cfgState.editingId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    } else {
      await api('/api/llm-configs', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeModal();
    toast('已保存', 'ok');
    cfgState.llmCat = payload.category || cfgState.llmCat;
    $$('#llm-cat .seg').forEach((s) => s.classList.toggle('active', s.dataset.cat === cfgState.llmCat));
    await refreshLlm();
  } finally { btn.disabled = false; }
}

async function testLlmForm() {
  const payload = llmFormPayload();
  if (!payload.base_url || !payload.model) { showModalMsg('请先填写接口地址与模型名', true); return; }
  const btn = $('#modal-test');
  btn.disabled = true;
  showModalMsg('测试中…', false);
  try {
    const url = cfgState.editingId ? `/api/llm-configs/${cfgState.editingId}/test` : '/api/llm-configs/test';
    const res = await api(url, { method: 'POST', body: JSON.stringify(payload) });
    showModalMsg(res.ok ? `✓ ${res.message}（${res.latency_ms}ms）${res.reply ? ' · ' + res.reply : ''}` : `✗ ${res.message}`, !res.ok);
  } finally { btn.disabled = false; }
}

/* -------- 运行环境 -------- */
async function refreshSettings() {
  const data = await api('/api/settings');
  cfgState.settings = data.items || [];
  cfgState.settingCats = data.categories || [];
  cfgState.promptGroups = data.prompt_groups || [];
  if (!cfgState.pblock && cfgState.promptGroups.length) {
    const g = cfgState.promptGroups[0];
    cfgState.pblock = `${g.platform}:${g.form}`;
    cfgState.pstage = (g.stages[0] || {}).stage || 'body';
  }
  renderSettings();
}

/* 把页面上还没保存的输入写回内存状态：切换提示词块/页签时不丢编辑 */
function stashSettingsInputs() {
  $$('#settings-groups [data-key]').forEach((el) => {
    const key = el.dataset.key;
    const hit = cfgState.settings.find((s) => s.key === key);
    if (hit) hit.value = el.value;
    cfgState.promptGroups.forEach((g) => g.stages.forEach((st) => {
      if (st.key === key) st.value = el.value;
    }));
  });
}

function renderSettings() {
  const prompt = promptCenterHtml();
  $('#settings-groups').innerHTML = prompt + cfgState.settingCats
    .filter((cat) => cat.key !== 'prompt')       // 提示词走专门的分块面板
    .map((cat) => {
      const rows = cfgState.settings.filter((s) => s.category === cat.key);
      if (!rows.length) return '';
      // 旧通用提示词收进折叠区：只在分块模板为空时兜底，平时不用看
      const legacy = cat.key === 'rewrite'
        ? rows.filter((s) => String(s.key).startsWith('rewrite.prompt_')) : [];
      const main = rows.filter((s) => !legacy.includes(s));
      return `<div class="cfg-card">
      <div class="cfg-head"><span class="cfg-name">${escapeHtml(cat.title)}</span>
        <span class="muted">${escapeHtml(cat.desc)}</span></div>
      <div class="form-grid">${main.map(settingFieldHtml).join('')}</div>
      ${legacy.length ? `<details class="legacy-prompts">
        <summary>旧·通用提示词（兜底，通常不用改）</summary>
        <div class="form-grid">${legacy.map(settingFieldHtml).join('')}</div>
      </details>` : ''}
    </div>`;
    }).join('');
}

/* 提示词面板：平台块 → 阶段页签。平台再多也只纵向加块，不再平铺一页 */
function promptCenterHtml() {
  const groups = cfgState.promptGroups || [];
  if (!groups.length) return '';
  let active = groups.find((g) => `${g.platform}:${g.form}` === cfgState.pblock) || groups[0];
  let stage = (active.stages || []).find((s) => s.stage === cfgState.pstage) || active.stages[0];
  if (!stage) return '';
  const noVar = !String(stage.value || '').includes('{{article_text}}');
  const blocks = groups.map((g) => `<button class="seg${g === active ? ' active' : ''}"
      data-pblock="${escapeHtml(g.platform + ':' + g.form)}"
      title="${escapeHtml(g.hint || '')}">${escapeHtml(g.title)}</button>`).join('');
  const stages = active.stages.map((s) => `<button class="seg${s === stage ? ' active' : ''}"
      data-pstage="${escapeHtml(s.stage)}">${escapeHtml(s.label)}</button>`).join('');
  return `<div class="cfg-card" id="prompt-center">
    <div class="cfg-head"><span class="cfg-name">提示词模板</span>
      <span class="muted">按「平台 · 形态」分块维护，各块互不影响；切换块/页签不会丢未保存的编辑</span></div>
    <div class="prompt-blocks segmented inline">${blocks}</div>
    <div class="drawer-note">${escapeHtml(active.hint || '')}${active.hint ? '。' : ''}当前阶段：${escapeHtml(stage.label)}</div>
    <div class="segmented inline">${stages}</div>
    <div class="form-grid">
      <div class="field span2">
        <label>${escapeHtml(stage.label)}<em class="hint">${escapeHtml(stage.hint || '')}</em></label>
        <div class="field-row tall">
          <textarea id="s-${escapeHtml(stage.key)}" data-key="${escapeHtml(stage.key)}"
            data-secret="0" class="input" rows="18">${escapeHtml(stage.value)}</textarea>
          <button class="btn small ghost" data-reset="${escapeHtml(stage.key)}">恢复默认</button>
        </div>
        <div class="field-foot">${String(stage.value || '').length} 字 · ${noVar
          ? '<span class="err">缺少 {{article_text}} 变量，正文会追加在末尾</span>'
          : '<span class="ok">已包含 {{article_text}} 变量</span>'}</div>
      </div>
    </div>
  </div>`;
}

function settingFieldHtml(s) {
  const common = `id="s-${escapeHtml(s.key)}" data-key="${escapeHtml(s.key)}" data-secret="${s.is_secret ? 1 : 0}" class="input"`;
  const isPrompt = s.input_type === 'prompt';
  let control = '';
  if (s.input_type === 'select') {
    const opts = (s.options || []).map((o) => {
      const v = o.value ?? o[0];
      const t = o.label ?? o[1];
      return `<option value="${escapeHtml(v)}"${String(s.value) === String(v) ? ' selected' : ''}>${escapeHtml(t)}</option>`;
    }).join('');
    control = `<select ${common}>${opts}</select>`;
  } else if (isPrompt || s.input_type === 'textarea') {
    control = `<textarea ${common} rows="${isPrompt ? 16 : 3}">${escapeHtml(s.value)}</textarea>`;
  } else if (s.input_type === 'number') {
    control = `<input type="number" ${common} value="${escapeHtml(s.value)}" />`;
  } else {
    control = `<input type="${s.is_secret ? 'password' : 'text'}" ${common} value="${escapeHtml(s.value)}" autocomplete="off" />`;
  }
  const probe = s.prober
    ? `<button class="btn small ghost" data-probe="${escapeHtml(s.key)}" data-prober="${escapeHtml(s.prober)}">探测</button>` : '';
  const reset = `<button class="btn small ghost" data-reset="${escapeHtml(s.key)}">恢复默认</button>`;
  const noVar = isPrompt && !String(s.value).includes('{{article_text}}');
  const counter = isPrompt
    ? `<div class="field-foot">${String(s.value || '').length} 字 · ${
        noVar ? '<span class="err">缺少 {{article_text}} 变量，正文会追加在末尾</span>' : '<span class="ok">已包含 {{article_text}} 变量</span>'}</div>`
    : '';
  return `<div class="field${isPrompt || s.input_type === 'textarea' ? ' span2' : ''}">
    <label>${escapeHtml(s.label || s.key)}${s.hint ? `<em class="hint">${escapeHtml(s.hint)}</em>` : ''}</label>
    <div class="field-row${isPrompt ? ' tall' : ''}">${control}${probe}${reset}</div>
    ${counter}
  </div>`;
}

async function saveSettingsForm() {
  const items = {};
  $$('#settings-groups [data-key]').forEach((el) => {
    const key = el.dataset.key;
    const val = el.value;
    if (el.dataset.secret === '1' && !val.trim()) return;  // 留空 = 不修改
    items[key] = val;
  });
  if (!Object.keys(items).length) { toast('没有改动', 'warn'); return; }
  const btn = $('#btn-settings-save');
  btn.disabled = true;
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ items }) });
    toast(`已保存 ${Object.keys(items).length} 项`, 'ok');
    await refreshSettings();
  } finally { btn.disabled = false; }
}

async function resetSetting(key) {
  if (!confirm(`确定把「${key}」恢复为默认提示词/默认值？`)) return;
  await api('/api/settings/reset', { method: 'POST', body: JSON.stringify({ keys: [key] }) });
  toast('已恢复默认', 'ok');
  await refreshSettings();
}

async function probeSetting(key, prober) {
  const el = $(`#s-${CSS.escape(key)}`);
  const res = await api('/api/settings/probe', {
    method: 'POST',
    body: JSON.stringify({ key, value: el ? el.value : '', prober }),
  });
  toast(res.message, res.ok ? 'ok' : 'error');
}

async function resetSettingsForm() {
  if (!confirm('确定把运行环境全部恢复默认值？')) return;
  await api('/api/settings/reset', { method: 'POST', body: JSON.stringify({}) });
  toast('已恢复默认', 'ok');
  await refreshSettings();
}

/* -------- 数据清理 -------- */
function fmtBytes(n) {
  n = Number(n || 0);
  if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return Math.max(0, Math.round(n / 1024)) + ' KB';
}

async function scanCleanup() {
  $('#cleanup-hint').textContent = '正在扫描占用…';
  try {
    const d = await api('/api/cleanup/scan');
    cfgState.cleanup = d;
    renderCleanup(d);
    $('#cleanup-hint').textContent = `共占用 ${fmtBytes(d.total_bytes)}（扫描于 ${d.scanned_at}）`;
  } catch (e) {
    $('#cleanup-hint').textContent = '扫描失败：' + (e.message || '');
  }
}

function cleanupRow(key, title, desc, count, bytes) {
  return `<div class="place-row">
    <div class="place-info">
      <div class="place-reason">${escapeHtml(title)}</div>
      <div class="muted">${escapeHtml(desc)}</div>
    </div>
    <div class="grow"></div>
    <span class="chip">${escapeHtml(String(count ?? 0))}</span>
    <span class="muted">${fmtBytes(bytes)}</span>
    <button class="btn small ghost" data-clean="${escapeHtml(key)}">清理</button>
  </div>`;
}

function renderCleanup(d) {
  const batches = d.batches || {};
  const cache = d.cache || {};
  const media = d.media || {};
  const pub = d.publish || {};
  const st = pub.by_status || {};
  const rw = d.rewrite || {};
  const orphans = d.orphans || {};
  const profiles = d.profiles || {};
  const logs = d.logs || {};
  const orphanNames = (orphans.items || []).slice(0, 3).map((i) => i.name).join('、') || '无';
  $('#cleanup-body').innerHTML = `
    <div class="cfg-card">
      <div class="cfg-head"><span class="cfg-name">业务数据</span>
        <span class="muted">删数据源表格会连带清掉它的原文缓存、素材、改写与交付目录</span></div>
      ${cleanupRow('batches', '数据源表格', `批次 ${batches.count || 0} 个（含其全部文章与产物）`, batches.count, 0)}
      ${cleanupRow('cache', '采样缓存', `原文抓取缓存 ${cache.rows || 0} 条，清完可重新抓`, cache.rows, cache.bytes)}
      ${cleanupRow('media', '素材缓存', `无主素材记录 ${media.rows || 0} 条`, media.rows, media.bytes)}
      ${cleanupRow('rewrites', '改写 / 排版历史', '每个平台版本只留最新 1 条，草稿排版全删', rw.rewrites || 0, 0)}
    </div>
    <div class="cfg-card">
      <div class="cfg-head"><span class="cfg-name">发布记录</span>
        <span class="muted">共 ${pub.total || 0} 条 · 成功 ${st.success || 0} · 失败 ${st.failed || 0} · 卡住 ${st.running || 0}</span></div>
      ${cleanupRow('publish', '清理发布记录', '默认删 30 天前的；卡住的任务建议一并清掉', pub.total, 0)}
    </div>
    <div class="cfg-card">
      <div class="cfg-head"><span class="cfg-name">磁盘</span>
        <span class="muted">磁盘上有、库里没有引用的目录与账号数据</span></div>
      ${cleanupRow('orphans', '孤儿输出目录', orphanNames, orphans.count, orphans.bytes)}
      ${cleanupRow('profiles', '无主账号数据', '浏览器 profile 里没有对应账号', profiles.count, profiles.bytes)}
      ${cleanupRow('logs', '运行日志', `data/*.log ${logs.count || 0} 个，超 5MB 会清空`, logs.count, logs.bytes)}
    </div>`;
}

async function runCleanup(key) {
  const label = {
    batches: '数据源表格（含全部文章与产物）', cache: '采样缓存', media: '无主素材',
    rewrites: '改写 / 排版历史', publish: '发布记录', orphans: '孤儿输出目录',
    profiles: '无主账号数据', logs: '运行日志',
  }[key] || key;
  if (!confirm(`确定清理「${label}」？此操作不可恢复。`)) return;
  const body = { target: key };
  if (key === 'publish') body.days = 30;
  try {
    const res = await api('/api/cleanup/run', { method: 'POST', body: JSON.stringify(body) });
    const r = res.result || {};
    toast(`已清理 ${r.rows || 0} 条，释放 ${fmtBytes(r.freed_bytes || 0)}`, 'ok');
    await scanCleanup();
  } catch (e) { toast(e.message || '清理失败', 'error'); }
}

async function vacuumDb() {
  if (!confirm('数据库收缩会重建 app.db，期间不要操作，继续？')) return;
  try {
    const r = await api('/api/cleanup/vacuum', { method: 'POST' });
    toast(`收缩完成：${fmtBytes(r.before)} → ${fmtBytes(r.after)}`, 'ok');
  } catch (e) { toast(e.message || '收缩失败', 'error'); }
}

function switchSettingsSub(sub) {
  cfgState.sub = sub;
  $('#panel-llm').classList.toggle('hidden', sub !== 'llm');
  $('#panel-runtime').classList.toggle('hidden', sub !== 'runtime');
  $('#panel-cleanup').classList.toggle('hidden', sub !== 'cleanup');
  if (sub === 'cleanup' && !cfgState.cleanup) scanCleanup();
  $('#btn-llm-add').classList.toggle('hidden', sub !== 'llm');
  $('#btn-settings-save').classList.toggle('hidden', sub !== 'runtime');
  $('#btn-settings-reset').classList.toggle('hidden', sub !== 'runtime');
  $('#settings-hint').classList.toggle('hidden', sub !== 'llm');
}

/* ---------------- 生产流水线 ---------------- */
const PLATFORM_BADGE = { toutiao: '头条', baijia: '百家', other: '其他' };
const PLATFORM_META = {
  toutiao: { label: '头条', cls: 'toutiao', mark: '头' },
  baijia: { label: '百家', cls: 'baijia', mark: '百' },
  other: { label: '其他', cls: 'other', mark: '他' },
};

function platMeta(platform) {
  const key = String(platform || '');
  return PLATFORM_META[key]
    || { label: key || '未知', cls: 'other', mark: (key || '?').slice(0, 1).toUpperCase() };
}

/** 只有图标，用于筛选按钮等需要纯图标的位置 */
function platMark(platform) {
  const meta = platMeta(platform);
  return `<i class="plat-mark plat-mark-${meta.cls}" title="${escapeHtml(meta.label)}">${escapeHtml(meta.mark)}</i>`;
}

/** 平台徽标：品牌色方块 + 平台名，用于发布记录 / 账号等列表区分头条 / 百家 */
function platChip(platform, text) {
  const meta = platMeta(platform);
  const label = text == null ? meta.label : String(text);
  return `<span class="plat plat-${meta.cls}" title="${escapeHtml(meta.label)}">`
    + `${platMark(platform)}${escapeHtml(label)}</span>`;
}
const PIPE_STAGES = [
  { key: 'fetch', title: '待抓原文', hint: '未抓 / 排队 / 抓取中' },
  { key: 'media', title: '采素材', hint: '原文就绪，待采样' },
  { key: 'rewrite', title: 'AI 改写', hint: '素材就绪，待改写' },
  { key: 'done', title: '已交付', hint: 'output_finish' },
  { key: 'failed', title: '异常', hint: '抓取 / 素材 / 改写失败' },
];

const PIPE_STATUS_TEXT = {
  pending: '未抓取', queued: '排队中', fetching: '抓取中', running: '进行中',
  success: '完成', failed: '失败', empty: '无素材',
};

async function refreshPipeline() {
  let d;
  try { d = await api('/api/pipeline?limit=10'); }
  catch (e) { $('#pipeline-hint').textContent = '读取失败：' + (e.message || ''); return; }
  const c = d.counts || {};
  const q = d.queues || {};
  const fmt = (k) => `${(q[k] || {}).running || 0}/${(q[k] || {}).queued || 0}`;
  $('#pipeline-hint').textContent =
    `共 ${c.total || 0} 条 · 未抓 ${c.fetch_pending || 0} · 抓取中 ${c.fetch_running || 0}`
    + ` · 采样中 ${c.media_running || 0} · 改写中 ${c.rewrite_running || 0}`
    + ` · 已交付 ${c.done || 0} · 异常 ${c.failed || 0}`
    + ` · 无素材(正常) ${c.media_empty || 0}`
    + ` · 队列(运行/排队) 抓取 ${fmt('fetch')} 素材 ${fmt('media')} 改写 ${fmt('rewrite')}`
    + ` · 更新于 ${(d.server_time || '').slice(11)}`;
  const clean = $('#btn-pipeline-clean');
  if (clean) clean.textContent = `清理已交付(${c.done || 0})`;
  renderPipelineStats(d);
  $('#pipeline-board').innerHTML = PIPE_STAGES.map((stage) => {
    const items = (d.stages || {})[stage.key] || [];
    const n = (d.counts || {})[stage.key] || 0;
    return `<div class="pipe-col">
      <div class="pipe-head"><h4>${stage.title}</h4><span class="chip">${n}</span>
        <div class="grow"></div><span class="muted" style="font-size:11px">${stage.hint}</span></div>
      <div class="pipe-body">${items.length
        ? items.map((a) => pipeItem(a, stage.key)).join('')
        : '<div class="muted">暂无</div>'}</div>
    </div>`;
  }).join('');
}

/** 流水线统计卡 + 来源/分类分布 + 近 14 天导入与交付趋势（口径对齐发布中心） */
function renderPipelineStats(d) {
  const box = $('#pipeline-stats');
  if (!box) return;
  const c = d.counts || {};
  const s = d.stats || {};
  const source = s.by_source || [];
  const cats = s.by_category || [];
  const daily = s.daily || [];
  const maxSrc = Math.max(1, ...source.map((x) => Number(x.total) || 0));
  const maxCat = Math.max(1, ...cats.map((x) => Number(x.total) || 0));
  const maxDay = Math.max(1, ...daily.map((x) =>
    Math.max(Number(x.total) || 0, Number(x.done) || 0)));
  const cards = [
    ['素材库条目', c.total || 0, ''],
    ['待抓原文', c.fetch || 0, 'blue'],
    ['待改写', c.rewrite || 0, 'blue'],
    ['已交付', c.done || 0, 'green'],
    ['异常', c.failed || 0, 'red'],
    ['无素材跳过', c.media_empty || 0, ''],
  ];
  box.innerHTML = `
    <div class="stat-cards">${cards.map(([label, value, cls]) =>
      `<div class="stat-card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`).join('')}</div>
    <div class="chart-grid">
      <div class="cfg-card">
        <div class="cfg-head"><span class="cfg-name">按来源</span>
          <span class="muted">绿色=已交付 · 红色=异常</span></div>
        <div class="bar-list">${source.length
          ? source.map((x) => pubBar(PLATFORM_BADGE[x.name] || x.name || '其他',
              x.media_empty ? `无素材 ${x.media_empty}` : '',
              x.total, x.done, x.failed, maxSrc, platMark(x.name))).join('')
          : '<div class="muted">还没有导入资讯</div>'}</div>
      </div>
      <div class="cfg-card">
        <div class="cfg-head"><span class="cfg-name">按分类</span>
          <span class="muted">标题分类维度 · 最多 12 类</span></div>
        <div class="bar-list">${cats.length
          ? cats.map((x) => pubBar(x.name, '', x.total, x.done, x.failed, maxCat)).join('')
          : '<div class="muted">还没有导入资讯</div>'}</div>
      </div>
      <div class="cfg-card">
        <div class="cfg-head"><span class="cfg-name">近 ${s.days || 14} 天导入 / 交付</span>
          <span class="muted">蓝=导入 · 绿=交付</span></div>
        <div class="trend">${daily.length
          ? daily.map((x) => `<div class="trend-col"
              title="${escapeHtml(x.date)}：导入 ${x.total} 条 · 交付 ${x.done} 条">
              <div class="trend-bars">
                <div class="trend-bar" style="height:${Math.max(3, (Number(x.total) / maxDay) * 72).toFixed(0)}px"></div>
                <div class="trend-bar done" style="height:${Math.max(3, (Number(x.done) / maxDay) * 72).toFixed(0)}px"></div>
              </div>
              <div class="trend-label">${escapeHtml(String(x.date || '').slice(5))}</div>
            </div>`).join('')
          : '<div class="muted">近 14 天没有导入记录</div>'}</div>
      </div>
    </div>`;
}

function pipeStatus(a, stage) {
  if (stage === 'fetch') return a.fetch_status || 'pending';
  if (stage === 'media') return a.media_status || 'pending';
  if (stage === 'rewrite') return a.rewrite_status || 'pending';
  if (stage === 'done') return 'success';
  return 'failed';
}

function pipeItem(a, stage) {
  const plat = PLATFORM_BADGE[a.source_norm] || '其他';
  const dirs = String(a.finish_dir || '').split(',').map((x) => x.trim()).filter(Boolean);
  const st = pipeStatus(a, stage);
  // 失败列要指明是哪一步失败，不能把素材阶段的「没采到视频」提示当成异常原因
  const failStage = a.fetch_status === 'failed' ? 'fetch'
    : a.media_status === 'failed' ? 'media'
      : a.rewrite_status === 'failed' ? 'rewrite'
        : a.finish_status === 'failed' ? 'finish' : '';
  const err = stage === 'fetch' ? (a.fetch_error || '')
    : stage === 'media' ? (a.media_status === 'failed' ? (a.media_error || '') : '')
      : stage === 'rewrite' ? (a.rewrite_error || a.media_error || '')
        : stage === 'failed' ? (
          failStage === 'fetch' ? (a.fetch_error || '')
            : failStage === 'media' ? (a.media_error || '')
              : failStage === 'rewrite' ? (a.rewrite_error || '')
                : failStage === 'finish'
                  ? '交付失败：多为敏感词未过或缺合格改写稿，可重新改写或看对比'
                  : (a.fetch_error || a.media_error || a.rewrite_error || ''))
          : '';
  const errIsProblem = stage === 'failed' || st === 'failed';
  const sensBlocked = String(a.sensitive_status || '') === 'blocked';
  // 各阶段的「还没开始」用各自的说法，避免改写列也显示「未抓取」
  const pendingText = { fetch: '未抓取', media: '待采样', rewrite: '待改写' };
  const failLabel = { fetch: '抓取失败', media: '素材失败', rewrite: '改写失败', finish: '交付失败' };
  const tagText = stage === 'failed' ? (failLabel[failStage] || '失败')
    : st === 'pending' ? (pendingText[stage] || '待处理')
      : (PIPE_STATUS_TEXT[st] || st);
  const tagCls = stage === 'failed' ? 'failed'
    : ['queued', 'fetching', 'running'].includes(st) ? 'fetching'
      : st === 'success' ? 'success' : st === 'failed' ? 'failed' : 'pending';
  return `<div class="pipe-item">
    <div class="t">${escapeHtml((a.title || '(无标题)').slice(0, 42))}</div>
    <div class="pipe-row">
      <span class="tag ${tagCls}">${escapeHtml(tagText)}</span>
      ${sensBlocked ? '<span class="tag failed" title="润色后校验命中拦截级敏感词，修复轮数用尽仍未通过">敏感词未过</span>' : ''}
      <span class="muted" style="font-size:11px">${escapeHtml(a.source || '-')} · ${plat}</span>
    </div>
    ${dirs.length ? `<div class="muted pipe-dir">${dirs.map((d) =>
      `<a href="/finish/${encodeURI(d)}" target="_blank" rel="noreferrer">${escapeHtml(d.split('/').pop())}</a>`).join('、')}</div>` : ''}
    ${err ? `<div class="row-msg${errIsProblem ? '' : ' note'}"
      title="${escapeHtml(err)}">${escapeHtml(String(err).slice(0, 70))}</div>` : ''}
    <div class="pipe-actions">
      ${stage === 'done' ? `<button class="btn small ghost" data-pipe-finish="${a.id}" title="按最新排版与素材重建成稿">重新交付</button>` : ''}
      ${stage === 'done' ? `<button class="btn small danger" data-pipe-del="${a.id}">清理</button>` : ''}
      ${sensBlocked ? `<button class="btn small accent" data-pipe-view="${a.id}"
        title="查看命中的敏感词，可人工放行或重新改写">查看/放行</button>` : ''}
      ${stage === 'failed' && sensBlocked ? `<button class="btn small ghost" data-pipe-recheck="${a.id}"
        title="按当前词库与规则重新校验，合格则恢复为已改写（不用重跑模型）">重查敏感词</button>` : ''}
      ${stage === 'failed' ? `<button class="btn small ghost" data-pipe-rewrite="${a.id}"
        title="重新入队改写（会覆盖这篇现有的改写稿）">重新改写</button>` : ''}
      ${['fetch', 'failed'].includes(stage) && a.id ? `<button class="btn small ghost" data-pipe-open="${a.id}">定位</button>` : ''}
    </div>
  </div>`;
}

async function onPipelineClick(e) {
  const view = e.target.closest('[data-pipe-view]');
  if (view) {
    await openRewriteCompare(Number(view.dataset.pipeView), true);
    return;
  }
  const rewrite = e.target.closest('[data-pipe-rewrite]');
  if (rewrite) {
    await retryRewrite(rewrite.dataset.pipeRewrite);
    return;
  }
  const recheck = e.target.closest('[data-pipe-recheck]');
  if (recheck) {
    const res = await api(`/api/sensitive/recheck/${recheck.dataset.pipeRecheck}?repair=true`,
      { method: 'POST' });
    const back = res.restored || 0;
    toast(back ? `已恢复 ${back} 条合格稿，到排版页确认后交付`
      : '按当前规则仍不合格，需人工处理', back ? 'ok' : 'warn');
    await Promise.all([refreshPipeline(), refreshStats()]);
    return;
  }
  const ref = e.target.closest('[data-pipe-finish]');
  if (ref) {
    await api(`/api/finish/${ref.dataset.pipeFinish}`, { method: 'POST' });
    toast('已重新交付（素材与排版按最新状态重建）', 'ok');
    await Promise.all([refreshPipeline(), refreshStats()]);
    return;
  }
  const del = e.target.closest('[data-pipe-del]');
  if (del) {
    const id = del.dataset.pipeDel;
    if (!confirm('确定清理这篇的交付成品？\n只会删除 output_finish 里的成稿目录，文章与素材保留，可随时重新交付。')) return;
    const res = await api(`/api/finish/${id}`, { method: 'DELETE' });
    toast(`已清理 ${res.dirs || 0} 个目录，释放 ${fmtSize(res.freed_bytes || 0)}`, 'ok');
    await Promise.all([refreshPipeline(), refreshStats()]);
    return;
  }
  const open = e.target.closest('[data-pipe-open]');
  if (open) {
    // 跳到资讯页并搜索这篇标题，方便人工处理
    const item = open.closest('.pipe-item');
    const title = (item && item.querySelector('.t') || {}).textContent || '';
    const box = $('#q');
    if (box) box.value = title.slice(0, 18);
    state.q = title.slice(0, 18);
    $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === 'news'));
    state.view = 'news';
    ['news', 'cache', 'settings', 'accounts', 'pipeline', 'publish'].forEach((v) => {
      const el = $(`#view-${v}`);
      if (el) el.classList.toggle('active', v === 'news');
    });
    await refreshGroups();
  }
}

async function cleanupDelivered() {
  if (!confirm('确定清理全部已交付成品？\n会删除 output_finish 下的成稿目录，文章与素材保留，可重新交付。')) return;
  const btn = $('#btn-pipeline-clean');
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/finish/cleanup', {
      method: 'POST', body: JSON.stringify({ all: true }),
    });
    toast(`已清理 ${res.articles || 0} 篇（${res.dirs || 0} 个目录），释放 ${fmtSize(res.freed_bytes || 0)}`, 'ok');
    await Promise.all([refreshPipeline(), refreshStats()]);
  } finally { if (btn) btn.disabled = false; }
}

async function runPipelineAll() {
  const btn = $('#btn-pipeline-run');
  btn.disabled = true;
  try {
    const res = await api('/api/pipeline/run', { method: 'POST' });
    const e = res.enqueued || {};
    toast(`已入队：抓取 ${e.fetch || 0} · 素材 ${e.media || 0} · 改写 ${e.rewrite || 0}`, 'ok');
    setTimeout(safe(refreshPipeline), 1200);
  } catch (err) { toast(err.message || '启动失败', 'error'); }
  finally { btn.disabled = false; }
}

/* ---------------- 发布中心 ---------------- */
const pubState = { stats: null };

async function refreshPublishCenter() {
  const status = ($('#pub-filter-status') || {}).value || '';
  const accId = ($('#pub-filter-account') || {}).value || '';
  let d;
  let stats;
  try {
    [d, stats] = await Promise.all([
      api('/api/publish/tasks?limit=200'),
      api('/api/publish/stats?days=14').catch(() => null),
    ]);
  } catch (e) { $('#publish-hint').textContent = '读取失败：' + (e.message || ''); return; }
  pubState.stats = stats;
  if (stats) renderPublishStats(stats);
  let items = d.items || [];
  if (status) items = items.filter((x) => String(x.status) === status);
  if (accId) items = items.filter((x) => String(x.account_id || '') === accId);
  const ok = items.filter((x) => x.status === 'success').length;
  const bad = items.filter((x) => x.status === 'failed').length;
  const rate = stats ? Math.round((stats.success_rate || 0) * 100) : 0;
  $('#publish-hint').textContent =
    `共 ${items.length} 条 · 成功 ${ok} · 失败 ${bad}`
    + (stats ? ` · 总体成功率 ${rate}% · 涉及账号 ${(stats.by_account || []).length} 个` : '');
  $('#publish-center').innerHTML = items.length
    ? items.map(pubTaskRow).join('')
    : '<div class="empty">还没有发布记录，去「热点资讯 → 排版 → 发布到…」发起一次</div>';
}

function pubBar(label, chip, total, ok, bad, max, icon) {
  const w = (n) => `${Math.max(0, Math.min(100, (Number(n || 0) / Math.max(1, max)) * 100)).toFixed(1)}%`;
  return `<div class="bar-row">
    <span class="bar-label" title="${escapeHtml(label)}">${icon || ''}${escapeHtml(label)}${chip ? ` <span class="chip">${escapeHtml(chip)}</span>` : ''}</span>
    <span class="bar-track">
      <span class="bar-ok" style="width:${w(ok)}"></span>
      <span class="bar-bad" style="width:${w(bad)}"></span>
    </span>
    <span class="bar-val">${ok || 0}/${total || 0}</span>
  </div>`;
}

function renderPublishStats(s) {
  const box = $('#publish-stats');
  if (!box) return;
  const st = s.by_status || {};
  const cards = [
    ['发布任务', s.total || 0, ''],
    ['成功', st.success || 0, 'green'],
    ['失败', st.failed || 0, 'red'],
    ['成功率', `${Math.round((s.success_rate || 0) * 100)}%`, 'blue'],
  ];
  const accounts = s.by_account || [];
  const maxAcc = Math.max(1, ...accounts.map((a) => Number(a.total) || 0));
  const platforms = s.by_platform || [];
  const maxPlat = Math.max(1, ...platforms.map((p) => Number(p.total) || 0));
  const daily = s.daily || [];
  const maxDay = Math.max(1, ...daily.map((x) => Number(x.total) || 0));

  box.innerHTML = `
    <div class="stat-cards">${cards.map(([label, value, cls]) =>
      `<div class="stat-card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`).join('')}</div>
    <div class="chart-grid">
      <div class="cfg-card">
        <div class="cfg-head"><span class="cfg-name">按账号</span>
          <span class="muted">绿色=成功 · 红色=失败</span></div>
        <div class="bar-list">${accounts.length
          ? accounts.map((a) => pubBar(a.name, a.deleted ? '账号已删' : '',
              a.total, a.success, a.failed, maxAcc, platChip(a.platform))).join('')
          : '<div class="muted">还没有发布记录</div>'}</div>
      </div>
      <div class="cfg-card">
        <div class="cfg-head"><span class="cfg-name">按入口</span>
          <span class="muted">平台 · 形态（头条图文 / 微头条 / 百家图文 / 动态）各自成功率</span></div>
        <div class="bar-list">${platforms.length
          ? platforms.map((p) => pubBar('', '', p.total, p.success, p.failed, maxPlat,
              platChip(p.platform, variantLabel({ target_platform: p.platform,
                content_form: p.content_form })))).join('')
          : '<div class="muted">还没有发布记录</div>'}</div>
      </div>
      <div class="cfg-card">
        <div class="cfg-head"><span class="cfg-name">近 ${s.days || 14} 天发布量</span>
          <span class="muted">柱高=当日任务数</span></div>
        <div class="trend">${daily.length
          ? daily.map((x) => `<div class="trend-col" title="${escapeHtml(x.date)}：${x.total} 条（成功 ${x.success || 0}）">
              <div class="trend-bar" style="height:${Math.max(4, (Number(x.total) / maxDay) * 72).toFixed(0)}px"></div>
              <div class="trend-label">${escapeHtml(String(x.date || '').slice(5))}</div>
            </div>`).join('')
          : '<div class="muted">近 14 天没有发布记录</div>'}</div>
      </div>
    </div>`;
  syncPubAccountFilter(accounts);
}

function syncPubAccountFilter(accounts) {
  const sel = $('#pub-filter-account');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">全部账号</option>' + accounts.map((a) => {
    const tag = PLATFORM_BADGE[a.platform] || a.platform || '';
    return `<option value="${a.id}">${tag ? `【${escapeHtml(tag)}】` : ''}${escapeHtml(a.name || ('账号#' + a.id))}（${a.total} 条）</option>`;
  }).join('');
  sel.value = current;
}

function pubTaskRow(t) {
  const cls = { success: 'success', failed: 'failed', cancelled: 'failed',
                running: 'queued', pending: 'queued', interrupted: 'failed' }[t.status] || '';
  const busy = t.status === 'pending' || t.status === 'running';
  const canRetry = ['failed', 'cancelled', 'interrupted'].includes(t.status);
  const form = t.content_form || 'article';
  return `<div class="place-row" data-acc="${escapeHtml(String(t.account_id || ''))}">
    <span class="tag ${cls}">${escapeHtml(PUB_STATUS_TEXT[t.status] || t.status || '')}</span>
    ${platChip(t.platform)}
    <span class="chip">${escapeHtml(formBadge(t.platform, form))}</span>
    <div class="place-info">
      <div class="place-reason">${escapeHtml((t.title
        || (form === 'micro' ? `（${formBadge(t.platform, form)} · 无标题）` : '(无标题)')).slice(0, 46))}</div>
      <div class="muted">
        账号：<b>${escapeHtml(t.account_name || ('账号#' + (t.account_id || '-')))}</b>
        · ${escapeHtml(String(t.created_at || '').slice(5, 16))}
        ${t.image_count ? ` · 图 ${t.image_count} 张` : ''}
        ${t.error ? ' · ' + escapeHtml(String(t.error).slice(0, 80)) : ''}
      </div>
    </div>
    <div class="grow"></div>
    ${t.post_url ? `<a class="btn small ghost" href="${escapeHtml(t.post_url)}" target="_blank" rel="noreferrer">打开</a>` : ''}
    ${canRetry ? `<button class="btn small" data-pub-retry="${t.id}">重试</button>` : ''}
    ${busy ? `<button class="btn small ghost" data-pub-cancel="${t.id}">取消</button>` : ''}
    <button class="btn small danger" data-pub-del="${t.id}">删除</button>
  </div>`;
}

/* ---------------- 账号管理 ---------------- */
const accState = { platform: '', meta: null, items: [], editingId: null, polling: new Map() };

async function initAccounts() {
  if (!accState.meta) {
    const data = await api('/api/accounts');
    accState.meta = data.meta;
    $('#acc-platform').innerHTML =
      `<button class="seg${accState.platform ? '' : ' active'}" data-plat="">全部</button>` +
      accState.meta.platforms.map((p) =>
        `<button class="seg${accState.platform === p.key ? ' active' : ''}" data-plat="${escapeHtml(p.key)}">${platMark(p.key)}${escapeHtml(p.title)}</button>`).join('');
  }
  await refreshAccounts();
}

async function refreshAccounts() {
  const data = await api('/api/accounts' + (accState.platform ? `?platform=${encodeURIComponent(accState.platform)}` : ''));
  accState.items = data.items || [];
  accState.meta = data.meta || accState.meta;
  $('#acc-list').innerHTML = accState.items.map(accountCardHtml).join('');
  $('#acc-empty').classList.toggle('hidden', accState.items.length > 0);
  const expiring = accState.items.filter((a) => ['expired', 'expiring', 'empty'].includes(a.cookie_state)).length;
  $('#acc-hint').textContent = accState.meta.browser_ready
    ? (expiring ? `${expiring} 个账号需要刷新 cookie` : '所有账号 cookie 正常')
    : `浏览器不可用：${accState.meta.hint}`;
}

function cookieTag(a) {
  const map = {
    valid: ['success', '有效'], expiring: ['queued', '即将过期'], expired: ['failed', '已过期'],
    empty: ['', '未抓取'], unknown: ['', '状态未知'], error: ['failed', '刷新失败'],
  };
  const hit = map[a.cookie_state] || ['', a.cookie_state || '未知'];
  return `<span class="tag ${hit[0]}">${hit[1]}</span>`;
}

function accountCardHtml(a) {
  const busy = accState.polling.has(a.id);
  return `<div class="cfg-card" data-id="${a.id}">
    <div class="cfg-head">
      <span class="cfg-name">${escapeHtml(a.name || '(未命名)')}</span>
      ${platChip(a.platform, a.platform_title || PLATFORM_BADGE[a.platform] || a.platform)}
      ${a.enabled ? '' : '<span class="tag">已停用</span>'}
      ${cookieTag(a)}
      <div class="grow"></div>
      <button class="btn small primary" data-aact="qrcode"${busy ? ' disabled' : ''}>${busy ? '登录中…' : '扫码登录'}</button>
      <details class="acc-more">
        <summary class="btn small ghost">更多</summary>
        <div class="acc-more-body">
          <button class="btn small ghost" data-aact="login"${busy ? ' disabled' : ''}>有头刷新 Cookie</button>
          <button class="btn small ghost" data-aact="paste">粘贴 Cookie</button>
          <button class="btn small ghost" data-aact="test">测登录</button>
          <button class="btn small ghost" data-aact="insight">看数据</button>
          ${a.platform === 'toutiao' ? '<button class="btn small ghost" data-aact="probe">探测端点</button>' : ''}
          <button class="btn small ghost" data-aact="auto">${a.auto_refresh ? '自动巡检：开' : '自动巡检：关'}</button>
          <button class="btn small" data-aact="edit">编辑</button>
          <button class="btn small ghost" data-aact="toggle">${a.enabled ? '停用' : '启用'}</button>
          <div class="acc-more-sep"></div>
          <div class="acc-more-tip">危险操作（需二次确认）</div>
          <button class="btn small danger" data-aact="del">删除账号</button>
        </div>
      </details>
    </div>
    <div class="cfg-meta">
      <span>登录名 <b class="${a.login_user ? 'acc-login' : 'muted'}">${escapeHtml(a.login_user || '未获取')}</b></span>
      <span>首页 ${escapeHtml(a.home_url || '-')}</span>
      <span>数据目录 ${escapeHtml(a.profile_dir || '<b class="muted">未配置</b>')}</span>
      <span>cookie ${a.cookie_count || 0} 条</span>
      <span>过期 ${escapeHtml(a.cookie_expire_at || '-')}</span>
      <span>检查 ${escapeHtml(a.last_checked_at || '-')}</span>
    </div>
    ${a.last_error ? `<div class="cfg-note err">${escapeHtml(a.last_error)}</div>` : ''}
    ${a.note ? `<div class="cfg-note">备注：${escapeHtml(a.note)}</div>` : ''}
    <div class="cfg-test hidden" data-role="refresh-box"></div>
  </div>`;
}

async function onAccountClick(e) {
  const card = e.target.closest('.cfg-card');
  if (!card) return;
  const id = Number(card.dataset.id);
  const act = e.target.dataset.aact;
  const a = accState.items.find((x) => x.id === id);
  // 选中「更多」里的任何动作后先收起面板，避免面板悬在页面上挡住后续操作
  const det = e.target.closest('.acc-more');
  if (act && det) det.open = false;
  if (act === 'edit') { await openAccountForm(id); return; }
  if (act === 'del') {
    const btn = e.target.closest('[data-aact="del"]');
    // 二次确认：第一次点击只把按钮切成「确认删除」，5 秒内再点一次才真正删除，
    // 防止从「更多」里点偏直接把账号删掉（删除会连带清空登录态，不可恢复）。
    if (btn && btn.dataset.armed !== '1') {
      btn.dataset.armed = '1';
      btn.textContent = '确认删除（再点一次）';
      btn.classList.add('armed');
      toast(`再点一次删除账号「${a ? a.name : id}」，5 秒内有效`, 'warn');
      setTimeout(() => {
        if (!btn.isConnected) return;
        btn.dataset.armed = '';
        btn.textContent = '删除账号';
        btn.classList.remove('armed');
      }, 5000);
      return;
    }
    const r = await api(`/api/accounts/${id}`, { method: 'DELETE' });
    toast(r.freed_bytes
      ? `已删除，释放 ${(r.freed_bytes / 1024 / 1024).toFixed(1)} MB`
      : '已删除', 'ok');
    await refreshAccounts();
    return;
  }
  if (act === 'toggle') {
    await api(`/api/accounts/${id}/enable`, { method: 'POST', body: JSON.stringify({ enabled: !a.enabled }) });
    await refreshAccounts();
    return;
  }
  if (act === 'auto') {
    await api(`/api/accounts/${id}`, { method: 'PATCH', body: JSON.stringify({ auto_refresh: a.auto_refresh ? 0 : 1 }) });
    toast(a.auto_refresh ? '已关闭自动续期' : '已开启自动续期', 'ok');
    await refreshAccounts();
    return;
  }
  if (act === 'insight') { await showAccountInsight(id, a); return; }
  if (act === 'qrcode') { await startAccountLogin(id, card); return; }
  if (act === 'login') { await startAccountRefresh(id, card); return; }
  if (act === 'paste') {
    const box = accountBox(card);
    box.innerHTML = `
      <div class="muted">在浏览器里登录后把 Cookie 粘进来：
        <b>F12 → Network → 随便点一条请求 → Request Headers → Cookie</b> 整行复制；
        或用 EditThisCookie / Cookie-Editor 插件导出 JSON 数组。</div>
      <textarea id="paste-ck-${id}" class="input block" rows="4"
        placeholder="name=value; name2=value2"></textarea>
      <button class="btn small primary" data-boxact="paste-save">保存 Cookie</button>
      <button class="btn small ghost" data-boxact="paste-cancel">取消</button>`;
    return;
  }
  if (act === 'test') {
    toast('正在测登录…', 'ok');
    const res = await api(`/api/accounts/${id}/test`, { method: 'POST' });
    const u = res.auth || {};
    toast(res.ok ? `${res.driver.name}：已登录${u.username ? '（' + u.username + '）' : ''}` : (res.auth.error || '未登录'),
      res.ok ? 'ok' : 'error');
    return;
  }
  if (act === 'probe') {
    const box = accountBox(card);
    box.innerHTML = '<span class="muted">正在打开创作页抓接口，约需 20-30 秒（会弹出浏览器窗口，别关）…</span>';
    toast('正在抓头条真实接口…', 'ok');
    let res;
    try {
      res = await api(`/api/accounts/${id}/probe`, { method: 'POST' });
    } catch (e) {
      box.innerHTML = `<span class="err">${escapeHtml(e.message || '探测失败')}</span>`;
      return;
    }
    const b = res.buckets || {};
    const label = { user_info: '用户信息', upload: '图片上传', publish: '发布 / 存草稿' };
    const rows = Object.keys(label).map((k) => {
      const urls = b[k] || [];
      return `<div class="probe-row">
        <span class="chip">${label[k]}</span>
        <div class="place-info">${urls.length
          ? urls.map((u) => `<div class="muted"><code>${escapeHtml(u)}</code></div>`).join('')
          : '<div class="muted">未抓到</div>'}</div>
      </div>`;
    }).join('');
    box.innerHTML = `
      <div class="cfg-note">${res.saved
        ? '已抓到头条真实接口并存入账号，发布时直接用这套，不用再抓。'
        : '没抓到可用的接口，确认浏览器里是登录状态后再试。'}</div>
      <div class="place-list">${rows}</div>
      <details class="probe-detail">
        <summary class="muted">页面请求明细（${(res.items || []).length} 条）</summary>
        ${(res.items || []).map((it) => `<div class="muted">
          <b>${escapeHtml(it.name || '')}</b>${it.method ? ' ' + escapeHtml(it.method) : ''}
          ${it.status ? ' · HTTP ' + it.status : ''}
          <code>${escapeHtml((it.url || it.text || '').slice(0, 90))}</code>
        </div>`).join('')}
      </details>`;
    return;
  }
  const boxAct = e.target.dataset.boxact;
  if (boxAct === 'save') { await finishAccountRefresh(id, true); return; }
  if (boxAct === 'cancel') { await finishAccountRefresh(id, false); return; }
  if (boxAct === 'paste-cancel') { await refreshAccounts(); return; }
  if (boxAct === 'paste-save') {
    const txt = ($(`#paste-ck-${id}`) || {}).value || '';
    if (!txt.trim()) { toast('粘贴内容为空', 'warn'); return; }
    try {
      const res = await api(`/api/accounts/${id}/cookie`, {
        method: 'POST', body: JSON.stringify({ cookie: txt.trim() }),
      });
      toast(`已导入 ${res.item.cookie_count} 条 cookie，点「测登录」验证`, 'ok');
    } catch (err) { toast(err.message || '导入失败', 'error'); return; }
    await refreshAccounts();
  }
}

function accountBox(card) {
  let box = card.querySelector('[data-role=refresh-box]');
  if (!box) { card.insertAdjacentHTML('beforeend', '<div class="cfg-test" data-role="refresh-box"></div>'); box = card.querySelector('[data-role=refresh-box]'); }
  box.classList.remove('hidden');
  return box;
}

/* -------- 账号数据（只读采集） -------- */
async function showAccountInsight(id, a) {
  $('#drawer-title').textContent = `账号数据 · ${a ? (a.name || '') : id}`;
  $('#drawer-body').innerHTML =
    '<div class="muted">正在采集（约 20~40 秒，会在后台打开无头浏览器）…</div>';
  $('#drawer').classList.add('wide');
  openDrawer();
  try {
    const res = await api(`/api/insight/collect/${id}`, {
      method: 'POST', body: JSON.stringify({}),
    });
    if (!res.ok) {
      $('#drawer-body').innerHTML =
        `<div class="err">${escapeHtml(res.error || '采集失败')}</div>`;
      return;
    }
    const d = await api(`/api/insight/overview?account_id=${id}`);
    renderInsight(d, res);
  } catch (e) {
    $('#drawer-body').innerHTML =
      `<div class="err">${escapeHtml(e.message || '采集失败')}</div>`;
  }
}

function renderInsight(d, res) {
  const t = d.totals || {};
  const works = d.works || [];
  const acc = d.account || {};
  const last = (d.snapshots || [])[0] || {};
  const head = [acc.name, acc.fans ? `粉丝 ${fmtNum(acc.fans)}` : '',
                acc.media_id ? `ID ${acc.media_id}` : ''].filter(Boolean).join(' · ');
  $('#drawer-body').innerHTML = `
    ${head ? `<div class="drawer-note">${escapeHtml(head)}</div>` : ''}
    <div class="meta-grid">
      <div>作品<b>${t.works || 0}</b></div>
      <div>总展现<b>${fmtNum(t.show || 0)}</b></div>
      <div>总阅读<b>${fmtNum(t.read || 0)}</b></div>
      <div>评论<b>${fmtNum(t.comment || 0)}</b></div>
    </div>
    <div class="section-title bar"><span>作品数据</span>
      <span class="diff-stat">采集于 ${escapeHtml(last.captured_at || '-')}</span></div>
    <div class="place-list">${works.length ? works.map((w) => `<div class="place-row">
      <span class="chip">${escapeHtml(w.status || '已发布')}</span>
      <div class="place-info">
        <div class="place-reason">${escapeHtml((w.title || '').slice(0, 46))}</div>
        <div class="muted">展现 ${fmtNum(w.show_count)} · 阅读 ${fmtNum(w.read_count)} · 评论 ${fmtNum(w.comment_count)}</div>
      </div>
      <div class="grow"></div>
      <span class="muted">${escapeHtml(w.publish_time || '')}</span>
    </div>`).join('') : `<div class="muted">这个账号还没有已发布作品（草稿不算），所以没有作品数据。
      发布并通过审核后再采集一次就能看到阅读 / 展现。</div>`}</div>
    <div class="drawer-note">命中接口：${escapeHtml(JSON.stringify(res.kinds || {}))}</div>`;
}

/* -------- 扫码登录：二维码由后端截图，这里只负责展示和轮询 -------- */
async function startAccountLogin(id, card) {
  if (!accState.meta.browser_ready) { toast(accState.meta.hint, 'error'); return; }
  let res;
  try {
    res = await api(`/api/accounts/${id}/login`, { method: 'POST' });
  } catch (e) { toast(e.message || '启动登录失败', 'error'); return; }
  accState.polling.set(id, true);
  accountBox(card).innerHTML =
    `<span class="muted">${escapeHtml((res.state || {}).message || '正在打开登录页…')}</span>`;
  pollAccountLogin(id);
}

function pollAccountLogin(id) {
  const tick = async () => {
    let data;
    try { data = await api(`/api/accounts/${id}/refresh`); }
    catch (e) { setTimeout(safe(tick), 3000); return; }
    const st = data.state || {};
    const card = document.querySelector(`.cfg-card[data-id="${id}"]`);
    if (card && st.running) {
      // qr_at 当缓存键：二维码没换就不重新拉图，换了自然取到新图
      const stamp = st.qr_at || 0;
      accountBox(card).innerHTML = `
        ${stamp
          ? `<div class="qr-wrap"><img class="qr-img" src="/api/accounts/${id}/login/qr?t=${stamp}" alt="登录二维码" /></div>`
          : '<div class="muted">正在生成二维码…</div>'}
        <div class="muted">${escapeHtml(st.message || '请用手机 App 扫码登录')}</div>
        <button class="btn small ghost" data-boxact="cancel">取消</button>`;
    }
    if (!st.running) {
      accState.polling.delete(id);
      const msg = st.message || '';
      await refreshAccounts();
      const c = document.querySelector(`.cfg-card[data-id="${id}"]`);
      if (c && msg) {
        accountBox(c).innerHTML =
          `<span class="${st.phase === 'done' ? 'ok' : 'err'}">${escapeHtml(msg)}</span>`;
      }
      if (st.phase === 'done') toast(msg || '登录成功', 'ok');
      return;
    }
    setTimeout(safe(tick), 2500);
  };
  tick();
}

async function startAccountRefresh(id, card) {
  if (!accState.meta.browser_ready) { toast(accState.meta.hint, 'error'); return; }
  let res;
  try {
    res = await api(`/api/accounts/${id}/refresh`, { method: 'POST', body: JSON.stringify({ mode: 'manual' }) });
  } catch (e) { toast(e.message || '启动失败', 'error'); return; }
  accState.polling.set(id, true);
  accountBox(card).innerHTML = `<span class="muted">${escapeHtml(res.state.message || '正在启动浏览器…')}</span>`;
  pollAccountRefresh(id);
}

function pollAccountRefresh(id) {
  const tick = async () => {
    const card = document.querySelector(`.cfg-card[data-id="${id}"]`);
    let data;
    try {
      data = await api(`/api/accounts/${id}/refresh`);
    } catch (e) { return; }
    const st = data.state || {};
    if (card) {
      const box = accountBox(card);
      const msg = escapeHtml(st.message || '');
      box.innerHTML = st.running
        ? `<span class="muted">${msg}</span>${st.phase === 'waiting'
          ? ' <button class="btn small" data-boxact="save">我已登录，保存 Cookie</button>'
            + ' <button class="btn small ghost" data-boxact="cancel">取消</button>' : ''}`
        : `<span class="${st.phase === 'done' ? 'ok' : st.phase === 'failed' ? 'err' : ''}">${msg}</span>`;
    }
    if (!st.running) {
      accState.polling.delete(id);
      const msg = st.message || '';
      await refreshAccounts();
      // 列表重渲染会清掉卡片上的提示，失败信息要重新贴回去，不能一闪而过
      if (st.phase === 'failed' && msg) {
        const c = document.querySelector(`.cfg-card[data-id="${id}"]`);
        if (c) accountBox(c).innerHTML = `<span class="err">${escapeHtml(msg)}</span>`;
      }
      return;
    }
    setTimeout(safe(tick), 2000);
  };
  tick();
}

async function finishAccountRefresh(id, save) {
  try {
    if (save) {
      const res = await api(`/api/accounts/${id}/refresh/save`, { method: 'POST' });
      toast(`已保存 ${res.item.cookie_count} 条 cookie`, 'ok');
    } else {
      await api(`/api/accounts/${id}/refresh/cancel`, { method: 'POST' });
      toast('已取消', 'ok');
    }
  } catch (e) {
    const msg = e.message || '操作失败';
    const card = document.querySelector(`.cfg-card[data-id="${id}"]`);
    if (card) {
      const box = accountBox(card);
      box.innerHTML = `<span class="err">${escapeHtml(msg)}</span>`;
    }
    toast(msg, 'error');
  }
  accState.polling.delete(id);
  await refreshAccounts();
}

/* -------- 账号表单 -------- */
function openAccModal(title) {
  $('#acc-modal-title').textContent = title;
  $('#acc-modal-msg').classList.add('hidden');
  $('#acc-modal').classList.remove('hidden');
  $('#acc-modal-mask').classList.remove('hidden');
}
function closeAccModal() {
  $('#acc-modal').classList.add('hidden');
  $('#acc-modal-mask').classList.add('hidden');
  accState.editingId = null;
}

async function openAccountForm(id) {
  accState.editingId = id || null;
  const plats = accState.meta.platforms;
  $('#a-profile_dir').placeholder =
    `留空自动生成，如 ${accState.meta.profile_root || '/var/lib/autonews/profiles'}/${plats[0].key}-xxx`;
  $('#a-platform').innerHTML = plats.map((p) =>
    `<option value="${escapeHtml(p.key)}">${escapeHtml(p.title)}</option>`).join('');
  if (id) {
    const a = accState.items.find((x) => x.id === id) || await api(`/api/accounts/${id}`).then((d) => d.item || d);
    $('#a-name').value = a.name || '';
    $('#a-platform').value = a.platform || 'custom';
    $('#a-login_user').value = a.login_user || '';
    $('#a-home_url').value = a.home_url || '';
    $('#a-profile_dir').value = a.profile_dir || '';
    $('#a-executable_path').value = a.executable_path || '';
    $('#a-proxy').value = a.proxy || '';
    $('#a-refresh_hours').value = a.refresh_hours || 12;
    $('#a-enabled').checked = !!a.enabled;
    $('#a-headless').checked = !!a.headless;
    $('#a-auto_refresh').checked = !!a.auto_refresh;
    $('#a-note').value = a.note || '';
    openAccModal('编辑账号');
  } else {
    ['a-name', 'a-login_user', 'a-home_url', 'a-profile_dir', 'a-executable_path', 'a-proxy', 'a-note']
      .forEach((k) => { $('#' + k).value = ''; });
    $('#a-platform').value = accState.platform || 'toutiao';
    $('#a-refresh_hours').value = 12;
    $('#a-enabled').checked = true;
    $('#a-headless').checked = false;
    $('#a-auto_refresh').checked = true;
    openAccModal('新增账号');
  }
}

function accModalMsg(text, bad) {
  const box = $('#acc-modal-msg');
  box.textContent = text;
  box.classList.remove('hidden');
  box.className = `drawer-note${bad ? ' err' : ''}`;
}

function accountPayload() {
  return {
    name: $('#a-name').value.trim(),
    platform: $('#a-platform').value,
    login_user: $('#a-login_user').value.trim(),
    home_url: $('#a-home_url').value.trim(),
    profile_dir: $('#a-profile_dir').value.trim(),
    executable_path: $('#a-executable_path').value.trim(),
    proxy: $('#a-proxy').value.trim(),
    refresh_hours: Number($('#a-refresh_hours').value) || 12,
    enabled: $('#a-enabled').checked ? 1 : 0,
    headless: $('#a-headless').checked ? 1 : 0,
    auto_refresh: $('#a-auto_refresh').checked ? 1 : 0,
    note: $('#a-note').value.trim(),
  };
}

async function saveAccountForm() {
  const payload = accountPayload();
  if (!payload.name) { accModalMsg('请填写账号名称', true); return; }
  try {
    if (accState.editingId) {
      await api(`/api/accounts/${accState.editingId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    } else {
      await api('/api/accounts', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeAccModal();
    toast('已保存', 'ok');
    await refreshAccounts();
  } catch (e) { accModalMsg(e.message || '保存失败', true); }
}

async function accModalLogin() {
  const payload = accountPayload();
  if (!payload.profile_dir) { accModalMsg('先填写浏览器用户数据目录', true); return; }
  try {
    const id = accState.editingId || (await api('/api/accounts', { method: 'POST', body: JSON.stringify(payload) })).id;
    if (!accState.editingId) {
      accState.editingId = id;
      toast('账号已保存，正在打开浏览器', 'ok');
    }
    await api(`/api/accounts/${id}/refresh`, { method: 'POST', body: JSON.stringify({ mode: 'manual' }) });
    closeAccModal();
    accState.polling.set(id, true);
    await refreshAccounts();
    const card = document.querySelector(`.cfg-card[data-id="${id}"]`);
    if (card) accountBox(card).innerHTML = '<span class="muted">浏览器已打开，请在里面登录</span>';
    pollAccountRefresh(id);
  } catch (e) { accModalMsg(e.message || '启动失败', true); }
}

/* ---------------- init ---------------- */
(async function init() {
  // 把未捕获错误显式展示出来，调试时一眼看到原因
  window.addEventListener('error', (e) => toast('脚本错误：' + (e.message || e.error?.message || '未知')));
  window.addEventListener('unhandledrejection', (e) => toast('异步错误：' + (e.reason?.message || e.reason || '未知')));
  try {
    initUpload();
    initEvents();
    switchSettingsSub(cfgState.sub);
  } catch (e) { toast('初始化失败：' + (e.message || e)); }
  const tasks = [refreshStats(), loadDicts(), refreshGroups(), refreshCache()];
  const results = await Promise.allSettled(tasks);
  results.forEach((r) => r.status === 'rejected' && toast('加载失败：' + (r.reason?.message || r.reason)));
  // 页面打开就接管进度：上一轮没跑完的抓取/采样/改写会自动继续显示进度
  resumePollingFromDom();
  initAutoRefresh();
})();
