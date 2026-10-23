// Η Ατζέντα των Ειδήσεων — λογική της σελίδας.
// Όλα τα δεδομένα (τίτλοι, κατηγορίες κ.λπ.) μπαίνουν στη σελίδα ως κείμενο (textContent), ποτέ ως HTML.
'use strict';

const MONTHS_GR = ['Ιανουάριος', 'Φεβρουάριος', 'Μάρτιος', 'Απρίλιος', 'Μάιος', 'Ιούνιος',
                   'Ιούλιος', 'Αύγουστος', 'Σεπτέμβριος', 'Οκτώβριος', 'Νοέμβριος', 'Δεκέμβριος'];
const PAGE_SIZE = 30;
const fmt = new Intl.NumberFormat('el-GR');
const $ = (id) => document.getElementById(id);

let meta = null;          // από /api/meta
let lastResult = null;    // τελευταία απάντηση του /api/search
let searchCtrl = null;    // AbortController της τρέχουσας αναζήτησης
let debounceTimer = null;
let summaryCtrl = null;   // AbortController για τις συνόψεις της τρέχουσας αναζήτησης
let summaryObserver = null;
let llmEnabled = false;   // ο επισκέπτης άνοιξε τον διακόπτη (και ο server το επιβεβαίωσε)
let captchaModal = null;
let captchaWidget = null;
let turnstileLoading = null;
let keyModal = null;
let openKeyAfterCaptcha = false;  // μετά το «δεν είμαι ρομπότ» ανοίγει το παράθυρο του κλειδιού
let byokState = { active: false };  // από /api/byok/* — ποτέ το ίδιο το κλειδί
let byokExpiresAt = 0;
let byokTimer = null;
let byokRenewDismissed = false;

// ---------------------------------------------------------------- βοηθητικά

