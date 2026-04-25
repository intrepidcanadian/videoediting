// Trailer Maker — review-as-you-go UI
//
// State machine. The user moves through four phases per run:
//   storyboard → keyframes → shots → stitch
//
// Each phase has an approval gate. Regenerating an individual keyframe/shot keeps
// the rest. We poll GET /api/runs/<id> every 2s while anything is "generating".

const _inflight = new Set();
function guard(key, fn) {
  if (_inflight.has(key)) return Promise.resolve();
  _inflight.add(key);
  return fn().finally(() => _inflight.delete(key));
}

// Build an asset URL with percent-encoded path segments. Backend paths usually
// look like "keyframes/shot_01.png"; unusual characters in uploaded filenames
// (spaces, #, ?, &, %) would otherwise break the URL or mis-route.
function assetUrl(runId, relPath, ts) {
  if (!relPath) return '';
  const encoded = String(relPath).split('/').map(encodeURIComponent).join('/');
  const qs = ts ? `?ts=${encodeURIComponent(ts)}` : '';
  return `/assets/${encodeURIComponent(runId)}/${encoded}${qs}`;
}

async function _r(resp) {
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { const d = await resp.json(); msg = d.detail || msg; } catch { /* resp body not JSON */ }
    throw new Error(msg);
  }
  return resp.json();
}

const api = {
  listRuns:  ()       => fetch('/api/runs').then(_r),
  getRun:    (id)     => fetch(`/api/runs/${id}`).then(_r),
  deleteRun: (id)     => fetch(`/api/runs/${id}`, { method: 'DELETE' }).then(_r),
  createRun: (form)   => fetch('/api/runs',           { method: 'POST', body: form }).then(_r),
  runStory:  (id, nOptions = 1) => {
                          const fd = new FormData(); fd.append('n_options', String(nOptions));
                          return fetch(`/api/runs/${id}/storyboard`, { method: 'POST', body: fd }).then(_r);
                        },
  pickStoryOption: (id, idx) => fetch(`/api/runs/${id}/storyboard/pick/${idx}`, { method: 'POST' }).then(_r),
  saveStory: (id, s)  => fetch(`/api/runs/${id}/storyboard`, {
                          method: 'PUT', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify(s) }).then(_r),
  runKf:     (id, i, override) => {
                          const fd = new FormData();
                          if (override != null) fd.append('prompt_override', override);
                          return fetch(`/api/runs/${id}/keyframes/${i}`, { method: 'POST', body: fd }).then(_r);
                        },
  editKf:    (id, i, editPrompt) => {
                          const fd = new FormData();
                          fd.append('edit_prompt', editPrompt);
                          return fetch(`/api/runs/${id}/keyframes/${i}/edit`, { method: 'POST', body: fd }).then(_r);
                        },
  runAllKf:  (id)     => fetch(`/api/runs/${id}/keyframes`, { method: 'POST' }).then(_r),
  runShot:   (id, i, override) => {
                          const fd = new FormData();
                          if (override != null) fd.append('prompt_override', override);
                          return fetch(`/api/runs/${id}/shots/${i}`, { method: 'POST', body: fd }).then(_r);
                        },
  runAllShots: (id)   => fetch(`/api/runs/${id}/shots`, { method: 'POST' }).then(_r),
  autoRegenFlagged: (id) => fetch(`/api/runs/${id}/cut-plan/auto-regen`, { method: 'POST' }).then(_r),
  refineTimeline: (id) => fetch(`/api/runs/${id}/cut-plan/refine-timeline`, { method: 'POST' }).then(_r),
  setPrimaryVariant: (id, shotIdx, variantIdx) =>
                          fetch(`/api/runs/${id}/shots/${shotIdx}/variants/${variantIdx}/primary`, { method: 'POST' }).then(_r),
  regenVariant: (id, shotIdx, variantIdx) =>
                          fetch(`/api/runs/${id}/shots/${shotIdx}/variants/${variantIdx}/regenerate`, { method: 'POST' }).then(_r),
  attachVideoRef: (id, idx, file, slotIdx = null) => {
                          const fd = new FormData(); fd.append('video', file);
                          if (slotIdx != null) fd.append('slot_idx', String(slotIdx));
                          return fetch(`/api/runs/${id}/shots/${idx}/video-ref`, { method: 'POST', body: fd }).then(_r);
                        },
  detachVideoRef: (id, idx) => fetch(`/api/runs/${id}/shots/${idx}/video-ref`, { method: 'DELETE' }).then(_r),
  detachVideoRefSlot: (id, idx, slotIdx) => fetch(`/api/runs/${id}/shots/${idx}/video-ref/${slotIdx}`, { method: 'DELETE' }).then(_r),
  ripUpload: (form)   => fetch('/api/rip/upload', { method: 'POST', body: form }).then(_r),
  discoverAssets: (id) => fetch(`/api/runs/${id}/assets/discover`, { method: 'POST' }).then(_r),
  uploadAsset: (id, assetId, file) => {
                          const fd = new FormData(); fd.append('file', file);
                          return fetch(`/api/runs/${id}/assets/${assetId}/upload`, { method: 'POST', body: fd }).then(_r);
                        },
  generateAsset: (id, assetId, promptOverride) => {
                          const fd = new FormData();
                          if (promptOverride != null) fd.append('prompt_override', promptOverride);
                          return fetch(`/api/runs/${id}/assets/${assetId}/generate`, { method: 'POST', body: fd }).then(_r);
                        },
  skipAsset: (id, assetId) => fetch(`/api/runs/${id}/assets/${assetId}/skip`, { method: 'POST' }).then(_r),
  generateAllAssets: (id) => fetch(`/api/runs/${id}/assets/generate-all`, { method: 'POST' }).then(_r),
  getLog: (id, since) => fetch(`/api/runs/${id}/log${since ? '?since=' + encodeURIComponent(since) : ''}`).then(_r),
  getCosts: (id) => fetch(`/api/runs/${id}/costs`).then(_r),
  getMusicScore: (id) => fetch(`/api/runs/${id}/music/score`).then(_r),
  attachMusic: (id, file) => {
    const fd = new FormData(); fd.append('audio', file);
    return fetch(`/api/runs/${id}/music`, { method: 'POST', body: fd }).then(_r);
  },
  detachMusic: (id) => fetch(`/api/runs/${id}/music`, { method: 'DELETE' }).then(_r),
  snapToMusic: (id) => fetch(`/api/runs/${id}/music/snap`, { method: 'POST' }).then(_r),
  generateTitleCard: (id, titleText, styleHint, holdSeconds, animate) => {
    const fd = new FormData();
    if (titleText) fd.append('title_text', titleText);
    if (styleHint) fd.append('style_hint', styleHint);
    fd.append('hold_seconds', String(holdSeconds || 2.5));
    fd.append('animate', animate ? 'true' : 'false');
    return fetch(`/api/runs/${id}/title-card`, { method: 'POST', body: fd }).then(_r);
  },
  removeTitleCard: (id) => fetch(`/api/runs/${id}/title-card`, { method: 'DELETE' }).then(_r),
  cloneRun: (id, newTitle) => {
    const fd = new FormData(); if (newTitle) fd.append('new_title', newTitle);
    return fetch(`/api/runs/${id}/clone`, { method: 'POST', body: fd }).then(_r);
  },
  audioStatus: () => fetch('/api/audio/status').then(_r),
  getTaste: () => fetch('/api/taste').then(_r),
  refreshTaste: () => fetch('/api/taste/refresh', { method: 'POST' }).then(_r),
  resetTaste: () => fetch('/api/taste', { method: 'DELETE' }).then(_r),
  sweepShot: (id, idx, n = 3) => fetch(`/api/runs/${id}/shots/${idx}/sweep`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ n }),
  }).then(_r),
  faceLockKf: (id, idx, refIdx = 0) => fetch(`/api/runs/${id}/keyframes/${idx}/face-lock`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reference_idx: refIdx }),
  }).then(_r),
  faceLockAll: (id, refIdx = 0) => fetch(`/api/runs/${id}/keyframes/face-lock-all`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reference_idx: refIdx }),
  }).then(_r),
  buildAnimatic: (id) => fetch(`/api/runs/${id}/animatic`, { method: 'POST' }).then(_r),
  listGenres: () => fetch('/api/genres').then(_r),
  composeMusic: (id, vibe = '') => fetch(`/api/runs/${id}/music/compose`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vibe }),
  }).then(_r),
  runContinuity: (id) => fetch(`/api/runs/${id}/continuity`, { method: 'POST' }).then(_r),
  listPlatformVariants: () => fetch('/api/platform-variants').then(_r),
  exportPlatformVariants: (id, presets, burnSubtitles = false) => fetch(`/api/runs/${id}/export`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ presets, burn_subtitles: burnSubtitles }),
  }).then(_r),
  buildSubtitles: (id, fmt = 'srt') => fetch(`/api/runs/${id}/subtitles`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format: fmt }),
  }).then(_r),
  listLibrary: (kind) => fetch('/api/library' + (kind ? `?kind=${kind}` : '')).then(_r),
  saveLibraryItem: (kind, formData) => fetch(`/api/library/${kind}`, { method: 'POST', body: formData }).then(_r),
  deleteLibraryItem: (kind, slug) => fetch(`/api/library/${kind}/${slug}`, { method: 'DELETE' }).then(_r),
  // Playground — standalone Seedance clip generation, no storyboard required.
  listPlaygroundClips: () => fetch('/api/playground/clips').then(_r),
  getPlaygroundClip: (clipId) => fetch(`/api/playground/clips/${clipId}`).then(_r),
  generatePlaygroundClip: (formData) => fetch('/api/playground/generate', { method: 'POST', body: formData }).then(_r),
  generatePlaygroundImage: (formData) => fetch('/api/playground/generate-image', { method: 'POST', body: formData }).then(_r),
  promotePlaygroundClip: (clipId, formData) => fetch(`/api/playground/clips/${clipId}/promote`, { method: 'POST', body: formData }).then(_r),
  deletePlaygroundClip: (clipId) => fetch(`/api/playground/clips/${clipId}`, { method: 'DELETE' }).then(_r),
  injectLibrary: (runId, kind, slug, target = 'references') => {
    const fd = new FormData(); fd.append('kind', kind); fd.append('slug', slug); fd.append('target', target);
    return fetch(`/api/runs/${runId}/library/inject`, { method: 'POST', body: fd }).then(_r);
  },
  promoteAsset: (runId, assetId, name, description, tags) => {
    const fd = new FormData();
    if (name) fd.append('name', name);
    if (description) fd.append('description', description);
    if (tags) fd.append('tags', tags);
    return fetch(`/api/runs/${runId}/assets/${assetId}/promote`, { method: 'POST', body: fd }).then(_r);
  },
  promoteAllAssets: (runId) => fetch(`/api/runs/${runId}/assets/promote-all`, { method: 'POST' }).then(_r),
  promoteToLibrary: (runId, kind, name, fileRelPaths, description, tags) => {
    const fd = new FormData();
    fd.append('kind', kind); fd.append('name', name);
    fd.append('file_rel_paths', fileRelPaths.join(','));
    if (description) fd.append('description', description);
    if (tags) fd.append('tags', tags);
    return fetch(`/api/runs/${runId}/library/promote`, { method: 'POST', body: fd }).then(_r);
  },
  listLooks: () => fetch('/api/looks').then(_r),
  setLook: (id, lookId) => fetch(`/api/runs/${id}/look`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ look: lookId }),
  }).then(_r),
  directorSend: (id, message) => fetch(`/api/runs/${id}/director`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  }).then(_r),
  directorHistory: (id) => fetch(`/api/runs/${id}/director`).then(_r),
  directorReset: (id) => fetch(`/api/runs/${id}/director`, { method: 'DELETE' }).then(_r),
  generateVoScript: (id, vibe) => fetch(`/api/runs/${id}/vo/script`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vibe: vibe || null }),
  }).then(_r),
  saveVoScript: (id, lines, voiceId) => fetch(`/api/runs/${id}/vo/script`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lines, voice_id: voiceId }),
  }).then(_r),
  synthesizeVo: (id) => fetch(`/api/runs/${id}/vo/synthesize`, { method: 'POST' }).then(_r),
  removeVo: (id) => fetch(`/api/runs/${id}/vo`, { method: 'DELETE' }).then(_r),
  previewSegments: (id, params) => {
    const fd = new FormData();
    for (const [k, v] of Object.entries(params || {})) fd.append(k, String(v));
    return fetch(`/api/runs/${id}/rip/preview-segments`, { method: 'POST', body: fd }).then(_r);
  },
  stitch:    (id, xf, useCutPlan = true) => {
                          const fd = new FormData();
                          fd.append('crossfade', xf ? 'true' : 'false');
                          fd.append('use_cut_plan', useCutPlan ? 'true' : 'false');
                          return fetch(`/api/runs/${id}/stitch`, { method: 'POST', body: fd }).then(_r);
                        },
  runCutPlan: (id)    => fetch(`/api/runs/${id}/cut-plan`, { method: 'POST' }).then(_r),
  saveCutPlan:(id, p) => fetch(`/api/runs/${id}/cut-plan`, {
                          method: 'PUT', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify(p) }).then(_r),
  deleteCutPlan: (id) => fetch(`/api/runs/${id}/cut-plan`, { method: 'DELETE' }).then(_r),
  ideate:    (form)   => fetch('/api/ideate/concepts', { method: 'POST', body: form }).then(_r),
  enhance:   (kind, text, context = {}) => fetch('/api/ideate/enhance', {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ kind, text, context }),
                        }).then(_r),
  ecomExtract: (url) => fetch('/api/ecommerce/extract', {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ url }),
                        }).then(_r),
  getRules: ()        => fetch('/api/rules').then(_r),
  saveRules: (data)   => fetch('/api/rules', {
                          method: 'PUT', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify(data),
                        }).then(_r),
  resetRules: ()      => fetch('/api/rules/reset', { method: 'POST' }).then(_r),
  testRules: (prompt, target) => fetch('/api/rules/test', {
                          method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ prompt, target }),
                        }).then(_r),
};

let _rulesBuffer = null;  // working copy, saved on click

const state = {
  view: 'runs',            // runs | new | run
  currentRunId: null,
  currentRun: null,
  pollTimer: null,
  logTimer: null,
  lastLogTs: null,
};

// ─── View routing ────────────────────────────────────────────────────────

function showView(name, runId = null) {
  state.view = name;
  stopPolling();
  stopLogPolling();
  document.querySelectorAll('section[id^="view-"]').forEach(el => el.classList.add('hidden'));
  document.getElementById(`view-${name}`).classList.remove('hidden');
  // Director toggle only shown in run view
  const dirToggle = document.getElementById('director-toggle');
  if (dirToggle) dirToggle.classList.toggle('hidden', name !== 'run');
  if (name !== 'run') document.getElementById('director-drawer')?.classList.add('hidden');
  document.querySelectorAll('.nav-btn').forEach(b => {
    const active = b.dataset.view === name;
    b.classList.toggle('text-white', active);
    b.classList.toggle('text-zinc-400', !active);
    // Lamp-amber underline on active nav (Cutting Room style)
    b.style.borderBottomColor = active ? 'var(--lamp)' : 'transparent';
  });

  const drawer = document.getElementById('log-drawer');
  if (name === 'runs') loadRuns();
  if (name === 'rules') loadRules();
  if (name === 'library') { loadLibrary(); _invalidateCastLibCache(); }
  if (name === 'taste') loadTaste();
  if (name === 'playground') loadPlayground();
  if (name === 'new') { loadCastPickers(); loadLocationPickers(); loadPropPickers(); }
  // Stop the playground poll when leaving the tab — it's pointless to keep
  // fetching /api/playground/clips when the user is elsewhere.
  if (name !== 'playground') stopPlaygroundPolling();
  if (name === 'run' && runId) {
    state.currentRunId = runId;
    document.getElementById('current-run-label').textContent = runId;
    refreshRun();
    drawer.classList.remove('hidden');
    resetLogView();
    startLogPolling();
  } else {
    document.getElementById('current-run-label').textContent = '';
    state.currentRunId = null;
    state.currentRun = null;
    drawer.classList.add('hidden');
  }
}

document.querySelectorAll('[data-view]').forEach(b => {
  b.addEventListener('click', () => showView(b.dataset.view));
});

// ─── Rules editor ────────────────────────────────────────────────────────

async function loadRules() {
  try {
    _rulesBuffer = await api.getRules();
    renderRules();
  } catch (err) {
    toast('Load rules failed: ' + (err.message || err));
  }
}

