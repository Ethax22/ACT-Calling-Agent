/* Outbound campaign screens of the phone test page: the campaigns list, the 4-step
   "Schedule campaign" wizard, a campaign's page and the DND list. Modelled on the Indus
   outbound campaign screens. Uses helpers from test.html ($, el, fmtDate, openDetails,
   showView, copy, startCall). Calls ring on this page until the Vobiz line is wired. */
(() => {
  const api = async (path, opts = {}) => {
    const r = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...opts,
      body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  };
  const toast = (text, bad) => {
    const t = el('div', 'toast' + (bad ? ' bad' : ''), text); $('toasts').appendChild(t);
    setTimeout(() => t.remove(), 3500);
  };
  const pct = x => `${Math.round((x || 0) * 100)}%`;
  const STATUS = {scheduled: 'Scheduled', running: 'Running', completed: 'Completed', cancelled: 'Cancelled'};
  const CALL_STATUS = {pending: 'pending', retry: 'retry scheduled', ringing: 'ringing', in_call: 'in call', completed: 'connected',
    busy: 'busy', no_answer: 'no answer', failed: 'failed', short_duration: 'short call', internal_error: 'error',
    skipped_dnd: 'DND', stopped: 'stopped', cancelled: 'cancelled'};
  const RESULT_COLOR = {connected: 'var(--c-connected)', no_answer: 'var(--c-noanswer)', busy: 'var(--c-busy)',
    failed: 'var(--c-failed)', short_duration: 'var(--c-failed)', internal_error: 'var(--c-failed)', provider_error: 'var(--c-failed)', cancelled: 'var(--faint)'};
  const OUTCOME_WORDS = s => s ? s.replace(/_/g, ' ') : '—';

  function rel(iso) {
    if (!iso) return '—';
    const s = (new Date(iso) - Date.now()) / 1000, a = Math.abs(s);
    const unit = a < 60 ? null : a < 3600 ? ['minute', 60] : a < 86400 ? ['hour', 3600] : ['day', 86400];
    if (!unit) return s > 0 ? 'in less than a minute' : 'less than a minute ago';
    const n = Math.round(a / unit[1]), w = `${n} ${unit[0]}${n === 1 ? '' : 's'}`;
    return s > 0 ? `in ${w}` : `${w} ago`;
  }
  const istInput = d => new Date(d.getTime() + 330 * 60000).toISOString().slice(0, 16);  // for datetime-local, India time
  const fromIstInput = v => v ? `${v}:00+05:30` : null;

  /* ---------------------------------------------------------------- charts */
  function barChart(box, buckets, keys) {
    // buckets: [{label, counts:{key:n}}]; keys: [[key, colour]]
    box.innerHTML = '';
    const total = buckets.reduce((s, b) => s + keys.reduce((t, [k]) => t + (b.counts[k] || 0), 0), 0);
    if (!total) {
      const ghosts = [36, 55, 44, 70, 52, 64, 40, 75, 58, 66, 48, 72, 54, 62];
      box.innerHTML = `<svg viewBox="0 0 700 200" preserveAspectRatio="none" aria-hidden="true">${ghosts.map((h, i) =>
        `<rect x="${i * 50 + 12}" y="${190 - h * 2.2}" width="26" height="${h * 2.2}" rx="3" fill="var(--soft)"/>`).join('')}</svg>
        <div class="empty-chart">No data available</div>`;
      return;
    }
    const W = 700, H = 200, padL = 28, padB = 26, max = Math.max(...buckets.map(b => keys.reduce((t, [k]) => t + (b.counts[k] || 0), 0)), 1);
    const step = (W - padL) / buckets.length, bw = Math.min(28, step * 0.6);
    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Calls over time">`;
    for (let g = 0; g <= 3; g++) {
      const y = (H - padB) - (H - padB - 8) * g / 3, v = Math.round(max * g / 3 * 10) / 10;
      svg += `<line x1="${padL}" x2="${W}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-dasharray="${g ? '4 4' : ''}"/>`;
      svg += `<text x="${padL - 6}" y="${y + 4}" font-size="11" text-anchor="end" fill="var(--muted)">${v}</text>`;
    }
    const every = Math.ceil(buckets.length / 8);
    buckets.forEach((b, i) => {
      let y = H - padB; const x = padL + i * step + (step - bw) / 2;
      for (const [k, colour] of keys) {
        const n = b.counts[k] || 0; if (!n) continue;
        const h = (H - padB - 8) * n / max; y -= h;
        svg += `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${colour}"><title>${b.label}: ${n} ${k.replace(/_/g, ' ')}</title></rect>`;
      }
      if (i % every === 0) svg += `<text x="${x + bw / 2}" y="${H - 8}" font-size="11" text-anchor="middle" fill="var(--muted)">${b.label}</text>`;
    });
    box.innerHTML = svg + '</svg>';
  }
  const istLabel = (d, withDate) => d.toLocaleString('en-IN', {timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false,
    ...(withDate ? {day: '2-digit', month: 'short'} : {})});

  /* ---------------------------------------------------------------- list */
  let campaigns = [], tab = 'active', hours = 24, listTimer = 0;
  async function loadList() {
    try { campaigns = (await api('/test/api/campaigns')).campaigns; } catch (e) { campaigns = []; }
    renderList();
    try {
      const at = (await api(`/test/api/attempts?hours=${hours}`)).attempts;
      $('cCallsTotal').textContent = at.length;
      const size = hours <= 1 ? 5 : hours <= 12 ? 30 : 60, n = Math.ceil(hours * 60 / size), now = Date.now();
      const start = now - n * size * 60000, buckets = [];
      for (let i = 0; i < n; i++) buckets.push({label: istLabel(new Date(start + i * size * 60000)), counts: {calls: 0}});
      for (const a of at) { const i = Math.floor((new Date(a.started_at) - start) / (size * 60000)); if (buckets[i]) buckets[i].counts.calls++; }
      barChart($('cChart'), buckets, [['calls', 'var(--c-calls)']]);
      const st = await api('/test/api/ringing');
      $('cDialler').textContent = st.state === 'idle' ? '· dialler idle' : `· dialler ${st.state.replace('_', ' ')}`;
    } catch (e) {}
  }
  function renderList() {
    const q = $('cSearch').value.trim().toLowerCase(), st = $('cStatus').value, days = +$('cTime').value;
    const active = c => c.status === 'scheduled' || c.status === 'running';
    const rows = campaigns.filter(c => (tab === 'active') === active(c))
      .filter(c => !q || c.name.toLowerCase().includes(q)).filter(c => !st || c.status === st)
      .filter(c => !days || Date.now() - new Date(c.created_at) <= days * 86400000);
    const body = $('cRows'); body.innerHTML = '';
    if (!rows.length) { body.innerHTML = `<tr><td colspan="6" class="empty-row">${tab === 'active' ? 'No active campaigns. Create one to start calling.' : 'No past campaigns'}</td></tr>`; return; }
    for (const c of rows) {
      const tr = el('tr'); tr.tabIndex = 0;
      tr.appendChild(el('td', null, c.name));
      const ag = el('td'); ag.innerHTML = '<span class="agent-cell"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="7" width="14" height="12" rx="3"/><path d="M12 7V4M9 12h.01M15 12h.01"/></svg></span>';
      ag.firstChild.appendChild(document.createTextNode(c.agent)); tr.appendChild(ag);
      const s = el('td'); s.appendChild(el('span', 'pill ' + c.status, STATUS[c.status] || c.status)); tr.appendChild(s);
      tr.appendChild(el('td', 'num', c.stats.audience));
      tr.appendChild(el('td', 'num', c.stats.attempts ? pct(c.stats.pickup_rate) : '—'));
      const when = c.status === 'scheduled' && new Date(c.start_at) > Date.now() ? `starts ${rel(c.start_at)}` : rel(c.created_at);
      tr.appendChild(el('td', 'dim', when));
      tr.onclick = tr.onkeydown = e => { if (e.type === 'click' || e.key === 'Enter') openCampaign(c.id); };
      body.appendChild(tr);
    }
  }
  for (const [k, v] of Object.entries(STATUS)) $('cStatus').appendChild(new Option(v, k));
  document.querySelectorAll('#cTabs button').forEach(b => b.onclick = () => {
    tab = b.dataset.t; document.querySelectorAll('#cTabs button').forEach(x => x.classList.toggle('on', x === b)); renderList();
  });
  document.querySelectorAll('#cRange button').forEach(b => b.onclick = () => {
    hours = +b.dataset.h; document.querySelectorAll('#cRange button').forEach(x => x.classList.toggle('on', x === b)); loadList();
  });
  ['cSearch', 'cStatus', 'cTime'].forEach(id => $(id).addEventListener('input', renderList));

  function panel(which) {
    $('cList').hidden = which !== 'list'; $('cPage').hidden = which !== 'page'; $('cDnd').hidden = which !== 'dnd';
    clearInterval(listTimer); clearInterval(pageTimer);
    if (which === 'list') { loadList(); listTimer = setInterval(() => !$('view-campaigns').hidden && loadList(), 10000); }
  }

  /* ---------------------------------------------------------------- one campaign */
  let current = null, pageTimer = 0, bucket = 'minute', cohortTab = 'cohorts';
  async function openCampaign(id) {
    current = id; panel('page'); await loadCampaign();
    pageTimer = setInterval(() => !$('view-campaigns').hidden && !$('cPage').hidden && loadCampaign(), 3000);
  }
  async function loadCampaign() {
    if (!current) return;
    let c; try { c = await api(`/test/api/campaigns/${current}?status=${encodeURIComponent($('cpCallStatus').value)}`); } catch (e) { toast(e.message, true); return; }
    $('cpName').textContent = c.name;
    const s = $('cpStatus'); s.className = 'pill ' + c.status; s.textContent = STATUS[c.status] || c.status;
    $('cpWhen').textContent = c.status === 'scheduled' ? (new Date(c.start_at) > Date.now() ? `Starts ${rel(c.start_at)}` : 'Starting now')
      : c.status === 'running' ? `Running · ends ${rel(c.end_at)}`
      : `Ended ${rel(c.ended_at || c.end_at)}`;
    $('cpAudience').textContent = c.stats.audience;
    $('cpCompletion').textContent = pct(c.stats.completion_rate);
    $('cpPickup').textContent = c.stats.attempts ? pct(c.stats.pickup_rate) : '0%';
    const closed = c.status === 'completed' || c.status === 'cancelled';
    document.querySelectorAll('#cpMenu button').forEach(b => b.disabled = closed);
    $('cpAddCohort').disabled = $('cpAddStops').disabled = closed;
    // chart
    const map = new Map();
    for (const t of c.timeline) {
      const key = bucket === 'hour' ? t.minute.slice(0, 13) : t.minute;
      if (!map.has(key)) map.set(key, {label: istLabel(new Date(key + (bucket === 'hour' ? ':00' : '') + ':00Z'), bucket === 'hour'), counts: {}});
      const r = ['short_duration', 'internal_error', 'provider_error'].includes(t.result) ? 'failed' : t.result;
      map.get(key).counts[r] = (map.get(key).counts[r] || 0) + t.n;
    }
    barChart($('cpChart'), [...map.values()], [['connected', RESULT_COLOR.connected], ['no_answer', RESULT_COLOR.no_answer], ['busy', RESULT_COLOR.busy], ['failed', RESULT_COLOR.failed]]);
    // cohorts
    const cr = $('cpCohortRows'); cr.innerHTML = '';
    for (const k of c.cohorts) {
      const tr = el('tr');
      const n = el('td'); const idc = el('div', 'idcell'); idc.appendChild(el('span', null, k.name));
      const cp = el('button', 'mini'); cp.title = 'Copy cohort name'; cp.innerHTML = $('copyId').innerHTML; cp.onclick = () => copy(k.name, cp); idc.appendChild(cp); n.appendChild(idc); tr.appendChild(n);
      tr.appendChild(el('td', 'dim', k.source === 'sheet' ? 'Leads Sheet' : `CSV${k.file_name ? ' · ' + k.file_name : ''}`));
      const st = el('td'); st.appendChild(el('span', 'pill ok', k.status === 'completed' ? 'Completed' : k.status)); tr.appendChild(st);
      tr.appendChild(el('td', 'num', k.valid)); tr.appendChild(el('td', 'num', k.invalid)); tr.appendChild(el('td', 'dim', fmtDate(k.uploaded_at)));
      cr.appendChild(tr);
    }
    $('cpStopCount').textContent = c.stop_count;
    // attempted calls
    const body = $('cpCallRows'); body.innerHTML = '';
    if (!c.calls.length) body.innerHTML = '<tr><td colspan="8" class="empty-row">No calls yet</td></tr>';
    for (const r of c.calls) {
      const tr = el('tr');
      tr.appendChild(el('td', null, r.phone_masked.replace(/^X+/, '••••••')));
      tr.appendChild(el('td', null, [r.lead_name, r.lead_course].filter(Boolean).join(' · ') || '—'));
      const st = el('td'); st.appendChild(el('span', 'status-txt ' + r.status, CALL_STATUS[r.status] || r.status)); tr.appendChild(st);
      tr.appendChild(el('td', 'num', r.attempts || 0));
      const next = r.status === 'retry' && r.next_attempt_at ? `next try ${rel(r.next_attempt_at)}` : '';
      tr.appendChild(el('td', 'dim', [r.failure_reason, next].filter(Boolean).join(' · ') || '—'));
      tr.appendChild(el('td', null, OUTCOME_WORDS(r.outcome) + (r.do_not_call === 'yes' ? ' · do not call' : '')));
      tr.appendChild(el('td', 'dim', r.last_attempt_at ? rel(r.last_attempt_at) : '—'));
      const act = el('td'); act.style.textAlign = 'right';
      if (r.last_call_id) { const v = el('button', 'link', 'View'); v.onclick = () => openDetails(r.last_call_id); act.appendChild(v); }
      tr.appendChild(act); body.appendChild(tr);
    }
    current_c = c;
  }
  let current_c = null;
  for (const [k, v] of Object.entries(CALL_STATUS)) $('cpCallStatus').appendChild(new Option(v, k));
  $('cpCallStatus').onchange = loadCampaign;
  document.querySelectorAll('#cpBucket button').forEach(b => b.onclick = () => {
    bucket = b.dataset.b; document.querySelectorAll('#cpBucket button').forEach(x => x.classList.toggle('on', x === b)); loadCampaign();
  });
  document.querySelectorAll('#cpCohortTabs button').forEach(b => b.onclick = () => {
    cohortTab = b.dataset.t; document.querySelectorAll('#cpCohortTabs button').forEach(x => x.classList.toggle('on', x === b));
    $('cpCohorts').hidden = cohortTab !== 'cohorts'; $('cpStops').hidden = cohortTab !== 'stops';
  });
  $('cBack').onclick = $('cCrumb').onclick = $('dBack').onclick = $('dCrumb').onclick = () => panel('list');
  $('cpActions').onclick = e => { e.stopPropagation(); $('cpMenu').hidden = !$('cpMenu').hidden; };
  document.addEventListener('click', () => { $('cpMenu').hidden = true; });
  $('cpMenu').onclick = e => {
    const a = e.target.closest('button'); if (!a || a.disabled) return; $('cpMenu').hidden = true;
    if (a.dataset.a === 'cancel') confirmCancel(); else if (a.dataset.a === 'cohort') Wizard.open('cohort'); else stopFileDialog();
  };
  $('cpAddCohort').onclick = () => Wizard.open('cohort');
  $('cpAddStops').onclick = stopFileDialog;

  /* ---------------------------------------------------------------- small dialogs */
  function dialog(title, bodyNode, yes, onYes, no = 'Cancel') {
    $('dlgTitle').textContent = title; $('dlgBody').innerHTML = ''; $('dlgBody').appendChild(bodyNode);
    $('dlgYes').textContent = yes; $('dlgNo').textContent = no; $('dlgYes').hidden = !yes;
    $('dlg').hidden = false; $('wizScrim').hidden = false;
    const close = () => { $('dlg').hidden = true; if ($('wiz').hidden) $('wizScrim').hidden = true; };
    $('dlgNo').onclick = $('dlgClose').onclick = close;
    $('dlgYes').onclick = async () => { $('dlgYes').disabled = true; try { await onYes(); close(); } catch (e) { toast(e.message, true); } $('dlgYes').disabled = false; };
  }
  function confirmCancel() {
    const p = el('p', 'dlg-text', `Are you sure you want to cancel this campaign? This cannot be undone. Campaign: ${current_c ? current_c.name : ''}`);
    dialog('Cancel campaign', p, 'Yes', async () => {
      await api(`/test/api/campaigns/${current}/cancel`, {method: 'POST'}); toast('Campaign status updated successfully'); loadCampaign();
    }, 'No');
  }
  function stopFileDialog() {
    const wrap = el('div');
    wrap.appendChild(el('p', 'dlg-text', 'Numbers this campaign must not call. Paste them (one per line, or separated by commas) or choose a CSV file.'));
    const area = el('textarea', 'dlg-area'); area.placeholder = '9876543210\n98765 01234'; wrap.appendChild(area);
    const file = el('input'); file.type = 'file'; file.accept = '.csv,.txt,text/csv,text/plain'; file.style.marginTop = '10px';
    file.onchange = async () => { if (file.files[0]) area.value = await file.files[0].text(); };
    wrap.appendChild(file);
    dialog('Add stop file', wrap, 'Add numbers', async () => {
      const r = await api(`/test/api/campaigns/${current}/stops`, {method: 'POST', body: {text: area.value}});
      toast(`${r.added} numbers added to the stop list`); loadCampaign();
    });
  }
  $('cpDetailsBtn').onclick = () => {
    const c = current_c; if (!c) return;
    const d = el('div', 'review'); d.innerHTML = reviewHtml({
      name: c.name, agent: c.agent, connection: c.connection, cohort: c.cohorts.map(k => k.name).join(', '),
      rows: `${c.stats.audience} recipients`, start: c.start_at, end: c.end_at, days: c.days, from: c.window_start, to: c.window_end, cps: c.cps,
      retry: c.max_retries > 0, max: c.max_retries, interval: c.retry_interval_min, on: c.retry_on, err: c.error_retries});
    dialog('Campaign details', d, '', () => {}, 'Close');
  };

  /* ---------------------------------------------------------------- DND list */
  async function loadDnd() {
    const body = $('dRows'); body.innerHTML = '';
    let list = []; try { list = (await api('/test/api/dnd')).dnd; } catch (e) {}
    if (!list.length) body.innerHTML = '<tr><td colspan="5" class="empty-row">No numbers on the DND list</td></tr>';
    for (const d of list) {
      const tr = el('tr');
      tr.appendChild(el('td', null, d.phone_masked.replace(/^X+/, '••••••')));
      tr.appendChild(el('td', null, d.reason || '—'));
      tr.appendChild(el('td', 'dim', d.source.startsWith('call') ? 'Lead asked on a call' : 'Added by hand'));
      tr.appendChild(el('td', 'dim', fmtDate(d.added_at)));
      const act = el('td'); act.style.textAlign = 'right';
      const rm = el('button', 'link', 'Remove'); rm.onclick = async () => { await api(`/test/api/dnd/${d.phone}`, {method: 'DELETE'}); toast('Removed from the DND list'); loadDnd(); };
      act.appendChild(rm); tr.appendChild(act); body.appendChild(tr);
    }
  }
  $('cDndBtn').onclick = () => { panel('dnd'); loadDnd(); };
  $('dAdd').onclick = async () => {
    try { await api('/test/api/dnd', {method: 'POST', body: {phone: $('dPhone').value, reason: $('dReason').value}}); }
    catch (e) { toast(e.message, true); return; }
    $('dPhone').value = $('dReason').value = ''; toast('Added to the DND list'); loadDnd();
  };

  /* ---------------------------------------------------------------- review summary (wizard + details) */
  const esc = s => String(s ?? '').replace(/[&<>"]/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));
  const DAY_LETTERS = 'MTWTFSS';
  const RETRY_WORDS = {busy: 'Busy', no_answer: 'No Answer', failed: 'Failed', short_duration: 'Short Duration', provider_error: 'Provider Error', internal_error: 'Internal Error'};
  function reviewHtml(s) {
    const dots = [...DAY_LETTERS].map((l, i) => `<span class="${s.days[i] === '1' ? 'on' : ''}">${l}</span>`).join('');
    const errs = Object.entries(s.err || {}).filter(([k]) => (s.on || []).includes(k))
      .map(([k, v]) => `${RETRY_WORDS[k]} · ${v.max}x / ${v.interval_min} min`).join(' · ') || '—';
    const on = (s.on || []).filter(k => !k.endsWith('_error')).map(k => RETRY_WORDS[k]).join(', ');
    return `<div class="rv"><h3>Agent</h3><dl><dt>Agent</dt><dd>${esc(s.agent)} · current version</dd><dt>Campaign name</dt><dd>${esc(s.name)}</dd>
        <dt>Connection</dt><dd>${esc(s.connection)}</dd><dt>Phone numbers</dt><dd>Rings the test page</dd></dl></div>
      <div class="rv"><h3>Recipients</h3><dl><dt>Cohort</dt><dd>${esc(s.cohort)}</dd><dt>Rows</dt><dd>${esc(s.rows)}</dd>${s.mapping ? `<dt>Columns</dt><dd>${esc(s.mapping)}</dd>` : ''}</dl></div>
      <div class="rv"><h3>Schedule</h3><dl><dt>Schedule</dt><dd>${esc(fmtDate(s.start))} → ${esc(fmtDate(s.end))}</dd>
        <dt>Allowed time</dt><dd>${esc(s.from)} – ${esc(s.to)} (India time)</dd><dt>Days</dt><dd><span class="day-dots">${dots}</span></dd>
        <dt>CPS</dt><dd>${esc(s.cps)} calls/sec</dd>
        <dt>Retries</dt><dd>${s.retry ? `${s.max} retries · every ${s.interval} min · on ${esc(on || 'nothing')}` : 'Off'}</dd>
        <dt>Error retries</dt><dd>${s.retry ? esc(errs) : 'Off'}</dd></dl></div>`;
  }

  /* ---------------------------------------------------------------- wizard */
  const FIELDS = [
    {k: 'phone', label: 'Phone Number *', def: ''}, {k: 'lead_name', label: 'lead_name', def: 'there'},
    {k: 'lead_course', label: 'lead_course', def: 'engineering'}, {k: 'enquiry_source', label: 'enquiry_source', def: 'our website'},
    {k: 'enquiry_date', label: 'enquiry_date', def: 'recently'},
  ];
  const Wizard = window.CampaignWizard = (() => {
    let mode = 'new', step = 0, steps = [0, 1, 2, 3], parsed = null, mapping = {}, required = new Set(['phone']),
      validated = null, sample = 0, source = 'csv', fileName = '';
    const days = ['1', '1', '1', '1', '1', '1', '0'];
    const show = s => {
      step = s; const idx = steps.indexOf(s);
      document.querySelectorAll('.wpanel').forEach(p => p.hidden = +p.dataset.p !== s);
      const st = $('wizSteps'); st.classList.toggle('one', steps.length === 1);
      [...st.children].forEach((d, i) => { d.hidden = !steps.includes(i); d.className = i === s ? 'on' : steps.indexOf(i) < idx ? 'done' : ''; });
      $('wStepText').textContent = `Step ${idx + 1} of ${steps.length}`;
      $('wBack').textContent = idx === 0 ? 'Cancel' : 'Back';
      $('wError').hidden = true; nextLabel();
      if (s === 3) review();
    };
    const nextLabel = () => {
      const last = steps.indexOf(step) === steps.length - 1;
      $('wNext').textContent = step === 1 && !validated ? 'Validate' : mode === 'cohort' ? 'Add cohort' : last ? 'Launch' : 'Next';
      $('wNext').disabled = step === 3 && !$('wConfirm').checked;
    };
    const err = m => { $('wError').textContent = m; $('wError').hidden = false; };

    function open(m) {
      mode = m; steps = m === 'cohort' ? [1] : [0, 1, 2, 3];
      $('wizTitle').textContent = m === 'cohort' ? 'Add cohort' : 'Schedule campaign';
      if (m === 'new') resetAll(); else resetRecipients();
      $('wiz').hidden = $('wizScrim').hidden = false; $('paused').hidden = true;
      show(steps[0]); setTimeout(() => (m === 'new' ? $('wName') : $('wPick')).focus(), 50);
    }
    function close() { $('wiz').hidden = $('wizScrim').hidden = true; }
    function resetAll() {
      $('wName').value = ''; resetRecipients();
      setSeg('wStartMode', 'now'); setSeg('wEndMode', 'today'); $('wStart').hidden = $('wEnd').hidden = true;
      $('wCps').value = 0.5; $('wFrom').value = '09:00'; $('wTo').value = '19:00';
      days.splice(0, 7, '1', '1', '1', '1', '1', '1', '0'); drawDays();
      $('wRetry').checked = true; $('wMaxRetries').value = 2; $('wInterval').value = 180;
      drawRetryOn(['busy', 'no_answer', 'failed']); $('wConfirm').checked = false; $('wTestDial').open = false;
      $('wEndToday').textContent = `ends today at 11:59 PM`;
    }
    function resetRecipients() {
      parsed = null; mapping = {}; required = new Set(['phone']); validated = null; sample = 0; fileName = '';
      $('wFile').value = ''; $('wDropEmpty').hidden = false; $('wDropFile').hidden = true;
      $('wCohortWrap').hidden = $('wMapWrap').hidden = true; $('wValid').hidden = true; $('wCohort').value = ''; $('wSheetInfo').textContent = '';
      setSource('csv');
    }
    function setSeg(id, v) { document.querySelectorAll(`#${id} button`).forEach(b => b.classList.toggle('on', (b.dataset.m || b.dataset.s) === v)); }
    function setSource(s) {
      source = s; setSeg('wSource', s); $('wCsvBox').hidden = s !== 'csv'; $('wSheetBox').hidden = s !== 'sheet';
    }
    document.querySelectorAll('#wSource button').forEach(b => b.onclick = () => { resetRecipients(); setSource(b.dataset.s); });

    // CSV upload
    $('wPick').onclick = () => $('wFile').click();
    $('wFile').onchange = () => $('wFile').files[0] && readFile($('wFile').files[0]);
    const drop = $('wDrop');
    drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
    drop.ondragleave = () => drop.classList.remove('over');
    drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); const f = e.dataTransfer.files[0]; if (f) readFile(f); };
    async function readFile(f) {
      if (f.size > 10 * 1024 * 1024) { err('The file is larger than 10 MB.'); return; }
      try { useRows(await api('/test/api/recipients/parse', {method: 'POST', body: {text: await f.text()}}), f.name.replace(/\.csv$/i, '')); }
      catch (e) { err(e.message); return; }
      fileName = f.name; $('wFileName').textContent = f.name; $('wFileSize').textContent = f.size < 1024 ? `${f.size} B` : `${(f.size / 1024).toFixed(1)} KB`;
      $('wDropEmpty').hidden = true; $('wDropFile').hidden = false;
    }
    $('wFileRemove').onclick = () => resetRecipients();
    $('wSheetLoad').onclick = async () => {
      $('wSheetLoad').disabled = true; $('wSheetInfo').textContent = 'Reading the Sheet…';
      try { const d = await api('/test/api/recipients/sheet'); useRows(d, `Leads Sheet ${new Date().toLocaleDateString('en-IN', IST)}`); fileName = 'Leads Sheet'; $('wSheetInfo').textContent = `${d.rows.length} rows read`; }
      catch (e) { $('wSheetInfo').textContent = ''; err(e.message); }
      $('wSheetLoad').disabled = false;
    };
    function useRows(d, name) {
      parsed = d; mapping = {...d.suggested}; required = new Set(['phone']); validated = null; sample = 0;
      $('wCohort').value = name; $('wCohortWrap').hidden = $('wMapWrap').hidden = false; $('wValid').hidden = true;
      $('wRowCount').textContent = `${d.rows.length} rows`; drawSample(); drawMap(); nextLabel();
    }
    function drawSample() {
      const box = $('wSample'); box.innerHTML = '';
      const n = Math.min(5, parsed.rows.length);
      for (let i = 0; i < n; i++) { const b = el('button', i === sample ? 'on' : '', String(i + 1)); b.onclick = () => { sample = i; drawSample(); drawMap(); }; box.appendChild(b); }
      if (parsed.rows.length > 1) { const r = el('button', '', 'Random'); r.style.padding = '0 10px'; r.onclick = () => { sample = Math.floor(Math.random() * parsed.rows.length); drawSample(); drawMap(); }; box.appendChild(r); }
    }
    function drawMap() {
      const body = $('wMap'); body.innerHTML = ''; const row = parsed.rows[sample] || {};
      for (const f of FIELDS) {
        const tr = el('tr'); tr.appendChild(el('td', null, f.label));
        const sel = el('select'); sel.setAttribute('aria-label', `Column for ${f.k}`);
        if (f.k !== 'phone') sel.appendChild(new Option('None', ''));
        for (const c of parsed.columns) sel.appendChild(new Option(c, c));
        sel.value = mapping[f.k] || ''; sel.onchange = () => { mapping[f.k] = sel.value; validated = null; $('wValid').hidden = true; drawMap(); nextLabel(); };
        const tdSel = el('td'); tdSel.appendChild(sel); tr.appendChild(tdSel);
        const req = el('input'); req.type = 'checkbox'; req.checked = required.has(f.k); req.disabled = f.k === 'phone';
        req.setAttribute('aria-label', `${f.k} required`);
        req.onchange = () => { req.checked ? required.add(f.k) : required.delete(f.k); validated = null; $('wValid').hidden = true; nextLabel(); };
        const tdReq = el('td'); tdReq.appendChild(req); tr.appendChild(tdReq);
        const v = mapping[f.k] ? row[mapping[f.k]] : '';
        tr.appendChild(el('td', 'prev' + (v ? '' : ' def'), v || (f.def ? `Default: ${f.def}` : '—')));
        body.appendChild(tr);
      }
    }
    async function validate() {
      if (!parsed) { err(source === 'csv' ? 'Upload a CSV file first.' : 'Load the leads from the Sheet first.'); return false; }
      if (!mapping.phone) { err('Choose the column that holds the phone number.'); return false; }
      if (!$('wCohort').value.trim()) { err('Give the cohort a name.'); return false; }
      const r = await api('/test/api/recipients/validate', {method: 'POST', body: {rows: parsed.rows, mapping: {...mapping, _required: [...required]}}});
      validated = r;
      const box = $('wValid'); box.hidden = false; box.className = 'validation ' + (r.valid ? 'ok' : 'bad');
      box.innerHTML = '';
      box.appendChild(el('b', null, `${r.valid} valid rows · ${r.invalid.length} removed`));
      if (r.invalid.length) {
        const ul = el('ul'); r.invalid.slice(0, 8).forEach(i => ul.appendChild(el('li', null, `Row ${i.row}: ${i.reason}`)));
        if (r.invalid.length > 8) ul.appendChild(el('li', null, `and ${r.invalid.length - 8} more`)); box.appendChild(ul);
      }
      box.appendChild(el('div', null, 'Rows that are removed are not called.'));
      nextLabel();
      if (!r.valid) { err('There are no valid rows to call.'); return false; }
      return true;
    }
    const cohortSpec = () => ({name: $('wCohort').value.trim(), source, file_name: fileName, rows: parsed.rows, mapping: {...mapping, _required: [...required]}});

    // schedule
    document.querySelectorAll('#wStartMode button').forEach(b => b.onclick = () => {
      setSeg('wStartMode', b.dataset.m); $('wStart').hidden = b.dataset.m !== 'custom';
      if (!$('wStart').value) $('wStart').value = istInput(new Date(Date.now() + 10 * 60000));
    });
    document.querySelectorAll('#wEndMode button').forEach(b => b.onclick = () => {
      setSeg('wEndMode', b.dataset.m); $('wEnd').hidden = b.dataset.m !== 'custom'; $('wEndToday').hidden = b.dataset.m === 'custom';
      if (!$('wEnd').value) $('wEnd').value = istInput(new Date(Date.now() + 7 * 86400000)).slice(0, 11) + '19:00';
    });
    function drawDays() {
      const box = $('wDays'); box.innerHTML = '';
      [...DAY_LETTERS].forEach((l, i) => {
        const b = el('button', days[i] === '1' ? 'on' : '', l); b.type = 'button';
        b.setAttribute('aria-pressed', days[i] === '1'); b.setAttribute('aria-label', ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][i]);
        b.onclick = () => { days[i] = days[i] === '1' ? '0' : '1'; drawDays(); }; box.appendChild(b);
      });
    }
    function drawRetryOn(on) {
      const box = $('wRetryOn'); box.innerHTML = '';
      for (const [k, w] of Object.entries(RETRY_WORDS)) {
        const l = el('label'); const c = el('input'); c.type = 'checkbox'; c.value = k; c.checked = on.includes(k) || k.endsWith('_error');
        c.onchange = drawErrBox; l.appendChild(c); l.appendChild(document.createTextNode(w)); box.appendChild(l);
      }
      drawErrBox();
    }
    function drawErrBox() {
      const box = $('wErrBox'), keep = {};
      box.querySelectorAll('input').forEach(i => keep[i.id] = i.value); box.innerHTML = '';
      for (const k of ['provider_error', 'internal_error']) {
        if (!retryOn().includes(k)) continue;
        const d = el('div', 'eb'); d.appendChild(el('b', null, RETRY_WORDS[k]));
        d.insertAdjacentHTML('beforeend', `<div class="two"><label class="f">Max retries<input type="number" id="w_${k}_max" min="0" max="20" value="${keep[`w_${k}_max`] || 3}"></label><label class="f">Interval (min)<input type="number" id="w_${k}_int" min="1" max="1440" value="${keep[`w_${k}_int`] || 5}"></label></div>`);
        box.appendChild(d);
      }
    }
    const retryOn = () => [...$('wRetryOn').querySelectorAll('input:checked')].map(i => i.value);
    $('wRetry').onchange = () => { $('wRetryBox').hidden = !$('wRetry').checked; };
    function scheduleSpec() {
      const custom = id => document.querySelector(`#${id} button.on`).dataset.m === 'custom';
      const err = {};
      for (const k of ['provider_error', 'internal_error']) if ($(`w_${k}_max`)) err[k] = {max: +$(`w_${k}_max`).value, interval_min: +$(`w_${k}_int`).value};
      return {
        start_at: custom('wStartMode') ? fromIstInput($('wStart').value) : 'now',
        end_at: custom('wEndMode') ? fromIstInput($('wEnd').value) : null,
        days: days.join(''), window_start: $('wFrom').value, window_end: $('wTo').value, cps: +$('wCps').value,
        max_retries: $('wRetry').checked ? +$('wMaxRetries').value : 0, retry_interval_min: +$('wInterval').value,
        retry_on: $('wRetry').checked ? retryOn() : [], error_retries: $('wRetry').checked ? err : {},
      };
    }
    function checkSchedule() {
      const s = scheduleSpec();
      if (!s.days.includes('1')) return 'Pick at least one calling day.';
      if (!s.window_start || !s.window_end || s.window_end <= s.window_start) return 'The calling window must end after it starts.';
      if (!(s.cps >= 0.1 && s.cps <= 10)) return 'Attempts per second must be between 0.1 and 10.';
      if (s.start_at !== 'now' && !s.start_at) return 'Pick a start date and time.';
      const start = s.start_at === 'now' ? Date.now() : new Date(s.start_at);
      if (s.end_at && new Date(s.end_at) <= start) return 'The campaign must end after it starts.';
      if ($('wRetry').checked && !(+$('wMaxRetries').value >= 0 && +$('wMaxRetries').value <= 20)) return 'Max retries must be 0 to 20.';
      return '';
    }

    // review and launch
    function review() {
      const s = scheduleSpec(), endToday = new Date(); endToday.setHours(23, 59, 0, 0);
      const mapped = FIELDS.filter(f => mapping[f.k]).map(f => `${f.k} ← ${mapping[f.k]}`).join(', ');
      $('wReview').innerHTML = reviewHtml({
        name: $('wName').value.trim(), agent: $('wAgent').value, connection: 'Browser test line',
        cohort: `${$('wCohort').value.trim()}${fileName && fileName !== 'Leads Sheet' ? ' (' + fileName + ')' : source === 'sheet' ? ' (Leads Sheet)' : ''}`,
        rows: `${validated.valid} valid · ${validated.invalid.length} removed`, mapping: mapped,
        start: s.start_at === 'now' ? new Date().toISOString() : s.start_at, end: s.end_at || istEndOfToday(),
        days: s.days, from: s.window_start, to: s.window_end, cps: s.cps, retry: $('wRetry').checked, max: s.max_retries,
        interval: s.retry_interval_min, on: s.retry_on, err: s.error_retries,
      });
      if (!windowReachable(s)) {
        $('wReview').insertAdjacentHTML('afterbegin', `<div class="form-error">No calls can be made: between the start and the end of
          this campaign there is no allowed day inside the calling window (${s.window_start}–${s.window_end}, India time). Change the
          end date, days or window in Schedule.</div>`);
      }
      const sel = $('wTestLead'); sel.innerHTML = ''; const seen = new Set();
      parsed.rows.forEach((r, i) => {  // valid leads only (same rule as the server)
        const phone = validPhone(r[mapping.phone]); if (!phone || seen.has(phone) || sel.options.length >= 20) return; seen.add(phone);
        const name = mapping.lead_name ? r[mapping.lead_name] : '';
        sel.appendChild(new Option(`${name || 'Unnamed lead'} · ••••${phone.slice(-4)}`, i));
      });
      $('wConfirm').checked = false; nextLabel();
    }
    function validPhone(raw) {
      let d = String(raw || '').replace(/\D/g, '');
      if (d.length === 12 && d.startsWith('91')) d = d.slice(2); else if (d.length === 11 && d.startsWith('0')) d = d.slice(1);
      return /^[6-9]\d{9}$/.test(d) ? d : null;
    }
    function windowReachable(s) {
      // Walk the campaign period in 5-minute steps (India time) looking for one allowed moment.
      const start = s.start_at === 'now' ? Date.now() : +new Date(s.start_at), end = +new Date(s.end_at || istEndOfToday());
      for (let t = start; t < end && t < start + 60 * 86400000; t += 5 * 60000) {
        const ist = new Date(t + 330 * 60000), day = (ist.getUTCDay() + 6) % 7, hm = ist.toISOString().slice(11, 16);
        if (s.days[day] === '1' && hm >= s.window_start && hm < s.window_end) return true;
      }
      return false;
    }
    function istEndOfToday() { const ist = istInput(new Date()).slice(0, 10); return `${ist}T23:59:00+05:30`; }
    $('wConfirm').onchange = nextLabel;
    $('wTestGo').onclick = () => {
      if (micCtx || ws) { toast('Finish the current call first.', true); return; }
      const r = parsed.rows[+$('wTestLead').value] || {}, get = k => (mapping[k] ? r[mapping[k]] : '') || '';
      document.querySelector('#mode button[data-dir=outbound]').click();
      $('leadName').value = get('lead_name'); $('leadCourse').value = get('lead_course');
      $('leadSource').value = get('enquiry_source'); $('leadDate').value = get('enquiry_date');
      $('caller').value = get('phone').replace(/\D/g, '').slice(-10);
      close(); $('paused').hidden = false; showView('call');
      startCall().catch(e => { toast('Could not start: ' + (e.message || e), true); endCall(false); });
    };
    $('pausedBack').onclick = () => { $('paused').hidden = true; $('wiz').hidden = $('wizScrim').hidden = false; showView('campaigns'); show(3); };

    $('wizClose').onclick = () => { close(); $('paused').hidden = true; };
    $('wBack').onclick = () => { const i = steps.indexOf(step); if (i === 0) { close(); return; } show(steps[i - 1]); };
    $('wNext').onclick = async () => {
      $('wError').hidden = true; $('wNext').disabled = true;
      try {
        if (step === 0) {
          if (!$('wName').value.trim()) { err('Give the campaign a name.'); return; }
          show(1);
        } else if (step === 1) {
          if (!validated) { await validate(); return; }   // first click validates, the next goes on
          if (!validated.valid) return;
          if (mode === 'cohort') {
            await api(`/test/api/campaigns/${current}/cohorts`, {method: 'POST', body: cohortSpec()});
            close(); toast('Cohort added'); loadCampaign(); return;
          }
          show(2);
        } else if (step === 2) {
          const m = checkSchedule(); if (m) { err(m); return; }
          show(3);
        } else {
          const c = await api('/test/api/campaigns', {method: 'POST', body: {name: $('wName').value.trim(), ...scheduleSpec(), cohort: cohortSpec()}});
          close(); toast('Campaign created successfully'); openCampaign(c.id);
        }
      } catch (e) { err(e.message); }
      finally { nextLabel(); }
    };
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('wiz').hidden && $('dlg').hidden) close(); });
    drawDays(); drawRetryOn(['busy', 'no_answer', 'failed']);
    return {open};
  })();
  $('cCreate').onclick = () => Wizard.open('new');

  window.Campaigns = {show: () => { if ($('cPage').hidden && $('cDnd').hidden) panel('list'); else if (!$('cPage').hidden) loadCampaign(); else loadDnd(); }, open: openCampaign};
})();