function monthLabel(ym) {
  const [y, m] = ym.split('-').map(Number);
  return `${MONTHS_GR[m - 1]} ${y}`;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function showAlert(message, kind = 'warning') {
  const box = $('alert');
  box.className = `alert alert-${kind}`;
  box.textContent = message;
}
function hideAlert() { $('alert').className = 'alert d-none'; }

async function getJSON(path, params, signal) {
  const url = `${path}?${params.toString()}`;
  const res = await fetch(url, { signal, headers: { Accept: 'application/json' } });
  if (!res.ok) {
    let detail = `Σφάλμα ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body.detail === 'string') detail = body.detail;
      else if (Array.isArray(body.detail)) detail = 'Μη έγκυρη τιμή σε κάποιο φίλτρο.';
    } catch { /* όχι JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function postJSON(path, payload = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(payload),
  });
  let body = {};
  try { body = await res.json(); } catch { /* όχι JSON */ }
  if (!res.ok) {
    const err = new Error(typeof body.detail === 'string' ? body.detail : `Σφάλμα ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---------------------------------------------------------------- κατάσταση φίλτρων ↔ URL

function readFilters() {
  return {
    q: $('term').value.trim(),
    all_forms: $('all-forms').checked,
    field: document.querySelector('input[name=field]:checked').value,
    metric: document.querySelector('input[name=metric]:checked').value,
    cat: [...document.querySelectorAll('#categories input:checked')].map((i) => i.value),
    year_from: Number($('year-from').value),
    year_to: Number($('year-to').value),
    method: $('method').value,
    n: Number($('n-peaks').value),
  };
}

function filtersToParams(f, keys) {
  const p = new URLSearchParams();
  for (const k of keys) {
    const v = f[k];
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x));
    else p.set(k, String(v));
  }
  return p;
}

const SEARCH_KEYS = ['q', 'all_forms', 'field', 'cat', 'year_from', 'year_to', 'metric', 'method', 'n'];
const ARTICLE_KEYS = ['q', 'all_forms', 'field', 'cat'];

function writeUrl(f) {
  // Μόνο ό,τι διαφέρει από τις προεπιλογές, ώστε οι σύνδεσμοι να είναι σύντομοι.
  const p = new URLSearchParams();
  p.set('q', f.q);
  if (!f.all_forms) p.set('all_forms', 'false');
  if (f.field !== 'text') p.set('field', f.field);
  if (f.metric !== 'share') p.set('metric', f.metric);
  f.cat.forEach((c) => p.append('cat', c));
  if (f.year_from !== meta.yearMin) p.set('year_from', f.year_from);
  if (f.year_to !== meta.yearMax) p.set('year_to', f.year_to);
  if (f.method !== 'prominence') p.set('method', f.method);
  if (f.n !== 6) p.set('n', f.n);
  history.replaceState(null, '', `?${p.toString()}`);
}

function applyUrl() {
  const p = new URLSearchParams(location.search);
  $('term').value = (p.get('q') || meta.examples[0]).slice(0, meta.max_term);
  $('all-forms').checked = p.get('all_forms') !== 'false';
  const field = p.get('field') === 'title' ? 'title' : 'text';
  $(`field-${field}`).checked = true;
  const metric = p.get('metric') === 'hits' ? 'hits' : 'share';
  $(`metric-${metric}`).checked = true;
  const cats = new Set(p.getAll('cat'));
  document.querySelectorAll('#categories input').forEach((i) => { i.checked = cats.has(i.value); });
  const yf = Number(p.get('year_from')), yt = Number(p.get('year_to'));
  if (yf >= meta.yearMin && yf <= meta.yearMax) $('year-from').value = yf;
  if (yt >= meta.yearMin && yt <= meta.yearMax) $('year-to').value = yt;
  if (p.get('method') in meta.methods) $('method').value = p.get('method');
  const n = Number(p.get('n'));
  if (n >= 1 && n <= 10) $('n-peaks').value = n;
}

// ---------------------------------------------------------------- αρχικοποίηση

function buildControls() {
  const examples = $('examples');
  meta.examples.forEach((t) => {
    const b = el('button', { type: 'button', class: 'pill', text: t });
    b.addEventListener('click', () => { $('term').value = t; runSearch(); });
    examples.append(b);
  });

  const cats = $('categories');
  meta.categories.forEach((c) => {
    const input = el('input', { type: 'checkbox', value: c });
    input.addEventListener('change', runSearch);
    cats.append(el('label', { class: 'pill' }, input, c));
  });

  // Κατηγορίες που δεν υπάρχουν από την αρχή του αρχείου (π.χ. «Ελλάδα», «Κόσμος» από το 2017).
  const late = meta.categories
    .map((c) => [c, Number((meta.category_since[c] || '').slice(0, 4))])
    .filter(([, y]) => y > meta.yearMin);
  if (late.length) {
    $('categories-note').textContent = 'Το ΑΠΕ-ΜΠΕ άλλαξε κατηγορίες με τα χρόνια: '
      + late.map(([c, y]) => `«${c}» από ${y}`).join(', ') + '.';
  }

  for (const id of ['year-from', 'year-to']) {
    for (let y = meta.yearMin; y <= meta.yearMax; y++) $(id).append(el('option', { value: y, text: y }));
  }
  $('year-from').value = meta.yearMin;
  $('year-to').value = meta.yearMax;

  for (const [k, label] of Object.entries(meta.methods)) $('method').append(el('option', { value: k, text: label }));
  for (let n = 1; n <= 10; n++) $('n-peaks').append(el('option', { value: n, text: n }));
  $('n-peaks').value = 6;

  $('search-form').addEventListener('submit', (e) => { e.preventDefault(); runSearch(); });
  $('term').addEventListener('input', () => {
    clearTimeout(debounceTimer);
    if ($('term').value.trim().length >= 3) debounceTimer = setTimeout(runSearch, 450);
  });
  document.querySelectorAll('#all-forms, input[name=field], input[name=metric], #method, #n-peaks')
    .forEach((x) => x.addEventListener('change', runSearch));
  for (const id of ['year-from', 'year-to']) {
    $(id).addEventListener('change', () => {
      // Το «Από» δεν μπορεί να είναι μετά το «Έως».
      if (Number($('year-from').value) > Number($('year-to').value)) {
        if (id === 'year-from') $('year-to').value = $('year-from').value;
        else $('year-from').value = $('year-to').value;
      }
      runSearch();
    });
  }
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((t) => new bootstrap.Tooltip(t));
}

async function init() {
  try {
    const m = await getJSON('/api/meta', new URLSearchParams());
    meta = { ...m, yearMin: Number(m.months[0].slice(0, 4)), yearMax: Number(m.months[1].slice(0, 4)) };
  } catch (e) {
    showAlert(`Η εφαρμογή δεν μπόρεσε να φορτώσει: ${e.message}`, 'danger');
    return;
  }
  buildControls();
  setupLlmToggle();
  if (meta.gated) $('logout').classList.remove('d-none');
  applyUrl();
  runSearch();
}

// ---------------------------------------------------------------- αναζήτηση

async function runSearch() {
  clearTimeout(debounceTimer);
  const f = readFilters();
  if (!f.q) { showAlert('Γράψτε έναν όρο ή επιλέξτε ένα παράδειγμα.', 'info'); return; }

  if (searchCtrl) searchCtrl.abort();
  searchCtrl = new AbortController();
  $('chart-loading').classList.remove('d-none');
  try {
    const data = await getJSON('/api/search', filtersToParams(f, SEARCH_KEYS), searchCtrl.signal);
    hideAlert();
    writeUrl(f);
    lastResult = { ...data, filters: f };
    render(lastResult);
  } catch (e) {
    if (e.name === 'AbortError') return;
    showAlert(e.message, e.status === 429 ? 'warning' : 'danger');
  } finally {
    $('chart-loading').classList.add('d-none');
  }
}

function render(r) {
  $('kpi-hits').textContent = fmt.format(r.total_hits);
  $('kpi-max').textContent = r.max_month ? monthLabel(r.max_month) : '–';
  $('kpi-max-sub').textContent = r.max_month ? `${r.max_share.toLocaleString('el-GR')}% των άρθρων` : '';
  $('kpi-label').textContent = r.label;
  $('kpi-label').title = r.label;

  $('month-panel').classList.add('d-none');
  $('month-panel').replaceChildren();

  if (r.total_hits === 0) {
    showAlert(`Δεν βρέθηκαν άρθρα για «${r.term}». Δοκιμάστε άλλη λέξη ή ενεργοποιήστε «Όλοι οι τύποι της λέξης».`);
    Plotly.purge('chart');
    $('peaks-title').textContent = 'Κορυφώσεις';
    $('peaks').replaceChildren();
    return;
  }
  drawChart(r);
  renderPeaks(r);
}

function drawChart(r) {
  const metric = r.filters.metric;
  const s = r.series;
  const y = metric === 'share' ? s.share : s.hits;
  const custom = s.month.map((_, i) => [s.hits[i], s.total[i], s.share[i]]);
  const line = {
    x: s.month.map((m) => `${m}-01`), y, customdata: custom, type: 'scatter', mode: 'lines',
    line: { color: '#2563eb', width: 2 }, name: r.label,
    hovertemplate: '%{x|%m/%Y}<br>%{customdata[0]:,} από %{customdata[1]:,} άρθρα<br>%{customdata[2]:.1f}%<extra></extra>',
  };
  const pk = r.peaks;
  const markers = {
    x: pk.map((p) => `${p.month}-01`), y: pk.map((p) => (metric === 'share' ? p.share : p.hits)),
    text: pk.map((p) => `#${p.rank}`), type: 'scatter', mode: 'markers+text', textposition: 'top center',
    marker: { size: 12, color: '#dc2626', line: { width: 1.5, color: '#fff' } },
    hovertemplate: 'Κορυφή %{text}: %{x|%m/%Y}<extra></extra>', name: 'κορυφές',
  };
  const ymax = Math.max(...y) * 1.15 || 1;
  const layout = {
    margin: { l: 55, r: 15, t: 25, b: 40 }, showlegend: false, hovermode: 'closest',
    yaxis: { title: metric === 'share' ? '% άρθρων του μήνα' : 'αριθμός άρθρων', range: [0, ymax], gridcolor: '#eef2f7' },
    xaxis: { gridcolor: '#eef2f7' }, separators: ',.',
    font: { family: "'Segoe UI', Tahoma, Arial, sans-serif", color: '#1f2937' },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
  };
  Plotly.react('chart', [line, markers], layout, { responsive: true, displaylogo: false,
    modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'] });

  const gd = $('chart');
  gd.removeAllListeners?.('plotly_click');
  gd.on('plotly_click', (ev) => {
    const ym = ev.points[0].x.slice(0, 7);
    const card = document.querySelector(`.peak-card[data-month="${ym}"]`);
    if (card) openCard(card);
    else showMonthPanel(ym);
  });
}

function renderPeaks(r) {
  $('peaks-title').textContent = `Κορυφώσεις (${r.peaks.length})`;
  const list = $('peaks');
  list.replaceChildren();
  resetSummaries();
  if (!r.peaks.length) {
    list.append(el('div', { class: 'alert alert-info', text: 'Δεν εντοπίστηκαν σαφείς κορυφές. Δοκιμάστε την άλλη μέθοδο ή ευρύτερο διάστημα ετών.' }));
    return;
  }
  const tpl = $('peak-template');
  for (const p of r.peaks) {
    const card = tpl.content.firstElementChild.cloneNode(true);
    card.dataset.month = p.month;
    card.querySelector('.rank-badge').textContent = `#${p.rank}`;
    card.querySelector('.peak-month').textContent = monthLabel(p.month);
    card.querySelector('.peak-hits').textContent = fmt.format(p.hits);
    card.querySelector('.peak-share').textContent = `${p.share.toLocaleString('el-GR')}%`;
    setupArticles(card, p.month, p.hits);
    list.append(card);
    setupSummary(card.querySelector('.summary'), p.month, true);
  }
}

// ---------------------------------------------------------------- σύνοψη «τι συνέβη» (LLM)

function resetSummaries() {
  if (summaryCtrl) summaryCtrl.abort();
  summaryCtrl = new AbortController();
  if (summaryObserver) summaryObserver.disconnect();
  // Η σύνοψη ζητείται μόνο όταν η κάρτα φανεί στην οθόνη — κάρτες που δεν βλέπει κανείς δεν κοστίζουν.
  summaryObserver = 'IntersectionObserver' in window
    ? new IntersectionObserver((entries) => entries.forEach((e) => {
        if (e.isIntersecting) { summaryObserver.unobserve(e.target); loadSummary(e.target); }
      }), { rootMargin: '150px' })
    : null;
}

function setupSummary(box, month, auto) {
  box.dataset.month = month;
  if (!meta.llm.available) {
    box.append(el('div', { class: 'text-muted small', text: 'Οι αυτόματες συνόψεις δεν είναι διαθέσιμες σε αυτή την έκδοση.' }));
    return;
  }
  if (!llmEnabled) {
    box.append(el('div', { class: 'text-muted small' }, el('i', { class: 'bi bi-stars me-1' }),
      'Ενεργοποιήστε τις «Αυτόματες συνόψεις (LLM)» για να δείτε τι συνέβη.'));
    return;
  }
  if (auto && summaryObserver) { summaryObserver.observe(box); return; }
  if (auto) { loadSummary(box); return; }
  const btn = el('button', { type: 'button', class: 'btn btn-sm btn-outline-primary', text: 'Σύνοψη «τι συνέβη»' });
  btn.prepend(el('i', { class: 'bi bi-stars me-1' }));
  btn.addEventListener('click', () => { btn.remove(); loadSummary(box); });
  box.append(btn);
}

async function loadSummary(box) {
  const params = filtersToParams(lastResult.filters, ARTICLE_KEYS);
  params.set('month', box.dataset.month);
  box.replaceChildren(el('div', { class: 'text-muted small' },
    el('span', { class: 'spinner-border spinner-border-sm me-1' }), 'Δημιουργία σύνοψης…'));
  try {
    const s = await getJSON('/api/summary', params, summaryCtrl ? summaryCtrl.signal : undefined);
    if (!s.bullets.length) {
      box.replaceChildren(el('div', { class: 'text-muted small', text: 'Δεν υπάρχουν αρκετοί τίτλοι για σύνοψη.' }));
      return;
    }
    const list = el('ul');
    s.bullets.forEach((b) => list.append(el('li', { text: b })));
    box.replaceChildren(el('div', { class: 'summary-box' },
      el('div', { class: 'summary-title' }, el('i', { class: 'bi bi-stars me-1' }), 'Τι συνέβη'),
      list,
      el('div', { class: 'summary-notice', text: `${s.notice} (${s.n_titles} τίτλοι · ${s.model})` })));
    if (!s.cached && byokState.active) { byokState.used += 1; renderByok(); }
  } catch (e) {
    if (e.name === 'AbortError') return;
    if (e.status === 403) { llmEnabled = false; $('llm-switch').checked = false; renderByok(); }  // π.χ. έληξε η ενεργοποίηση
    if (e.status === 428) {  // δεν υπάρχει έτοιμη σύνοψη και χρειάζεται κλειδί
      if (byokState.active) setByok({ active: false, reason: e.message });
      const add = el('button', { type: 'button', class: 'btn btn-sm btn-outline-primary ms-2', text: 'Προσθήκη κλειδιού' });
      add.addEventListener('click', () => showKeyModal());
      box.replaceChildren(el('div', { class: 'text-muted small' }, el('i', { class: 'bi bi-key me-1' }),
        'Δεν υπάρχει ακόμη σύνοψη για αυτόν τον μήνα· για νέα σύνοψη χρειάζεται το δικό σας κλειδί.', add));
      return;
    }
    box.replaceChildren(el('div', { class: 'text-muted small' }, el('i', { class: 'bi bi-info-circle me-1' }), e.message));
  }
}

// ---------------------------------------------------------------- κλειδί του επισκέπτη (BYOK)

function setByok(state) {
  byokState = state;
  byokRenewDismissed = false;
  byokExpiresAt = state.active ? Date.now() + state.expires_in * 1000 : 0;
  clearInterval(byokTimer);
  byokTimer = state.active ? setInterval(tickByok, 1000) : null;
  renderByok();
  tickByok();
}

function mmss(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function linkButton(text, onClick) {
  const b = el('button', { type: 'button', class: 'btn btn-link', text });
  b.addEventListener('click', onClick);
  return b;
}

function renderByok() {
  const row = $('byok-row');
  row.classList.toggle('d-none', !llmEnabled);
  if (!llmEnabled) { $('byok-renew').classList.add('d-none'); return; }
  if (byokState.active) {
    row.replaceChildren(el('i', { class: 'bi bi-key-fill me-1 text-success' }),
      'Κλειδί ενεργό · σβήνεται σε ', el('strong', { id: 'byok-left' }),
      ` · ${byokState.used}/${byokState.limit} νέες συνόψεις · `,
      linkButton('Διαγραφή κλειδιού', forgetKey));
  } else {
    row.replaceChildren(
      ...(byokState.reason ? [el('div', { class: 'text-danger mb-1', text: byokState.reason })] : []),
      el('i', { class: 'bi bi-key me-1' }), 'Χωρίς κλειδί: μόνο όσες συνόψεις υπάρχουν ήδη. ',
      linkButton('Προσθήκη κλειδιού', () => showKeyModal()));
  }
}

function tickByok() {
  if (!byokState.active) { $('byok-renew').classList.add('d-none'); return; }
  const left = (byokExpiresAt - Date.now()) / 1000;
  if (left <= 0) {
    setByok({ active: false, reason: 'Τα 30 λεπτά έληξαν και το κλειδί σβήστηκε. Επικολλήστε το ξανά για νέες συνόψεις.' });
    return;
  }
  const counter = $('byok-left');
  if (counter) counter.textContent = mmss(left);
  $('byok-renew-left').textContent = mmss(left);
  const show = llmEnabled && left <= byokState.renew_window && !byokRenewDismissed;
  $('byok-renew').classList.toggle('d-none', !show);
}

async function renewKey() {
  $('byok-renew-btn').disabled = true;
  try {
    setByok(await postJSON('/api/byok/renew'));
  } catch (e) {
    if (e.status === 428) setByok({ active: false, reason: e.message });
    else showAlert(e.message, 'warning');
  } finally {
    $('byok-renew-btn').disabled = false;
  }
}

async function forgetKey() {
  try { await postJSON('/api/byok/forget'); } catch { /* το κλειδί λήγει μόνο του σε 30 λεπτά */ }
  setByok({ active: false });
}

function showKeyModal() {
  $('key-error').classList.add('d-none');
  $('key-input').value = '';
  keyModal.show();
}

async function submitKey(event) {
  event.preventDefault();
  const input = $('key-input');
  const key = input.value.trim();
  input.value = '';  // το κλειδί δεν μένει στη σελίδα
  if (!key) return;
  $('key-submit').disabled = true;
  $('key-error').classList.add('d-none');
  try {
    setByok(await postJSON('/api/byok/key', { key }));
    keyModal.hide();
    refreshSummaries();
  } catch (e) {
    $('key-error').textContent = e.message;
    $('key-error').classList.remove('d-none');
  } finally {
    $('key-submit').disabled = false;
  }
}

// ---------------------------------------------------------------- διακόπτης LLM + «δεν είμαι ρομπότ»

function setupLlmToggle() {
  const sw = $('llm-switch');
  if (!meta.llm.available) {
    sw.disabled = true;
    $('llm-note').textContent = 'Δεν είναι διαθέσιμες σε αυτή την έκδοση.';
    return;
  }
  llmEnabled = meta.llm.enabled;
  sw.checked = llmEnabled;
  sw.addEventListener('change', () => (sw.checked ? enableLlm() : disableLlm()));
  captchaModal = new bootstrap.Modal($('captcha-modal'));
  keyModal = new bootstrap.Modal($('key-modal'));
  // Κλείσιμο του παραθύρου χωρίς επιβεβαίωση → ο διακόπτης γυρίζει πίσω.
  $('captcha-modal').addEventListener('hidden.bs.modal', () => {
    removeCaptcha();
    if (!llmEnabled) $('llm-switch').checked = false;
    if (openKeyAfterCaptcha) { openKeyAfterCaptcha = false; showKeyModal(); }
  });
  $('key-modal').addEventListener('hidden.bs.modal', () => { $('key-input').value = ''; });
  $('key-form').addEventListener('submit', submitKey);
  $('byok-renew-btn').addEventListener('click', renewKey);
  $('byok-renew-dismiss').addEventListener('click', () => { byokRenewDismissed = true; tickByok(); });
  setByok(meta.byok);
}

function loadTurnstile() {
  if (window.turnstile) return Promise.resolve();
  if (!turnstileLoading) {
    turnstileLoading = new Promise((resolve, reject) => {
      // Το Cloudflare ζητά να μη «παγώνει» το script (αλλάζει συχνά), γι' αυτό χωρίς SRI· το CSP επιτρέπει μόνο αυτό το origin.
      const s = el('script', { src: 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit' });
      s.async = true;
      s.onload = resolve;
      s.onerror = () => { turnstileLoading = null; reject(new Error('Δεν φορτώθηκε η επιβεβαίωση. Ελέγξτε τη σύνδεση.')); };
      document.head.append(s);
    });
  }
  return turnstileLoading;
}

async function enableLlm() {
  if (meta.llm.captcha === 'checkbox') { showCheckbox(); return; }
  if (!meta.llm.captcha_site_key) { activateLlm({}); return; }  // τοπικά, χωρίς επιβεβαίωση
  $('captcha-error').classList.add('d-none');
  captchaModal.show();
  try {
    await loadTurnstile();
  } catch (e) {
    showCaptchaError(e.message);
    return;
  }
  removeCaptcha();
  captchaWidget = turnstile.render('#captcha-box', {
    sitekey: meta.llm.captcha_site_key,
    language: 'el',
    theme: 'light',
    callback: (token) => activateLlm({ token }),
    'error-callback': () => showCaptchaError('Η επιβεβαίωση απέτυχε. Δοκιμάστε ξανά.'),
    'expired-callback': () => showCaptchaError('Η επιβεβαίωση έληξε. Τικάρετε ξανά το κουτί.'),
  });
}

// Δικό μας κουτί: πρόκληση μίας χρήσης από τον server, κουτί, κρυφό πεδίο-«παγίδα» για bots.
async function showCheckbox(keepError = false) {
  if (!keepError) $('captcha-error').classList.add('d-none');
  const box = $('captcha-box');
  box.replaceChildren(el('div', { class: 'text-muted small' },
    el('span', { class: 'spinner-border spinner-border-sm me-1' }), 'Φόρτωση…'));
  captchaModal.show();
  let ch;
  try {
    ch = await getJSON('/api/llm/challenge', new URLSearchParams());
  } catch (e) {
    box.replaceChildren();
    showCaptchaError(e.message);
    return;
  }
  const started = Date.now();
  const check = el('input', { class: 'form-check-input', type: 'checkbox', id: 'robot-check' });
  const trap = el('input', { type: 'text', name: 'website', tabindex: '-1', autocomplete: 'off', 'aria-hidden': 'true',
    class: 'robot-trap' });
  const go = el('button', { type: 'button', class: 'btn btn-primary w-100 mt-3', text: 'Ενεργοποίηση' });
  go.disabled = true;
  check.addEventListener('change', () => { go.disabled = !check.checked; });
  go.addEventListener('click', async () => {
    go.disabled = true;
    // Ο server δέχεται την επιβεβαίωση μόνο μετά από χρόνο ανθρώπου· αν ο χρήστης ήταν πολύ γρήγορος, περιμένουμε.
    const wait = ch.min_seconds * 1000 + 200 - (Date.now() - started);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    await activateLlm({ challenge: ch.challenge, confirm: check.checked, website: trap.value });
    if (!llmEnabled) showCheckbox(true);  // νέα πρόκληση για νέα προσπάθεια, με το μήνυμα λάθους ορατό
  });
  box.replaceChildren(el('div', { class: 'w-100' },
    el('div', { class: 'robot-box' },
      el('div', { class: 'form-check mb-0' }, check,
        el('label', { class: 'form-check-label fw-semibold', for: 'robot-check', text: 'Δεν είμαι ρομπότ' }))),
    trap, go));
}

async function activateLlm(payload) {
  try {
    const res = await fetch('/api/llm/enable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let detail = 'Η ενεργοποίηση απέτυχε.';
      try { detail = (await res.json()).detail || detail; } catch { /* όχι JSON */ }
      throw new Error(detail);
    }
    llmEnabled = true;
    $('llm-switch').checked = true;
    renderByok();
    if (!byokState.active) openKeyAfterCaptcha = true;
    if (captchaModal && $('captcha-modal').classList.contains('show')) captchaModal.hide();
    else if (openKeyAfterCaptcha) { openKeyAfterCaptcha = false; showKeyModal(); }
    refreshSummaries();
  } catch (e) {
    if (meta.llm.captcha) {
      showCaptchaError(e.message);
      if (window.turnstile && captchaWidget !== null) turnstile.reset(captchaWidget);
    } else {
      $('llm-switch').checked = false;
      showAlert(e.message, 'danger');
    }
  }
}

async function disableLlm() {
  llmEnabled = false;
  if (summaryCtrl) summaryCtrl.abort();
  // Ο server σβήνει και το κλειδί από τη μνήμη του.
  try { await fetch('/api/llm/disable', { method: 'POST' }); } catch { /* εκτός σύνδεσης: το cookie λήγει μόνο του */ }
  setByok({ active: false });
  refreshSummaries();
}

function removeCaptcha() {
  if (window.turnstile && captchaWidget !== null) turnstile.remove(captchaWidget);
  captchaWidget = null;
}

function showCaptchaError(message) {
  const box = $('captcha-error');
  box.textContent = message;
  box.classList.remove('d-none');
}

function refreshSummaries() {
  if (!lastResult) return;
  resetSummaries();
  document.querySelectorAll('#peaks .summary').forEach((box) => { box.replaceChildren(); setupSummary(box, box.dataset.month, true); });
  const panel = document.querySelector('#month-panel .summary');
  if (panel && panel.dataset.month) { panel.replaceChildren(); setupSummary(panel, panel.dataset.month, false); }
}

// ---------------------------------------------------------------- άρθρα ανά μήνα

function setupArticles(card, month, hits) {
  const btn = card.querySelector('.toggle-articles');
  const box = card.querySelector('.articles');
  btn.append(` (${fmt.format(hits)})`);
  btn.addEventListener('click', () => {
    const open = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', String(!open));
    box.classList.toggle('d-none', open);
    if (!open && !box.dataset.loaded) loadArticles(box, month, 0);
  });
}

async function loadArticles(box, month, offset) {
  box.dataset.loaded = '1';
  const f = lastResult.filters;
  const params = filtersToParams(f, ARTICLE_KEYS);
  params.set('month', month);
  params.set('limit', PAGE_SIZE);
  params.set('offset', offset);

  let list = box.querySelector('ul');
  if (!list) { list = el('ul'); box.append(list); }
  box.querySelector('.more')?.remove();
  const spinner = el('div', { class: 'text-muted small py-1' },
    el('span', { class: 'spinner-border spinner-border-sm me-1' }), 'Φόρτωση…');
  box.append(spinner);

  try {
    const data = await getJSON('/api/articles', params);
    for (const a of data.items) {
      const [y, m, d] = a.date.split('-');
      const title = a.url
        ? el('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer', text: a.title })
        : el('span', { text: a.title });
      list.append(el('li', {}, el('span', { class: 'a-date', text: `${d}/${m}` }), title, ' · ',
        el('span', { class: 'a-cat', text: a.category })));
    }
    const shown = offset + data.items.length;
    if (shown < data.total) {
      const more = el('button', { type: 'button', class: 'btn btn-link btn-sm more px-0',
        text: `Περισσότερα (${fmt.format(data.total - shown)} ακόμη)` });
      more.addEventListener('click', () => loadArticles(box, month, shown));
      box.append(more);
    }
  } catch (e) {
    box.append(el('div', { class: 'text-danger small', text: e.message }));
    delete box.dataset.loaded;
  } finally {
    spinner.remove();
  }
}

function openCard(card) {
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash');
  const btn = card.querySelector('.toggle-articles');
  if (btn.getAttribute('aria-expanded') !== 'true') btn.click();
}

function showMonthPanel(ym) {
  const i = lastResult.series.month.indexOf(ym);
  if (i < 0) return;
  const hits = lastResult.series.hits[i], share = lastResult.series.share[i];
  const panel = $('month-panel');
  const close = el('button', { type: 'button', class: 'btn-close', 'aria-label': 'Κλείσιμο' });
  close.addEventListener('click', () => panel.classList.add('d-none'));
  const box = el('div', { class: 'articles mt-2' });
  const card = el('div', { class: 'card month-card' }, el('div', { class: 'card-body' },
    el('div', { class: 'd-flex justify-content-between align-items-start gap-3' },
      el('h3', { class: 'peak-title mb-0', text: `Επιλεγμένος μήνας · ${monthLabel(ym)}` }),
      el('div', { class: 'd-flex align-items-start gap-3' },
        el('div', { class: 'text-end peak-stats' }, el('strong', { text: fmt.format(hits) }), ' άρθρα', el('br'),
          el('strong', { text: `${share.toLocaleString('el-GR')}%` }), ' του μήνα'),
        close)),
    el('div', { class: 'summary mt-2' }),
    box));
  panel.replaceChildren(card);
  if (hits > 0) setupSummary(card.querySelector('.summary'), ym, false);
  panel.classList.remove('d-none');
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (hits > 0) loadArticles(box, ym, 0);
  else box.append(el('div', { class: 'text-muted small', text: 'Κανένα άρθρο με τον όρο αυτόν τον μήνα.' }));
}

document.addEventListener('DOMContentLoaded', init);