function renderRules() {
  const list = document.getElementById('rules-list');
  const empty = document.getElementById('rules-empty');
  list.innerHTML = '';
  const rules = (_rulesBuffer && _rulesBuffer.rules) || [];
  if (!rules.length) {
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  // Group by target
  const groups = {};
  rules.forEach((r, idx) => {
    const t = r.target || '(no target)';
    if (!groups[t]) groups[t] = [];
    groups[t].push({ rule: r, idx });
  });

  for (const [target, items] of Object.entries(groups)) {
    const header = document.createElement('div');
    header.className = 'text-xs font-mono text-zinc-500 uppercase tracking-wider mt-4 mb-2';
    header.textContent = target;
    list.appendChild(header);
    for (const { rule, idx } of items) list.appendChild(ruleCard(rule, idx));
  }
}

function ruleCard(rule, idx) {
  const card = document.createElement('div');
  const borderColor = rule.enabled !== false ? 'border-zinc-800' : 'border-zinc-900 opacity-50';
  card.className = `bg-zinc-900/40 border ${borderColor} rounded p-3 text-xs`;
  const KIND_LABEL = {
    strip_regex: 'strip regex',
    strip_phrases: 'strip phrases',
    append: 'append',
    prepend: 'prepend',
    replace_regex: 'replace regex',
    clamp_length: 'clamp length',
  };
  card.innerHTML = `
    <div class="flex items-center gap-3 mb-2">
      <label class="flex items-center gap-2 cursor-pointer">
        <input type="checkbox" ${rule.enabled !== false ? 'checked' : ''} data-field="enabled">
        <span class="font-semibold text-zinc-200" data-field="name-display">${escapeHtml(rule.name || '(unnamed)')}</span>
      </label>
      <span class="text-[10px] px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400 font-mono">${KIND_LABEL[rule.kind] || rule.kind}</span>
      <span class="text-[10px] text-zinc-600 font-mono">${rule.id || ''}</span>
      <div class="flex-1"></div>
      <button class="text-[11px] text-zinc-500 hover:text-white" data-action="toggle-body">▾ details</button>
      <button class="text-[11px] text-zinc-500 hover:text-red-400" data-action="delete">✕</button>
    </div>
    <div class="space-y-2 hidden" data-section="body">
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Name</label>
          <input type="text" value="${escapeAttr(rule.name || '')}" data-field="name" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs">
        </div>
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Target</label>
          <select data-field="target" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs">
            ${['nano_banana_keyframe', 'nano_banana_edit', 'nano_banana_title', 'nano_banana_asset', 'seedance_motion'].map(t =>
              `<option value="${t}" ${t === rule.target ? 'selected' : ''}>${t}</option>`
            ).join('')}
          </select>
        </div>
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Kind</label>
          <select data-field="kind" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs">
            ${Object.keys(KIND_LABEL).map(k =>
              `<option value="${k}" ${k === rule.kind ? 'selected' : ''}>${KIND_LABEL[k]}</option>`
            ).join('')}
          </select>
        </div>
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">ID</label>
          <input type="text" value="${escapeAttr(rule.id || '')}" data-field="id" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">
        </div>
      </div>

      <!-- Kind-specific fields -->
      ${rule.kind === 'strip_regex' || rule.kind === 'replace_regex' ? `
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Pattern (regex)</label>
          <input type="text" value="${escapeAttr(rule.pattern || '')}" data-field="pattern" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">
        </div>
        <div class="grid grid-cols-[1fr_auto] gap-2">
          ${rule.kind === 'replace_regex' ? `
            <div>
              <label class="block text-[10px] text-zinc-500 mb-0.5">Replacement</label>
              <input type="text" value="${escapeAttr(rule.replacement || '')}" data-field="replacement" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">
            </div>
          ` : '<div></div>'}
          <div>
            <label class="block text-[10px] text-zinc-500 mb-0.5">Flags</label>
            <input type="text" value="${escapeAttr(rule.flags || '')}" data-field="flags" placeholder="i,m,s" class="w-20 bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">
          </div>
        </div>
      ` : ''}
      ${rule.kind === 'strip_phrases' ? `
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Phrases (one per line)</label>
          <textarea rows="3" data-field="phrases" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">${(rule.phrases || []).join('\n')}</textarea>
        </div>
      ` : ''}
      ${rule.kind === 'append' || rule.kind === 'prepend' ? `
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Value</label>
          <textarea rows="2" data-field="value" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">${escapeHtml(rule.value || '')}</textarea>
        </div>
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Skip if present (substring — rule is a no-op if prompt already contains this)</label>
          <input type="text" value="${escapeAttr(rule.skip_if_present || '')}" data-field="skip_if_present" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">
        </div>
      ` : ''}
      ${rule.kind === 'clamp_length' ? `
        <div>
          <label class="block text-[10px] text-zinc-500 mb-0.5">Max chars</label>
          <input type="number" min="50" max="5000" value="${rule.max_chars || 500}" data-field="max_chars" class="w-32 bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono">
        </div>
      ` : ''}

      <div>
        <label class="block text-[10px] text-zinc-500 mb-0.5">Notes (why this rule exists)</label>
        <textarea rows="2" data-field="notes" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs text-zinc-400">${escapeHtml(rule.notes || '')}</textarea>
      </div>
    </div>
  `;

  // Expand/collapse details
  const bodyEl = card.querySelector('[data-section=body]');
  card.querySelector('[data-action=toggle-body]').addEventListener('click', () => {
    bodyEl.classList.toggle('hidden');
  });

  // Wire every field to buffer
  card.querySelectorAll('[data-field]').forEach(el => {
    if (el.dataset.field === 'name-display') return;
    const commit = () => {
      const rules = _rulesBuffer.rules;
      const r = rules[idx];
      const field = el.dataset.field;
      if (field === 'enabled') r.enabled = el.checked;
      else if (field === 'max_chars') r[field] = parseInt(el.value, 10) || 500;
      else if (field === 'phrases') r[field] = el.value.split('\n').map(s => s.trim()).filter(Boolean);
      else r[field] = el.value;
      // Re-render on kind change so field layout updates
      if (field === 'kind') renderRules();
      if (field === 'name') card.querySelector('[data-field=name-display]').textContent = r.name || '(unnamed)';
      if (field === 'enabled') {
        card.className = `bg-zinc-900/40 border ${r.enabled !== false ? 'border-zinc-800' : 'border-zinc-900 opacity-50'} rounded p-3 text-xs`;
      }
    };
    el.addEventListener('change', commit);
    el.addEventListener('input', commit);
  });

  card.querySelector('[data-action=delete]').addEventListener('click', () => {
    if (!confirm(`Delete rule "${rule.name || rule.id}"?`)) return;
    _rulesBuffer.rules.splice(idx, 1);
    renderRules();
  });

  return card;
}

document.getElementById('btn-rules-save')?.addEventListener('click', async () => {
  if (!_rulesBuffer) return;
  try {
    await api.saveRules(_rulesBuffer);
    toast('✓ rules saved — active on next API call');
  } catch (err) { toast('Save failed: ' + (err.message || err)); }
});

document.getElementById('btn-rules-reset')?.addEventListener('click', async () => {
  if (!confirm('Reset all rules to shipped defaults? Your edits will be lost.')) return;
  try {
    _rulesBuffer = await api.resetRules();
    renderRules();
    toast('✓ rules reset');
  } catch (err) { toast('Reset failed: ' + (err.message || err)); }
});

function addNewRule() {
  if (!_rulesBuffer) _rulesBuffer = { version: 1, rules: [] };
  const id = 'custom_' + Math.random().toString(36).slice(2, 8);
  _rulesBuffer.rules.push({
    id, name: 'New rule', target: 'nano_banana_keyframe',
    kind: 'append', value: '', enabled: true, notes: '',
  });
  renderRules();
  // Scroll to bottom of list
  setTimeout(() => {
    const list = document.getElementById('rules-list');
    list.lastElementChild?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, 50);
}
document.getElementById('btn-rules-add')?.addEventListener('click', addNewRule);
document.getElementById('btn-rules-add-empty')?.addEventListener('click', addNewRule);

document.getElementById('btn-test-run')?.addEventListener('click', async () => {
  const prompt = document.getElementById('test-prompt').value.trim();
  const target = document.getElementById('test-target').value;
  if (!prompt) { toast('enter a prompt first'); return; }
  // Save current buffer before testing so current edits are reflected
  if (_rulesBuffer) {
    try { await api.saveRules(_rulesBuffer); } catch (e) { /* continue */ }
  }
  try {
    const r = await api.testRules(prompt, target);
    document.getElementById('test-result').classList.remove('hidden');
    document.getElementById('test-out').textContent = r.transformed;
    const applied = (r.applied || []).map(a => a.name).join(' · ');
    document.getElementById('test-applied').textContent = applied || '(no rules fired)';
  } catch (err) { toast('Test failed: ' + (err.message || err)); }
});

// ─── Asset library ───────────────────────────────────────────────────────

const LIB_KINDS = [
  { id: 'characters', label: '👤 Characters', blurb: 'Portraits anchored to consistent identity across runs' },
  { id: 'locations',  label: '📍 Locations', blurb: 'Environment references for world + rip-o-matic composition' },
  { id: 'props',      label: '🎯 Props', blurb: 'Signature objects (weapons, vehicles, artifacts) that stay consistent across shots' },
  { id: 'music',      label: '🎵 Music', blurb: 'Pre-analyzed tracks (BPM cached) — instant snap-to-beats in any run' },
  { id: 'looks',      label: '🎨 Looks', blurb: 'Saved color grades' },
];

async function loadLibrary() {
  const el = document.getElementById('lib-sections');
  el.innerHTML = '<div class="text-zinc-500 text-sm">Loading…</div>';
  try {
    const data = await api.listLibrary();
    renderLibrary(data);
  } catch (err) {
    el.innerHTML = `<div class="text-red-400 text-sm">Load failed: ${escapeHtml(err.message || String(err))}</div>`;
  }
}

function renderLibrary(data) {
  const el = document.getElementById('lib-sections');
  el.innerHTML = '';
  for (const kind of LIB_KINDS) {
    const items = data[kind.id] || [];
    const section = document.createElement('div');
    section.className = 'bg-zinc-900/40 border border-zinc-800 rounded';
    section.innerHTML = `
      <div class="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between">
        <div>
          <div class="font-semibold">${kind.label}</div>
          <div class="text-[11px] text-zinc-500">${kind.blurb}</div>
        </div>
        <div class="flex items-center gap-2">
          <span class="text-[10px] text-zinc-600 font-mono">${items.length} item${items.length === 1 ? '' : 's'}</span>
          ${kind.id === 'characters' ? `<button data-ta-new="characters" class="text-[11px] text-fuchsia-400 hover:text-fuchsia-300" title="Generate a 5-angle character turnaround via Nano Banana">✨ turnaround</button>` : ''}
          ${kind.id === 'locations' ? `<button data-ta-new="locations" class="text-[11px] text-fuchsia-400 hover:text-fuchsia-300" title="Generate a 5-angle environment sheet via Nano Banana (wide, medium, detail, golden hour, night)">✨ turnaround</button>` : ''}
          <button data-add-kind="${kind.id}" class="text-[11px] text-amber-400 hover:text-amber-300">+ add</button>
        </div>
      </div>
      <div class="p-3 grid grid-cols-${items.length ? 4 : 1} gap-3" data-items-kind="${kind.id}">
        ${items.length === 0 ? `<div class="text-[11px] text-zinc-500">Nothing here yet. <a class="text-amber-400 hover:text-amber-300 cursor-pointer" data-add-kind="${kind.id}">Add one</a> to reuse across runs.</div>` : ''}
      </div>
    `;
    const grid = section.querySelector(`[data-items-kind="${kind.id}"]`);
    for (const it of items) grid.appendChild(libraryItemCard(it));
    el.appendChild(section);
  }
  el.querySelectorAll('[data-add-kind]').forEach(btn => {
    btn.addEventListener('click', () => openLibraryAddModal(btn.dataset.addKind));
  });
  el.querySelectorAll('[data-ta-new]').forEach(btn => {
    btn.addEventListener('click', () => openTurnaroundModal(btn.dataset.taNew || 'characters'));
  });
  // If any character is still mid-generation, poll the list until everything
  // settles. Otherwise the user has to refresh manually to see new angles land.
  const anyGenerating = (data.characters || []).some(c => c?.turnaround?.status === 'generating')
    || (data.locations || []).some(l => l?.turnaround?.status === 'generating');
  if (anyGenerating) _startLibraryPolling();
  else _stopLibraryPolling();
}

let _libraryPollTimer = null;
function _startLibraryPolling() {
  if (_libraryPollTimer) return;
  _libraryPollTimer = setInterval(loadLibrary, 4000);
}
function _stopLibraryPolling() {
  if (_libraryPollTimer) {
    clearInterval(_libraryPollTimer);
    _libraryPollTimer = null;
  }
}

// Turnaround modal — name + description + tags → background generation.
function openTurnaroundModal(kind = 'characters') {
  const modal = document.getElementById('turnaround-modal');
  const nameEl = document.getElementById('ta-name');
  const descEl = document.getElementById('ta-description');
  const tagsEl = document.getElementById('ta-tags');
  const errEl = document.getElementById('ta-error');
  const confirmBtn = document.getElementById('ta-confirm');
  const cancelBtn = document.getElementById('ta-cancel');

  // Clear prior state.
  nameEl.value = '';
  descEl.value = '';
  tagsEl.value = '';
  errEl.classList.add('hidden');
  errEl.textContent = '';
  confirmBtn.disabled = false;
  confirmBtn.textContent = 'Generate turnaround';

  const close = () => {
    modal.classList.add('hidden');
    confirmBtn.onclick = null;
    cancelBtn.onclick = null;
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (e) => {
    if (e.key === 'Escape') close();
    else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) confirmBtn.click();
  };
  document.addEventListener('keydown', onKey);

  const showError = (msg) => {
    errEl.textContent = msg;
    errEl.classList.remove('hidden');
  };

  confirmBtn.onclick = async () => {
    errEl.classList.add('hidden');
    const name = nameEl.value.trim();
    const description = descEl.value.trim();
    const tags = tagsEl.value.trim();
    if (!name) { showError('Name is required.'); nameEl.focus(); return; }
    if (!description || description.length < 30) {
      showError(kind === 'locations'
        ? 'Description needs more detail (≥30 chars). Include architecture, materials, atmosphere.'
        : 'Description needs more detail (≥30 chars). Include age, build, hair, wardrobe.');
      descEl.focus();
      return;
    }

    const fd = new FormData();
    fd.append('name', name);
    fd.append('description', description);
    fd.append('kind', kind);
    if (tags) fd.append('tags', tags);

    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Queuing…';
    try {
      const { slug } = await fetch('/api/library/turnaround', { method: 'POST', body: fd }).then(_r);
      close();
      toast(`✨ turnaround started for "${name}" — 5 angles rendering…`);
      // Jump to library view + start polling so progress is visible.
      if (state.view !== 'library') showView('library');
      else await loadLibrary();
      _startLibraryPolling();
    } catch (err) {
      showError('Turnaround failed: ' + (err.message || String(err)));
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Generate turnaround';
    }
  };
  cancelBtn.onclick = close;

  modal.classList.remove('hidden');
  setTimeout(() => nameEl.focus(), 50);
}

function libraryItemCard(it) {
  const card = document.createElement('div');
  card.className = 'bg-zinc-950 border border-zinc-800 rounded overflow-hidden text-xs';
  const firstImage = (it.files || []).find(f => /\.(png|jpe?g|webp)$/i.test(f));
  const firstAudio = (it.files || []).find(f => /\.(mp3|wav|m4a|aac|ogg|flac)$/i.test(f));

  // Turnaround progress — when a character is mid-generation the item exists
  // but files are still landing. Show a progress bar + status chip so the
  // user sees "3/5 angles done" rather than "item with 3 files."
  const ta = it.turnaround || null;
  const isGenerating = ta && ta.status === 'generating';
  const taBanner = ta ? (() => {
    const total = ta.planned_count || 5;
    const done = ta.generated_count || 0;
    const pct = Math.min(100, Math.round((done / total) * 100));
    const failedNote = (ta.failed_angles && ta.failed_angles.length)
      ? ` · <span class="text-red-400">${ta.failed_angles.length} failed</span>` : '';
    const label = ta.status === 'generating'
      ? `✨ generating ${done}/${total}…`
      : ta.status === 'ready'
        ? `✨ turnaround · ${done}/${total} angles${failedNote}`
        : `✗ ${ta.error || 'turnaround failed'}`;
    const cls = ta.status === 'generating' ? 'text-amber-300 border-amber-900/40 bg-amber-950/30'
             : ta.status === 'ready'       ? 'text-emerald-300 border-emerald-900/40 bg-emerald-950/30'
             :                                'text-red-300 border-red-900/40 bg-red-950/30';
    return `
      <div class="${cls} border rounded px-2 py-1 mt-1.5 text-[10px] font-mono">
        <div class="flex items-center gap-1.5">
          ${ta.status === 'generating' ? '<div class="spin inline-block w-2.5 h-2.5 border border-amber-900 border-t-amber-300 rounded-full"></div>' : ''}
          <span>${label}</span>
        </div>
        ${ta.status === 'generating' ? `<div class="mt-1 h-0.5 bg-amber-950 rounded overflow-hidden"><div class="h-full bg-amber-400 transition-all" style="width: ${pct}%"></div></div>` : ''}
      </div>`;
  })() : '';

  card.innerHTML = `
    <div class="aspect-video bg-black flex items-center justify-center relative">
      ${firstImage
        ? `<img src="/library-assets/${firstImage}" class="w-full h-full object-cover">`
        : firstAudio
          ? `<div class="text-4xl opacity-40">🎵</div>`
          : isGenerating
            ? `<div class="text-zinc-500 text-center p-3">
                <div class="spin inline-block w-4 h-4 border-2 border-zinc-700 border-t-amber-400 rounded-full mb-1.5"></div>
                <div class="text-[10px]">waiting on seed angle…</div>
              </div>`
            : `<div class="text-zinc-700 text-xs">${(it.files || []).length} file(s)</div>`}
      ${(it.files || []).length > 1 ? `<div class="absolute bottom-1 right-1 text-[9px] px-1 rounded bg-black/70 text-zinc-300 font-mono">${(it.files || []).length} imgs</div>` : ''}
    </div>
    <div class="p-2.5">
      <div class="font-semibold text-zinc-200 truncate">${escapeHtml(it.name)}</div>
      <div class="text-[10px] text-zinc-500 font-mono truncate">${escapeHtml(it.slug)}</div>
      ${taBanner}
      ${it.description ? `<div class="text-[11px] text-zinc-400 mt-1 line-clamp-2">${escapeHtml(it.description)}</div>` : ''}
      ${(it.tags || []).length ? `
        <div class="flex flex-wrap gap-1 mt-1.5">
          ${it.tags.map(t => `<span class="text-[10px] px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400">${escapeHtml(t)}</span>`).join('')}
        </div>
      ` : ''}
      ${firstAudio ? `<audio src="/library-assets/${firstAudio}" controls class="w-full h-6 mt-1.5"></audio>` : ''}
      <div class="flex gap-1 mt-2">
        <button data-lib-inject="${it.kind}/${it.slug}" class="flex-1 text-[11px] px-2 py-1 rounded border border-zinc-800 hover:bg-zinc-800 text-zinc-400 hover:text-white disabled:opacity-40" ${isGenerating ? 'disabled' : ''} title="${isGenerating ? 'Waiting for turnaround to finish…' : 'Insert into the currently-open run'}">→ inject</button>
        <button data-lib-delete="${it.kind}/${it.slug}" class="text-[11px] px-2 py-1 rounded border border-zinc-800 hover:bg-red-900/30 text-zinc-500 hover:text-red-400">✕</button>
      </div>
    </div>
  `;
  card.querySelector('[data-lib-inject]').addEventListener('click', async () => {
    if (!state.currentRunId) { toast('Open a run first, then inject from library'); return; }
    const [kind, slug] = card.querySelector('[data-lib-inject]').dataset.libInject.split('/');
    const target = kind === 'music' ? 'music' : 'references';
    try {
      const r = await api.injectLibrary(state.currentRunId, kind, slug, target);
      toast(`✓ injected ${r.copied.length} file(s) into run's ${target}`);
    } catch (err) { toast('Inject failed: ' + (err.message || err)); }
  });
  card.querySelector('[data-lib-delete]').addEventListener('click', async () => {
    const [kind, slug] = card.querySelector('[data-lib-delete]').dataset.libDelete.split('/');
    if (!confirm(`Delete library item "${it.name}"? All files removed.`)) return;
    try {
      await api.deleteLibraryItem(kind, slug);
      await loadLibrary();
    } catch (err) { toast('Delete failed: ' + (err.message || err)); }
  });
  return card;
}

// Library add dialog — replaces the old triple-prompt() cascade. The prompt
// chain kept silently failing when a user hit Cancel on any of the three
// native dialogs or missed them entirely. A single in-app modal with all the
// fields visible means the user always sees what's happening and can recover.
function openLibraryAddModal(kind) {
  const modal = document.getElementById('lib-add-modal');
  const label = document.getElementById('lib-add-kind-label');
  const nameEl = document.getElementById('lib-add-name');
  const descEl = document.getElementById('lib-add-description');
  const tagsEl = document.getElementById('lib-add-tags');
  const filesEl = document.getElementById('lib-add-files');
  const previewEl = document.getElementById('lib-add-file-preview');
  const errorEl = document.getElementById('lib-add-error');
  const confirmBtn = document.getElementById('lib-add-confirm');
  const cancelBtn = document.getElementById('lib-add-cancel');

  // Reset every field — the modal is re-used, so stale data would leak.
  nameEl.value = '';
  descEl.value = '';
  tagsEl.value = '';
  filesEl.value = '';
  previewEl.textContent = '';
  errorEl.classList.add('hidden');
  errorEl.textContent = '';
  confirmBtn.disabled = false;
  confirmBtn.textContent = 'Add to library';

  const singular = kind.endsWith('s') ? kind.slice(0, -1) : kind;
  label.textContent = `kind: ${kind} · add a new ${singular}`;
  filesEl.accept = kind === 'music' ? 'audio/*' : 'image/*';
  filesEl.multiple = kind !== 'music';

  // Show selected filenames + sizes so the user can confirm they picked the
  // right thing before saving.
  const onFileChange = () => {
    const fs = Array.from(filesEl.files || []);
    if (!fs.length) { previewEl.textContent = ''; return; }
    const totalKB = fs.reduce((s, f) => s + f.size, 0) / 1024;
    previewEl.textContent =
      `${fs.length} file${fs.length > 1 ? 's' : ''} · ${totalKB < 1024 ? totalKB.toFixed(0) + ' KB' : (totalKB / 1024).toFixed(1) + ' MB'} total\n`
      + fs.map(f => `  • ${f.name} (${(f.size / 1024).toFixed(0)} KB)`).join('\n');
  };
  filesEl.onchange = onFileChange;

  const showError = (msg) => {
    errorEl.textContent = msg;
    errorEl.classList.remove('hidden');
  };

  const close = () => {
    modal.classList.add('hidden');
    // Detach so we don't double-fire next time the modal opens.
    confirmBtn.onclick = null;
    cancelBtn.onclick = null;
    filesEl.onchange = null;
    document.removeEventListener('keydown', onKey);
  };

  const onKey = (e) => {
    if (e.key === 'Escape') close();
    else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) confirmBtn.click();
  };
  document.addEventListener('keydown', onKey);

  confirmBtn.onclick = async () => {
    errorEl.classList.add('hidden');
    const name = nameEl.value.trim();
    const description = descEl.value.trim();
    const tags = tagsEl.value.trim();
    const files = Array.from(filesEl.files || []);
    // Validate in-modal so the user sees the complaint without losing their inputs.
    if (!name) { showError('Name is required.'); nameEl.focus(); return; }
    if (!files.length) { showError('At least one file is required.'); filesEl.focus(); return; }

    const fd = new FormData();
    fd.append('name', name);
    if (description) fd.append('description', description);
    if (tags) fd.append('tags', tags);
    for (const f of files) fd.append('files', f);

    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Adding…';
    try {
      await api.saveLibraryItem(kind, fd);
      close();
      toast(`✓ added "${name}" to ${kind}`);
      await loadLibrary();
    } catch (err) {
      // Keep the modal open on failure so the user can edit + retry.
      showError('Save failed: ' + (err.message || String(err)));
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Add to library';
    }
  };
  cancelBtn.onclick = close;

  modal.classList.remove('hidden');
  setTimeout(() => nameEl.focus(), 50);
}

// Promote modal for playground image → library. Reuses lib-add-modal with
// the file picker hidden since the image already exists server-side.
function openPlaygroundPromoteModal(clip) {
  const modal = document.getElementById('lib-add-modal');
  const label = document.getElementById('lib-add-kind-label');
  const nameEl = document.getElementById('lib-add-name');
  const descEl = document.getElementById('lib-add-description');
  const tagsEl = document.getElementById('lib-add-tags');
  const filesEl = document.getElementById('lib-add-files');
  const previewEl = document.getElementById('lib-add-file-preview');
  const errorEl = document.getElementById('lib-add-error');
  const confirmBtn = document.getElementById('lib-add-confirm');
  const cancelBtn = document.getElementById('lib-add-cancel');
  // Find the "Files *" label + the input row so we can hide them for promote.
  const filesLabel = filesEl?.previousElementSibling;

  // Defaults seeded from the clip: use first line of prompt as candidate name.
  const suggested = (clip.prompt || '').split('\n')[0].slice(0, 60);
  nameEl.value = suggested || '';
  descEl.value = clip.prompt || '';
  tagsEl.value = '';
  errorEl.classList.add('hidden');
  errorEl.textContent = '';
  confirmBtn.disabled = false;
  confirmBtn.textContent = 'Save to library';

  label.innerHTML = `promote playground image → <span id="pg-promote-kind-pill" class="font-mono text-amber-400">characters</span>
    <button type="button" id="pg-promote-switch-kind" class="ml-2 text-[10px] text-zinc-500 hover:text-amber-300 underline">change</button>`;
  // Hide the file input + its label + preview — image is already on disk.
  if (filesLabel) filesLabel.style.display = 'none';
  filesEl.style.display = 'none';
  previewEl.style.display = 'none';

  // Track which library kind we're saving to. Defaults to characters since
  // that's the most common use for playground images.
  let kind = 'characters';
  document.getElementById('pg-promote-switch-kind').onclick = () => {
    // Cycle through non-music library kinds. Music doesn't take PNGs.
    const options = ['characters', 'locations', 'props', 'looks'];
    const idx = options.indexOf(kind);
    kind = options[(idx + 1) % options.length];
    const pill = document.getElementById('pg-promote-kind-pill');
    if (pill) pill.textContent = kind;
  };

  const showError = (msg) => {
    errorEl.textContent = msg;
    errorEl.classList.remove('hidden');
  };
  const close = () => {
    modal.classList.add('hidden');
    // Restore the file picker for the next regular add flow.
    if (filesLabel) filesLabel.style.display = '';
    filesEl.style.display = '';
    previewEl.style.display = '';
    confirmBtn.onclick = null;
    cancelBtn.onclick = null;
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (e) => {
    if (e.key === 'Escape') close();
    else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) confirmBtn.click();
  };
  document.addEventListener('keydown', onKey);

  confirmBtn.onclick = async () => {
    errorEl.classList.add('hidden');
    const name = nameEl.value.trim();
    if (!name) { showError('Name is required.'); nameEl.focus(); return; }

    const fd = new FormData();
    fd.append('kind', kind);
    fd.append('name', name);
    fd.append('description', descEl.value.trim());
    fd.append('tags', tagsEl.value.trim());

    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Saving…';
    try {
      const { item } = await api.promotePlaygroundClip(clip.clip_id, fd);
      close();
      toast(`✓ saved "${name}" to ${kind} library`);
    } catch (err) {
      showError('Save failed: ' + (err.message || String(err)));
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Save to library';
    }
  };
  cancelBtn.onclick = close;

  modal.classList.remove('hidden');
  setTimeout(() => nameEl.focus(), 50);
}

// ─── Seedance playground ─────────────────────────────────────────────────
// Standalone clip generation. Each clip has its own status lifecycle
// (queued → generating → ready|failed); we poll the whole list as long as
// any clip is mid-flight so the UI transitions without manual refresh.

let _playgroundPollTimer = null;

function stopPlaygroundPolling() {
  if (_playgroundPollTimer) {
    clearInterval(_playgroundPollTimer);
    _playgroundPollTimer = null;
  }
}

function startPlaygroundPolling() {
  if (_playgroundPollTimer) return;
  _playgroundPollTimer = setInterval(loadPlayground, 3000);
}

async function loadPlayground() {
  const grid = document.getElementById('pg-clips');
  if (!grid) return;
  let clips = [];
  try {
    const resp = await api.listPlaygroundClips();
    clips = resp.clips || [];
  } catch (err) {
    grid.innerHTML = `<div class="text-red-400 text-sm col-span-full">Load failed: ${escapeHtml(err.message || String(err))}</div>`;
    return;
  }

  // Manage polling based on in-flight work — we don't want to thrash the
  // server with list fetches when nothing is changing.
  const anyActive = clips.some(c => c.status === 'queued' || c.status === 'generating');
  if (anyActive) startPlaygroundPolling(); else stopPlaygroundPolling();

  document.getElementById('pg-clips-count').textContent =
    clips.length ? `${clips.length} clip${clips.length > 1 ? 's' : ''}` : '';

  if (!clips.length) {
    grid.innerHTML = `<div class="text-[11px] text-zinc-500 col-span-full">No clips yet. Write a prompt and hit Generate.</div>`;
    return;
  }

  grid.innerHTML = clips.map(c => renderPlaygroundClipCard(c)).join('');
  // Delete buttons
  grid.querySelectorAll('[data-pg-delete]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const clipId = btn.dataset.pgDelete;
      if (!confirm('Delete this clip? This cannot be undone.')) return;
      try {
        await api.deletePlaygroundClip(clipId);
        await loadPlayground();
      } catch (err) { toast('Delete failed: ' + (err.message || err)); }
    });
  });
  // Save-to-library — opens a small modal pre-filled with the clip's prompt
  // as a starter name. One click beats the old download → library-add dance.
  grid.querySelectorAll('[data-pg-promote]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const clipId = btn.dataset.pgPromote;
      const clip = clips.find(c => c.clip_id === clipId);
      if (!clip) return;
      openPlaygroundPromoteModal(clip);
    });
  });

  // Reuse — pre-fill the compose form with this clip's prompt + settings,
  // and switch to the clip's mode so the right generator gets called.
  grid.querySelectorAll('[data-pg-copy]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const clipId = btn.dataset.pgCopy;
      const clip = clips.find(c => c.clip_id === clipId);
      if (!clip) return;
      setPgMode(clip.kind === 'image' ? 'image' : 'video');
      document.getElementById('pg-prompt').value = clip.prompt || '';
      if (clip.kind !== 'image') {
        document.getElementById('pg-duration').value = String(clip.duration || 5);
        document.getElementById('pg-ratio').value = clip.ratio || '16:9';
        document.getElementById('pg-quality').value = clip.quality || 'standard';
        document.getElementById('pg-audio').checked = !!clip.generate_audio;
      }
      document.getElementById('pg-prompt').focus();
      toast(`Prompt copied — edit and re-generate${clip.kind === 'image' ? ' (image mode)' : ''}`);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });
}

function renderPlaygroundClipCard(c) {
  const statusClasses = {
    queued:     'bg-zinc-800/60 text-zinc-400 border-zinc-700',
    generating: 'bg-amber-900/40 text-amber-300 border-amber-800',
    ready:      'bg-emerald-900/40 text-emerald-300 border-emerald-800',
    failed:     'bg-red-900/40 text-red-300 border-red-800',
  };
  const badge = statusClasses[c.status] || statusClasses.queued;
  const isImage = c.kind === 'image';

  const videoUrl = c.video_path
    ? `/playground-assets/${c.clip_id}/${c.video_path}?ts=${encodeURIComponent(c.updated_at || '')}`
    : '';
  const imageUrl = c.image_path
    ? `/playground-assets/${c.clip_id}/${c.image_path}?ts=${encodeURIComponent(c.updated_at || '')}`
    : '';
  const downloadUrl = isImage ? imageUrl : videoUrl;
  const refCount = (c.references || []).length;
  const vrefCount = (c.video_references || []).length;
  const costStr = c.cost_usd != null ? `$${c.cost_usd.toFixed(4)}` : '—';
  const elapsed = c.elapsed_s != null ? `${c.elapsed_s}s` : '—';

  // Spinner copy / ETA differs wildly between the two engines — surface it
  // so the user isn't staring at a spinner wondering how long to wait.
  const renderingLabel = isImage ? 'Nano Banana rendering…' : 'Seedance rendering…';
  const renderingEta   = isImage ? 'typically 5–15s'          : 'typically 60–180s';

  let media = '';
  if (c.status === 'ready') {
    if (isImage && imageUrl) {
      media = `<img src="${imageUrl}" alt="${escapeAttr(c.prompt || '')}" class="w-full h-full object-cover" loading="lazy">`;
    } else if (videoUrl) {
      media = `<video src="${videoUrl}" class="w-full h-full object-cover" controls muted loop playsinline preload="metadata"></video>`;
    }
  } else if (c.status === 'generating' || c.status === 'queued') {
    media = `<div class="text-center text-zinc-400 p-4">
      <div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full mb-2"></div>
      <div class="text-xs">${c.status === 'queued' ? 'queued…' : renderingLabel}</div>
      <div class="text-[10px] text-zinc-600 mt-1">${renderingEta}</div>
    </div>`;
  } else if (c.status === 'failed') {
    media = `<div class="text-red-400 p-3 text-center text-[11px]">✗ ${escapeHtml(c.error || 'failed')}</div>`;
  }

  // Meta row: image mode has no ratio/duration/quality/audio — show a
  // shorter summary that makes sense for a still.
  const metaRow = isImage
    ? `<span>🖼 image</span>
       <span>${refCount ? `${refCount} ref${refCount > 1 ? 's' : ''}` : 'no refs'}</span>
       <span class="ml-auto">${escapeHtml(elapsed)} · ${escapeHtml(costStr)}</span>`
    : `<span>🎬 ${escapeHtml(c.ratio || '')}</span>
       <span>${c.duration || 0}s</span>
       <span>${escapeHtml(c.quality || 'standard')}</span>
       ${c.generate_audio ? '<span>🔊</span>' : ''}
       <span class="ml-auto">${escapeHtml(elapsed)} · ${escapeHtml(costStr)}</span>`;

  return `
    <div class="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden text-xs">
      <div class="relative bg-black aspect-video flex items-center justify-center">
        ${media}
        <div class="absolute top-2 left-2 text-[10px] px-1.5 py-0.5 rounded font-mono ${badge}">${escapeHtml(c.status)}</div>
        <div class="absolute top-2 right-2 flex flex-col gap-1 items-end">
          ${refCount > 0 ? `<div class="text-[10px] px-1.5 py-0.5 rounded bg-sky-900/80 text-sky-200 font-mono">📷 ${refCount}</div>` : ''}
          ${vrefCount > 0 ? `<div class="text-[10px] px-1.5 py-0.5 rounded bg-fuchsia-900/80 text-fuchsia-200 font-mono" title="Video reference(s) — camera motion guide">🎞 ${vrefCount}</div>` : ''}
        </div>
      </div>
      <div class="p-2.5 space-y-1.5">
        <div class="text-zinc-300 line-clamp-3" title="${escapeAttr(c.prompt || '')}">${escapeHtml(c.prompt || '')}</div>
        <div class="flex items-center gap-3 text-[10px] text-zinc-500 font-mono">
          ${metaRow}
        </div>
        <div class="flex items-center gap-2 pt-1 flex-wrap">
          <button data-pg-copy="${escapeAttr(c.clip_id)}" class="text-[11px] px-2 py-0.5 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white border border-zinc-800">✎ reuse</button>
          ${downloadUrl ? `<a href="${downloadUrl}" download class="text-[11px] px-2 py-0.5 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white border border-zinc-800">⬇ download</a>` : ''}
          ${isImage && c.status === 'ready' ? `<button data-pg-promote="${escapeAttr(c.clip_id)}" class="text-[11px] px-2 py-0.5 rounded hover:bg-emerald-900/40 hover:text-emerald-200 text-zinc-400 border border-zinc-800" title="Save to library as a reusable character / location / etc.">📚 save to library</button>` : ''}
          <button data-pg-delete="${escapeAttr(c.clip_id)}" class="text-[11px] px-2 py-0.5 rounded hover:bg-red-900/40 hover:text-red-200 text-zinc-500 border border-zinc-800 ml-auto">delete</button>
        </div>
      </div>
    </div>
  `;
}

// ─── Playground mode (video / image) ─────────────────────────────────────
// Single source of truth for which generator the compose form targets.
// Mode persists across tab switches via localStorage.

let _pgMode = localStorage.getItem('pgMode') || 'video';

function setPgMode(mode) {
  if (mode !== 'video' && mode !== 'image') mode = 'video';
  _pgMode = mode;
  localStorage.setItem('pgMode', mode);

  // Toggle visibility of mode-specific fields.
  const videoOnly = document.getElementById('pg-video-controls');
  const imageHint = document.getElementById('pg-image-hint');
  const vrefs = document.getElementById('pg-vrefs-container');
  const refHint = document.getElementById('pg-ref-hint');
  if (videoOnly) videoOnly.classList.toggle('hidden', mode !== 'video');
  if (imageHint) imageHint.classList.toggle('hidden', mode !== 'image');
  if (vrefs) vrefs.classList.toggle('hidden', mode !== 'video');

  // Image mode widens ref-image column to fill the row and shows a tighter hint.
  const refsContainer = document.getElementById('pg-refs-container');
  if (refsContainer) {
    refsContainer.parentElement?.classList.toggle('grid-cols-2', mode === 'video');
    refsContainer.parentElement?.classList.toggle('grid-cols-1', mode === 'image');
  }
  const refsCap = document.getElementById('pg-refs-cap');
  if (refsCap) refsCap.textContent = mode === 'image' ? '(opt, ≤4)' : '(opt, ≤3)';
  if (refHint) {
    refHint.textContent = mode === 'image'
      ? 'Reference images lock subject identity, pose, or style. Up to 4 — Nano Banana weights them strongly.'
      : 'Video refs tell Seedance the motion to follow (camera path, pacing). Image refs tell it the subject and composition. Mix both for strongest guidance.';
  }

  // Nav chip styling.
  document.querySelectorAll('.pg-mode-btn').forEach(btn => {
    const on = btn.dataset.pgMode === mode;
    btn.classList.toggle('bg-amber-500', on);
    btn.classList.toggle('text-black', on);
    btn.classList.toggle('border-amber-500', on);
    btn.classList.toggle('text-zinc-400', !on);
    btn.classList.toggle('border-zinc-800', !on);
    btn.classList.toggle('hover:bg-zinc-900', !on);
  });

  // Contextual hint and button label.
  const hintEl = document.getElementById('pg-mode-hint');
  if (hintEl) {
    hintEl.textContent = mode === 'image'
      ? 'Nano Banana — single cinematic still, ~$0.04 per image'
      : 'Seedance 2.0 — animated clip, ~$0.40 per 5s at Standard';
  }
  const costHint = document.getElementById('pg-cost-hint');
  if (costHint) {
    costHint.textContent = mode === 'image'
      ? '~$0.04 per image · ~5–15s'
      : '~$0.40 per 5s clip at Standard';
  }
  const btn = document.getElementById('pg-generate');
  if (btn) btn.textContent = mode === 'image' ? 'Generate image' : 'Generate clip';
}

document.querySelectorAll('.pg-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => setPgMode(btn.dataset.pgMode));
});
// Run once at module init so the UI reflects the persisted mode on first paint.
setPgMode(_pgMode);

// File-picker previews — show filename + size so the user can confirm
// they picked the right thing before committing to a (paid) render.
function _renderPgFilePreview(inputId, previewId, kindLabel, maxCount) {
  const input = document.getElementById(inputId);
  const preview = document.getElementById(previewId);
  if (!input || !preview) return;
  input.addEventListener('change', () => {
    const fs = Array.from(input.files || []);
    if (!fs.length) { preview.textContent = ''; return; }
    const overCount = fs.length > maxCount ? ` ⚠ max ${maxCount} — extras will be dropped` : '';
    const totalKB = fs.reduce((s, f) => s + f.size, 0) / 1024;
    const sizeStr = totalKB < 1024 ? totalKB.toFixed(0) + ' KB' : (totalKB / 1024).toFixed(1) + ' MB';
    preview.textContent =
      `${fs.length} ${kindLabel}${fs.length > 1 ? 's' : ''} · ${sizeStr}${overCount}\n`
      + fs.slice(0, 3).map(f => `  • ${f.name} (${(f.size / 1024).toFixed(0)} KB)`).join('\n');
  });
}
_renderPgFilePreview('pg-refs',  'pg-refs-preview',  'image', 3);
_renderPgFilePreview('pg-vrefs', 'pg-vrefs-preview', 'video', 3);

// ✨ enhance — Claude rewrites the prompt with sharper grammar for the
// current mode. Video mode uses `motion_prompt` (camera verbs, action, end
// state). Image mode uses `keyframe_prompt` (subject, composition, lighting,
// style — no motion language).
document.getElementById('pg-enhance')?.addEventListener('click', async () => {
  const btn = document.getElementById('pg-enhance');
  const ta = document.getElementById('pg-prompt');
  const text = ta.value.trim();
  if (!text) { toast('Write something first, then enhance.'); return; }
  const original = text;
  btn.disabled = true;
  btn.textContent = '✨ thinking…';
  try {
    const kind = _pgMode === 'image' ? 'keyframe_prompt' : 'motion_prompt';
    const context = _pgMode === 'image'
      ? {}
      : { quality: document.getElementById('pg-quality')?.value || 'standard' };
    const { text: rewritten } = await api.enhance(kind, text, context);
    if (!rewritten || !rewritten.trim()) {
      toast('Claude returned nothing — keeping original.');
      return;
    }
    ta.dataset.preEnhance = original;
    ta.value = rewritten.trim();
    toast('✨ enhanced — hit undo in the textarea or re-enhance');
  } catch (err) {
    toast('Enhance failed: ' + (err.message || String(err)));
  } finally {
    btn.disabled = false;
    btn.textContent = '✨ enhance';
  }
});

// Generate button — branches on mode. Video hits Seedance with duration /
// ratio / quality / audio + both kinds of refs. Image hits Nano Banana with
// only prompt + image refs.
document.getElementById('pg-generate')?.addEventListener('click', async () => {
  const btn = document.getElementById('pg-generate');
  const errEl = document.getElementById('pg-error');
  const prompt = document.getElementById('pg-prompt').value.trim();
  const refs = Array.from(document.getElementById('pg-refs').files || []);

  errEl.classList.add('hidden');
  if (!prompt) {
    errEl.textContent = 'Prompt is required.';
    errEl.classList.remove('hidden');
    return;
  }

  const imageMode = _pgMode === 'image';
  const maxRefs = imageMode ? 4 : 3;
  if (refs.length > maxRefs) {
    errEl.textContent = `Max ${maxRefs} reference images in ${imageMode ? 'image' : 'video'} mode.`;
    errEl.classList.remove('hidden');
    return;
  }

  const fd = new FormData();
  fd.append('prompt', prompt);
  for (const f of refs) fd.append('reference_images', f);

  const defaultLabel = imageMode ? 'Generate image' : 'Generate clip';
  btn.disabled = true;

  try {
    if (imageMode) {
      btn.textContent = 'Queued · ~5–15s…';
      await api.generatePlaygroundImage(fd);
      toast('Image queued — Nano Banana rendering');
    } else {
      // Pull video-only fields + video refs on this path.
      const vrefs = Array.from(document.getElementById('pg-vrefs').files || []);
      if (vrefs.length > 3) {
        errEl.textContent = 'Max 3 reference videos.';
        errEl.classList.remove('hidden');
        btn.disabled = false;
        btn.textContent = defaultLabel;
        return;
      }
      fd.append('duration', document.getElementById('pg-duration').value);
      fd.append('ratio', document.getElementById('pg-ratio').value);
      fd.append('quality', document.getElementById('pg-quality').value);
      fd.append('generate_audio', document.getElementById('pg-audio').checked ? 'true' : 'false');
      for (const f of vrefs) fd.append('reference_videos', f);
      btn.textContent = vrefs.length ? 'Queued · normalizing video…' : 'Queued…';
      await api.generatePlaygroundClip(fd);
      toast(`Clip queued — ${vrefs.length ? 'normalizing video then ' : ''}rendering`);
      document.getElementById('pg-vrefs').value = '';
      document.getElementById('pg-vrefs-preview').textContent = '';
    }
    document.getElementById('pg-refs').value = '';
    document.getElementById('pg-refs-preview').textContent = '';
    await loadPlayground();
    startPlaygroundPolling();
  } catch (err) {
    errEl.textContent = 'Generate failed: ' + (err.message || String(err));
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = defaultLabel;
  }
});

// Global "+ new" button defaults to the most common kind (characters). Each
// kind section also has its own "+ add" button next to it for other kinds.
document.getElementById('btn-lib-new')?.addEventListener('click', () => {
  openLibraryAddModal('characters');
});

// ─── Taste profile ───────────────────────────────────────────────────────

async function loadTaste() {
  const el = document.getElementById('taste-content');
  el.innerHTML = '<div class="text-zinc-500 text-sm">Loading…</div>';
  try {
    const data = await api.getTaste();
    renderTaste(data);
  } catch (err) {
    el.innerHTML = `<div class="text-red-400 text-sm">Load failed: ${escapeHtml(err.message || String(err))}</div>`;
  }
}

function renderTaste(data) {
  const el = document.getElementById('taste-content');
  const ctx = data.context || '';
  const s = data.current_summary || {};
  const signalsTotal = s.total_signals || 0;

  el.innerHTML = `
    <div class="bg-zinc-900/40 border border-zinc-800 rounded p-4">
      <div class="text-[10px] text-zinc-500 uppercase mb-1">🎯 Current profile (prepended to every system prompt)</div>
      ${ctx ? `
        <div class="text-xs text-zinc-200 whitespace-pre-wrap leading-relaxed">${escapeHtml(ctx)}</div>
        <div class="text-[10px] text-zinc-600 mt-2 font-mono">based on ${data.based_on_signals || 0} signals · updated ${data.updated_at || '?'}</div>
      ` : `
        <div class="text-xs text-zinc-400">
          No taste profile yet. Needs ≥5 signals, then hit <span class="text-amber-400">↻ refresh</span> to have Claude summarize.
          Current signals: <span class="font-mono">${signalsTotal}</span>.
        </div>
      `}
    </div>

    <div class="grid grid-cols-2 gap-3">
      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="font-semibold text-zinc-200 mb-2">📊 Signal counts</div>
        <table class="w-full font-mono text-[11px]">
          <tbody>
            <tr><td class="text-zinc-500">total</td><td class="text-right text-zinc-300">${signalsTotal}</td></tr>
            <tr><td class="text-zinc-500">avg regens per run</td><td class="text-right text-zinc-300">${s.avg_regens_per_run ?? 0}</td></tr>
            <tr><td class="text-zinc-500">last signal at</td><td class="text-right text-zinc-300 truncate max-w-[140px]" title="${s.last_signal_at || ''}">${(s.last_signal_at || '—').slice(11, 19)}</td></tr>
          </tbody>
        </table>
      </div>

      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="font-semibold text-zinc-200 mb-2">🎨 Top looks used</div>
        ${Object.keys(s.look_picks || {}).length
          ? `<ul class="space-y-0.5 font-mono text-[11px]">${Object.entries(s.look_picks).map(([k, v]) => `<li class="flex justify-between"><span class="text-zinc-300">${escapeHtml(k)}</span><span class="text-zinc-500">×${v}</span></li>`).join('')}</ul>`
          : '<div class="text-zinc-500">no look choices yet</div>'}
      </div>

      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="font-semibold text-zinc-200 mb-2">🎙 Voice picks</div>
        ${Object.keys(s.voice_picks || {}).length
          ? `<ul class="space-y-0.5 font-mono text-[10px]">${Object.entries(s.voice_picks).map(([k, v]) => `<li class="flex justify-between gap-2"><span class="text-zinc-400 truncate" title="${k}">${escapeHtml(k.slice(0, 12))}…</span><span class="text-zinc-500">×${v}</span></li>`).join('')}</ul>`
          : '<div class="text-zinc-500">no voice choices yet</div>'}
      </div>

      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="font-semibold text-zinc-200 mb-2">📝 Recent keyframe edits</div>
        ${(s.common_edit_phrases || []).length
          ? `<ul class="space-y-1 text-[11px] text-zinc-400">${s.common_edit_phrases.slice(-5).map(e => `<li class="truncate">• ${escapeHtml(e)}</li>`).join('')}</ul>`
          : '<div class="text-zinc-500">no edits yet</div>'}
      </div>
    </div>

    <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs text-zinc-400">
      <div class="font-semibold text-zinc-200 mb-1">How it works</div>
      Every creative choice you make (which option you picked from 3 storyboards, which take you promoted to primary, what surgical edits you applied, what color grade you used, what voice) appends a structured signal to <code class="text-amber-400">taste.jsonl</code>. Hitting <span class="text-amber-400">↻ refresh</span> asks Claude to distill those signals into a 2-3 paragraph taste profile, which then gets prepended to every future <code class="text-zinc-300">storyboard</code> / <code class="text-zinc-300">ideate</code> / <code class="text-zinc-300">director</code> system prompt. After a few runs, Claude starts writing to YOUR taste.
    </div>
  `;
}

document.getElementById('btn-taste-refresh')?.addEventListener('click', async () => {
  toast('Claude is distilling your taste signals…');
  try {
    await api.refreshTaste();
    toast('✓ taste profile refreshed');
    await loadTaste();
  } catch (err) { toast('Refresh failed: ' + (err.message || err)); }
});
document.getElementById('btn-taste-reset')?.addEventListener('click', async () => {
  if (!confirm('Reset all taste signals? This deletes taste.jsonl and the current profile.')) return;
  try {
    await api.resetTaste();
    toast('Signals reset');
    await loadTaste();
  } catch (err) { toast('Reset failed: ' + (err.message || err)); }
});

// ─── Director chat ───────────────────────────────────────────────────────

const _director = { busy: false };

async function openDirector() {
  const drawer = document.getElementById('director-drawer');
  drawer.classList.remove('hidden');
  if (!state.currentRunId) return;
  await refreshDirectorHistory();
  setTimeout(() => document.getElementById('director-input').focus(), 50);
}

function closeDirector() {
  document.getElementById('director-drawer').classList.add('hidden');
}

async function refreshDirectorHistory() {
  if (!state.currentRunId) return;
  try {
    const { messages } = await api.directorHistory(state.currentRunId);
    renderDirectorMessages(messages || []);
  } catch (err) { /* silent — empty is ok */ }
}

function renderDirectorMessages(messages) {
  const box = document.getElementById('director-messages');
  box.innerHTML = '';
  if (!messages.length) {
    box.innerHTML = `
      <div style="font-family: var(--mono); font-size: 11px; color: var(--dim); padding: 14px; background: var(--ink-2); border: 1px solid var(--rule); border-radius: 2px;">
        <div class="cr-eyebrow" style="margin-bottom: 8px;">Try saying</div>
        <div class="cr-serif-italic" style="font-size: 13px; color: var(--bone-2); line-height: 1.7;">
          "shot 3 has face morph — regen it"<br>
          "swap shot 5 to take 2"<br>
          "make the detective's coat burgundy"<br>
          "rewrite the VO with more dread"<br>
          "snap the cut to the music beats"
        </div>
      </div>`;
    return;
  }
  for (const m of messages) {
    const wrap = document.createElement('div');
    const isUser = m.role === 'user';
    wrap.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';
    const time = (m.ts || '').split('T')[1]?.slice(0, 8) || '';
    const fontFamily = isUser ? 'var(--sans)' : 'var(--serif)';
    const fontStyle = isUser ? 'normal' : 'italic';
    const textColor = isUser ? 'var(--bone-2)' : 'var(--bone)';
    const eyebrowColor = isUser ? 'var(--dim)' : 'var(--lamp)';
    const speaker = isUser ? 'You' : 'Claude · sonnet 4.6';
    wrap.innerHTML = `
      <span class="cr-eyebrow" style="color: ${eyebrowColor};">${speaker}${time ? ` · ${time}` : ''}</span>
      <div style="font-family: ${fontFamily}; font-style: ${fontStyle}; font-size: ${isUser ? '13px' : '14px'}; color: ${textColor}; line-height: 1.55; white-space: pre-wrap;">${escapeHtml(m.content || '')}</div>
      ${(m.tool_trace || []).length ? `
        <details style="margin-top: 4px;">
          <summary class="cr-mono" style="font-size: 10px; color: var(--lamp); cursor: pointer; letter-spacing: 0.04em;">▸ ${m.tool_trace.length} tool action${m.tool_trace.length > 1 ? 's' : ''}</summary>
          <div style="margin-top: 4px; padding-left: 8px; border-left: 1px solid var(--rule); font-family: var(--mono); font-size: 10px; color: var(--dim); display: flex; flex-direction: column; gap: 4px;">
            ${m.tool_trace.map(t => `<div><span style="color: var(--lamp);">${escapeHtml(t.tool)}</span>(${escapeHtml(JSON.stringify(t.input).slice(0, 80))}) ${t.result?.ok ? '✓' : '✗'} ${escapeHtml(t.result?.message || t.result?.error || '')}</div>`).join('')}
          </div>
        </details>
      ` : ''}`;
    box.appendChild(wrap);
  }
  box.scrollTop = box.scrollHeight;
}

async function sendDirectorMessage() {
  if (_director.busy) return;
  const input = document.getElementById('director-input');
  const message = input.value.trim();
  if (!message) return;
  if (!state.currentRunId) { toast('Open a run first'); return; }

  _director.busy = true;
  const sendBtn = document.getElementById('director-send');
  sendBtn.disabled = true;
  const oldLabel = sendBtn.textContent;
  sendBtn.textContent = '…';

  // Optimistic render
  const box = document.getElementById('director-messages');
  const userMsg = document.createElement('div');
  userMsg.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';
  userMsg.innerHTML = `<span class="cr-eyebrow" style="color: var(--dim);">You</span><div style="font-family: var(--sans); font-size: 13px; color: var(--bone-2); line-height: 1.55; white-space: pre-wrap;">${escapeHtml(message)}</div>`;
  box.appendChild(userMsg);
  const thinking = document.createElement('div');
  thinking.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';
  thinking.innerHTML = `<span class="cr-eyebrow" style="color: var(--lamp);">Claude · sonnet 4.6</span><div style="font-family: var(--serif); font-style: italic; font-size: 13px; color: var(--dim);"><span class="spin inline-block w-3 h-3 border border-zinc-600 rounded-full" style="border-top-color: var(--lamp); margin-right: 6px;"></span>thinking…</div>`;
  box.appendChild(thinking);
  box.scrollTop = box.scrollHeight;
  input.value = '';

  try {
    await api.directorSend(state.currentRunId, message);
    await refreshDirectorHistory();
    // Director likely kicked off background work — refresh the run state too
    await refreshRun();
  } catch (err) {
    thinking.innerHTML = `<span class="text-red-400">✗ ${escapeHtml(err.message || String(err))}</span>`;
  } finally {
    _director.busy = false;
    sendBtn.disabled = false;
    sendBtn.textContent = oldLabel;
    input.focus();
  }
}

document.getElementById('director-toggle')?.addEventListener('click', openDirector);
document.getElementById('director-close')?.addEventListener('click', closeDirector);
document.getElementById('director-send')?.addEventListener('click', sendDirectorMessage);
document.getElementById('director-input')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendDirectorMessage();
  }
});
document.getElementById('director-reset')?.addEventListener('click', async () => {
  if (!confirm('Reset the Director conversation history for this run?')) return;
  try {
    await api.directorReset(state.currentRunId);
    await refreshDirectorHistory();
    toast('Conversation reset');
  } catch (err) { toast('Reset failed: ' + (err.message || err)); }
});

// ─── Activity log (live tail) ────────────────────────────────────────────

const LOG_LEVEL_CLR = {
  info: 'text-zinc-300',
  success: 'text-emerald-400',
  warn: 'text-amber-300',
  error: 'text-red-400',
};
const LOG_PHASE_CLR = {
  storyboard: 'text-amber-400',
  assets: 'text-sky-400',
  keyframes: 'text-violet-400',
  shots: 'text-emerald-400',
  review: 'text-cyan-400',
  stitch: 'text-rose-400',
  rip: 'text-fuchsia-400',
  costs: 'text-orange-400',
  music: 'text-pink-400',
};

function startLogPolling() {
  stopLogPolling();
  if (!state.currentRunId) return;
  pullLog(); // immediate
  state.logTimer = setInterval(pullLog, 1500);
}
function stopLogPolling() {
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
}
function resetLogView() {
  state.lastLogTs = null;
  const body = document.getElementById('log-body');
  body.innerHTML = '';
  document.getElementById('log-count').textContent = '';
}

async function pullLog() {
  if (!state.currentRunId) return;
  try {
    const { entries } = await api.getLog(state.currentRunId, state.lastLogTs);
    if (!entries || !entries.length) return;
    appendLogEntries(entries);
    state.lastLogTs = entries[entries.length - 1].ts;
  } catch (err) {
    // Silent — log endpoint going quiet shouldn't spam toasts
  }
}

// Per-drawer filter state. If `levels` is null, all levels are shown.
// `search` is a lowercase substring match against phase+msg.
const _logFilter = { levels: null, search: '' };

function _logRowMatchesFilter(el) {
  if (_logFilter.levels && !_logFilter.levels.has(el.dataset.level)) return false;
  if (_logFilter.search) {
    const hay = (el.textContent || '').toLowerCase();
    if (!hay.includes(_logFilter.search)) return false;
  }
  return true;
}

function applyLogFilter() {
  const body = document.getElementById('log-body');
  if (!body) return;
  let visible = 0;
  body.querySelectorAll('div[data-level]').forEach(el => {
    const show = _logRowMatchesFilter(el);
    el.style.display = show ? '' : 'none';
    if (show) visible += 1;
  });
  const total = body.querySelectorAll('div[data-level]').length;
  const countEl = document.getElementById('log-count');
  if (countEl) {
    countEl.textContent = visible === total
      ? `${total} entries`
      : `${visible} of ${total} entries`;
  }
}

function appendLogEntries(entries) {
  const body = document.getElementById('log-body');
  const shouldScroll = document.getElementById('log-autoscroll').checked;
  const frag = document.createDocumentFragment();
  for (const e of entries) {
    const line = document.createElement('div');
    line.className = 'flex gap-3 px-2 py-0.5 hover:bg-zinc-900';
    // dataset so the filter can check + toggle visibility without re-parsing.
    line.dataset.level = e.level || 'info';
    const time = (e.ts || '').split('T')[1] || e.ts || '';
    line.innerHTML = `
      <span class="text-zinc-600 shrink-0">${escapeHtml(time.slice(0, 12))}</span>
      <span class="shrink-0 ${LOG_PHASE_CLR[e.phase] || 'text-zinc-500'}">[${escapeHtml(e.phase || '·')}]</span>
      <span class="${LOG_LEVEL_CLR[e.level] || 'text-zinc-300'}">${escapeHtml(e.msg || '')}</span>
    `;
    if (!_logRowMatchesFilter(line)) line.style.display = 'none';
    frag.appendChild(line);
  }
  body.appendChild(frag);
  applyLogFilter();  // refresh the "n of N" counter
  if (shouldScroll) body.scrollTop = body.scrollHeight;
}

document.getElementById('log-toggle')?.addEventListener('click', () => {
  const body = document.getElementById('log-body');
  const caret = document.getElementById('log-caret');
  const hidden = body.classList.toggle('hidden');
  caret.textContent = hidden ? '▸' : '▾';
  if (!hidden) {
    body.scrollTop = body.scrollHeight;
  }
});
document.getElementById('log-copy')?.addEventListener('click', () => {
  const body = document.getElementById('log-body');
  // Copy only currently-visible rows so filter context comes along.
  const rows = Array.from(body.querySelectorAll('div[data-level]')).filter(d => d.style.display !== 'none');
  const text = rows.map(d => d.textContent.trim()).join('\n');
  navigator.clipboard.writeText(text).then(() => toast(`Log copied (${rows.length} rows)`));
});
document.getElementById('log-clear')?.addEventListener('click', () => {
  document.getElementById('log-body').innerHTML = '';
  document.getElementById('log-count').textContent = '';
});

// Level filter: clicking a chip toggles it. Empty selection = show all.
document.querySelectorAll('#log-level-filter .log-lvl-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const lvl = btn.dataset.level;
    if (!_logFilter.levels) _logFilter.levels = new Set();
    if (_logFilter.levels.has(lvl)) {
      _logFilter.levels.delete(lvl);
      btn.classList.remove('bg-zinc-800');
    } else {
      _logFilter.levels.add(lvl);
      btn.classList.add('bg-zinc-800');
    }
    if (_logFilter.levels.size === 0) _logFilter.levels = null;
    applyLogFilter();
  });
});

// Text filter: substring match against phase + msg. Debounced lightly.
document.getElementById('log-search')?.addEventListener('input', (e) => {
  _logFilter.search = (e.target.value || '').trim().toLowerCase();
  applyLogFilter();
});

// ─── Toast ───────────────────────────────────────────────────────────────

function toast(msg, ms = 2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add('hidden'), ms);
}

// ─── Modal (custom prompt for regeneration) ──────────────────────────────

function openModal({ title, body, defaultText = '', placeholder = '', confirmLabel = 'Regenerate' }) {
  return new Promise(resolve => {
    const m = document.getElementById('modal');
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').textContent = body;
    const ta = document.getElementById('modal-textarea');
    ta.value = defaultText;
    ta.placeholder = placeholder;
    document.getElementById('modal-confirm').textContent = confirmLabel;
    m.classList.remove('hidden');

    const close = (result) => {
      m.classList.add('hidden');
      document.getElementById('modal-confirm').onclick = null;
      document.getElementById('modal-cancel').onclick = null;
      resolve(result);
    };
    document.getElementById('modal-confirm').onclick = () => close(ta.value.trim() || null);
    document.getElementById('modal-cancel').onclick  = () => close(undefined);
    setTimeout(() => ta.focus(), 50);
  });
}

// ─── Runs list ───────────────────────────────────────────────────────────

// ─── Cast picker (New trailer + Rip-o-matic forms) ───────────────────────
// Loads library `characters` into both picker containers. Selection is stored
// on the container in `data-cast-selected` (space-separated slugs) and
// mirrored into the hidden `cast_slugs` input that gets posted with the form.

async function loadCastPickers() {
  // Runs once per showView('new') so the list reflects any characters added
  // since the user last visited this tab. Cheap (~50 KB).
  let chars = [];
  try {
    const data = await api.listLibrary('characters');
    chars = (data.characters || []).filter(c => {
      const st = c?.turnaround?.status;
      // Hide characters whose turnaround is still generating — their refs aren't
      // fully baked yet so using them as cast would ship a half-built anchor.
      return st !== 'generating' && st !== 'failed';
    });
  } catch { /* leave chars empty; picker will show a hint */ }

  document.querySelectorAll('[data-cast-picker]').forEach(picker => {
    const chips = picker.querySelector('[data-cast-chips]');
    const hidden = picker.querySelector('[data-cast-slugs]');
    // Preserve selection across reloads — user's picks shouldn't disappear.
    const selected = new Set((picker.dataset.castSelected || '').split(' ').filter(Boolean));

    if (!chars.length) {
      chips.innerHTML = `<div class="text-[11px] text-zinc-500">No characters in library yet. <a class="text-fuchsia-400 hover:text-fuchsia-300 cursor-pointer" data-view="library">Create one</a> — then come back.</div>`;
      chips.querySelectorAll('[data-view]').forEach(a => a.addEventListener('click', () => showView(a.dataset.view)));
      hidden.value = '';
      return;
    }

    chips.innerHTML = chars.map(c => {
      const on = selected.has(c.slug);
      const thumb = (c.files || []).find(f => /\.(png|jpe?g|webp)$/i.test(f));
      const thumbHtml = thumb
        ? `<img src="/library-assets/${thumb}" class="w-6 h-6 rounded-full object-cover" alt="">`
        : `<div class="w-6 h-6 rounded-full bg-zinc-800 flex items-center justify-center text-[10px] text-zinc-500">👤</div>`;
      return `
        <button type="button" data-cast-slug="${escapeAttr(c.slug)}"
          class="cast-chip flex items-center gap-1.5 px-2 py-1 rounded-full border text-[11px] transition-colors ${on ? 'border-fuchsia-500 bg-fuchsia-950/40 text-fuchsia-200' : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:border-zinc-600'}">
          ${thumbHtml}
          <span class="truncate max-w-[9rem]">${escapeHtml(c.name || c.slug)}</span>
          ${on ? '<span class="text-fuchsia-400 ml-1">✓</span>' : ''}
        </button>
      `;
    }).join('');

    chips.querySelectorAll('.cast-chip').forEach(btn => {
      btn.addEventListener('click', () => {
        const slug = btn.dataset.castSlug;
        if (selected.has(slug)) selected.delete(slug);
        else selected.add(slug);
        // Re-render so the check/unchecked styles update immediately.
        picker.dataset.castSelected = [...selected].join(' ');
        hidden.value = [...selected].join(',');
        loadCastPickers();   // cheap redraw; keeps selection synced
      });
    });

    picker.dataset.castSelected = [...selected].join(' ');
    hidden.value = [...selected].join(',');
  });
}

// Generic library-item picker for locations and props — same pattern as cast.
async function _loadLibraryPickers(kind, dataAttr, chipsAttr, slugsAttr, selectedAttr, emptyLabel, borderClass) {
  let items = [];
  try {
    const data = await api.listLibrary(kind);
    items = data[kind] || [];
  } catch { /* leave empty */ }

  document.querySelectorAll(`[${dataAttr}]`).forEach(picker => {
    const chips = picker.querySelector(`[${chipsAttr}]`);
    const hidden = picker.querySelector(`[${slugsAttr}]`);
    const selected = new Set((picker.dataset[selectedAttr] || '').split(' ').filter(Boolean));

    if (!items.length) {
      chips.innerHTML = `<div class="text-[11px] text-zinc-500">No ${kind} in library yet. <a class="text-emerald-400 hover:text-emerald-300 cursor-pointer" data-view="library">Create one</a></div>`;
      chips.querySelectorAll('[data-view]').forEach(a => a.addEventListener('click', () => showView(a.dataset.view)));
      hidden.value = '';
      return;
    }

    chips.innerHTML = items.map(c => {
      const on = selected.has(c.slug);
      const thumb = (c.files || []).find(f => /\.(png|jpe?g|webp)$/i.test(f));
      const thumbHtml = thumb
        ? `<img src="/library-assets/${thumb}" class="w-6 h-6 rounded-full object-cover" alt="">`
        : `<div class="w-6 h-6 rounded-full bg-zinc-800 flex items-center justify-center text-[10px] text-zinc-500">${emptyLabel}</div>`;
      return `
        <button type="button" data-picker-slug="${escapeAttr(c.slug)}"
          class="picker-chip flex items-center gap-1.5 px-2 py-1 rounded-full border text-[11px] transition-colors ${on ? borderClass + ' text-white' : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:border-zinc-600'}">
          ${thumbHtml}
          <span class="truncate max-w-[9rem]">${escapeHtml(c.name || c.slug)}</span>
          ${on ? '<span class="ml-1">✓</span>' : ''}
        </button>
      `;
    }).join('');

    chips.querySelectorAll('.picker-chip').forEach(btn => {
      btn.addEventListener('click', () => {
        const slug = btn.dataset.pickerSlug;
        if (selected.has(slug)) selected.delete(slug);
        else selected.add(slug);
        picker.dataset[selectedAttr] = [...selected].join(' ');
        hidden.value = [...selected].join(',');
        _loadLibraryPickers(kind, dataAttr, chipsAttr, slugsAttr, selectedAttr, emptyLabel, borderClass);
      });
    });

    picker.dataset[selectedAttr] = [...selected].join(' ');
    hidden.value = [...selected].join(',');
  });
}

function loadLocationPickers() {
  return _loadLibraryPickers('locations', 'data-location-picker', 'data-location-chips', 'data-location-slugs', 'locationSelected', '📍', 'border-emerald-500 bg-emerald-950/40 text-emerald-200');
}
function loadPropPickers() {
  return _loadLibraryPickers('props', 'data-prop-picker', 'data-prop-chips', 'data-prop-slugs', 'propSelected', '🎯', 'border-orange-500 bg-orange-950/40 text-orange-200');
}

async function loadRuns() {
  const { runs } = await api.listRuns();
  const list = document.getElementById('runs-list');
  const empty = document.getElementById('runs-empty');
  list.innerHTML = '';
  if (!runs.length) { empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');

  for (const r of runs) {
    const row = document.createElement('div');
    row.className = 'bg-zinc-900/50 border border-zinc-800 rounded p-3 flex items-center gap-4 hover:border-zinc-700 cursor-pointer';
    row.innerHTML = `
      <div class="flex-1 min-w-0">
        <div class="text-sm font-semibold truncate">${escapeHtml(r.title || '(untitled)')}</div>
        <div class="text-xs text-zinc-500 font-mono truncate">${r.run_id}</div>
      </div>
      <div class="text-xs text-zinc-500">${r.num_shots || '?'} shots · ${r.ratio || '?'}</div>
      <div class="text-xs px-2 py-0.5 rounded ${statusBadge(r.status)} font-mono">${r.status}</div>
    `;
    row.addEventListener('click', () => showView('run', r.run_id));
    list.appendChild(row);
  }
}

function statusBadge(status) {
  if (status === 'done') return 'bg-emerald-900/50 text-emerald-300 border border-emerald-800';
  if (status === 'failed') return 'bg-red-900/50 text-red-300 border border-red-800';
  if (String(status).includes('partial')) return 'bg-amber-900/50 text-amber-300 border border-amber-800';
  if (String(status).includes('ready')) return 'bg-sky-900/50 text-sky-300 border border-sky-800';
  return 'bg-zinc-900 text-zinc-400 border border-zinc-800';
}

// ─── New run form ────────────────────────────────────────────────────────

// Mode tabs on New trailer form
document.querySelectorAll('.mode-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    const mode = btn.dataset.mode;
    document.querySelectorAll('.mode-tab').forEach(b => {
      const active = b.dataset.mode === mode;
      b.classList.toggle('border-amber-500', active);
      b.classList.toggle('text-white', active);
      b.classList.toggle('font-semibold', active);
      b.classList.toggle('border-transparent', !active);
      b.classList.toggle('text-zinc-500', !active);
    });
    document.getElementById('mode-scratch').classList.toggle('hidden', mode !== 'scratch');
    document.getElementById('mode-rip').classList.toggle('hidden', mode !== 'rip');
  });
});

// Rip-o-matic form submit
document.getElementById('rip-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  btn.textContent = 'Uploading…';
  try {
    const fd = new FormData(e.target);
    const { run_id, detail } = await api.ripUpload(fd);
    if (!run_id) throw new Error(detail || 'no run_id returned');
    toast(`Ripping: scene detect + Claude translating…`);
    showView('run', run_id);
  } catch (err) {
    toast('Rip upload failed: ' + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Rip it →';
  }
});

document.getElementById('new-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  btn.textContent = 'Creating…';
  try {
    const fd = new FormData(e.target);
    const want3Options = fd.get('n_options') === 'on' || fd.get('n_options') === '3';
    const { run_id, state: st } = await api.createRun(fd);
    if (!run_id) throw new Error('no run_id in response');
    toast(`Run created: ${run_id}`);
    showView('run', run_id);
    // Immediately kick off storyboard generation
    await api.runStory(run_id, want3Options ? 3 : 1);
    await refreshRun();
  } catch (err) {
    toast('Create failed: ' + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create run';
  }
});

// ─── E-commerce URL importer ─────────────────────────────────────────────

document.getElementById('btn-ecom-extract').addEventListener('click', async () => {
  const btn = document.getElementById('btn-ecom-extract');
  const status = document.getElementById('ecom-status');
  const result = document.getElementById('ecom-result');
  const url = document.getElementById('ecom-url').value.trim();
  if (!url) { toast('Paste a product URL first'); return; }

  btn.disabled = true;
  btn.textContent = 'Extracting…';
  status.textContent = 'Fetching page + asking Claude to read it…';
  result.classList.add('hidden');
  result.innerHTML = '';

  try {
    const data = await api.ecomExtract(url);
    if (!data.product_name) {
      status.textContent = '';
      result.classList.remove('hidden');
      result.innerHTML = `<div class="text-amber-400">⚠ ${escapeHtml(data.extraction_notes || 'No product detected on this page.')}</div>`;
      return;
    }

    // Populate form fields
    const form = document.getElementById('new-form');
    form.querySelector('[name=concept]').value = data.ad_concept || '';
    form.querySelector('[name=style_intent]').value = data.style_intent || '';
    form.querySelector('[name=title]').value = data.suggested_title || '';
    if (data.suggested_shots) form.querySelector('[name=num_shots]').value = data.suggested_shots;
    if (data.suggested_ratio) form.querySelector('[name=ratio]').value = data.suggested_ratio;

    // Attach downloaded product images to the reference_images file input
    // via a DataTransfer so they're submitted with the form.
    const fileInput = form.querySelector('[name=reference_images]');
    let attachedCount = 0;
    if (fileInput && (data.images || []).length) {
      const dt = new DataTransfer();
      // Preserve any files the user already picked
      for (const f of (fileInput.files || [])) dt.items.add(f);
      for (const img of data.images) {
        try {
          const bin = atob(img.b64);
          const bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          const ext = (img.filename.split('.').pop() || 'jpg').toLowerCase();
          const mime = ext === 'png' ? 'image/png' : ext === 'webp' ? 'image/webp' : ext === 'gif' ? 'image/gif' : 'image/jpeg';
          const file = new File([bytes], img.filename, { type: mime });
          dt.items.add(file);
          attachedCount++;
        } catch (err) { /* skip bad image */ }
      }
      fileInput.files = dt.files;
    }

    status.textContent = '';
    result.classList.remove('hidden');
    const sellingPoints = (data.key_selling_points || []).map(p =>
      `<li class="ml-4 list-disc text-zinc-400">${escapeHtml(p)}</li>`
    ).join('');
    const imgPreviews = (data.images || []).map(img =>
      `<img src="data:image/jpeg;base64,${img.b64}" class="w-16 h-16 object-cover rounded border border-zinc-700" alt="${escapeAttr(img.filename)}">`
    ).join('');
    result.innerHTML = `
      <div class="flex items-baseline justify-between mb-2">
        <div class="font-semibold text-emerald-300">✓ ${escapeHtml(data.product_name)}</div>
        <div class="text-zinc-500 text-[11px]">${escapeHtml(data.brand || '')} ${data.price ? `· ${escapeHtml(data.price)}` : ''}</div>
      </div>
      <div class="text-zinc-500 text-[11px] mb-2">${escapeHtml(data.category || '')}</div>
      ${sellingPoints ? `<ul class="mb-3">${sellingPoints}</ul>` : ''}
      ${imgPreviews ? `<div class="flex gap-2 flex-wrap mb-2">${imgPreviews}</div>` : ''}
      <div class="text-zinc-500 text-[11px]">Concept + ${attachedCount} reference image${attachedCount === 1 ? '' : 's'} loaded into the form below. Edit before creating the run.</div>
    `;
    toast(`Loaded: ${data.product_name}`);
    form.querySelector('[name=concept]').scrollIntoView({ behavior: 'smooth', block: 'center' });
  } catch (err) {
    status.textContent = '';
    toast('Extract failed: ' + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Extract →';
  }
});


// ─── Ideate panel ────────────────────────────────────────────────────────

document.getElementById('btn-ideate').addEventListener('click', async () => {
  const btn = document.getElementById('btn-ideate');
  const status = document.getElementById('ideate-status');
  const results = document.getElementById('ideate-results');
  const theme = document.getElementById('ideate-theme').value.trim();
  const images = document.getElementById('ideate-images').files;
  const existing = document.querySelector('#new-form [name=concept]').value.trim();

  const fd = new FormData();
  if (theme) fd.append('theme', theme);
  if (existing) fd.append('existing_concept', existing);
  fd.append('n', '3');
  for (const f of images) fd.append('images', f);

  btn.disabled = true;
  btn.textContent = 'Thinking…';
  status.textContent = 'Claude is pitching concepts…';
  results.innerHTML = '';
  try {
    const { concepts, detail } = await api.ideate(fd);
    if (!concepts) throw new Error(detail || 'no concepts returned');
    renderIdeatedConcepts(concepts);
    status.textContent = '';
  } catch (err) {
    status.textContent = '';
    toast('Ideate failed: ' + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Brainstorm 3 concepts';
  }
});

function renderIdeatedConcepts(concepts) {
  const results = document.getElementById('ideate-results');
  results.innerHTML = '';
  concepts.forEach((c, i) => {
    const card = document.createElement('div');
    card.className = 'bg-zinc-950 border border-zinc-800 rounded p-3 text-xs';
    card.innerHTML = `
      <div class="flex items-baseline justify-between mb-1 gap-3">
        <div class="font-semibold text-sm text-amber-300">${escapeHtml(c.title || `Concept ${i+1}`)}</div>
        <button type="button" data-action="use" class="text-[11px] px-2 py-1 rounded bg-amber-500 hover:bg-amber-400 text-black font-semibold whitespace-nowrap">Use this</button>
      </div>
      <div class="italic text-zinc-300 mb-2">${escapeHtml(c.logline || '')}</div>
      <div class="text-zinc-400 whitespace-pre-wrap mb-2">${escapeHtml(c.concept || '')}</div>
      <div class="flex items-center gap-3 text-[11px] text-zinc-500">
        <span class="font-mono">${c.suggested_shots || '?'} shots</span>
        <span class="font-mono">${escapeHtml(c.suggested_ratio || '?')}</span>
        <span class="truncate">${escapeHtml(c.style_intent || '')}</span>
      </div>
    `;
    card.querySelector('[data-action=use]').onclick = () => {
      const form = document.getElementById('new-form');
      form.querySelector('[name=concept]').value = c.concept || '';
      form.querySelector('[name=style_intent]').value = c.style_intent || '';
      form.querySelector('[name=title]').value = c.title || '';
      if (c.suggested_shots) form.querySelector('[name=num_shots]').value = c.suggested_shots;
      if (c.suggested_ratio) form.querySelector('[name=ratio]').value = c.suggested_ratio;
      document.getElementById('ideate-panel').open = false;
      toast(`Loaded: ${c.title}`);
      form.querySelector('[name=concept]').scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    results.appendChild(card);
  });
}

// ─── Enhance (concept textarea) ──────────────────────────────────────────

document.getElementById('btn-enhance-concept').addEventListener('click', async () => {
  const ta = document.querySelector('#new-form [name=concept]');
  const text = ta.value.trim();
  if (!text) { toast('Write a concept first'); return; }
  const btn = document.getElementById('btn-enhance-concept');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '✨ enhancing…';
  try {
    const style = document.querySelector('#new-form [name=style_intent]').value.trim();
    const { text: rewritten, detail } = await api.enhance('concept', text, { style_intent: style });
    if (!rewritten) throw new Error(detail || 'no result');
    ta.value = rewritten;
    toast('Concept enhanced');
  } catch (err) {
    toast('Enhance failed: ' + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

// ─── Run detail ──────────────────────────────────────────────────────────

async function refreshRun() {
  if (!state.currentRunId) return;
  try {
    const st = await api.getRun(state.currentRunId);
    if (st.detail && /not found/i.test(st.detail)) {
      toast('Run was deleted'); showView('runs'); return;
    }
    state.currentRun = st;
    renderRun(st);
    maybeStartPolling(st);
    // Refresh the cost chip on every poll tick. Keeps spend visible as
    // shots/keyframes complete rather than waiting for a page reload. Fires
    // fire-and-forget so a slow /costs call doesn't block the UI update.
    refreshCostChip();
  } catch (err) {
    toast('Refresh failed: ' + (err.message || err));
  }
}

function maybeStartPolling(st) {
  const voStatus = (st.audio || {}).vo?.status;
  const anyGenerating =
    (st.keyframes || []).some(k => k.status === 'generating') ||
    (st.shots    || []).some(s => (s.variants || []).some(v => v.status === 'generating') || s.status === 'generating') ||
    (st.assets   || []).some(a => a.status === 'generating') ||
    st.status === 'stitching' ||
    st.status === 'ripping' ||
    st.status === 'translating' ||
    st.cut_plan_status === 'generating' ||
    st.asset_discovery_status === 'generating' ||
    voStatus === 'synthesizing' ||
    st.animatic_building;
  if (anyGenerating && !state.pollTimer) {
    state.pollTimer = setInterval(refreshRun, 2000);
  } else if (!anyGenerating && state.pollTimer) {
    stopPolling();
  }
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function renderRun(st) {
  // Header
  const story = st.story || {};
  document.getElementById('run-title').textContent = story.title || st.params?.title || '(untitled)';
  document.getElementById('run-logline').textContent = story.logline || st.concept?.slice(0, 140) || '';
  const chip = document.getElementById('run-status-chip');
  chip.textContent = st.status;
  // Map run status → cr-chip tone (single accent rule: amber for active, emerald for done, oxide for failed)
  const tone = (st.status === 'done' || st.status === 'ready') ? 'cr-chip-emerald'
             : (st.status === 'failed') ? 'cr-chip-oxide'
             : (st.status?.includes?.('partial') || st.status?.includes?.('generating')) ? 'cr-chip-amber'
             : '';
  chip.className = `cr-chip ${tone}`;

  // Phase bar + progress banner (banner only visible while a phase is active)
  renderPhaseBar(st);
  renderProgressBanner(st);

  // Phase sections
  renderStoryboard(st);
  renderAssets(st);
  renderCast(st);  // derives from state.assets[type=character], no network call
  renderLocationsProps(st);
  renderKeyframes(st);
  renderShots(st);
  renderReview(st);
  renderPolish(st);
  renderStitch(st);

  // Cost chip + clone/archive buttons
  refreshCostChip();
  document.getElementById('btn-clone-run').onclick = async () => {
    if (!confirm('Clone this run? Concept, storyboard, references, and assets are copied — but keyframes, shots, and the final trailer are cleared so you can re-render with different settings.')) return;
    try {
      const { run_id: newId } = await api.cloneRun(state.currentRunId, null);
      if (!newId) throw new Error('no run_id returned');
      toast(`Cloned → ${newId}`);
      showView('run', newId);
    } catch (err) { toast('Clone failed: ' + (err.message || err)); }
  };
  document.getElementById('btn-archive-run').href = `/api/runs/${state.currentRunId}/archive`;
}

// Cost thresholds in USD. Warn once per run per threshold so the user gets a
// heads-up without being nagged every poll. Ordered: first match wins.
const COST_THRESHOLDS = [
  { usd: 10, msg: '🚨 Spend over $10 on this run. Stop and review before continuing.' },
  { usd: 5,  msg: '⚠️ Spend over $5 on this run.' },
  { usd: 2,  msg: '💡 Spend passed $2.' },
];
// { runId: Set<number> } — which thresholds we've already shown for each run.
const _costThresholdsShown = {};

async function refreshCostChip() {
  if (!state.currentRunId) return;
  const rid = state.currentRunId;
  try {
    const { summary } = await api.getCosts(rid);
    const el = document.getElementById('run-cost-chip');
    if (!el) return;
    const total = (summary && typeof summary.total_usd === 'number') ? summary.total_usd : 0;
    el.textContent = `$${total.toFixed(2)}`;
    // By-phase tooltip with a total header so the user sees "where did the money go".
    const byPhase = summary?.by_phase || {};
    const tooltipLines = [
      `total: $${total.toFixed(4)}`,
      ...Object.entries(byPhase)
        .sort(([,a], [,b]) => (b || 0) - (a || 0))
        .map(([k, v]) => `  ${k}: $${typeof v === 'number' ? v.toFixed(4) : v}`),
    ];
    el.title = tooltipLines.join('\n');
    // Color escalation so a glance at the chip conveys urgency.
    el.classList.remove('text-zinc-400', 'text-amber-300', 'text-orange-300', 'text-red-400');
    if (total >= 10) el.classList.add('text-red-400');
    else if (total >= 5) el.classList.add('text-orange-300');
    else if (total >= 2) el.classList.add('text-amber-300');
    else el.classList.add('text-zinc-400');
    // One-shot threshold warnings.
    _costThresholdsShown[rid] = _costThresholdsShown[rid] || new Set();
    for (const t of COST_THRESHOLDS) {
      if (total >= t.usd && !_costThresholdsShown[rid].has(t.usd)) {
        _costThresholdsShown[rid].add(t.usd);
        toast(`${t.msg} ($${total.toFixed(2)})`);
        break;  // only one toast per refresh
      }
    }
  } catch (err) { console.warn('cost chip refresh failed:', err); }
}

// Per-phase time estimates in seconds. Derived from the worst-case wait times
// each provider advertises / the UI already shows elsewhere. These are
// intentionally a bit pessimistic so users aren't surprised by a missed ETA.
const _PHASE_SEC_PER_ITEM = {
  keyframe: 20,   // Nano Banana one image
  shot:     150,  // Seedance typical (UI says 60-180s)
  vo_line:  6,    // ElevenLabs TTS per line
  assets:   25,   // Nano Banana asset generation
};

function _countByStatus(items, predicate = (i) => i) {
  let ready = 0, generating = 0, pending = 0;
  for (const it of items || []) {
    const s = predicate(it);
    if (s === 'ready' || s === 'generated' || s === 'uploaded' || s === 'skipped') ready += 1;
    else if (s === 'generating' || s === 'synthesizing') generating += 1;
    else pending += 1;
  }
  return { ready, generating, pending, total: ready + generating + pending };
}

function _fmtEta(seconds) {
  if (seconds < 60) return `~${Math.max(5, Math.round(seconds / 10) * 10)}s`;
  const m = Math.max(1, Math.round(seconds / 60));
  return `~${m} min`;
}

function _activePhaseProgress(st) {
  // Order matters: show the FURTHEST-along active phase so a user rendering
  // shots doesn't see an outdated "assets: 3/5" banner from earlier.
  const kfCount = _countByStatus(st.keyframes || [], k => k.status);
  const assetCount = _countByStatus(st.assets || [], a => a.status);
  // Shots have variants — count as "generating" if any variant is generating.
  const shots = st.shots || [];
  let shotsReady = 0, shotsGen = 0;
  for (const s of shots) {
    const variants = s.variants || [];
    const anyGen = variants.some(v => v.status === 'generating');
    const allReady = variants.length > 0 && variants.every(v => v.status === 'ready');
    if (allReady) shotsReady += 1;
    else if (anyGen) shotsGen += 1;
  }

  const vo = (st.audio || {}).vo || {};
  const voLines = (vo.script?.lines) || [];
  const voAudio = vo.lines_audio || [];
  const voReady = voAudio.filter(a => a).length;

  // Stitching / ripping / translating are global single-step states; no fractional.
  if (st.status === 'stitching')
    return { label: 'Stitching final trailer', extra: '~30-60s', pct: null };
  if (st.status === 'ripping')
    return { label: 'Scene-detecting source', extra: '~1 min', pct: null };
  if (st.status === 'translating')
    return { label: 'Claude translating source → storyboard', extra: '~1 min', pct: null };

  if (shotsGen > 0) {
    const remaining = (shots.length - shotsReady) * _PHASE_SEC_PER_ITEM.shot;
    const pct = shots.length ? Math.round((shotsReady / shots.length) * 100) : 0;
    return { label: `Rendering shots: ${shotsReady}/${shots.length}`, extra: _fmtEta(remaining), pct };
  }
  if (kfCount.generating > 0) {
    const remaining = (kfCount.total - kfCount.ready) * _PHASE_SEC_PER_ITEM.keyframe;
    const pct = kfCount.total ? Math.round((kfCount.ready / kfCount.total) * 100) : 0;
    return { label: `Rendering keyframes: ${kfCount.ready}/${kfCount.total}`, extra: _fmtEta(remaining), pct };
  }
  if (vo.status === 'synthesizing' && voLines.length) {
    const remaining = (voLines.length - voReady) * _PHASE_SEC_PER_ITEM.vo_line;
    const pct = Math.round((voReady / voLines.length) * 100);
    return { label: `Synthesizing VO: ${voReady}/${voLines.length} lines`, extra: _fmtEta(remaining), pct };
  }
  if (assetCount.generating > 0) {
    const remaining = (assetCount.total - assetCount.ready) * _PHASE_SEC_PER_ITEM.assets;
    const pct = assetCount.total ? Math.round((assetCount.ready / assetCount.total) * 100) : 0;
    return { label: `Generating assets: ${assetCount.ready}/${assetCount.total}`, extra: _fmtEta(remaining), pct };
  }
  if (st.cut_plan_status === 'generating')
    return { label: 'Claude analyzing cut plan', extra: '~30s', pct: null };
  if (st.animatic_building)
    return { label: 'Building animatic preview', extra: '~15-30s', pct: null };
  return null;
}

function renderProgressBanner(st) {
  const el = document.getElementById('progress-banner');
  if (!el) return;
  const p = _activePhaseProgress(st);
  if (!p) { el.classList.add('hidden'); el.innerHTML = ''; return; }
  el.classList.remove('hidden');
  const bar = p.pct != null
    ? `<div class="mt-1.5 h-1 bg-amber-950 rounded overflow-hidden">
         <div class="h-full bg-amber-400" style="width: ${p.pct}%"></div>
       </div>`
    : '';
  el.innerHTML = `
    <div class="flex items-center gap-2">
      <div class="spin inline-block w-3 h-3 border-2 border-amber-900 border-t-amber-300 rounded-full"></div>
      <span class="flex-1">${escapeHtml(p.label)}</span>
      <span class="text-amber-400/80">${escapeHtml(p.extra || '')}</span>
    </div>
    ${bar}
  `;
}

function renderPhaseBar(st) {
  const allAssetsHandled = (st.assets || []).every(a => ['uploaded','generated','skipped'].includes(a.status));
  const hasAssetsPhase = st.asset_discovery_status === 'ready';
  const phases = [
    { key: 'storyboard', num: '01', label: 'Storyboard', done: !!st.story,
      generating: st.storyboard_status === 'generating' },
    ...(hasAssetsPhase ? [{ key: 'assets', num: '01·5', label: 'Assets', done: allAssetsHandled,
      generating: (st.assets || []).some(a => a.status === 'generating') }] : []),
    { key: 'keyframes',  num: '02', label: 'Keyframes',
      done: (st.keyframes || []).length > 0 && (st.keyframes || []).every(k => k.status === 'ready'),
      generating: (st.keyframes || []).some(k => k.status === 'generating') },
    { key: 'shots',      num: '03', label: 'Shots',
      done: (st.shots || []).length > 0 && (st.shots || []).every(s => s.status === 'ready'),
      generating: (st.shots || []).some(s => s.status === 'generating') },
    { key: 'review',     num: '03·5', label: 'Cut plan', done: !!(st.cut_plan && st.cut_plan.approved) },
    { key: 'stitch',     num: '04', label: 'Trailer', done: !!st.final },
  ];
  // First non-done phase is the active one
  const activeIdx = phases.findIndex(p => !p.done);
  phases.forEach((p, i) => { p.active = i === activeIdx; });

  const bar = document.getElementById('phase-bar');
  bar.className = 'mb-6';
  bar.innerHTML = `
    <div class="cr-phase-strip mb-2.5">
      ${phases.map(p => `<div class="cr-phase-cell ${p.done ? 'done' : ''} ${p.active || p.generating ? 'active' : ''}"></div>`).join('')}
    </div>
    <div class="flex">
      ${phases.map(p => {
        const numColor = p.active ? 'var(--lamp)' : p.done ? 'var(--bone-2)' : 'var(--dim-2)';
        const labelColor = p.active ? 'var(--bone)' : p.done ? 'var(--dim-2)' : 'var(--dim-3)';
        const marker = p.done ? '✓' : p.active ? '›' : '·';
        return `<div style="flex: 1; padding-right: 8px;">
          <div class="cr-mono" style="font-size: 9px; letter-spacing: 0.18em; text-transform: uppercase; color: ${numColor}; margin-bottom: 2px;">${marker} ${p.num}</div>
          <div class="cr-serif" style="font-size: 14px; color: ${labelColor}; letter-spacing: -0.01em;">${p.label}</div>
        </div>`;
      }).join('')}
    </div>`;
}

// ─── Phase 1: storyboard ─────────────────────────────────────────────────

function renderStoryboard(st) {
  const el = document.getElementById('phase-storyboard');
  // Multi-option storyboard picker
  if (!st.story && (st.storyboard_options || []).length > 0) {
    const opts = st.storyboard_options;
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">1. Storyboard <span class="text-zinc-600 font-normal normal-case ml-2">pick one of ${opts.length} options</span></h3>
        <button id="btn-regen-options" class="px-2 py-1 text-xs rounded hover:bg-zinc-900 text-zinc-400 hover:text-white">↻ regenerate options</button>
      </div>
      <div class="grid ${opts.length <= 1 ? 'grid-cols-1' : opts.length === 2 ? 'grid-cols-2' : 'grid-cols-3'} gap-3 mb-4">
        ${opts.map((o, oi) => `
          <div class="bg-zinc-900/50 border border-zinc-800 rounded p-3 text-xs hover:border-amber-500 cursor-pointer" data-option-idx="${oi}">
            <div class="font-semibold text-sm text-amber-300 mb-1">${escapeHtml(o.title || `Option ${oi+1}`)}</div>
            <div class="italic text-zinc-300 mb-2">${escapeHtml(o.logline || '')}</div>
            <div class="text-[10px] text-zinc-500 uppercase mb-0.5">Characters</div>
            <div class="text-zinc-400 line-clamp-2 mb-2">${escapeHtml(o.character_sheet || '')}</div>
            <div class="text-[10px] text-zinc-500 uppercase mb-0.5">World</div>
            <div class="text-zinc-400 line-clamp-2 mb-2">${escapeHtml(o.world_sheet || '')}</div>
            <div class="text-[10px] text-zinc-500 uppercase mb-0.5">Opening beats (first 2)</div>
            <div class="text-zinc-400 line-clamp-3 mb-2">${escapeHtml((o.shots || []).slice(0, 2).map(s => `${s.beat}: ${s.keyframe_prompt}`).join(' · '))}</div>
            <button class="mt-1 w-full px-2 py-1 text-[11px] rounded bg-amber-500 hover:bg-amber-400 text-black font-semibold">Use option ${oi+1}</button>
          </div>
        `).join('')}
      </div>
    `;
    el.querySelectorAll('[data-option-idx]').forEach(card => {
      card.addEventListener('click', async () => {
        const idx = parseInt(card.dataset.optionIdx, 10);
        toast(`Loading option ${idx+1}…`);
        try {
          await api.pickStoryOption(state.currentRunId, idx);
          await refreshRun();
        } catch (err) { toast('Pick failed: ' + err.message); }
      });
    });
    document.getElementById('btn-regen-options').onclick = async () => {
      if (!confirm('Regenerate all storyboard options?')) return;
      toast('Claude generating 3 new options…');
      try {
        await api.runStory(state.currentRunId, 3);
        await refreshRun();
      } catch (err) { toast('Failed: ' + err.message); }
    };
    if (!state.pollTimer) state.pollTimer = setInterval(refreshRun, 2000);
    return;
  }
  if (!st.story) {
    let label = 'Writing storyboard with Claude…';
    if (st.status === 'ripping') label = 'Scene-detecting source trailer…';
    else if (st.status === 'translating') label = 'Claude is translating segments into a new storyboard…';
    else if (st.status === 'failed') label = `✗ ${st.error || 'failed'}`;
    el.innerHTML = `
      <div class="border border-zinc-800 rounded p-6 text-center text-zinc-400 text-sm">
        ${st.status === 'failed' ? '' : '<div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full mb-3"></div>'}
        <div class="${st.status === 'failed' ? 'text-red-400' : ''}">${escapeHtml(label)}</div>
      </div>`;
    if (st.status !== 'failed' && !state.pollTimer) state.pollTimer = setInterval(refreshRun, 2000);
    return;
  }

  const approved = (st.keyframes || []).some(k => k.status !== 'pending');

  // Build the cinematic reel preview HTML (horizontal scrolling cards)
  const totalDuration = (st.story.shots || []).reduce((s, sh) => s + (sh.duration_s || 5), 0);
  const reelHtml = (st.story.shots || []).length ? `
    <div class="mb-2 flex items-baseline justify-between">
      <span class="cr-eyebrow">The reel</span>
      <span class="cr-mono" style="font-size: 10px; letter-spacing: 0.1em; color: var(--dim-2);">
        ← scroll · ${(st.story.shots || []).length} shots · ${totalDuration}s total
      </span>
    </div>
    <div class="cr-reel cr-fade-up" style="border: 1px solid var(--rule); margin-bottom: 14px;">
      ${(st.story.shots || []).map((shot, i) => {
        const tints = ['noir','dawn','amber','rain','fog','blood','jungle','cool'];
        const tint = tints[i % tints.length];
        const kf = (st.keyframes || [])[i];
        const hasKf = kf && kf.status === 'ready' && kf.path;
        const kfSrc = hasKf ? assetUrl(state.currentRunId, kf.path, kf.updated_at) : '';
        const featured = ((shot.featured_characters||[]).length || (shot.featured_locations||[]).length || (shot.featured_props||[]).length);
        return `
          <div class="cr-reel-item" style="width: 240px; padding: 12px; border-right: 1px solid var(--rule);">
            <div class="flex items-center justify-between" style="margin-bottom: 8px;">
              <span class="cr-mono" style="font-size: 11px; color: var(--lamp); letter-spacing: 0.1em;">SHOT ${String(i+1).padStart(2,'0')}</span>
              <span class="cr-mono" style="font-size: 10px; color: var(--dim);">${shot.duration_s || 5}s</span>
            </div>
            <div class="cr-thumb cr-thumb-${tint}" style="aspect-ratio: 21/9; position: relative;">
              ${hasKf ? `<img src="${kfSrc}" style="width:100%; height:100%; object-fit:cover; position:absolute; inset:0;" alt="kf ${i+1}">` : ''}
              <div style="position: absolute; top: 8px; left: 10px; font-family: var(--mono); font-size: 9px; letter-spacing: 0.15em; text-transform: uppercase; color: var(--bone); text-shadow: 0 1px 2px rgba(0,0,0,0.6); z-index: 2;">
                ${hasKf ? 'KF' : 'SH'} ${String(i+1).padStart(2,'0')}
              </div>
            </div>
            <div class="cr-serif-italic" style="font-size: 14px; color: var(--bone); margin-top: 10px; line-height: 1.35; min-height: 38px;">
              ${escapeHtml(shot.beat || '')}
            </div>
            <div class="cr-mono" style="font-size: 10px; margin-top: 8px; line-height: 1.5; height: 50px; overflow: hidden; color: var(--dim);">
              ${escapeHtml((shot.keyframe_prompt || '').slice(0, 110))}${(shot.keyframe_prompt || '').length > 110 ? '…' : ''}
            </div>
            <div class="flex items-center" style="gap: 6px; margin-top: 10px;">
              <button data-reel-jump="${i}" class="cr-mono" style="background: transparent; border: 0; padding: 0; color: var(--dim); font-size: 11px; cursor: pointer; letter-spacing: 0.04em;" onmouseover="this.style.color='var(--lamp)'" onmouseout="this.style.color='var(--dim)'">edit ↓</button>
              <span style="color: var(--dim-3);">·</span>
              ${featured ? `<span class="cr-chip cr-chip-amber" style="font-size: 9px; padding: 2px 6px;">featured</span>` : ''}
            </div>
          </div>
        `;
      }).join('')}
    </div>
  ` : '';

  el.innerHTML = `
    <div class="mb-1"><span class="cr-eyebrow">Phase 01 · Claude</span></div>
    <div class="flex items-baseline justify-between mb-3">
      <h2 class="cr-h3 cr-serif" style="font-size: 22px;">The storyboard.</h2>
      <div class="flex gap-3 text-xs">
        <button id="btn-regen-story" class="cr-mono" style="background: transparent; border: 0; color: var(--dim); cursor: pointer; font-size: 11px; letter-spacing: 0.04em;" onmouseover="this.style.color='var(--lamp)'" onmouseout="this.style.color='var(--dim)'">↻ regenerate all</button>
      </div>
    </div>
    ${reelHtml}

    ${st.rip_mode ? `
      <details class="mb-3 bg-sky-950/30 border border-sky-900/50 rounded text-xs">
        <summary class="cursor-pointer px-3 py-2 font-semibold text-sky-300 select-none flex items-center gap-2">
          <span>🎞 Rip-o-matic — ${(st.source_video?.segments || []).length} source segments</span>
          <span class="text-zinc-500 font-normal">(click to view / tune)</span>
        </summary>
        <div class="px-3 pb-3 border-t border-sky-900/50">
          <div class="text-zinc-400 mt-2 mb-2">Auto-attached as per-shot camera refs. Each scene keyframe uses the segment's first frame as a composition anchor.</div>
          ${(st.source_video?.cut_timeline || []).length ? `
            <div class="text-[10px] text-zinc-500 font-mono mb-2">
              source: ${st.source_video.duration.toFixed(1)}s · ${st.source_video.cut_timeline.length} cuts detected in full timeline · ${(st.source_video.segments || []).length} grouped into rendering segments
            </div>
          ` : ''}
          <div class="grid grid-cols-5 gap-1 mb-3">
            ${(st.source_video?.segments || []).map((s, i) => `
              <div class="relative cursor-pointer group" data-seg-idx="${i}">
                ${s.first_frame_path
                  ? `<img src="${assetUrl(state.currentRunId, s.first_frame_path)}" class="w-full aspect-video object-cover rounded border border-zinc-800 group-hover:border-sky-500">`
                  : `<div class="w-full aspect-video bg-black rounded border border-zinc-800"></div>`}
                <div class="absolute top-0.5 left-0.5 text-[9px] px-1 rounded bg-black/70 text-sky-300 font-mono">seg ${i + 1}</div>
                <div class="text-[10px] text-center mt-0.5 text-zinc-500 font-mono">${s.duration.toFixed(1)}s</div>
              </div>
            `).join('')}
          </div>
          <details class="bg-zinc-950 border border-zinc-800 rounded">
            <summary class="cursor-pointer px-2 py-1.5 text-[11px] text-zinc-400 hover:text-white select-none">⚙ tune segmentation</summary>
            <div class="p-3 space-y-2">
              <div class="text-zinc-500 text-[11px] mb-2">Re-run scene detection with different thresholds. Doesn't re-translate the storyboard — do that separately if you want to re-plan shots.</div>
              <div class="grid grid-cols-3 gap-2">
                <div>
                  <label class="block text-[10px] text-zinc-500 mb-0.5">threshold</label>
                  <input id="seg-threshold" type="number" step="0.05" min="0.1" max="0.6" value="${st.segmentation_params?.scene_threshold ?? 0.30}" class="w-full bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs">
                </div>
                <div>
                  <label class="block text-[10px] text-zinc-500 mb-0.5">min shots</label>
                  <input id="seg-min-shots" type="number" min="3" max="15" value="${st.segmentation_params?.min_shots ?? 4}" class="w-full bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs">
                </div>
                <div>
                  <label class="block text-[10px] text-zinc-500 mb-0.5">max shots</label>
                  <input id="seg-max-shots" type="number" min="5" max="20" value="${st.segmentation_params?.max_shots ?? 10}" class="w-full bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs">
                </div>
                <div>
                  <label class="block text-[10px] text-zinc-500 mb-0.5">min seg (s)</label>
                  <input id="seg-min-s" type="number" step="0.5" min="1" max="10" value="${st.segmentation_params?.min_segment_s ?? 2.5}" class="w-full bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs">
                </div>
                <div>
                  <label class="block text-[10px] text-zinc-500 mb-0.5">max seg (s)</label>
                  <input id="seg-max-s" type="number" step="0.5" min="3" max="15" value="${st.segmentation_params?.max_segment_s ?? 12.0}" class="w-full bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs">
                </div>
                <div class="flex items-end">
                  <button id="btn-resegment" class="w-full px-2 py-1 text-[11px] bg-sky-600 hover:bg-sky-500 text-white rounded font-semibold">↻ re-segment</button>
                </div>
              </div>
            </div>
          </details>
        </div>
      </details>
    ` : ''}
    <div class="grid grid-cols-2 gap-3 mb-4 text-xs">
      <div class="bg-zinc-900/50 border border-zinc-800 rounded p-3">
        <div class="text-zinc-500 mb-1">CHARACTER SHEET</div>
        <div class="text-zinc-300">${escapeHtml(st.story.character_sheet || '(none)')}</div>
      </div>
      <div class="bg-zinc-900/50 border border-zinc-800 rounded p-3">
        <div class="text-zinc-500 mb-1">WORLD SHEET</div>
        <div class="text-zinc-300">${escapeHtml(st.story.world_sheet || '(none)')}</div>
      </div>
    </div>

    <div id="shots-editor" class="space-y-2 mb-4"></div>

    <div class="flex items-center gap-3">
      <button id="btn-save-story" class="px-3 py-1.5 text-xs rounded border border-zinc-800 hover:bg-zinc-900 text-zinc-400 hover:text-white">Save edits</button>
      <button id="btn-approve-story" class="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-black font-semibold rounded text-sm ${approved ? 'opacity-50' : ''}">
        ${approved ? 'Storyboard approved ✓' : 'Approve → generate keyframes'}
      </button>
      ${approved ? '<span class="text-xs text-zinc-500">(you can still edit + save, but existing keyframes will not regenerate)</span>' : ''}
    </div>
  `;

  const shotsEditor = document.getElementById('shots-editor');
  (st.story.shots || []).forEach((shot, i) => {
    const row = document.createElement('div');
    row.className = 'bg-zinc-900/50 border border-zinc-800 rounded p-3 text-xs';
    row.innerHTML = `
      <div class="flex items-baseline gap-3 mb-2">
        <div class="font-mono text-amber-400">shot ${i + 1}</div>
        <input type="text" data-field="beat" class="flex-1 bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs" value="${escapeAttr(shot.beat || '')}">
        <label class="text-zinc-500 text-xs">duration (s)</label>
        <input type="number" min="3" max="10" data-field="duration_s" class="w-16 bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs" value="${shot.duration_s || 5}">
      </div>
      ${((shot.featured_characters||[]).length || (shot.featured_locations||[]).length || (shot.featured_props||[]).length) ? `<div class="flex flex-wrap gap-1 mb-2">${(shot.featured_characters||[]).map(n=>`<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-sky-900/40 text-sky-300 border border-sky-800/50">👤 ${escapeHtml(n)}</span>`).join('')}${(shot.featured_locations||[]).map(n=>`<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-emerald-900/40 text-emerald-300 border border-emerald-800/50">📍 ${escapeHtml(n)}</span>`).join('')}${(shot.featured_props||[]).map(n=>`<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-violet-900/40 text-violet-300 border border-violet-800/50">🔧 ${escapeHtml(n)}</span>`).join('')}</div>` : ''}
      <div class="grid grid-cols-2 gap-3">
        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="text-zinc-500">KEYFRAME PROMPT</span>
            <button type="button" data-enhance="keyframe_prompt" class="text-[11px] text-amber-400 hover:text-amber-300">✨ enhance</button>
          </div>
          <textarea rows="4" data-field="keyframe_prompt" class="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs font-mono focus:border-amber-500 focus:outline-none">${escapeHtml(shot.keyframe_prompt || '')}</textarea>
        </div>
        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="text-zinc-500">MOTION PROMPT</span>
            <button type="button" data-enhance="motion_prompt" class="text-[11px] text-amber-400 hover:text-amber-300">✨ enhance</button>
          </div>
          <textarea rows="4" data-field="motion_prompt" class="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs font-mono focus:border-amber-500 focus:outline-none">${escapeHtml(shot.motion_prompt || '')}</textarea>
        </div>
      </div>
    `;
    // Wire up enhance buttons for this row
    row.querySelectorAll('[data-enhance]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const kind = btn.dataset.enhance;
        const ta = row.querySelector(`[data-field="${kind}"]`);
        const text = ta.value.trim();
        if (!text) { toast('Write the prompt first'); return; }
        btn.disabled = true;
        const old = btn.textContent;
        btn.textContent = '✨ …';
        try {
          const { text: rewritten, detail } = await api.enhance(kind, text, {
            beat: row.querySelector('[data-field=beat]').value,
            aspect_ratio: st.params?.ratio || '',
            style_intent: st.params?.style_intent || '',
            character_sheet: st.story?.character_sheet || '',
            world_sheet: st.story?.world_sheet || '',
          });
          if (!rewritten) throw new Error(detail || 'no result');
          ta.value = rewritten;
          toast(`Shot ${i+1} ${kind.replace('_', ' ')} enhanced`);
        } catch (err) {
          toast('Enhance failed: ' + (err.message || err));
        } finally {
          btn.disabled = false;
          btn.textContent = old;
        }
      });
    });
    shotsEditor.appendChild(row);
  });

  // Reel cards "edit ↓" jumps to the matching editor row below
  el.querySelectorAll('[data-reel-jump]').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.reelJump, 10);
      const target = shotsEditor.children[idx];
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.style.transition = 'box-shadow 200ms';
        target.style.boxShadow = '0 0 0 1px var(--lamp), 0 0 24px -8px rgba(232,184,92,0.6)';
        setTimeout(() => { target.style.boxShadow = ''; }, 1400);
      }
    });
  });

  document.getElementById('btn-save-story').onclick = async () => {
    const edited = gatherStoryEdits(st);
    try {
      await api.saveStory(state.currentRunId, edited);
      toast('Storyboard saved');
      await refreshRun();
    } catch (err) { toast('Save failed: ' + err.message); }
  };
  document.getElementById('btn-regen-story').onclick = async () => {
    if (!confirm('Rewrite the whole storyboard from scratch? Any existing keyframes/shots stay on disk but will no longer match.')) return;
    toast('Regenerating storyboard…');
    try {
      await api.runStory(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Failed: ' + err.message); }
  };
  document.getElementById('btn-resegment')?.addEventListener('click', async () => {
    if (!confirm('Re-run scene detection? Keyframes/shots already rendered will NOT be invalidated — but the source segments, cut timeline, and auto-attached video refs will be replaced. Usually you want to also regenerate the storyboard after this.')) return;
    const params = {
      scene_threshold: parseFloat(document.getElementById('seg-threshold').value) || 0.30,
      min_shots: parseInt(document.getElementById('seg-min-shots').value, 10) || 4,
      max_shots: parseInt(document.getElementById('seg-max-shots').value, 10) || 10,
      min_segment_s: parseFloat(document.getElementById('seg-min-s').value) || 2.5,
      max_segment_s: parseFloat(document.getElementById('seg-max-s').value) || 12.0,
    };
    toast('Re-segmenting source…');
    try {
      const result = await api.previewSegments(state.currentRunId, params);
      toast(`✓ ${result.segments?.length || 0} segments · ${result.cut_count} cuts in source`);
      await refreshRun();
    } catch (err) { toast('Re-segment failed: ' + (err.message || err)); }
  });

  document.getElementById('btn-approve-story').onclick = async () => {
    try {
      // Save edits first (ensures what's on screen = what we animate)
      const edited = gatherStoryEdits(st);
      await api.saveStory(state.currentRunId, edited);
      // If asset discovery has never run, run it first. Otherwise go straight to keyframes.
      if (st.asset_discovery_status !== 'ready') {
        toast('Scanning for concrete assets…');
        await api.discoverAssets(state.currentRunId);
        await refreshRun();
        document.getElementById('phase-assets').scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else {
        toast('Generating keyframes…');
        await api.runAllKf(state.currentRunId);
        await refreshRun();
      }
    } catch (err) { toast('Failed: ' + (err.message || err)); }
  };
}

function gatherStoryEdits(st) {
  const shots = [];
  document.querySelectorAll('#shots-editor > div').forEach((row, i) => {
    const orig = st.story.shots[i] || {};
    shots.push({
      ...orig,
      beat:             row.querySelector('[data-field=beat]').value,
      duration_s:       parseInt(row.querySelector('[data-field=duration_s]').value, 10) || 5,
      keyframe_prompt:  row.querySelector('[data-field=keyframe_prompt]').value,
      motion_prompt:    row.querySelector('[data-field=motion_prompt]').value,
    });
  });
  return { ...st.story, shots };
}

// ─── Phase 1.5: assets ───────────────────────────────────────────────────

function renderAssets(st) {
  const el = document.getElementById('phase-assets');
  const status = st.asset_discovery_status;
  if (!st.story || !status) { el.innerHTML = ''; return; }

  const TYPE_ICON = { logo: '🏷', product: '📦', location: '📍', character: '👤', prop: '🎯' };

  // Discovery in flight
  if (status === 'generating') {
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">1.5 Assets <span class="text-zinc-600 font-normal normal-case ml-2">Claude scan</span></h3>
      </div>
      <div class="border border-zinc-800 rounded p-6 text-center text-zinc-400 text-sm">
        <div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full mb-3"></div>
        <div>Looking for logos, branded products, named locations…</div>
      </div>`;
    return;
  }
  if (status === 'failed') {
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">1.5 Assets</h3>
      </div>
      <div class="border border-red-900/50 rounded p-4 text-sm text-red-400">
        ✗ Asset discovery failed: ${escapeHtml(st.asset_discovery_error || '')}
      </div>`;
    return;
  }

  const items = st.assets || [];
  const allHandled = items.every(a => ['uploaded','generated','skipped'].includes(a.status));
  const keyframesStarted = (st.keyframes || []).some(k => k.status !== 'pending');
  const pendingCount = items.filter(a => ['pending','failed'].includes(a.status)).length;
  const generatingCount = items.filter(a => a.status === 'generating').length;

  // Empty — nothing flagged
  if (items.length === 0) {
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">1.5 Assets <span class="text-zinc-600 font-normal normal-case ml-2">Claude scan</span></h3>
      </div>
      <div class="border border-zinc-800 rounded p-4 text-sm text-zinc-400 flex items-center gap-4">
        <div class="flex-1">
          <div class="text-zinc-300">✓ No external assets needed.</div>
          <div class="text-xs text-zinc-500 mt-1">${escapeHtml(st.asset_discovery_reasoning || '')}</div>
        </div>
        ${keyframesStarted ? '<span class="text-xs text-emerald-400">keyframes already running ✓</span>' : `
          <button id="btn-to-keyframes" class="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-black font-semibold rounded text-sm whitespace-nowrap">Generate keyframes →</button>
        `}
      </div>`;
    const btn = document.getElementById('btn-to-keyframes');
    if (btn) {
      btn.onclick = async () => {
        toast('Generating keyframes…');
        await api.runAllKf(state.currentRunId);
        await refreshRun();
      };
    }
    return;
  }

  const promotableCount = items.filter(a => ['uploaded','generated'].includes(a.status) && a.path).length;

  el.innerHTML = `
    <div class="flex items-baseline justify-between mb-3">
      <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">1.5 Assets <span class="text-zinc-600 font-normal normal-case ml-2">Claude scan</span></h3>
      <div class="flex gap-2 text-xs">
        ${pendingCount > 0 ? `<button id="btn-gen-all-assets" class="px-2 py-1 rounded bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 hover:text-amber-200 font-medium">✨ generate all (${pendingCount})</button>` : ''}
        ${generatingCount > 0 ? `<span class="px-2 py-1 text-amber-400">generating ${generatingCount}…</span>` : ''}
        ${promotableCount > 0 ? `<button id="btn-promote-all-assets" class="px-2 py-1 rounded border border-emerald-800 hover:bg-emerald-900/40 text-emerald-400 hover:text-emerald-200 font-medium">📚 save all to library (${promotableCount})</button>` : ''}
        <button id="btn-rediscover" class="px-2 py-1 rounded hover:bg-zinc-900 text-zinc-400 hover:text-white">↻ re-scan</button>
      </div>
    </div>
    ${st.asset_discovery_reasoning ? `<div class="text-xs text-zinc-500 mb-3">${escapeHtml(st.asset_discovery_reasoning)}</div>` : ''}
    <div id="assets-grid" class="grid grid-cols-2 gap-3 mb-4"></div>
    <div class="flex items-center gap-3">
      <button id="btn-approve-assets" class="px-4 py-2 ${allHandled ? 'bg-amber-500 hover:bg-amber-400 text-black' : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'} font-semibold rounded text-sm" ${allHandled ? '' : 'disabled'}>
        ${keyframesStarted ? 'Assets approved ✓' : 'Approve assets → generate keyframes'}
      </button>
      ${allHandled ? '' : `<span class="text-xs text-zinc-500">handle every asset (upload / generate / skip) first</span>`}
    </div>`;

  const grid = document.getElementById('assets-grid');
  items.forEach(a => grid.appendChild(assetCard(st, a, TYPE_ICON)));

  const genAllBtn = document.getElementById('btn-gen-all-assets');
  if (genAllBtn) {
    genAllBtn.onclick = async () => {
      toast(`Generating ${pendingCount} asset(s)…`);
      try {
        await api.generateAllAssets(state.currentRunId);
        await refreshRun();
      } catch (err) { toast('Batch generation failed: ' + (err.message || err)); }
    };
  }
  const promoteAllBtn = document.getElementById('btn-promote-all-assets');
  if (promoteAllBtn) {
    promoteAllBtn.onclick = async () => {
      if (!confirm(`Save ${promotableCount} asset(s) to the library?`)) return;
      try {
        const res = await api.promoteAllAssets(state.currentRunId);
        toast(`Saved ${res.promoted?.length || 0} asset(s) to library${res.errors?.length ? ` (${res.errors.length} failed)` : ''}`);
        await refreshRun();
      } catch (err) { toast('Batch promote failed: ' + (err.message || err)); }
    };
  }
  document.getElementById('btn-rediscover').onclick = async () => {
    if (!confirm('Re-scan the storyboard? Existing asset choices will be wiped.')) return;
    try {
      await api.discoverAssets(state.currentRunId);
      toast('Re-scanning…');
      await refreshRun();
    } catch (err) { toast('Re-scan failed: ' + (err.message || err)); }
  };
  const approveBtn = document.getElementById('btn-approve-assets');
  if (allHandled && !keyframesStarted) {
    approveBtn.onclick = async () => {
      toast('Generating keyframes…');
      try {
        await api.runAllKf(state.currentRunId);
        await refreshRun();
      } catch (err) { toast('Keyframe generation failed: ' + (err.message || err)); }
    };
  }
}

function assetCard(st, a, TYPE_ICON) {
  const card = document.createElement('div');
  // data-asset-id lets the cast panel scroll to + highlight a specific asset
  // card when the user clicks ⬆ upload on a character.
  card.dataset.assetId = a.id || '';
  const badgeClr = {
    pending: 'bg-zinc-900 text-zinc-400 border-zinc-800',
    uploaded: 'bg-emerald-900/50 text-emerald-300 border-emerald-800',
    generated: 'bg-sky-900/50 text-sky-300 border-sky-800',
    skipped: 'bg-zinc-900 text-zinc-600 border-zinc-800',
    generating: 'bg-amber-900/50 text-amber-300 border-amber-800',
    failed: 'bg-red-900/50 text-red-300 border-red-800',
  }[a.status] || 'bg-zinc-900 text-zinc-400 border-zinc-800';

  const preview = a.path
    ? `<img src="${assetUrl(state.currentRunId, a.path, a.updated_at)}" class="w-full h-32 object-contain bg-black rounded">`
    : (a.status === 'generating'
      ? `<div class="w-full h-32 bg-black rounded flex items-center justify-center">
          <div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full"></div>
         </div>`
      : `<div class="w-full h-32 bg-zinc-950 rounded border border-dashed border-zinc-800 flex items-center justify-center text-zinc-600 text-xs">
          ${a.status === 'skipped' ? '(skipped)' : '(no asset yet)'}
         </div>`);

  card.className = 'bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs';
  card.innerHTML = `
    <div class="flex items-baseline gap-2 mb-2">
      <span class="text-lg leading-none">${TYPE_ICON[a.type] || '·'}</span>
      <div class="flex-1 min-w-0">
        <div class="font-semibold text-zinc-200 truncate">${escapeHtml(a.name)}</div>
        <div class="text-[10px] text-zinc-500 uppercase font-mono">${a.type} · shots ${(a.shots || []).map(i => i+1).join(', ') || '?'}</div>
      </div>
      <span class="px-1.5 py-0.5 rounded border font-mono text-[10px] ${badgeClr}">${a.status}</span>
    </div>
    <div class="text-zinc-400 mb-2">${escapeHtml(a.description || '')}</div>
    ${preview}
    <div class="mt-2 grid ${['uploaded','generated'].includes(a.status) ? 'grid-cols-4' : 'grid-cols-3'} gap-1">
      <label class="text-[11px] text-center px-2 py-1 rounded border border-zinc-800 hover:bg-zinc-800 hover:text-white text-zinc-400 cursor-pointer">
        upload
        <input type="file" accept="image/*" class="hidden" data-action="upload-asset">
      </label>
      <button data-action="gen-asset" class="text-[11px] px-2 py-1 rounded border border-zinc-800 hover:bg-zinc-800 hover:text-white text-zinc-400">✨ generate</button>
      <button data-action="skip-asset" class="text-[11px] px-2 py-1 rounded border border-zinc-800 hover:bg-zinc-800 hover:text-red-300 text-zinc-400">skip</button>
      ${['uploaded','generated'].includes(a.status) ? '<button data-action="promote-asset" class="text-[11px] px-2 py-1 rounded border border-emerald-800 hover:bg-emerald-900 hover:text-emerald-200 text-emerald-400">library</button>' : ''}
    </div>
    ${a.error ? `<div class="text-red-400 text-[10px] mt-2">✗ ${escapeHtml(a.error)}</div>` : ''}
  `;

  card.querySelector('[data-action=upload-asset]').onchange = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    toast(`Uploading ${a.name}…`);
    try {
      await api.uploadAsset(state.currentRunId, a.id, file);
      await refreshRun();
    } catch (err) { toast('Upload failed: ' + err.message); }
  };
  card.querySelector('[data-action=gen-asset]').onclick = async () => {
    const override = await openModal({
      title: `Generate ${a.name}`,
      body: `Claude's suggested prompt is pre-filled. Edit if you want more control. Leave as-is to use it unchanged.`,
      defaultText: a.generation_prompt || a.suggested_generation_prompt || '',
      placeholder: 'Minimalist logo on white background…',
      confirmLabel: 'Generate with Nano Banana',
    });
    if (override === undefined) return;
    try {
      await api.generateAsset(state.currentRunId, a.id, override || null);
      toast(`Generating ${a.name}…`);
      await refreshRun();
    } catch (err) { toast('Generation failed: ' + (err.message || err)); }
  };
  card.querySelector('[data-action=skip-asset]').onclick = async () => {
    try {
      await api.skipAsset(state.currentRunId, a.id);
      toast(`Skipped ${a.name}`);
      await refreshRun();
    } catch (err) { toast('Skip failed: ' + (err.message || err)); }
  };
  const promoteBtn = card.querySelector('[data-action=promote-asset]');
  if (promoteBtn) {
    promoteBtn.onclick = async () => {
      try {
        await api.promoteAsset(state.currentRunId, a.id, a.name, a.description, '');
        toast(`Saved "${a.name}" to library`);
      } catch (err) { toast('Promote failed: ' + (err.message || err)); }
    };
  }
  return card;
}

// ─── Phase 1.6: cast panel ───────────────────────────────────────────────
// Pulls character-type entries out of the asset discovery output and renders
// them as a coverage matrix: which characters appear in which shots, and
// whether a reference anchors them. Goal: let the user see "shot 4 has an
// uncovered character" BEFORE they spend money rendering.

// A character is considered "covered" if any of:
//   1. The discovered asset has status uploaded / generated (user provided a ref)
//   2. There's at least 1 reference image in state.references (a flat list — we
//      can't tell which character a ref is of, so ref count > 0 is a weak "yes")
//   3. A library item was injected that matches the character's name/slug
//
// #1 is the strong signal; #2 and #3 are hedges for when refs exist outside
// the asset-discovery flow (legacy runs, rip-o-matic, direct library injects).
function _characterCoverage(char, refs, injectedLibSlugs, cachedLibItems) {
  if (['uploaded', 'generated'].includes(char.status)) {
    return { covered: true, why: 'asset provided' };
  }
  // Try to match the character's name against injected library character slugs.
  const slugish = (char.name || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 20);
  const matchingLib = (cachedLibItems || []).find(i => {
    if (i.kind !== 'characters') return false;
    const libSlug = (i.slug || '').toLowerCase();
    const libName = (i.name || '').toLowerCase();
    return injectedLibSlugs.has(libSlug) &&
           (libSlug.includes(slugish) || slugish.includes(libSlug) ||
            libName.includes((char.name || '').toLowerCase().split(/[—\-]/)[0].trim()));
  });
  if (matchingLib) {
    return { covered: true, why: `library: ${matchingLib.name}` };
  }
  if (refs > 0) {
    // We can't know the ref is for THIS character, but refs > 0 is at least a
    // guardrail against "zero anchors of any kind." Soft-covered, not strong.
    return { covered: 'soft', why: `${refs} reference(s) in run (unverified match)` };
  }
  return { covered: false, why: 'no anchor' };
}

// We cache library characters on the first cast render so we can cross-reference
// injected refs against library item names. Refreshed whenever the cast panel
// re-renders — cheap enough (single /api/library GET).
let _castLibCache = null;

async function _ensureLibCache() {
  if (_castLibCache !== null) return _castLibCache;
  try {
    const data = await api.listLibrary();
    _castLibCache = [];
    for (const kind of Object.keys(data)) {
      for (const it of (data[kind] || [])) {
        _castLibCache.push({ ...it, kind });
      }
    }
  } catch { _castLibCache = []; }
  return _castLibCache;
}

function _invalidateCastLibCache() { _castLibCache = null; }

async function renderCast(st) {
  const el = document.getElementById('phase-cast');
  const assets = st.assets || [];
  const assetCharacters = assets.filter(a => a.type === 'character');

  // Two sources of cast info:
  //   1. state.cast — the user-defined cast (set at create-run, Claude uses
  //      these as the only named roles). Each has library provenance.
  //   2. state.assets[type=character] — discovered by Claude when assets
  //      phase runs and the user hadn't defined a cast upfront.
  // If state.cast exists we prefer it since it's authoritative; assets may
  // redundantly describe the same characters.
  const preDefinedCast = (st.cast || []);
  // Build virtual "character" entries from state.cast so the existing coverage
  // + card renderer works uniformly. These are always covered (the library
  // item's images were injected at create time).
  const fromCast = preDefinedCast.map(c => ({
    name: c.name || c.slug,
    description: c.description || '',
    // No specific shot mapping — pre-defined cast applies to whatever Claude
    // writes. Leave shots empty; the card will say "any shot".
    shots: [],
    status: 'uploaded',          // strong-covered
    _source: 'cast',
    _slug: c.slug,
  }));

  const characters = fromCast.length ? fromCast : assetCharacters;
  if (!characters.length) { el.innerHTML = ''; return; }

  const refs = (st.references || []).length;

  // Determine which library slugs are injected. Injected refs live under
  // references/ with filenames like `characters_<slug>_<file>.jpg`, so we can
  // reverse-engineer which library items are in the run.
  const injectedLibSlugs = new Set();
  for (const r of (st.references || [])) {
    // The library.inject_into_run naming convention: <kind>_<slug>_<origname>
    const m = /^references\/(characters)_([a-z0-9_-]+)_/i.exec(r);
    if (m) injectedLibSlugs.add(m[2].toLowerCase());
  }
  const libItems = await _ensureLibCache();

  // Compute per-shot coverage: a shot is "uncovered" if any character
  // appearing in it isn't covered.
  const shotCount = (st.shots || []).length || (st.story?.shots?.length || 0);
  const shotUncovered = new Set();
  const coverages = characters.map(c => ({
    char: c,
    cov: _characterCoverage(c, refs, injectedLibSlugs, libItems),
  }));
  for (const { char, cov } of coverages) {
    if (!cov.covered) {
      for (const shotIdx of (char.shots || [])) shotUncovered.add(shotIdx);
    }
  }

  const coveredCount = coverages.filter(c => c.cov.covered === true).length;
  const softCount = coverages.filter(c => c.cov.covered === 'soft').length;
  const uncoveredCount = coverages.filter(c => !c.cov.covered).length;

  // Top banner — summary + uncovered shots list.
  const shotsUncoveredList = [...shotUncovered].sort((a, b) => a - b)
    .map(i => i + 1).join(', ');
  const bannerClass = uncoveredCount === 0
    ? 'bg-emerald-950/30 border-emerald-900/50 text-emerald-200'
    : 'bg-red-950/30 border-red-900/50 text-red-200';
  const bannerText = uncoveredCount === 0
    ? `✓ All ${characters.length} characters covered${softCount ? ` (${softCount} with loose matches)` : ''}`
    : `⚠ ${uncoveredCount} of ${characters.length} characters uncovered — shots ${shotsUncoveredList} will drift`;

  // Build per-shot tracking matrix from featured_characters in storyboard
  const storyShots = st.story?.shots || [];
  const charNames = characters.map(c => c.name || '(unnamed)');
  let matrixHtml = '';
  if (storyShots.length && charNames.length) {
    const rows = charNames.map(name => {
      const nameLower = name.toLowerCase();
      const cells = storyShots.map((shot, si) => {
        const featured = (shot.featured_characters || []).map(n => n.toLowerCase());
        const present = featured.some(f => f.includes(nameLower) || nameLower.includes(f));
        return `<td class="px-1 py-0.5 text-center border border-zinc-800 ${present ? 'bg-sky-900/40 text-sky-300' : 'text-zinc-700'}">${present ? '●' : '·'}</td>`;
      }).join('');
      return `<tr><td class="px-2 py-0.5 text-right text-zinc-400 border border-zinc-800 whitespace-nowrap max-w-[120px] truncate" title="${escapeAttr(name)}">${escapeHtml(name)}</td>${cells}</tr>`;
    }).join('');
    const header = storyShots.map((_, i) => `<th class="px-1 py-0.5 text-center text-zinc-500 border border-zinc-800 font-mono">${i+1}</th>`).join('');
    matrixHtml = `
      <details class="mb-4">
        <summary class="text-[11px] text-zinc-500 cursor-pointer hover:text-zinc-300 select-none mb-1">Shot coverage matrix</summary>
        <div class="overflow-x-auto mt-1">
          <table class="text-[10px] border-collapse">
            <thead><tr><th class="px-2 py-0.5 text-right text-zinc-600 border border-zinc-800">Character</th>${header}</tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </details>`;
  }

  el.innerHTML = `
    <div class="flex items-baseline justify-between mb-3">
      <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">🎭 Cast</h3>
      <span class="text-[11px] text-zinc-500 font-mono">${coveredCount}/${characters.length} covered${softCount ? ` · ${softCount} soft` : ''}</span>
    </div>
    <div class="${bannerClass} border rounded px-3 py-2 mb-4 text-xs font-mono">${bannerText}</div>
    ${matrixHtml}
    <div class="grid grid-cols-1 md:grid-cols-2 gap-3" id="cast-grid"></div>
  `;

  const grid = el.querySelector('#cast-grid');
  for (const { char, cov } of coverages) {
    grid.appendChild(_castCard(char, cov, libItems));
  }
}

function _castCard(char, cov, libItems) {
  const card = document.createElement('div');
  const covClass = cov.covered === true
    ? 'border-emerald-900/50 bg-emerald-950/10'
    : cov.covered === 'soft'
      ? 'border-amber-900/50 bg-amber-950/10'
      : 'border-red-900/50 bg-red-950/10';
  card.className = `border ${covClass} rounded p-3 text-xs`;

  const covBadge = cov.covered === true
    ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-300 font-mono">✓ covered</span>'
    : cov.covered === 'soft'
      ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/50 text-amber-300 font-mono">~ soft</span>'
      : '<span class="text-[10px] px-1.5 py-0.5 rounded bg-red-900/50 text-red-300 font-mono">✗ uncovered</span>';

  // Suggest library matches for uncovered characters so the user has one click
  // to fix coverage if the right item already exists.
  const libMatches = cov.covered !== true
    ? (libItems || []).filter(i => {
        if (i.kind !== 'characters') return false;
        const name = (i.name || '').toLowerCase();
        const charName = (char.name || '').toLowerCase().split(/[—\-]/)[0].trim();
        return charName && name.includes(charName);
      }).slice(0, 3)
    : [];

  card.innerHTML = `
    <div class="flex items-start gap-2 mb-2">
      <div class="flex-1 min-w-0">
        <div class="font-semibold text-zinc-200 truncate">${escapeHtml(char.name || '(unnamed)')}</div>
        ${char.description ? `<div class="text-[11px] text-zinc-500 line-clamp-2 mt-0.5">${escapeHtml(char.description)}</div>` : ''}
      </div>
      ${covBadge}
    </div>
    <div class="flex flex-wrap gap-1 mb-2">
      ${(char.shots || []).length
        ? (char.shots || []).map(s => `<span class="text-[10px] px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400 font-mono">shot ${s + 1}</span>`).join('')
        : (char._source === 'cast'
            ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-fuchsia-900/40 text-fuchsia-300 font-mono">🎭 fixed cast — any shot</span>'
            : '<span class="text-[10px] text-zinc-600">no shots assigned</span>')}
    </div>
    <div class="text-[10px] text-zinc-500 mb-2">coverage: ${escapeHtml(cov.why)}</div>
    ${libMatches.length ? `
      <div class="text-[10px] text-zinc-500 mb-1">Library suggestions:</div>
      <div class="flex flex-wrap gap-1 mb-2">
        ${libMatches.map(m => `
          <button data-cast-inject="characters/${escapeAttr(m.slug)}" class="text-[10px] px-1.5 py-0.5 rounded border border-zinc-700 hover:bg-emerald-900/30 hover:text-emerald-200 text-zinc-400 font-mono">📚 ${escapeHtml(m.name)}</button>
        `).join('')}
      </div>
    ` : ''}
    <div class="flex gap-1 pt-1 border-t border-zinc-800">
      <button data-cast-turnaround="${escapeAttr(char.name)}" data-cast-desc="${escapeAttr(char.description || '')}"
        class="flex-1 text-[11px] px-2 py-1 rounded border border-zinc-800 hover:bg-fuchsia-900/30 hover:text-fuchsia-200 text-zinc-400" title="Generate a 5-angle turnaround in the library for this character">✨ turnaround</button>
      <button data-cast-upload="${escapeAttr(char.id || '')}" class="text-[11px] px-2 py-1 rounded border border-zinc-800 hover:bg-zinc-800 text-zinc-400 hover:text-white" title="Upload a reference image">⬆ upload</button>
    </div>
  `;

  card.querySelector('[data-cast-turnaround]')?.addEventListener('click', () => {
    // Open the turnaround modal with this character's name + description prefilled.
    openTurnaroundModal();
    setTimeout(() => {
      document.getElementById('ta-name').value = char.name || '';
      document.getElementById('ta-description').value = char.description || '';
    }, 60);
  });

  card.querySelectorAll('[data-cast-inject]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!state.currentRunId) return;
      const [kind, slug] = btn.dataset.castInject.split('/');
      try {
        await api.injectLibrary(state.currentRunId, kind, slug, 'references');
        toast(`Injected ${slug} into run`);
        _invalidateCastLibCache();
        await refreshRun();
      } catch (err) { toast('Inject failed: ' + (err.message || err)); }
    });
  });

  card.querySelector('[data-cast-upload]')?.addEventListener('click', () => {
    // The asset has its own upload flow via the assets phase. Delegate by
    // scrolling to that section and opening the upload picker on the matching
    // asset card. If no asset_id is set, fall back to a generic toast.
    const assetId = card.querySelector('[data-cast-upload]').dataset.castUpload;
    if (!assetId) { toast('Use the Assets section to upload a reference'); return; }
    const assetCard = document.querySelector(`[data-asset-id="${assetId}"]`);
    if (assetCard) {
      assetCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
      assetCard.classList.add('ring-2', 'ring-amber-500');
      setTimeout(() => assetCard.classList.remove('ring-2', 'ring-amber-500'), 1500);
    } else {
      toast('Scroll up to the Assets section to upload');
    }
  });

  return card;
}


// ─── Locations & Props summary (pre-defined) ────────────────────────────

function renderLocationsProps(st) {
  const el = document.getElementById('phase-locations-props');
  const locs = st.locations || [];
  const props = st.props || [];
  if (!locs.length && !props.length) { el.innerHTML = ''; return; }

  let html = '';
  if (locs.length) {
    html += `<div class="mb-3">
      <div class="text-[11px] font-semibold text-emerald-400 mb-1.5">📍 Locations (${locs.length})</div>
      <div class="flex flex-wrap gap-2">
        ${locs.map(l => {
          const thumb = (l.ref_paths || []).find(f => /\.(png|jpe?g|webp)$/i.test(f));
          const thumbHtml = thumb
            ? `<img src="${assetUrl(state.currentRunId, thumb)}" class="w-8 h-8 rounded object-cover" alt="">`
            : `<div class="w-8 h-8 rounded bg-zinc-800 flex items-center justify-center text-[10px]">📍</div>`;
          return `<div class="flex items-center gap-2 bg-zinc-900/60 border border-emerald-900/30 rounded px-2 py-1.5">
            ${thumbHtml}
            <div><div class="text-[11px] font-semibold text-zinc-200">${escapeHtml(l.name)}</div>
            <div class="text-[10px] text-zinc-500 truncate max-w-[16rem]">${escapeHtml(l.description || '')}</div></div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }
  if (props.length) {
    html += `<div>
      <div class="text-[11px] font-semibold text-orange-400 mb-1.5">🎯 Props (${props.length})</div>
      <div class="flex flex-wrap gap-2">
        ${props.map(p => {
          const thumb = (p.ref_paths || []).find(f => /\.(png|jpe?g|webp)$/i.test(f));
          const thumbHtml = thumb
            ? `<img src="${assetUrl(state.currentRunId, thumb)}" class="w-8 h-8 rounded object-cover" alt="">`
            : `<div class="w-8 h-8 rounded bg-zinc-800 flex items-center justify-center text-[10px]">🎯</div>`;
          return `<div class="flex items-center gap-2 bg-zinc-900/60 border border-orange-900/30 rounded px-2 py-1.5">
            ${thumbHtml}
            <div><div class="text-[11px] font-semibold text-zinc-200">${escapeHtml(p.name)}</div>
            <div class="text-[10px] text-zinc-500 truncate max-w-[16rem]">${escapeHtml(p.description || '')}</div></div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }
  el.innerHTML = `<div class="bg-zinc-900/30 border border-zinc-800 rounded p-3">${html}</div>`;
}

// ─── Phase 2: keyframes ──────────────────────────────────────────────────

function renderKeyframes(st) {
  const el = document.getElementById('phase-keyframes');
  if (!st.story) { el.innerHTML = ''; return; }

  const kfs = st.keyframes || [];
  const allReady = kfs.length && kfs.every(k => k.status === 'ready');
  const shotsStarted = (st.shots || []).some(s => s.status !== 'pending');

  el.innerHTML = `
    <div class="flex items-baseline justify-between mb-3">
      <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">2. Keyframes <span class="text-zinc-600 font-normal normal-case ml-2">Nano Banana</span></h3>
      <div class="flex gap-2 text-xs flex-wrap">
        <button id="btn-regen-all-kf" class="px-2 py-1 rounded hover:bg-zinc-900 text-zinc-400 hover:text-white">↻ regenerate missing</button>
        ${allReady ? `<button id="btn-face-lock-all" class="px-2 py-1 rounded hover:bg-emerald-900/40 hover:text-emerald-200 text-zinc-400 border border-emerald-900/50">🎭 face-lock all</button>` : ''}
        ${allReady ? `<button id="btn-build-animatic" class="px-2 py-1 rounded hover:bg-fuchsia-900/40 hover:text-fuchsia-200 text-zinc-400 border border-fuchsia-900/50" title="Fast Ken-Burns preview from keyframes + music + VO — no Seedance calls">📽 preview animatic</button>` : ''}
      </div>
    </div>
    ${st.animatic ? `
      <details class="mb-3 bg-fuchsia-950/20 border border-fuchsia-900/50 rounded">
        <summary class="cursor-pointer px-3 py-2 font-semibold text-fuchsia-300 select-none flex items-center gap-2 text-xs">
          📽 Animatic preview — built ${st.animatic_generated_at || '?'}
          <span class="text-zinc-500 font-normal">(rough Ken-Burns motion, actual Seedance shots below)</span>
        </summary>
        <div class="p-3 border-t border-fuchsia-900/50">
          <video src="${assetUrl(state.currentRunId, st.animatic, st.animatic_generated_at)}" controls class="w-full rounded bg-black" preload="metadata"></video>
          <p class="text-[10px] text-zinc-500 mt-2">This is a preview using just keyframes + motion + audio — no video renders. Use it to sanity-check pacing before spending on Seedance.</p>
        </div>
      </details>
    ` : ''}
    <div id="kf-grid" class="grid grid-cols-3 gap-3 mb-4"></div>
    <div class="flex items-center gap-3">
      <button id="btn-approve-kf" class="px-4 py-2 ${allReady ? 'bg-amber-500 hover:bg-amber-400 text-black' : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'} font-semibold rounded text-sm" ${allReady ? '' : 'disabled'}>
        ${shotsStarted ? 'Keyframes approved ✓' : 'Approve → render shots'}
      </button>
      ${allReady ? '' : `<span class="text-xs text-zinc-500">${kfs.filter(k => k.status === 'ready').length}/${kfs.length} ready</span>`}
    </div>
  `;

  const grid = document.getElementById('kf-grid');
  kfs.forEach((kf, i) => grid.appendChild(kfCard(st, kf, i)));

  document.getElementById('btn-regen-all-kf').onclick = async () => {
    try {
      await api.runAllKf(state.currentRunId);
      toast('Regenerating missing keyframes…');
      await refreshRun();
    } catch (err) { toast('Keyframe regen failed: ' + (err.message || err)); }
  };
  document.getElementById('btn-face-lock-all')?.addEventListener('click', async () => {
    const n = (st.keyframes || []).filter(k => k.status === 'ready').length;
    if (!confirm(`Face-lock all ${n} keyframe(s)? Runs sequentially, costs ~$${(n*0.04).toFixed(2)} + ~${n*8}s total.`)) return;
    try {
      await api.faceLockAll(state.currentRunId, 0);
      toast(`🎭 Face-locking ${n} keyframes…`);
      await refreshRun();
    } catch (err) { toast('Batch face-lock failed: ' + (err.message || err)); }
  });
  document.getElementById('btn-build-animatic')?.addEventListener('click', async () => {
    try {
      await api.buildAnimatic(state.currentRunId);
      toast('📽 Building animatic…');
      // Poll once after a bit — animatic builds in ~15-30s for 6 keyframes
      setTimeout(() => refreshRun(), 4000);
    } catch (err) { toast('Animatic failed: ' + (err.message || err)); }
  });
  const approveBtn = document.getElementById('btn-approve-kf');
  if (allReady) {
    approveBtn.onclick = async () => {
      toast('Rendering shots…');
      try {
        await api.runAllShots(state.currentRunId);
        await refreshRun();
      } catch (err) { toast('Failed: ' + err.message); }
    };
  }
}

function kfCard(st, kf, i) {
  const card = document.createElement('div');
  card.className = 'bg-zinc-900/50 border border-zinc-800 rounded overflow-hidden text-xs';
  const shot = st.story.shots[i] || {};
  const imgSrc = kf.path ? assetUrl(state.currentRunId, kf.path, kf.updated_at) : '';

  card.innerHTML = `
    <div class="relative bg-black aspect-video flex items-center justify-center">
      ${kf.status === 'ready' && imgSrc
        ? `<img src="${imgSrc}" class="w-full h-full object-cover" alt="shot ${i+1}">`
        : kf.status === 'generating'
          ? `<div class="text-center text-zinc-400">
              <div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full mb-2"></div>
              <div>generating…</div></div>`
          : kf.status === 'failed'
            ? `<div class="text-red-400 p-3 text-center">✗ ${escapeHtml(kf.error || 'failed')}</div>`
            : `<div class="text-zinc-600">pending</div>`
      }
      <div class="absolute top-2 left-2 text-[10px] px-1.5 py-0.5 rounded bg-black/70 text-amber-400 font-mono">${i + 1}</div>
      ${kf.face_locked ? `<div class="absolute top-2 right-2 text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/80 text-emerald-200 font-mono" title="face-locked against ${escapeAttr(kf.face_lock_ref || 'ref')}">🎭 locked</div>` : ''}
    </div>
    <div class="p-2.5">
      <div class="text-[10px] text-zinc-500 uppercase mb-1">${escapeHtml(shot.beat || '')}</div>
      ${((shot.featured_characters||[]).length || (shot.featured_locations||[]).length || (shot.featured_props||[]).length) ? `<div class="flex flex-wrap gap-0.5 mb-1">${(shot.featured_characters||[]).map(n=>`<span class="px-1 py-0 rounded text-[9px] bg-sky-900/30 text-sky-400">${escapeHtml(n)}</span>`).join('')}${(shot.featured_locations||[]).map(n=>`<span class="px-1 py-0 rounded text-[9px] bg-emerald-900/30 text-emerald-400">${escapeHtml(n)}</span>`).join('')}${(shot.featured_props||[]).map(n=>`<span class="px-1 py-0 rounded text-[9px] bg-violet-900/30 text-violet-400">${escapeHtml(n)}</span>`).join('')}</div>` : ''}
      <div class="text-zinc-400 line-clamp-3">${escapeHtml(shot.keyframe_prompt || '').slice(0, 200)}</div>
      <div class="grid grid-cols-4 gap-1 mt-2">
        <button data-action="regen" class="text-[11px] px-2 py-1 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white border border-zinc-800">↻ regen</button>
        <button data-action="edit" class="text-[11px] px-2 py-1 rounded hover:bg-sky-900/40 hover:text-sky-200 text-zinc-400 border border-zinc-800">✏ edit</button>
        <button data-action="face-lock" class="text-[11px] px-2 py-1 rounded hover:bg-emerald-900/40 hover:text-emerald-200 text-zinc-400 border border-zinc-800" title="Nano Banana multi-ref edit — preserve everything, swap face with reference image 1">🎭 lock</button>
        <button data-action="custom" class="text-[11px] px-2 py-1 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white border border-zinc-800">✎ custom</button>
      </div>
      ${kf.last_edit ? `<div class="text-[10px] text-sky-400 mt-1 truncate" title="${escapeAttr(kf.last_edit)}">↳ edit: ${escapeHtml(kf.last_edit)}</div>` : ''}
      ${(kf.refs_used||[]).length ? `
        <details class="mt-1.5">
          <summary class="text-[10px] text-zinc-500 cursor-pointer hover:text-zinc-300 select-none">${kf.refs_used.length} ref${kf.refs_used.length>1?'s':''} used</summary>
          <div class="mt-1 flex flex-wrap gap-1">${kf.refs_used.map(r => {
            const color = r.label === 'character' ? 'sky' : r.label === 'location' ? 'emerald' : r.label === 'prop' ? 'violet' : r.label === 'continuity' ? 'amber' : 'zinc';
            return `<span class="inline-flex items-center gap-1 px-1 py-0 rounded text-[9px] bg-${color}-900/30 text-${color}-400" title="${escapeAttr(r.path || '')}"><span class="font-medium">${escapeHtml(r.label||'ref')}</span> ${escapeHtml(r.source||'')}</span>`;
          }).join('')}</div>
        </details>` : ''}
    </div>
  `;
  card.querySelector('[data-action=regen]').onclick = () => guard(`kf-${i}`, async () => {
    await api.runKf(state.currentRunId, i, null);
    toast(`Regenerating keyframe ${i+1}…`);
    await refreshRun();
  });
  card.querySelector('[data-action=edit]').onclick = async () => {
    if (kf.status !== 'ready') { toast('Generate the keyframe first'); return; }
    const editPrompt = await openModal({
      title: `Keyframe ${i+1} — edit with Nano Banana`,
      body: 'Describe a SURGICAL change. Gemini will preserve everything else (composition, lighting, identity, style) and apply just this. Examples: "make her hair longer", "change jacket to red leather", "remove the car on the left", "shift time of day to dusk".',
      defaultText: '',
      placeholder: 'make her hair longer and darker',
      confirmLabel: 'Apply edit',
    });
    if (editPrompt === undefined || !editPrompt) return;
    try {
      await api.editKf(state.currentRunId, i, editPrompt);
      toast(`Editing keyframe ${i+1}…`);
      await refreshRun();
    } catch (err) { toast('Edit failed: ' + (err.message || err)); }
  };
  card.querySelector('[data-action=face-lock]').onclick = async () => {
    if (kf.status !== 'ready') { toast('Generate the keyframe first'); return; }
    const refs = st.references || [];
    const characterAssets = (st.assets || []).filter(a => a.type === 'character' && ['uploaded','generated'].includes(a.status));
    if (!refs.length && !characterAssets.length) {
      toast('No face reference found — upload a character ref or let asset discovery generate one');
      return;
    }
    if (!confirm(`Face-lock keyframe ${i+1}: swap the face with your character reference while preserving composition. Costs ~$0.04, ~5-10s.`)) return;
    try {
      await api.faceLockKf(state.currentRunId, i, 0);
      toast(`🎭 Locking keyframe ${i+1}…`);
      await refreshRun();
    } catch (err) { toast('Face-lock failed: ' + (err.message || err)); }
  };
  card.querySelector('[data-action=custom]').onclick = async () => {
    const current = kf.prompt_override || '';
    const override = await openModal({
      title: `Keyframe ${i+1} — custom prompt`,
      body: 'Overrides the generated keyframe prompt. Leave blank to revert to the auto prompt. Character/world sheets are NOT added automatically when you override — include them here if you want them.',
      defaultText: current,
      placeholder: 'Describe this single frame: subject, composition, lens, lighting, mood…',
      confirmLabel: 'Regenerate',
    });
    if (override === undefined) return;
    try {
      await api.runKf(state.currentRunId, i, override || '');
      toast(`Regenerating keyframe ${i+1} with custom prompt…`);
      await refreshRun();
    } catch (err) { toast('Custom keyframe failed: ' + (err.message || err)); }
  };
  return card;
}

// ─── Phase 3: shots ──────────────────────────────────────────────────────

function renderShots(st) {
  const el = document.getElementById('phase-shots');
  const kfs = st.keyframes || [];
  const allKfReady = kfs.length && kfs.every(k => k.status === 'ready');
  if (!allKfReady) { el.innerHTML = ''; return; }

  const shots = st.shots || [];
  const anyShotDone = shots.some(s => s.status === 'ready');
  const allReady = shots.length && shots.every(s => s.status === 'ready');

  if (!anyShotDone && !shots.some(s => s.status === 'generating')) {
    // Keyframes ready but shots not started — show CTA
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">3. Shots <span class="text-zinc-600 font-normal normal-case ml-2">Seedance 2.0</span></h3>
      </div>
      <div class="border border-zinc-800 rounded p-6 text-center text-sm text-zinc-400">
        Approve keyframes above to start rendering shots.
      </div>`;
    return;
  }

  el.innerHTML = `
    <div class="flex items-baseline justify-between mb-3">
      <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">3. Shots <span class="text-zinc-600 font-normal normal-case ml-2">Seedance 2.0</span></h3>
      <div class="flex gap-2 text-xs">
        <button id="btn-regen-all-shots" class="px-2 py-1 rounded hover:bg-zinc-900 text-zinc-400 hover:text-white">↻ render missing</button>
      </div>
    </div>
    <div id="shot-grid" class="grid grid-cols-3 gap-3 mb-4"></div>
    <div class="flex items-center gap-3">
      <label class="flex items-center gap-2 text-xs text-zinc-400">
        <input type="checkbox" id="cb-crossfade" ${st.params?.crossfade ? 'checked' : ''}>
        crossfade between cuts (slower, re-encodes)
      </label>
      <button id="btn-approve-shots" class="ml-auto px-4 py-2 ${allReady ? 'bg-amber-500 hover:bg-amber-400 text-black' : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'} font-semibold rounded text-sm" ${allReady ? '' : 'disabled'}>
        ${st.final ? 'Trailer stitched ✓' : 'Approve → stitch trailer'}
      </button>
      ${allReady ? '' : `<span class="text-xs text-zinc-500">${shots.filter(s => s.status === 'ready').length}/${shots.length} ready</span>`}
    </div>
  `;

  const grid = document.getElementById('shot-grid');
  shots.forEach((shot, i) => grid.appendChild(shotCard(st, shot, i)));

  document.getElementById('btn-regen-all-shots').onclick = async () => {
    try {
      await api.runAllShots(state.currentRunId);
      toast('Rendering remaining shots…');
      await refreshRun();
    } catch (err) { toast('Shot rendering failed: ' + (err.message || err)); }
  };

  if (allReady) {
    const approveBtn = document.getElementById('btn-approve-shots');
    // Button now opens review phase instead of jumping to stitch
    approveBtn.textContent = st.cut_plan ? 'Review generated ✓' : 'Approve → review & cut plan';
    approveBtn.onclick = async () => {
      if (st.cut_plan) {
        document.getElementById('phase-review').scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      toast('Asking Claude to watch the shots…');
      try {
        await api.runCutPlan(state.currentRunId);
        await refreshRun();
        document.getElementById('phase-review').scrollIntoView({ behavior: 'smooth', block: 'start' });
      } catch (err) { toast('Failed: ' + err.message); }
    };
  }
}

function openCompareOverlay(st, shot, shotIdx) {
  const variants = (shot.variants || []).filter(v => v.status === 'ready' && v.path);
  if (variants.length < 2) { toast('Need at least 2 ready takes to compare'); return; }
  const primaryIdx = shot.primary_variant ?? 0;
  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 z-50 bg-black/90 flex flex-col items-center justify-center p-4';
  overlay.innerHTML = `
    <div class="w-full max-w-6xl">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-sm font-semibold text-zinc-300">Shot ${shotIdx + 1} — compare ${variants.length} takes</h3>
        <div class="flex gap-3 items-center">
          <button id="cmp-sync-play" class="text-xs px-3 py-1 rounded border border-zinc-700 hover:border-amber-500 text-zinc-300 hover:text-amber-300">play all</button>
          <button id="cmp-close" class="text-xs px-3 py-1 rounded border border-zinc-700 hover:border-red-500 text-zinc-400 hover:text-red-300">close</button>
        </div>
      </div>
      <div class="grid grid-cols-${Math.min(variants.length, 4)} gap-3">
        ${variants.map((v, vi) => {
          const origIdx = (shot.variants || []).indexOf(v);
          const isPrimary = origIdx === primaryIdx;
          const src = assetUrl(state.currentRunId, v.path, v.updated_at);
          return `
            <div class="flex flex-col gap-2">
              <div class="relative aspect-video bg-black rounded overflow-hidden border ${isPrimary ? 'border-amber-500' : 'border-zinc-800'}">
                <video src="${src}" class="w-full h-full object-contain cmp-video" muted loop playsinline controls preload="metadata"></video>
                ${isPrimary ? '<div class="absolute top-1 right-1 text-[9px] px-1.5 py-0.5 rounded bg-amber-500/80 text-black font-mono">primary</div>' : ''}
              </div>
              <div class="flex items-center justify-between">
                <span class="text-[10px] text-zinc-400 font-mono">take ${origIdx + 1}</span>
                ${!isPrimary ? `<button data-pick-variant="${origIdx}" class="text-[10px] px-2 py-0.5 rounded bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 font-medium">pick this</button>` : '<span class="text-[10px] text-amber-400">current pick</span>'}
              </div>
            </div>`;
        }).join('')}
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#cmp-close').onclick = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('#cmp-sync-play').onclick = () => {
    overlay.querySelectorAll('.cmp-video').forEach(v => { v.currentTime = 0; v.play(); });
  };
  overlay.querySelectorAll('[data-pick-variant]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const vi = parseInt(btn.dataset.pickVariant, 10);
      try {
        await api.setPrimaryVariant(state.currentRunId, shotIdx, vi);
        toast(`Shot ${shotIdx + 1}: take ${vi + 1} picked`);
        overlay.remove();
        await refreshRun();
      } catch (err) { toast('Pick failed: ' + (err.message || err)); }
    });
  });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', esc); }
  });
}

function shotCard(st, shot, i) {
  const card = document.createElement('div');
  card.className = 'bg-zinc-900/50 border border-zinc-800 rounded overflow-hidden text-xs';
  const storyShot = st.story.shots[i] || {};
  const variants = shot.variants || [];
  const primaryIdx = shot.primary_variant ?? 0;
  const primaryVariant = variants[primaryIdx] || shot;
  const videoSrc = primaryVariant.path ? assetUrl(state.currentRunId, primaryVariant.path, primaryVariant.updated_at) : '';
  // Video refs: support up to 3 slots. Backward-compat: legacy single video_ref
  // is migrated into video_refs[0].
  let vrefs = shot.video_refs || [];
  if ((!vrefs || vrefs.length === 0) && shot.video_ref) vrefs = [shot.video_ref];
  const vref = vrefs[0] || null;
  const userRefCount = (st.references || []).length;
  const vrefCount = vrefs.filter(v => v && v.path).length;
  const numVariants = variants.length;
  const showVariants = numVariants > 1;

  // Primary preview uses the currently-picked variant
  const primStatus = primaryVariant.status || shot.status || 'pending';
  card.innerHTML = `
    <div class="relative bg-black aspect-video flex items-center justify-center">
      ${primStatus === 'ready' && videoSrc
        ? `<video src="${videoSrc}" class="w-full h-full object-cover" muted loop playsinline controls preload="metadata"></video>`
        : primStatus === 'generating'
          ? `<div class="text-center text-zinc-400">
              <div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full mb-2"></div>
              <div>Seedance rendering…</div>
              <div class="text-[10px] text-zinc-600 mt-1">typically 60–180s</div></div>`
          : primStatus === 'failed'
            ? `<div class="text-red-400 p-3 text-center text-[11px] flex flex-col items-center gap-2">
                <div>✗ ${escapeHtml(primaryVariant.error || shot.error || 'failed')}</div>
                <button data-action="retry-primary" class="px-2 py-0.5 rounded bg-red-950 hover:bg-red-900 text-red-200 text-[10px] font-mono">↻ retry</button>
              </div>`
            : `<div class="text-zinc-600">pending</div>`
      }
      <div class="absolute top-2 left-2 text-[10px] px-1.5 py-0.5 rounded bg-black/70 text-amber-400 font-mono">${i + 1}</div>
      ${vrefCount > 0 ? `<div class="absolute top-2 right-2 text-[10px] px-1.5 py-0.5 rounded bg-sky-900/80 text-sky-200 font-mono">📹 ${vrefCount} ref${vrefCount > 1 ? 's' : ''}</div>` : ''}
      ${showVariants ? `<div class="absolute bottom-2 right-2 text-[10px] px-1.5 py-0.5 rounded bg-black/70 text-amber-300 font-mono">take ${primaryIdx + 1}/${numVariants}</div>` : ''}
      ${shot.stale || primaryVariant.stale ? `<div class="absolute bottom-2 left-2 text-[10px] px-1.5 py-0.5 rounded bg-orange-900/80 text-orange-200 font-mono" title="${escapeAttr(primaryVariant.stale_reason || 'keyframe changed since this was rendered')}">⚠ stale</div>` : ''}
    </div>
    ${showVariants ? `
      <div class="flex gap-1 px-2 pt-2 bg-zinc-900/60">
        ${variants.map((v, vi) => {
          const isActive = vi === primaryIdx;
          const vSrc = v.path ? assetUrl(state.currentRunId, v.path, v.updated_at) : '';
          const st = v.status || 'pending';
          return `
            <div class="flex-1 relative cursor-pointer group" data-variant-idx="${vi}">
              <div class="aspect-video bg-black rounded border ${isActive ? 'border-amber-500' : 'border-zinc-800 hover:border-zinc-600'} overflow-hidden">
                ${st === 'ready' && vSrc
                  ? `<video src="${vSrc}" class="w-full h-full object-cover" muted loop playsinline preload="metadata"></video>`
                  : st === 'generating'
                    ? `<div class="w-full h-full flex items-center justify-center">
                        <div class="spin inline-block w-3 h-3 border border-zinc-600 border-t-amber-400 rounded-full"></div>
                       </div>`
                    : st === 'failed'
                      ? `<div class="w-full h-full flex flex-col items-center justify-center text-red-400 text-[10px] gap-1" title="${escapeAttr(v.error || 'failed')}">
                          <div>✗</div>
                          <button data-variant-regen="${vi}" class="px-1 py-0.5 rounded bg-red-950 hover:bg-red-900 text-red-200 text-[9px] font-mono">↻</button>
                        </div>`
                      : `<div class="w-full h-full flex items-center justify-center text-zinc-600 text-[10px]">·</div>`}
              </div>
              <div class="text-[9px] text-center mt-0.5 ${isActive ? 'text-amber-300' : 'text-zinc-500'} font-mono">take ${vi + 1}${isActive ? ' ✓' : ''}</div>
              ${st === 'ready' ? `<button data-variant-regen="${vi}" class="absolute top-0.5 right-0.5 text-[9px] px-1 rounded bg-black/70 text-zinc-400 opacity-0 group-hover:opacity-100 hover:text-white">↻</button>` : ''}
            </div>`;
        }).join('')}
      </div>
    ` : ''}
    <div class="p-2.5">
      <div class="text-[10px] text-zinc-500 uppercase mb-1">${escapeHtml(storyShot.beat || '')} · ${storyShot.duration_s || 5}s</div>
      <div class="text-zinc-400 line-clamp-3">${escapeHtml(storyShot.motion_prompt || '').slice(0, 200)}</div>

      <!-- Video ref slots (up to 3, per Ark) -->
      <div class="mt-2 pt-2 border-t border-zinc-800 space-y-1">
        <div class="flex items-baseline justify-between mb-1">
          <span class="text-[10px] text-zinc-500 uppercase">📹 Camera refs <span class="text-zinc-600">(up to 3)</span></span>
          <span class="text-[10px] text-zinc-600 font-mono">${vrefCount}/3</span>
        </div>
        ${[0, 1, 2].map(slot => {
          const v = vrefs[slot];
          if (v && v.path) {
            const src = assetUrl(state.currentRunId, v.path);
            return `
              <div class="flex items-center gap-2 bg-zinc-950/50 border border-zinc-800 rounded p-1.5">
                <span class="text-[10px] text-sky-300 font-mono">@video${slot+1}</span>
                <video src="${src}" class="w-14 h-9 object-cover rounded bg-black" muted loop playsinline></video>
                <div class="flex-1 min-w-0">
                  <div class="text-[10px] text-zinc-400 truncate">${escapeHtml(v.filename || 'ref')}</div>
                  <div class="text-[10px] text-zinc-500">${v.duration}s · ${v.width || '?'}×${v.height || '?'}</div>
                </div>
                <button data-detach-vref-slot="${slot}" class="text-[11px] text-zinc-500 hover:text-red-400" title="remove slot ${slot+1}">✕</button>
              </div>`;
          }
          if (slot <= vrefCount && vrefCount < 3) {
            return `
              <label class="flex items-center gap-2 text-[11px] text-zinc-500 hover:text-sky-300 cursor-pointer">
                <span class="px-2 py-0.5 rounded border border-dashed border-zinc-700 hover:border-sky-700 text-[10px] font-mono">@video${slot+1}</span>
                <span class="text-[10px] text-zinc-600">+ attach video (mp4/mov, &lt;15s)</span>
                <input type="file" accept="video/*" class="hidden" data-attach-vref-slot="${slot}">
              </label>`;
          }
          return '';
        }).join('')}
      </div>

      <!-- Anchor palette: click to insert into currently-focused prompt field -->
      ${(userRefCount > 0 || vrefCount > 0) ? `
        <div class="mt-2 pt-2 border-t border-zinc-800">
          <div class="text-[10px] text-zinc-500 uppercase mb-1">Anchors <span class="text-zinc-600">(click to copy)</span></div>
          <div class="flex flex-wrap gap-1">
            ${Array.from({ length: userRefCount }, (_, k) => k + 1).map(n =>
              `<button data-anchor="@image${n}" class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-950/40 text-emerald-300 hover:bg-emerald-900/50 font-mono border border-emerald-900/50">@image${n}</button>`
            ).join('')}
            ${Array.from({ length: vrefCount }, (_, k) => k + 1).map(n =>
              `<button data-anchor="@video${n}" class="text-[10px] px-1.5 py-0.5 rounded bg-sky-950/40 text-sky-300 hover:bg-sky-900/50 font-mono border border-sky-900/50">@video${n}</button>`
            ).join('')}
          </div>
        </div>
      ` : ''}

      <div class="grid grid-cols-${showVariants ? '4' : '3'} gap-1 mt-2">
        <button data-action="regen" class="text-[11px] px-2 py-1 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white border border-zinc-800">↻ regen</button>
        <button data-action="sweep" class="text-[11px] px-2 py-1 rounded hover:bg-fuchsia-900/40 hover:text-fuchsia-200 text-zinc-400 border border-zinc-800" title="Generate 3 distinct motion prompts + render them all — pick the winner">🧪 sweep</button>
        <button data-action="custom" class="text-[11px] px-2 py-1 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white border border-zinc-800">✎ custom</button>
        ${showVariants ? `<button data-action="compare" class="text-[11px] px-2 py-1 rounded hover:bg-violet-900/40 hover:text-violet-200 text-zinc-400 border border-zinc-800" title="Side-by-side comparison of all takes">⊞ compare</button>` : ''}
      </div>
    </div>
  `;
  // Variant pick + per-variant regen
  if (showVariants) {
    card.querySelectorAll('[data-variant-idx]').forEach(el => {
      el.addEventListener('click', async (e) => {
        if (e.target.closest('[data-variant-regen]')) return; // handled below
        const vi = parseInt(el.dataset.variantIdx, 10);
        if (vi === primaryIdx) return;
        const v = variants[vi];
        if (!v || v.status !== 'ready') {
          if (v && v.status !== 'ready') toast(`Take ${vi+1} not ready yet`);
          return;
        }
        try {
          await api.setPrimaryVariant(state.currentRunId, i, vi);
          toast(`Shot ${i+1}: take ${vi+1} picked`);
          await refreshRun();
        } catch (err) { toast('Variant pick failed: ' + (err.message || err)); }
      });
    });
    card.querySelectorAll('[data-variant-regen]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const vi = parseInt(btn.dataset.variantRegen, 10);
        try {
          await api.regenVariant(state.currentRunId, i, vi);
          toast(`Re-rendering shot ${i+1} take ${vi+1}…`);
          await refreshRun();
        } catch (err) { toast('Variant regen failed: ' + (err.message || err)); }
      });
    });
  }
  card.querySelector('[data-action=regen]').onclick = () => guard(`shot-${i}`, async () => {
    await api.runShot(state.currentRunId, i, null);
    toast(`Re-rendering shot ${i+1}…${vref ? ' (using camera ref)' : ''}`);
    await refreshRun();
  });
  // One-click retry on the primary-failed preview. Re-renders just the primary
  // variant (same path as ↻ on a failed take thumbnail) so a single hiccup on
  // shot 3/6 doesn't force "Regenerate all".
  card.querySelector('[data-action=retry-primary]')?.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      await api.regenVariant(state.currentRunId, i, primaryIdx);
      toast(`↻ retrying shot ${i+1} (take ${primaryIdx + 1})…`);
      await refreshRun();
    } catch (err) { toast('Retry failed: ' + (err.message || err)); }
  });
  card.querySelector('[data-action=sweep]').onclick = async () => {
    const n = 3;
    if (!confirm(`Sweep shot ${i+1}: Claude will write ${n} distinct motion prompts, then render each as a take. This costs ~$${(0.40*n).toFixed(2)} and takes ~${Math.ceil(n * 2)} min. Continue?`)) return;
    try {
      await api.sweepShot(state.currentRunId, i, n);
      toast(`🧪 Sweep of ${n} takes queued for shot ${i+1}`);
      await refreshRun();
    } catch (err) { toast('Sweep failed: ' + (err.message || err)); }
  };
  card.querySelector('[data-action=custom]').onclick = async () => {
    const current = shot.prompt_override || '';
    const override = await openModal({
      title: `Shot ${i+1} — custom motion prompt`,
      body: 'Overrides the motion prompt for Seedance. The keyframe image is still used as the reference. Describe camera + subject motion.',
      defaultText: current,
      placeholder: 'Slow dolly in. The detective turns his head as rain hits the brim of his hat. End on a tight close-up of his eyes.',
      confirmLabel: 'Re-render',
    });
    if (override === undefined) return;
    await api.runShot(state.currentRunId, i, override || '');
    toast(`Re-rendering shot ${i+1}…`);
    await refreshRun();
  };
  // Video ref slot handlers (multi-slot)
  card.querySelectorAll('[data-attach-vref-slot]').forEach(inp => {
    inp.onchange = async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const slot = parseInt(inp.dataset.attachVrefSlot, 10);
      toast(`Uploading @video${slot+1} for shot ${i+1}…`);
      try {
        const { video_ref, detail } = await api.attachVideoRef(state.currentRunId, i, file, slot);
        if (!video_ref) throw new Error(detail || 'attach failed');
        const trimmed = video_ref.trimmed_from ? ` (trimmed from ${video_ref.trimmed_from}s)` : '';
        toast(`@video${slot+1} attached: ${video_ref.duration}s${trimmed}`);
        await refreshRun();
      } catch (err) {
        toast('Attach failed: ' + (err.message || err));
      }
    };
  });
  card.querySelectorAll('[data-detach-vref-slot]').forEach(btn => {
    btn.onclick = async () => {
      const slot = parseInt(btn.dataset.detachVrefSlot, 10);
      if (!confirm(`Remove @video${slot+1} from shot ${i+1}?`)) return;
      try {
        await api.detachVideoRefSlot(state.currentRunId, i, slot);
        await refreshRun();
      } catch (err) { toast('Detach failed: ' + (err.message || err)); }
    };
  });
  // Anchor palette — click to copy the anchor token to clipboard for easy paste into any prompt
  card.querySelectorAll('[data-anchor]').forEach(btn => {
    btn.addEventListener('click', () => {
      const token = btn.dataset.anchor;
      navigator.clipboard.writeText(token).then(() => toast(`Copied ${token} — paste into any prompt`));
    });
  });
  // Side-by-side compare overlay
  card.querySelector('[data-action=compare]')?.addEventListener('click', () => {
    openCompareOverlay(st, shot, i);
  });
  // Legacy handlers (now unused — kept for safety)
  const _legacyDetach = card.querySelector('[data-action=detach-vref]');
  if (_legacyDetach) {
    _legacyDetach.onclick = async () => {
      await api.detachVideoRef(state.currentRunId, i);
      toast(`Removed camera ref from shot ${i+1}`);
      await refreshRun();
    };
  }
  const _legacyAttach = card.querySelector('[data-action=attach-vref]');
  if (_legacyAttach) {
    _legacyAttach.onchange = async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      toast(`Uploading camera ref for shot ${i+1}…`);
      try {
        const { video_ref, detail } = await api.attachVideoRef(state.currentRunId, i, file);
        if (!video_ref) throw new Error(detail || 'attach failed');
        const trimmed = video_ref.trimmed_from ? ` (trimmed from ${video_ref.trimmed_from}s → 15s max)` : '';
        toast(`Camera ref attached: ${video_ref.duration}s${trimmed}`);
        await refreshRun();
      } catch (err) {
        toast('Attach failed: ' + (err.message || err));
      }
    };
  }
  return card;
}

// ─── Phase 3.5: review & cut plan ────────────────────────────────────────

function renderReview(st) {
  const el = document.getElementById('phase-review');
  const shots = st.shots || [];
  const allShotsReady = shots.length && shots.every(s => s.status === 'ready');
  if (!allShotsReady) { el.innerHTML = ''; return; }

  const plan = st.cut_plan;
  const status = st.cut_plan_status || (plan ? 'ready' : null);

  // Not started yet
  if (!plan && status !== 'generating') {
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">3.5 Review & Cut Plan <span class="text-zinc-600 font-normal normal-case ml-2">Claude vision</span></h3>
      </div>
      <div class="border border-zinc-800 rounded p-6 text-sm text-zinc-400 flex items-center gap-4">
        <div class="flex-1">
          <p class="mb-2">Claude watches every shot's contact sheet and decides the actual cut points — so your trailer isn't paced by a stopwatch.</p>
          <p class="text-xs text-zinc-500">Outputs: per-shot cut_in / cut_out, defect flags, quality scores, continuity notes. Takes ~30-60s.</p>
        </div>
        <button id="btn-run-cut-plan" class="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-black font-semibold rounded text-sm whitespace-nowrap">Analyze shots →</button>
        <button id="btn-skip-review" class="px-3 py-2 text-xs text-zinc-500 hover:text-zinc-300 underline">skip, stitch raw</button>
      </div>`;

    document.getElementById('btn-run-cut-plan').onclick = async () => {
      toast('Claude is watching…');
      try {
        await api.runCutPlan(state.currentRunId);
        await refreshRun();
      } catch (err) { toast('Failed: ' + err.message); }
    };
    document.getElementById('btn-skip-review').onclick = async () => {
      if (!confirm('Skip review? Trailer will be stitched from raw shots with no trims.')) return;
      const xf = (st.params?.crossfade) ? true : false;
      await api.stitch(state.currentRunId, xf, false);
      await refreshRun();
    };
    return;
  }

  // Generating
  if (status === 'generating') {
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">3.5 Review & Cut Plan</h3>
      </div>
      <div class="border border-zinc-800 rounded p-6 text-center text-zinc-400 text-sm">
        <div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full mb-3"></div>
        <div>Claude is watching the rushes and writing the cut plan…</div>
        <div class="text-[11px] text-zinc-600 mt-2">typically 30-60s</div>
      </div>`;
    return;
  }

  // Ready — render the plan
  const approved = !!plan.approved;
  const timeline = plan.timeline;
  const hasTimeline = timeline && timeline.entries && timeline.entries.length > 0;

  // Visual track-lane timeline — proportional clips (video) + waveform/beat ticks (music)
  const totalSecs = hasTimeline ? timeline.total_duration : 0;
  const beats = (st.music?.beats || []);
  const visualTimelineHtml = hasTimeline ? `
    <div class="card cr-fade-up" style="background: var(--ink-2); border: 1px solid var(--rule); border-radius: 2px; padding: 14px; margin-bottom: 14px;">
      <div class="flex items-center justify-between" style="margin-bottom: 12px;">
        <div class="flex items-center gap-3">
          <span class="cr-eyebrow">Timeline</span>
          <span class="cr-mono" style="font-size: 11px; color: var(--dim);">${totalSecs.toFixed(1)}s · ${timeline.entries.length} cuts · ⌀ ${(totalSecs/timeline.entries.length).toFixed(1)}s per cut</span>
        </div>
        ${beats.length ? `<span class="cr-chip cr-chip-emerald">${beats.length} beats</span>` : ''}
      </div>
      <div style="display: grid; grid-template-columns: 80px 1fr; gap: 0; font-family: var(--mono); font-size: 10px;">
        <div style="color: var(--dim); border-right: 1px solid var(--rule); padding: 6px 8px; letter-spacing: 0.08em; text-transform: uppercase;">Video</div>
        <div style="display: flex; height: 56px; border-bottom: 1px solid var(--rule-soft); position: relative;">
          ${timeline.entries.map((entry, i) => {
            const len = (entry.cut_out - entry.cut_in) || 0;
            const w = totalSecs > 0 ? (len / totalSecs * 100) : 0;
            const tints = ['noir','dawn','amber','rain','fog','blood','jungle','cool'];
            const tint = tints[(entry.shot_idx ?? i) % tints.length];
            return `<div data-tl-slice="${i}" class="cr-thumb cr-thumb-${tint}" style="flex-shrink: 0; width: ${w}%; border-right: 1px solid var(--ink); display: flex; align-items: flex-end; padding: 4px 6px; font-size: 9px; color: var(--bone); cursor: pointer; text-shadow: 0 1px 2px rgba(0,0,0,0.6);" title="Slice ${i+1} · shot ${(entry.shot_idx ?? 0)+1} · take ${(entry.variant_idx ?? 0)+1} · ${len.toFixed(2)}s">S${(entry.shot_idx ?? 0)+1}·T${(entry.variant_idx ?? 0)+1}</div>`;
          }).join('')}
        </div>
        <div style="color: var(--dim); border-right: 1px solid var(--rule); padding: 6px 8px; letter-spacing: 0.08em; text-transform: uppercase;">Music</div>
        <div style="height: 44px; background: var(--ink-3); position: relative; overflow: hidden; border-bottom: 1px solid var(--rule-soft);">
          ${beats.slice(0, 200).map((b, bi) => {
            const left = totalSecs > 0 ? (b / totalSecs * 100) : 0;
            const strong = bi % 4 === 0;
            return `<div style="position: absolute; top: 0; bottom: 0; left: ${left}%; width: ${strong ? 2 : 1}px; background: rgba(232,184,92,${strong ? 0.9 : 0.4});"></div>`;
          }).join('')}
          <div style="display: flex; align-items: center; gap: 1px; height: 100%; padding: 0 6px;">
            ${Array.from({length: 80}).map((_, wi) => {
              const e = Math.abs(Math.sin(wi * 0.4) + Math.sin(wi * 0.13) * 0.6 + (wi > 30 && wi < 60 ? 0.4 : 0));
              return `<div style="width: 2px; background: var(--teal); opacity: 0.7; border-radius: 1px; height: ${20 + e * 30}%;"></div>`;
            }).join('')}
          </div>
        </div>
      </div>
    </div>
  ` : '';

  el.innerHTML = `
    <div class="mb-1"><span class="cr-eyebrow">Phase 03·5 · Claude vision</span></div>
    <div class="flex items-baseline justify-between mb-3">
      <h2 class="cr-h3 cr-serif" style="font-size: 22px;">The cut plan.</h2>
      <div class="flex gap-3 text-xs">
        <button id="btn-redo-plan" class="cr-mono" style="background: transparent; border: 0; color: var(--dim); cursor: pointer; font-size: 11px;" onmouseover="this.style.color='var(--lamp)'" onmouseout="this.style.color='var(--dim)'">↻ re-analyze</button>
        <button id="btn-discard-plan" class="cr-mono" style="background: transparent; border: 0; color: var(--dim-2); cursor: pointer; font-size: 11px;" onmouseover="this.style.color='var(--oxide)'" onmouseout="this.style.color='var(--dim-2)'">discard</button>
      </div>
    </div>
    <p class="cr-serif" style="font-size: 17px; line-height: 1.5; color: var(--bone-2); max-width: 760px; margin-bottom: 18px;">
      ${hasTimeline ? `${timeline.entries.length} slices, intercut from your takes — a reconstruction of the source trailer's cut rhythm.` : 'Claude watched every shot and decided where to cut.'}
      <span class="cr-serif-italic" style="color: var(--dim);"> ${hasTimeline ? 'Drag a slice\'s edge to retime; click to swap takes.' : 'Approve when the trims feel right.'}</span>
    </p>
    ${visualTimelineHtml}
    <div class="card" style="background: var(--ink-2); border: 1px solid var(--rule); border-radius: 2px; padding: 12px; margin-bottom: 14px;">
      <div class="cr-eyebrow" style="margin-bottom: 6px;">Overall notes</div>
      <div class="whitespace-pre-wrap" style="line-height: 1.6; color: var(--bone-2); font-size: 13px;">${escapeHtml(plan.overall_notes || '')}</div>
    </div>
    ${hasTimeline ? `
      <div class="mb-4 bg-fuchsia-950/20 border border-fuchsia-900/50 rounded p-3 text-xs">
        <div class="flex items-baseline justify-between mb-2 gap-3">
          <div class="font-semibold text-fuchsia-300">🎞 Source-rhythm timeline</div>
          <div class="flex items-center gap-2">
            ${timeline.vision_refined_at ? `<span class="text-[10px] text-emerald-400 font-mono">✓ vision-refined</span>` : ''}
            <button id="btn-refine-timeline" class="text-[11px] px-2 py-0.5 rounded border border-fuchsia-700 hover:bg-fuchsia-900/40 text-fuchsia-300">✨ refine with vision</button>
          </div>
        </div>
        <div class="font-mono text-[10px] text-zinc-500 mb-2">${timeline.entries.length} slices · ${timeline.total_duration.toFixed(2)}s · matches ${timeline.source_cut_count} source cuts</div>
        <div class="text-zinc-400 mb-2">Each row is one slice of the final trailer. Edit variant / in / out and hit approve to commit. Vision refine uses Claude to look at every take and re-pick the strongest composition per slice.</div>
        <div id="timeline-rows" class="space-y-1 max-h-80 overflow-y-auto scroll-muted pr-1 font-mono text-[11px]"></div>
      </div>
    ` : ''}
    <div class="mb-2 text-[11px] text-zinc-500">Per-scene quality review:</div>
    <div id="cut-plan-grid" class="space-y-3 mb-4"></div>
    <div class="flex items-center gap-3 flex-wrap">
      <button id="btn-approve-plan" class="px-4 py-2 ${approved ? 'bg-emerald-600 text-white' : 'bg-amber-500 hover:bg-amber-400 text-black'} font-semibold rounded text-sm">
        ${approved ? 'Cut plan approved ✓' : `Approve ${hasTimeline ? 'timeline + cut' : 'cut plan'}`}
      </button>
      <button id="btn-continuity" class="px-3 py-2 text-xs rounded border border-amber-900/50 bg-amber-950/20 text-amber-300 hover:bg-amber-900/30 font-semibold" title="Claude watches every adjacent shot pair for eyeline / prop / lighting continuity breaks">🔍 continuity check</button>
      ${(plan.shots || []).some(s => s.regenerate_recommended) ? `
        <button id="btn-auto-regen-flagged" class="px-3 py-2 text-xs rounded border border-red-900/50 bg-red-950/30 text-red-300 hover:bg-red-900/40 font-semibold">
          ↻ auto-regen ${(plan.shots || []).filter(s => s.regenerate_recommended).length} flagged shot(s)
        </button>
      ` : ''}
      ${approved ? '<span class="text-xs text-zinc-500">Stitch phase now uses these trims.</span>' : '<span class="text-xs text-zinc-500">Edit trims below, then approve.</span>'}
    </div>
  `;

  if (hasTimeline) {
    const rows = document.getElementById('timeline-rows');
    const SHOT_CLR = ['text-amber-300', 'text-sky-300', 'text-emerald-300', 'text-rose-300', 'text-violet-300', 'text-cyan-300', 'text-fuchsia-300', 'text-lime-300', 'text-orange-300', 'text-blue-300'];
    let cum = 0;
    timeline.entries.forEach((e, ei) => {
      const row = document.createElement('div');
      row.className = 'flex gap-3 items-center hover:bg-zinc-900/50 px-2 py-0.5 rounded';
      const fin = cum;
      const fout = cum + e.duration;
      cum = fout;
      const shot = (st.shots || [])[e.shot_idx] || {};
      const variantOptions = (shot.variants || [])
        .map((v, vi) => `<option value="${vi}" ${vi === e.variant_idx ? 'selected' : ''} ${v.status !== 'ready' ? 'disabled' : ''}>take ${vi + 1}${v.status !== 'ready' ? ' (not ready)' : ''}</option>`)
        .join('');
      row.innerHTML = `
        <span class="text-zinc-600 w-10 text-right">${ei + 1}.</span>
        <span class="text-zinc-500 w-28">${fin.toFixed(2)}–${fout.toFixed(2)}s</span>
        <span class="${SHOT_CLR[e.shot_idx % SHOT_CLR.length]} w-16">shot ${e.shot_idx + 1}</span>
        <select data-slice-idx="${ei}" data-field="variant_idx" class="bg-zinc-950 border border-zinc-800 rounded px-1 py-0.5 text-[11px] text-zinc-300 w-20">${variantOptions}</select>
        <span class="text-zinc-500">@</span>
        <input type="number" step="0.1" min="0" data-slice-idx="${ei}" data-field="slice_in" value="${e.slice_in.toFixed(2)}" class="bg-zinc-950 border border-zinc-800 rounded px-1 py-0.5 text-[11px] text-zinc-300 w-14">
        <span class="text-zinc-500">–</span>
        <input type="number" step="0.1" min="0" data-slice-idx="${ei}" data-field="slice_out" value="${e.slice_out.toFixed(2)}" class="bg-zinc-950 border border-zinc-800 rounded px-1 py-0.5 text-[11px] text-zinc-300 w-14">
        <span class="text-zinc-600 truncate flex-1">${escapeHtml(e.reasoning || '')}</span>
      `;
      rows.appendChild(row);
    });
    // Track edits
    rows.querySelectorAll('[data-slice-idx]').forEach(el => {
      el.addEventListener('change', () => {
        el.classList.add('text-amber-300');
      });
    });
  }

  // Click a clip in the visual timeline → scroll its detailed editor row into view + highlight
  el.querySelectorAll('[data-tl-slice]').forEach(clip => {
    clip.addEventListener('click', () => {
      const idx = parseInt(clip.dataset.tlSlice, 10);
      const rowsEl = document.getElementById('timeline-rows');
      const row = rowsEl?.children[idx];
      if (row) {
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        row.style.transition = 'box-shadow 200ms';
        row.style.boxShadow = '0 0 0 1px var(--lamp)';
        setTimeout(() => { row.style.boxShadow = ''; }, 1400);
      }
      // Highlight the clicked clip in the visual lane
      el.querySelectorAll('[data-tl-slice]').forEach(c => c.style.outline = '');
      clip.style.outline = '2px solid var(--lamp)';
      clip.style.zIndex = '3';
    });
  });

  const grid = document.getElementById('cut-plan-grid');
  (plan.shots || []).forEach((sp, i) => {
    grid.appendChild(cutPlanCard(st, plan, sp, i));
  });

  document.getElementById('btn-redo-plan').onclick = async () => {
    if (!confirm('Re-run Claude vision? The current plan will be overwritten.')) return;
    try {
      await api.runCutPlan(state.currentRunId);
      toast('Re-analyzing…');
      await refreshRun();
    } catch (err) { toast('Re-analysis failed: ' + (err.message || err)); }
  };
  document.getElementById('btn-discard-plan').onclick = async () => {
    if (!confirm('Discard cut plan? Stitch will use raw (untrimmed) shots.')) return;
    try {
      await api.deleteCutPlan(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Discard failed: ' + (err.message || err)); }
  };
  document.getElementById('btn-approve-plan').onclick = async () => {
    const edited = gatherPlanEdits(plan);
    // Harvest timeline edits too
    if (hasTimeline) {
      const newEntries = timeline.entries.map((e, ei) => ({ ...e }));
      document.querySelectorAll('#timeline-rows [data-slice-idx]').forEach(el => {
        const si = parseInt(el.dataset.sliceIdx, 10);
        const field = el.dataset.field;
        if (field === 'variant_idx') newEntries[si].variant_idx = parseInt(el.value, 10);
        else if (field === 'slice_in') newEntries[si].slice_in = parseFloat(el.value) || 0;
        else if (field === 'slice_out') newEntries[si].slice_out = parseFloat(el.value) || 0;
      });
      for (const e of newEntries) {
        e.duration = Math.max(0.15, (e.slice_out || 0) - (e.slice_in || 0));
      }
      edited.timeline = { ...timeline, entries: newEntries, total_duration: newEntries.reduce((s, e) => s + e.duration, 0) };
    }
    edited.approved = true;
    await api.saveCutPlan(state.currentRunId, edited);
    toast('Cut plan + timeline approved');
    await refreshRun();
  };
  document.getElementById('btn-auto-regen-flagged')?.addEventListener('click', async () => {
    toast('Auto-regenerating flagged shots (up to 2 retries each)…');
    try {
      await api.autoRegenFlagged(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Auto-regen failed: ' + err.message); }
  });
  document.getElementById('btn-refine-timeline')?.addEventListener('click', async () => {
    toast('Claude is watching every variant…');
    try {
      await api.refineTimeline(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Refine failed: ' + err.message); }
  });
  document.getElementById('btn-continuity')?.addEventListener('click', async () => {
    toast('Claude checking continuity on adjacent pairs…');
    try {
      await api.runContinuity(state.currentRunId);
      setTimeout(() => refreshRun(), 5000);
    } catch (err) { toast('Continuity check failed: ' + err.message); }
  });
  // Render continuity findings if present
  if (st.continuity) renderContinuity(st.continuity);
}

function renderContinuity(c) {
  // Inject a panel into the review phase
  const host = document.getElementById('phase-review');
  if (!host) return;
  const existing = document.getElementById('continuity-findings');
  if (existing) existing.remove();
  const panel = document.createElement('div');
  panel.id = 'continuity-findings';
  panel.className = 'mt-4 bg-amber-950/20 border border-amber-900/50 rounded p-3 text-xs';
  const sev = c.summary || {};
  const pairs = c.pairs || [];
  const sevClr = { ok: 'text-emerald-400', minor: 'text-amber-300', major: 'text-red-400', unsure: 'text-zinc-500' };
  panel.innerHTML = `
    <div class="flex items-baseline justify-between mb-2">
      <div class="font-semibold text-amber-300">🔍 Continuity check</div>
      <div class="font-mono text-[10px] text-zinc-500">${pairs.length} pairs · ${sev.major || 0} major · ${sev.minor || 0} minor · ${sev.ok || 0} clean</div>
    </div>
    <div class="text-zinc-400 mb-2 text-[11px]">${escapeHtml(c.overall_notes || '')}</div>
    <div class="space-y-1">
      ${pairs.map(p => `
        <div class="flex gap-2 items-start px-2 py-1 rounded ${p.severity === 'major' ? 'bg-red-950/30' : p.severity === 'minor' ? 'bg-amber-950/30' : ''}">
          <span class="${sevClr[p.severity] || 'text-zinc-400'} font-mono text-[10px] w-14 shrink-0">${p.severity}</span>
          <span class="text-zinc-500 font-mono w-16 shrink-0">${p.from_shot + 1}→${p.to_shot + 1}</span>
          <div class="flex-1 text-zinc-300">
            ${p.issue ? escapeHtml(p.issue) : '<span class="text-zinc-500">clean</span>'}
            ${p.suggested_fix ? `<div class="text-[10px] text-sky-300 mt-0.5">→ ${escapeHtml(p.suggested_fix)}</div>` : ''}
          </div>
        </div>
      `).join('')}
    </div>
  `;
  host.appendChild(panel);
}

function cutPlanCard(st, plan, sp, i) {
  const card = document.createElement('div');
  const shotIdx = sp.idx;
  const storyShot = st.story?.shots?.[shotIdx] || {};
  const shotMeta = st.shots?.[shotIdx] || {};
  const sheets = (st.contact_sheets || []).find(s => s.idx === shotIdx);
  const frames = sheets?.frames || [];
  const duration = (plan.shot_durations || [])[shotIdx] || 5;
  const videoSrc = shotMeta.path ? assetUrl(state.currentRunId, shotMeta.path) : '';
  const trimmedDuration = Math.max(0.1, (sp.cut_out || duration) - (sp.cut_in || 0));
  const badLevel = sp.regenerate_recommended || sp.quality_score < 5;
  const badMark = badLevel ? 'ring-1 ring-red-900/50' : '';

  const qualityClr = sp.quality_score >= 8 ? 'text-emerald-400'
                   : sp.quality_score >= 5 ? 'text-amber-300' : 'text-red-400';

  card.className = `bg-zinc-900/40 border border-zinc-800 rounded ${badMark} overflow-hidden`;
  card.innerHTML = `
    <div class="grid grid-cols-[auto_1fr] gap-4 p-3">
      <!-- preview -->
      <div class="w-64">
        ${videoSrc
          ? `<video src="${videoSrc}" class="w-full rounded bg-black aspect-video object-cover" muted loop playsinline controls preload="metadata"></video>`
          : '<div class="w-full rounded bg-black aspect-video"></div>'}
        <div class="flex gap-0.5 mt-1">
          ${frames.map(f => `
            <img src="${assetUrl(state.currentRunId, f.path)}" class="w-full flex-1 rounded-sm ${f.t < sp.cut_in || f.t > sp.cut_out ? 'opacity-30' : ''}" title="t=${f.t.toFixed(2)}s">
          `).join('')}
        </div>
        <div class="text-[10px] text-zinc-500 font-mono mt-1">
          raw ${duration.toFixed(2)}s  →  trim ${trimmedDuration.toFixed(2)}s
        </div>
      </div>

      <!-- plan -->
      <div class="text-xs space-y-2 min-w-0">
        <div class="flex items-baseline gap-3">
          <div class="font-mono text-amber-400">shot ${shotIdx + 1}</div>
          <div class="text-zinc-500">${escapeHtml(storyShot.beat || '')}</div>
          <div class="ml-auto font-mono ${qualityClr}">quality ${sp.quality_score}/10</div>
          ${sp.regenerate_recommended ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-red-900/50 text-red-300 border border-red-800 font-mono">regenerate</span>' : ''}
        </div>

        ${sp.defects ? `<div class="text-red-300 bg-red-950/30 border border-red-900/50 rounded p-2">⚠ ${escapeHtml(sp.defects)}</div>` : ''}

        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-[10px] text-zinc-500 mb-0.5">cut in (s)</label>
            <input type="number" step="0.1" min="0" max="${duration}" data-field="cut_in" value="${(sp.cut_in || 0).toFixed(2)}"
              class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono focus:border-amber-500 focus:outline-none">
          </div>
          <div>
            <label class="block text-[10px] text-zinc-500 mb-0.5">cut out (s)</label>
            <input type="number" step="0.1" min="0" max="${duration}" data-field="cut_out" value="${(sp.cut_out || duration).toFixed(2)}"
              class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs font-mono focus:border-amber-500 focus:outline-none">
          </div>
        </div>

        <div>
          <div class="text-[10px] text-zinc-500 uppercase mb-0.5">reasoning</div>
          <div class="text-zinc-400">${escapeHtml(sp.reasoning || '')}</div>
        </div>
        ${sp.continuity_to_next ? `
          <div>
            <div class="text-[10px] text-zinc-500 uppercase mb-0.5">continuity → next</div>
            <div class="text-zinc-400">${escapeHtml(sp.continuity_to_next)}</div>
          </div>
        ` : ''}
        ${sp.regenerate_recommended ? `
          <div class="pt-1">
            <button data-action="regen-shot" class="text-[11px] px-2 py-1 rounded border border-red-900/50 bg-red-950/30 text-red-300 hover:bg-red-900/40">↻ regenerate shot ${shotIdx + 1}</button>
          </div>
        ` : ''}
      </div>
    </div>
  `;

  card.querySelector('[data-action=regen-shot]')?.addEventListener('click', async () => {
    try {
      await api.runShot(state.currentRunId, shotIdx, null);
      toast(`Re-rendering shot ${shotIdx + 1}…`);
      await refreshRun();
    } catch (err) { toast('Regen failed: ' + (err.message || err)); }
  });

  return card;
}

function gatherPlanEdits(plan) {
  const edited = { ...plan, shots: [] };
  document.querySelectorAll('#cut-plan-grid > div').forEach((card, i) => {
    const orig = plan.shots[i] || {};
    edited.shots.push({
      ...orig,
      cut_in:  parseFloat(card.querySelector('[data-field=cut_in]')?.value)  || 0,
      cut_out: parseFloat(card.querySelector('[data-field=cut_out]')?.value) || orig.cut_out,
    });
  });
  return edited;
}

// ─── Phase 3.6: polish — music + title card ──────────────────────────────

function renderPolish(st) {
  const el = document.getElementById('phase-polish');
  const shots = st.shots || [];
  const allShotsReady = shots.length && shots.every(s => s.status === 'ready');
  if (!allShotsReady) { el.innerHTML = ''; return; }

  const music = st.music;
  const titleCard = st.title_card;
  const cutPlan = st.cut_plan;
  const timeline = cutPlan?.timeline;
  const snapReport = timeline?.snap_report;

  const voMeta = (st.audio || {}).vo || null;

  // Build a Cutting Room waveform: synthesize bars from beats array, accent at energy peaks
  let waveformHtml = '';
  if (music && music.analysis) {
    const beats = music.analysis.beats || [];
    const dur = music.analysis.duration || 1;
    // Energy peaks: estimate from beat density (denser regions are likely louder)
    const peakSeconds = beats.length > 12
      ? [beats[Math.floor(beats.length * 0.25)], beats[Math.floor(beats.length * 0.5)], beats[Math.floor(beats.length * 0.75)], beats[beats.length - 4]].filter(Boolean)
      : [];
    const N_BARS = 80;
    const bars = [];
    for (let i = 0; i < N_BARS; i++) {
      const t = (i / N_BARS) * dur;
      const isPeak = peakSeconds.some(p => Math.abs(p - t) < dur / N_BARS * 1.5);
      // Pseudo-energy: sinusoidal blend boosted near peaks
      const e = Math.abs(Math.sin(i * 0.25) + Math.cos(i * 0.13)) * (isPeak ? 1.6 : 1);
      const h = 15 + e * 25;
      const color = isPeak ? 'var(--lamp)' : 'var(--teal)';
      bars.push(`<div style="width: 2px; background: ${color}; opacity: 0.7; border-radius: 1px; height: ${h}%;"></div>`);
    }
    const fmt = (s) => {
      const m = Math.floor(s / 60);
      const sec = String(Math.floor(s % 60)).padStart(2, '0');
      return `${m}:${sec}`;
    };
    waveformHtml = `
      <div style="height: 56px; background: var(--ink); border: 1px solid var(--rule); padding: 8px 10px; position: relative; margin-bottom: 8px;">
        <div style="display: flex; align-items: center; gap: 1px; height: 100%;">${bars.join('')}</div>
        <span class="cr-mono" style="position: absolute; bottom: 4px; left: 10px; font-size: 9px; color: var(--dim-2);">0:00</span>
        <span class="cr-mono" style="position: absolute; bottom: 4px; right: 10px; font-size: 9px; color: var(--dim-2);">${fmt(dur)}</span>
      </div>`;
  }

  el.innerHTML = `
    <div class="mb-1"><span class="cr-eyebrow">Phase 03·6 · awaiting your move</span></div>
    <div class="flex items-baseline justify-between mb-3">
      <h2 class="cr-h3 cr-serif" style="font-size: 22px;">Polish.</h2>
      <span class="cr-mono" style="font-size: 11px; color: var(--dim-2);">music · VO · title card</span>
    </div>
    <div class="grid grid-cols-3 gap-3">
      <!-- Music card -->
      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="flex items-baseline justify-between mb-2">
          <div class="cr-serif" style="font-size: 17px; color: var(--bone);">Music bed</div>
          ${music ? `<button id="btn-detach-music" class="text-[11px] text-zinc-500 hover:text-red-400">✕ remove</button>` : ''}
        </div>
        ${music ? `
          <div class="text-zinc-400 mb-1 truncate">${escapeHtml(music.filename || 'track.mp3')}</div>
          <div class="text-[10px] text-zinc-500 font-mono mb-2">${music.analysis.bpm} BPM · ${music.analysis.beats.length} beats · ${music.analysis.duration.toFixed(1)}s · ${music.analysis.dynamic_range} LU range</div>
          ${waveformHtml}
          <audio src="${assetUrl(state.currentRunId, music.path)}" controls class="w-full h-8 mb-2"></audio>
          ${timeline ? `
            <div class="bg-zinc-950 border border-zinc-800 rounded p-2 text-[11px] font-mono">
              ${snapReport
                ? `snapped ${snapReport.snapped}/${snapReport.total} · sync <span class="text-amber-300">${Math.round(snapReport.sync_score * 100)}%</span>`
                : '<span class="text-zinc-500">not snapped yet</span>'}
            </div>
            <div id="music-score-row" class="bg-zinc-950 border border-zinc-800 rounded p-2 text-[11px] font-mono mt-1 text-zinc-500">loading score…</div>
            <button id="btn-snap-music" class="w-full mt-2 px-3 py-1.5 text-[11px] rounded bg-fuchsia-600 hover:bg-fuchsia-500 text-white font-semibold">
              ${snapReport ? '↻ re-snap timeline to beats' : '⚡ snap timeline to beats'}
            </button>
          ` : '<div class="text-[11px] text-zinc-500">Timeline not ready — run cut plan first.</div>'}
        ` : `
          <p class="text-zinc-500 mb-2">Upload a music track <strong>or</strong> let Claude + ElevenLabs compose a bespoke score from the storyboard.</p>
          <div class="grid grid-cols-2 gap-2 mb-2">
            <label class="block cursor-pointer">
              <span class="inline-block w-full text-center px-3 py-1.5 rounded border border-dashed border-zinc-700 hover:border-sky-700 hover:text-sky-300 text-zinc-400 text-[11px]">📁 upload</span>
              <input id="music-file" type="file" accept="audio/*" class="hidden">
            </label>
            <button id="btn-compose-music" class="w-full px-3 py-1.5 rounded bg-fuchsia-700 hover:bg-fuchsia-600 text-white font-semibold text-[11px]">✨ compose</button>
          </div>
          <p class="text-[10px] text-zinc-600">Compose: Claude writes a brief from your storyboard + genre, ElevenLabs Music composes. ~$0.15 for 45s.</p>
        `}
      </div>
      <!-- Title card -->
      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="flex items-baseline justify-between mb-2">
          <div class="cr-serif" style="font-size: 17px; color: var(--bone);">Title card</div>
          ${titleCard ? `<button id="btn-remove-title" class="text-[11px] text-zinc-500 hover:text-red-400">✕ remove</button>` : ''}
        </div>
        ${titleCard ? `
          <img src="${assetUrl(state.currentRunId, titleCard.path, titleCard.updated_at)}" class="w-full rounded bg-black mb-2 border border-zinc-800">
          <div class="text-[10px] text-zinc-500 font-mono">"${escapeHtml(titleCard.text)}" · held ${titleCard.hold_seconds}s${titleCard.animated_path ? ' · Seedance animated ✓' : ''}</div>
          <button id="btn-regen-title" class="w-full mt-2 px-3 py-1.5 text-[11px] rounded border border-zinc-800 hover:bg-zinc-900 text-zinc-400">↻ regenerate</button>
        ` : `
          <p class="text-zinc-500 mb-2">Nano Banana renders a cinematic title card using your run's title + style. Optionally Seedance micro-animates it (subtle push + dust particles).</p>
          <input id="title-text" type="text" placeholder="${escapeAttr(st.story?.title || st.params?.title || 'TRAILER')}"
            class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-xs mb-2 focus:border-amber-500 focus:outline-none">
          <label class="flex items-center gap-2 text-[11px] text-zinc-400 mb-2">
            <input type="checkbox" id="title-animate">
            animate with Seedance <span class="text-zinc-600">(+60-180s, +$0.40)</span>
          </label>
          <button id="btn-gen-title" class="w-full px-3 py-1.5 text-[11px] rounded bg-amber-500 hover:bg-amber-400 text-black font-semibold">
            ✨ generate title card
          </button>
        `}
      </div>
      <!-- VO card -->
      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="flex items-baseline justify-between mb-2">
          <div class="cr-serif" style="font-size: 17px; color: var(--bone);">Narrator VO</div>
          ${voMeta ? `<button id="btn-remove-vo" class="text-[11px] text-zinc-500 hover:text-red-400">✕ remove</button>` : ''}
        </div>
        <div id="vo-body"><div class="text-zinc-500">loading…</div></div>
      </div>
    </div>
  `;

  // Render VO body asynchronously (need to fetch available voices)
  renderVoCard(st, voMeta);

  // Music handlers
  const musicFile = document.getElementById('music-file');
  if (musicFile) {
    musicFile.addEventListener('change', async (e) => {
      const f = e.target.files?.[0]; if (!f) return;
      toast(`Analyzing ${f.name} with librosa…`);
      try {
        await api.attachMusic(state.currentRunId, f);
        await refreshRun();
      } catch (err) { toast('Analyze failed: ' + err.message); }
    });
  }
  document.getElementById('btn-compose-music')?.addEventListener('click', async () => {
    const vibe = prompt('Optional vibe guidance (e.g. "dread", "triumphant with brass"):') || '';
    if (!confirm('Compose bespoke music? Costs ~$0.15 for 45s + a Claude brief (~$0.01). Takes ~60-90s.')) return;
    toast('Claude briefing + ElevenLabs composing music…');
    try {
      await api.composeMusic(state.currentRunId, vibe);
      setTimeout(() => refreshRun(), 5000);
    } catch (err) { toast('Compose failed: ' + (err.message || err)); }
  });
  document.getElementById('btn-detach-music')?.addEventListener('click', async () => {
    if (!confirm('Remove music track and all snap data?')) return;
    try {
      await api.detachMusic(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Detach music failed: ' + (err.message || err)); }
  });
  document.getElementById('btn-snap-music')?.addEventListener('click', async () => {
    toast('Snapping timeline to beats…');
    try {
      await api.snapToMusic(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Snap failed: ' + err.message); }
  });

  // Fetch and display music-vs-timeline score
  const scoreRow = document.getElementById('music-score-row');
  if (scoreRow && state.currentRunId) {
    api.getMusicScore(state.currentRunId).then(({ score }) => {
      if (!score) { scoreRow.textContent = 'no score (need music + timeline)'; return; }
      const syncPct = Math.round(score.beat_sync * 100);
      const arcPct = Math.round(score.arc_fit * 100);
      const syncClr = syncPct >= 60 ? 'text-emerald-400' : syncPct >= 30 ? 'text-amber-300' : 'text-red-400';
      const arcClr = arcPct >= 50 ? 'text-emerald-400' : arcPct >= 25 ? 'text-amber-300' : 'text-zinc-500';
      scoreRow.innerHTML = `beat sync <span class="${syncClr}">${syncPct}%</span> · arc fit <span class="${arcClr}">${arcPct}%</span>`;
    }).catch(err => { console.warn('music score fetch failed:', err); scoreRow.textContent = '—'; });
  }

  // Title handlers
  document.getElementById('btn-gen-title')?.addEventListener('click', async () => {
    const text = document.getElementById('title-text').value.trim();
    const animate = document.getElementById('title-animate').checked;
    toast('Generating title card…');
    try {
      await api.generateTitleCard(state.currentRunId, text || null, '', 2.5, animate);
      await refreshRun();
    } catch (err) { toast('Title failed: ' + err.message); }
  });
  document.getElementById('btn-regen-title')?.addEventListener('click', async () => {
    toast('Regenerating title card…');
    try {
      await api.generateTitleCard(state.currentRunId, titleCard.text, titleCard.style_hint, titleCard.hold_seconds, !!titleCard.animated_path);
      await refreshRun();
    } catch (err) { toast('Regen title failed: ' + (err.message || err)); }
  });
  document.getElementById('btn-remove-title')?.addEventListener('click', async () => {
    if (!confirm('Remove title card?')) return;
    try {
      await api.removeTitleCard(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Remove title failed: ' + (err.message || err)); }
  });
  document.getElementById('btn-remove-vo')?.addEventListener('click', async () => {
    if (!confirm('Remove VO? Script and synthesized audio will be deleted.')) return;
    try {
      await api.removeVo(state.currentRunId);
      await refreshRun();
    } catch (err) { toast('Remove VO failed: ' + (err.message || err)); }
  });
}

async function renderVoCard(st, voMeta) {
  const body = document.getElementById('vo-body');
  if (!body) return;
  let audioStatus;
  try { audioStatus = await api.audioStatus(); } catch { audioStatus = { elevenlabs_configured: false, voices: [] }; }

  if (!audioStatus.elevenlabs_configured) {
    body.innerHTML = `
      <p class="text-zinc-500 mb-2">VO needs an ElevenLabs API key.</p>
      <p class="text-[10px] text-zinc-600">Add <code class="text-amber-400">ELEVENLABS_API_KEY=...</code> to <code class="text-zinc-400">.env</code> and restart. Free tier ~10k chars/month.</p>
      <p class="text-[10px] text-zinc-600 mt-2">Get a key: <a class="text-sky-400 hover:underline" href="https://elevenlabs.io/app/settings/api-keys" target="_blank">elevenlabs.io</a></p>
    `;
    return;
  }

  const voices = audioStatus.voices || [];
  const currentVoice = voMeta?.voice_id || audioStatus.default_voice_id;
  const script = voMeta?.script || { lines: [] };
  const lines = script.lines || [];
  const ready = voMeta?.status === 'ready';
  const synthesizing = voMeta?.status === 'synthesizing';
  const failed = voMeta?.status === 'failed';

  if (!lines.length) {
    body.innerHTML = `
      <p class="text-zinc-500 mb-2">Claude reads your storyboard and writes 1-4 lines of narrator VO ("In a world where…"). Then ElevenLabs synthesizes with your picked voice.</p>
      <div class="flex items-center gap-2">
        <input id="vo-vibe" type="text" placeholder="tone vibe (optional) e.g. 'dread', 'triumphant'"
          class="flex-1 bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-[11px]">
        <button id="btn-gen-vo" class="px-3 py-1 rounded bg-amber-500 hover:bg-amber-400 text-black text-[11px] font-semibold whitespace-nowrap">✨ write VO</button>
      </div>
    `;
    document.getElementById('btn-gen-vo').onclick = async () => {
      const vibe = document.getElementById('vo-vibe').value.trim();
      toast('Claude is writing VO…');
      try {
        await api.generateVoScript(state.currentRunId, vibe);
        await refreshRun();
      } catch (err) { toast('VO script failed: ' + (err.message || err)); }
    };
    return;
  }

  body.innerHTML = `
    <div class="mb-2">
      <label class="block text-[10px] text-zinc-500 mb-0.5">Voice</label>
      <select id="vo-voice" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-[11px]">
        ${voices.map(v => `<option value="${escapeAttr(v.id)}" ${v.id === currentVoice ? 'selected' : ''} title="${escapeAttr(v.style)}">${escapeHtml(v.name)} — ${escapeHtml(v.style.slice(0, 40))}</option>`).join('')}
      </select>
    </div>
    <div id="vo-lines" class="space-y-2 mb-2"></div>
    <div class="text-[10px] text-zinc-500 mb-2">${script.total_chars || 0} chars · ~$${((script.total_chars || 0) / 1000 * 0.30).toFixed(3)} at ElevenLabs rates</div>
    <div class="grid grid-cols-2 gap-2">
      <button id="btn-save-vo" class="px-2 py-1 rounded border border-zinc-800 hover:bg-zinc-900 text-zinc-400 hover:text-white text-[11px]">💾 save edits</button>
      <button id="btn-synth-vo" class="px-2 py-1 rounded ${ready ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-amber-500 hover:bg-amber-400 text-black'} text-[11px] font-semibold">
        ${synthesizing ? '⏳ synthesizing…' : ready ? '↻ re-synthesize' : '🎙 synthesize VO'}
      </button>
    </div>
    ${failed ? `<div class="mt-2 text-[11px] text-red-400">✗ ${escapeHtml(voMeta?.error || 'failed')}</div>` : ''}
    <button id="btn-regen-script" class="w-full mt-2 text-[10px] text-zinc-500 hover:text-amber-300">↻ regenerate script with Claude</button>
  `;

  const linesEl = document.getElementById('vo-lines');
  lines.forEach((line, i) => {
    const row = document.createElement('div');
    row.className = 'space-y-1';
    const audioPath = (voMeta?.lines_audio || [])[i];
    const audioSrc = audioPath ? assetUrl(state.currentRunId, audioPath, voMeta?.generated_at) : '';
    row.innerHTML = `
      <div class="flex gap-1 items-center text-[10px] text-zinc-500">
        <span class="font-mono">${i+1}.</span>
        <span class="bg-zinc-800 text-zinc-400 px-1 rounded">${line.beat_role || 'beat'}</span>
        <span>@</span>
        <input type="number" step="0.1" min="0" value="${(line.suggested_start_s ?? 0).toFixed(1)}" data-line-idx="${i}" data-field="suggested_start_s" class="w-14 bg-zinc-950 border border-zinc-800 rounded px-1 text-[10px] font-mono">
        <span>s</span>
        <span class="ml-auto">${(line.text || '').length} chars</span>
      </div>
      <textarea rows="2" data-line-idx="${i}" data-field="text" class="w-full bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-[11px] font-mono">${escapeHtml(line.text || '')}</textarea>
      ${audioSrc ? `<audio src="${audioSrc}" controls class="w-full h-6"></audio>` : ''}
    `;
    linesEl.appendChild(row);
  });

  document.getElementById('btn-save-vo').onclick = async () => {
    const voiceId = document.getElementById('vo-voice').value;
    const newLines = lines.map((_, i) => ({
      text: linesEl.querySelector(`[data-line-idx="${i}"][data-field="text"]`).value,
      suggested_start_s: parseFloat(linesEl.querySelector(`[data-line-idx="${i}"][data-field="suggested_start_s"]`).value) || 0,
    }));
    try {
      await api.saveVoScript(state.currentRunId, newLines, voiceId);
      toast('VO script saved');
      await refreshRun();
    } catch (err) { toast('Save failed: ' + (err.message || err)); }
  };
  document.getElementById('btn-synth-vo').onclick = async () => {
    // Save edits first
    const voiceId = document.getElementById('vo-voice').value;
    const newLines = lines.map((_, i) => ({
      text: linesEl.querySelector(`[data-line-idx="${i}"][data-field="text"]`).value,
      suggested_start_s: parseFloat(linesEl.querySelector(`[data-line-idx="${i}"][data-field="suggested_start_s"]`).value) || 0,
    }));
    try {
      await api.saveVoScript(state.currentRunId, newLines, voiceId);
      await api.synthesizeVo(state.currentRunId);
      toast('Synthesizing VO with ElevenLabs…');
      await refreshRun();
    } catch (err) { toast('Synthesize failed: ' + (err.message || err)); }
  };
  document.getElementById('btn-regen-script').onclick = async () => {
    if (!confirm('Regenerate the VO script? Existing audio will be discarded.')) return;
    try {
      await api.generateVoScript(state.currentRunId, null);
      await refreshRun();
    } catch (err) { toast('Regen failed: ' + (err.message || err)); }
  };
}

// ─── Phase 4: stitch / final ─────────────────────────────────────────────

function renderStitch(st) {
  const el = document.getElementById('phase-stitch');
  const shots = st.shots || [];
  const allShotsReady = shots.length && shots.every(s => s.status === 'ready');
  const planApproved = !!(st.cut_plan && st.cut_plan.approved);

  // Stitching in progress
  if (st.status === 'stitching') {
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">4. Trailer</h3>
      </div>
      <div class="border border-zinc-800 rounded p-6 text-center text-zinc-400 text-sm">
        <div class="spin inline-block w-5 h-5 border-2 border-zinc-600 border-t-amber-400 rounded-full mb-3"></div>
        <div>ffmpeg stitching${planApproved ? ' with cut plan trims' : ''}…</div>
      </div>`;
    return;
  }

  // Final exists — show player + platform exports
  if (st.final) {
    const src = assetUrl(state.currentRunId, st.final);
    const exports = st.exports || {};
    const exportList = Object.entries(exports);
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">4. Trailer ${st.params?.applied_cut_plan ? '<span class="text-[10px] text-emerald-400 ml-1">(cut plan applied)</span>' : ''}${st.params?.look && st.params.look !== 'none' ? `<span class="text-[10px] text-amber-400 ml-1">(${st.params.look} grade)</span>` : ''}</h3>
        <div class="flex gap-2 text-xs">
          <button id="btn-restitch" class="px-3 py-1.5 rounded border border-zinc-800 hover:bg-zinc-900 text-zinc-400">↻ re-stitch</button>
          <a href="${src}" download class="px-3 py-1.5 rounded bg-amber-500 hover:bg-amber-400 text-black font-semibold">Download .mp4</a>
        </div>
      </div>
      <div class="bg-black rounded overflow-hidden border border-zinc-800 mb-3">
        <video src="${src}" controls class="w-full" preload="metadata"></video>
      </div>
      <div class="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-xs">
        <div class="flex items-baseline justify-between mb-2">
          <div class="font-semibold text-zinc-200">📦 Platform exports <span class="text-zinc-500 font-normal ml-2">one master → multiple deliveries</span></div>
          <div class="flex items-center gap-3 text-[11px]">
            ${(st.audio?.vo?.status === 'ready') ? `
              <label class="flex items-center gap-1 text-zinc-400 cursor-pointer">
                <input type="checkbox" id="cb-burn-cc" class="accent-amber-500">
                burn-in captions (from VO)
              </label>
              <button id="btn-download-srt" class="text-sky-400 hover:text-sky-300" title="download raw SRT">⬇ SRT</button>
            ` : '<span class="text-[10px] text-zinc-600">VO not ready — no captions available</span>'}
          </div>
        </div>
        <div id="platform-variant-row" class="flex items-center gap-2 flex-wrap">loading…</div>
        ${exportList.length ? `
          <div class="mt-3 pt-3 border-t border-zinc-800 grid grid-cols-${Math.min(4, exportList.length)} gap-2">
            ${exportList.map(([preset, rel]) => `
              <div class="bg-zinc-950 rounded border border-zinc-800 p-2">
                <video src="${assetUrl(state.currentRunId, rel)}" class="w-full rounded bg-black mb-1" muted playsinline preload="metadata"></video>
                <div class="flex items-center gap-1">
                  <span class="text-[10px] text-zinc-500 font-mono">${preset}</span>
                  <a href="${assetUrl(state.currentRunId, rel)}" download class="ml-auto text-[10px] text-amber-400 hover:text-amber-300">⬇</a>
                </div>
              </div>
            `).join('')}
          </div>
        ` : ''}
      </div>`;
    document.getElementById('btn-restitch').onclick = async () => {
      const xf = !!st.params?.crossfade;
      toast('Re-stitching…');
      try {
        await api.stitch(state.currentRunId, xf, planApproved);
        await refreshRun();
      } catch (err) { toast('Re-stitch failed: ' + (err.message || err)); }
    };
    // Populate platform variant buttons
    api.listPlatformVariants().then(({ variants }) => {
      const row = document.getElementById('platform-variant-row');
      if (!row) return;
      row.innerHTML = '';
      for (const v of variants) {
        const btn = document.createElement('button');
        const hasIt = !!exports[v.preset];
        btn.className = `px-2.5 py-1 rounded text-[11px] font-mono ${hasIt ? 'bg-emerald-950/40 text-emerald-300 border border-emerald-900/50' : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-white border border-zinc-800'}`;
        btn.textContent = `${hasIt ? '✓ ' : '+ '}${v.preset}  (${v.w}×${v.h})`;
        btn.title = v.label;
        btn.onclick = async () => {
          const burn = document.getElementById('cb-burn-cc')?.checked || false;
          toast(`Exporting ${v.preset}${burn ? ' + burn-in CC' : ''}…`);
          try {
            await api.exportPlatformVariants(state.currentRunId, [v.preset], burn);
            setTimeout(() => refreshRun(), 4000);
          } catch (err) { toast('Export failed: ' + (err.message || err)); }
        };
        row.appendChild(btn);
      }
      const missing = variants.filter(v => !exports[v.preset]);
      if (missing.length) {
        const allBtn = document.createElement('button');
        allBtn.className = 'px-2.5 py-1 rounded text-[11px] bg-amber-500 hover:bg-amber-400 text-black font-semibold';
        allBtn.textContent = `+ all missing (${missing.length})`;
        allBtn.onclick = async () => {
          const burn = document.getElementById('cb-burn-cc')?.checked || false;
          toast(`Exporting ${missing.length} variants${burn ? ' + burn-in CC' : ''}…`);
          try {
            await api.exportPlatformVariants(state.currentRunId, missing.map(v => v.preset), burn);
            setTimeout(() => refreshRun(), 6000);
          } catch (err) { toast('Export failed: ' + (err.message || err)); }
        };
        row.appendChild(allBtn);
      }
    }).catch(err => { console.warn('platform variants fetch failed:', err); });
    // SRT download button
    document.getElementById('btn-download-srt')?.addEventListener('click', async () => {
      try {
        const { path } = await api.buildSubtitles(state.currentRunId, 'srt');
        window.open(assetUrl(state.currentRunId, path), '_blank');
      } catch (err) { toast('SRT build failed: ' + (err.message || err)); }
    });
    return;
  }

  // Ready to stitch (plan approved)
  if (allShotsReady && planApproved) {
    const currentLook = st.params?.look || 'none';
    el.innerHTML = `
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold uppercase tracking-wider text-zinc-400">4. Trailer</h3>
      </div>
      <div class="border border-zinc-800 rounded p-4 text-sm space-y-3">
        <div class="flex items-center gap-3 flex-wrap">
          <span class="text-[10px] text-zinc-500 uppercase">🎨 Look</span>
          <select id="look-select" class="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs max-w-xs">
            <option value="${currentLook}">Loading looks…</option>
          </select>
          <span id="look-desc" class="text-[11px] text-zinc-500 flex-1"></span>
        </div>
        <div class="flex items-center gap-4 pt-1 border-t border-zinc-800">
          <label class="flex items-center gap-2 text-xs text-zinc-400">
            <input type="checkbox" id="cb-crossfade-final" ${st.params?.crossfade ? 'checked' : ''}>
            crossfade
          </label>
          <div class="flex-1 text-xs text-zinc-500">Cut plan approved. Final trailer will include${st.music ? ' music' : ''}${(st.audio?.vo?.status === 'ready') ? ' + VO' : ''}${st.title_card ? ' + title card' : ''}${currentLook !== 'none' ? ` + ${currentLook} grade` : ''}.</div>
          <button id="btn-stitch-final" class="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-black font-semibold rounded text-sm">Stitch trailer →</button>
        </div>
      </div>`;

    // Populate look dropdown
    api.listLooks().then(({ looks }) => {
      if (!looks) return;
      const sel = document.getElementById('look-select');
      const descEl = document.getElementById('look-desc');
      if (!sel || !descEl) return;
      sel.innerHTML = '';
      for (const l of looks) {
        const opt = document.createElement('option');
        opt.value = l.id;
        opt.textContent = l.label;
        opt.title = l.description;
        if (l.id === currentLook) opt.selected = true;
        sel.appendChild(opt);
      }
      const updateDesc = () => {
        const l = looks.find(x => x.id === sel.value);
        descEl.textContent = l?.description || '';
      };
      updateDesc();
      sel.onchange = async () => {
        updateDesc();
        try {
          await api.setLook(state.currentRunId, sel.value);
          toast(`Look → ${sel.options[sel.selectedIndex].text}`);
        } catch (err) { toast('Look update failed: ' + (err.message || err)); }
      };
    }).catch(err => { console.warn('looks fetch failed:', err); });

    document.getElementById('btn-stitch-final').onclick = async () => {
      const xf = document.getElementById('cb-crossfade-final').checked;
      toast('Stitching…');
      try {
        await api.stitch(state.currentRunId, xf, true);
        await refreshRun();
      } catch (err) { toast('Stitch failed: ' + (err.message || err)); }
    };
    return;
  }

  el.innerHTML = '';
}

// ─── Delete run ──────────────────────────────────────────────────────────

document.getElementById('btn-delete-run').addEventListener('click', async () => {
  if (!state.currentRunId) return;
  if (!confirm(`Delete run ${state.currentRunId}? This removes all keyframes, shots, and the final trailer.`)) return;
  try {
    await api.deleteRun(state.currentRunId);
    toast('Run deleted');
    showView('runs');
  } catch (err) { toast('Delete failed: ' + (err.message || err)); }
});

// ─── Utils ───────────────────────────────────────────────────────────────

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}
function escapeAttr(s) { return escapeHtml(s); }

// ─── Boot ────────────────────────────────────────────────────────────────

showView('runs');
