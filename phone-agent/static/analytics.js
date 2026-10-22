/* Agent Analytics tab of the phone test page: Overview, Connectivity, Engagement, Tools,
   Goals, Sentiment, Group by and Call logs for the inbound or outbound agent (modelled on Indus Agent
   Analytics). Data: GET /test/api/analytics (analytics.py, which also documents every
   definition). Charts are plain SVG with hover tooltips; colours are the dataviz reference
   palette's categorical slots, assigned per entity in fixed order. */
(() => {
  const S = i => `var(--s${i})`;
  const RESULT_COLOR = {connected: S(1), busy: S(2), no_answer: S(3), failed: S(4), cancelled: S(5),
    short_duration: S(6), internal_error: S(8), 'in progress': 'var(--chart-muted)'};
  const END_COLOR = {AGENT_ENDS: S(1), USER_ENDS: S(2), UNKNOWN: 'var(--chart-muted)'};
  const OUTCOME_ORDER = ['visit_booked', 'interested', 'callback_requested', 'not_interested', 'wrong_number', 'voicemail',
    'no_answer', 'transferred', 'answered', 'lead_captured', 'unanswered_question', 'abandoned'];
  const words = s => String(s ?? '').replace(/_/g, ' ');
  const nf = n => n == null ? '—' : Number(n).toLocaleString('en-IN');
  const pct = (x, d = 1) => x == null ? '—' : `${(x * 100).toFixed(d).replace(/\.0$/, '')}%`;
  const ms = x => x == null ? '—' : `${nf(x)} ms`;
  const dur = s => s == null ? '—' : `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.round(s % 60)).padStart(2, '0')}`;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));

  let sentFilter = null, data = null, tab = 'overview', unit = 'n', engTab = 'hour', groupBy = 'language', logsAll = false;

  /* ---------------------------------------------------------------- tooltip */
  const tip = $('aTip');
  document.addEventListener('mousemove', e => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (!t || $('view-analytics').hidden) { tip.hidden = true; return; }
    tip.innerHTML = t.dataset.tip; tip.hidden = false;
    const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8), y = Math.max(8, e.clientY - tip.offsetHeight - 10);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });

  /* ---------------------------------------------------------------- chart pieces */
  function card(title, note, inner) {
    return `<div class="a-card"><h3>${esc(title)}</h3>${note ? `<p class="note">${note}</p>` : ''}${inner}</div>`;
  }
  const legend = items => `<div class="a-legend">${items.map(([l, c]) => `<span><i style="background:${c}"></i>${esc(l)}</span>`).join('')}</div>`;
  function topRounded(x, y, w, h, r = 4) {  // a bar with rounded data-end, anchored square to the baseline
    if (h <= 0) return '';
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  /** Vertical bars. labels: x categories; series: [{label, color, values[], tips?[]}]; grouped side by side. */
  function vbars(labels, series, {fmt = nf, height = 200, max: forcedMax} = {}) {
    const all = series.flatMap(s => s.values.filter(v => v != null));
    if (!all.length || all.every(v => !v)) return `<div class="empty">No data for this period</div>`;
    const W = 720, H = height, padL = 44, padB = 26, padT = 8;
    // Counts: round the top up so the four gridlines land on whole numbers (no "43.5 calls").
    const top = Math.max(...all) || 1, whole = all.every(v => Number.isInteger(v));
    const max = forcedMax || (whole ? Math.max(4, Math.ceil(top / 4) * 4) : top), plotH = H - padB - padT, step = (W - padL) / labels.length;
    const n = series.length, gap = 2, bw = Math.max(2, Math.min(26, (step * 0.7 - gap * (n - 1)) / n));
    let g = '';
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - plotH * i / 4;
      g += `<line x1="${padL}" x2="${W}" y1="${y}" y2="${y}" stroke="${i ? 'var(--grid)' : 'var(--axis)'}" ${i ? 'stroke-dasharray="3 4"' : ''}/>`;
      g += `<text x="${padL - 8}" y="${y + 4}" font-size="11" text-anchor="end" fill="var(--chart-muted)">${fmt(max * i / 4)}</text>`;
    }
    const every = Math.ceil(labels.length / 10);
    labels.forEach((lab, i) => {
      const x0 = padL + i * step + (step - (bw * n + gap * (n - 1))) / 2;
      series.forEach((s, k) => {
        const v = s.values[i]; if (!v) return;
        const h = plotH * v / max, x = x0 + k * (bw + gap);
        g += `<path class="mark" d="${topRounded(x, padT + plotH - h, bw, h)}" fill="${s.color}" data-tip="<b>${esc(lab)}</b><br>${esc(s.label)}: ${esc(s.tips ? s.tips[i] : fmt(v))}"/>`;
      });
      if (i % every === 0) g += `<text x="${padL + i * step + step / 2}" y="${H - 8}" font-size="11" text-anchor="middle" fill="var(--chart-muted)">${esc(lab)}</text>`;
    });
    return `${series.length > 1 ? legend(series.map(s => [s.label, s.color])) : ''}<div class="a-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map(s => s.label).join(', '))}">${g}</svg></div>`;
  }
  /** Donut with a 2px surface gap between segments and a legend with values. */
  function donut(entries, centre, centreSub) {
    const total = entries.reduce((s, e) => s + e.n, 0);
    if (!total) return `<div class="empty">No data for this period</div>`;
    const R = 70, r = 46, cx = 85, cy = 85;
    let a = -Math.PI / 2, g = '';
    for (const e of entries) {
      if (!e.n) continue;
      const frac = e.n / total, a2 = a + frac * Math.PI * 2;
      const share = unit === 'pct' ? pct(frac) : `${nf(e.n)} (${pct(frac)})`;
      if (frac >= 0.9999) {
        g += `<circle class="mark" cx="${cx}" cy="${cy}" r="${(R + r) / 2}" fill="none" stroke="${e.color}" stroke-width="${R - r}" data-tip="<b>${esc(e.label)}</b><br>${share}"/>`;
      } else {
        const p = (rad, ang) => `${cx + rad * Math.cos(ang)},${cy + rad * Math.sin(ang)}`, large = frac > 0.5 ? 1 : 0;
        g += `<path class="mark" d="M${p(R, a)}A${R},${R} 0 ${large} 1 ${p(R, a2)}L${p(r, a2)}A${r},${r} 0 ${large} 0 ${p(r, a)}Z" fill="${e.color}"
               stroke="var(--card)" stroke-width="2" data-tip="<b>${esc(e.label)}</b><br>${share}"/>`;
      }
      a = a2;
    }
    g += `<text x="${cx}" y="${cy + 2}" text-anchor="middle" font-size="22" font-weight="600" fill="var(--ink)">${esc(centre)}</text>
          <text x="${cx}" y="${cy + 20}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(centreSub)}</text>`;
    const leg = entries.filter(e => e.n).map(e => `<div><i style="background:${e.color}"></i>${esc(e.label)}<b>${unit === 'pct' ? pct(e.n / total) : nf(e.n)}</b></div>`).join('');
    return `<div class="donut-wrap"><svg viewBox="0 0 170 170" role="img" aria-label="${esc(centreSub)}">${g}</svg><div class="donut-legend">${leg}</div></div>`;
  }
  /** Horizontal bars, one hue (a single measure across categories). */
  function hbars(entries, color = S(1), total) {
    if (!entries.length || !entries.some(e => e.n)) return `<div class="empty">No data for this period</div>`;
    const max = Math.max(...entries.map(e => e.n)) || 1, sum = total || entries.reduce((s, e) => s + e.n, 0);
    return `<div class="hbars">${entries.map(e => {
      const shown = unit === 'pct' && sum ? pct(e.n / sum) : nf(e.n);
      const fl = e.filter ? ` data-filter="${esc(e.filter)}" role="button" tabindex="0" title="Show these calls"` : '';
      return `<div class="hbar${e.filter ? ' click' : ''}${e.filter && e.filter === sentFilter ? ' active' : ''}"${fl}><span class="lab" title="${esc(e.label)}">${esc(e.label)}</span>
        <span class="track"><span class="fill mark" style="width:${100 * e.n / max}%;background:${e.color || color}" data-tip="<b>${esc(e.label)}</b><br>${shown}${e.tip ? '<br>' + esc(e.tip) : ''}"></span></span>
        <span class="num">${shown}</span></div>`;
    }).join('')}</div>`;
  }
  /** Two cohorts side by side per label (percent of each cohort). */
  function pairBars(rows, a, b) {
    if (!rows.length) return `<div class="empty">No data for this period</div>`;
    const max = Math.max(1, ...rows.flatMap(r => [r.achieved_pct, r.not_pct]));
    return legend([[a, S(1)], [b, S(2)]]) + `<div class="hbars">${rows.map(r => `<div class="hbar pair"><span class="lab">${esc(words(r.label))}</span>
      <span class="track"><span class="fill mark" style="width:${100 * r.achieved_pct / max}%;background:${S(1)}" data-tip="<b>${esc(words(r.label))}</b><br>${esc(a)}: ${r.achieved_pct}%"></span>
      <span class="fill mark" style="width:${100 * r.not_pct / max}%;background:${S(2)}" data-tip="<b>${esc(words(r.label))}</b><br>${esc(b)}: ${r.not_pct}%"></span></span>
      <span class="num">${r.achieved_pct}% · ${r.not_pct}%</span></div>`).join('')}</div>`;
  }
  const tiles = list => `<div class="tiles">${list.map(([k, v, sub, help]) =>
    `<div class="tile"><div class="k"${help ? ` title="${esc(help)}"` : ''}>${esc(k)}</div><div class="v">${v}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`).join('')}</div>`;
  const table = (cols, rows) => `<div class="table-wrap"><table><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${
    rows.length ? rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${cols.length}" class="empty-row">No data for this period</td></tr>`}</tbody></table></div>`;
  const dayLabel = d => new Date(d + 'T00:00:00+05:30').toLocaleDateString('en-IN', {timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short'});
  const hourLabel = h => `${h % 12 || 12}${h < 12 ? 'AM' : 'PM'}`;
  const outcomeColor = (o, i) => { const k = OUTCOME_ORDER.indexOf(o); return o === 'no record' ? 'var(--chart-muted)' : S((k >= 0 ? k : i) % 8 + 1); };
  const isOut = () => data.agent === 'outbound', isWeb = () => data.agent === 'website';
  const unitWord = () => isWeb() ? 'conversation' : 'call';

  /* ---------------------------------------------------------------- tabs */
  function overview() {
    const o = data.overview, days = o.series.map(s => dayLabel(s.date));
    // Inbound calls are all answered, so attempted = connected: one series.
    const daily = !isOut() ? vbars(days, [{label: 'Calls', color: S(1), values: o.series.map(s => s.connected)}])
      : unit === 'pct'
      ? vbars(days, [{label: 'Connected of attempted', color: S(1), values: o.series.map(s => s.attempted ? s.connected / s.attempted : null)}],
        {fmt: v => pct(v, 0), max: 1})
      : vbars(days, [{label: 'Attempted', color: S(1), values: o.series.map(s => s.attempted)}, {label: 'Connected', color: S(2), values: o.series.map(s => s.connected)}]);
    const outcomes = Object.entries(o.outcomes).map(([k, n], i) => ({label: words(k), n, color: outcomeColor(k, i)}));
    const failures = Object.entries(o.failure_reasons).map(([k, n]) => ({label: k, n}));
    return tiles([
      [isOut() ? 'Calls attempted' : 'Calls', nf(o.attempted)], ['Connected calls', nf(o.connected), o.attempted ? pct(o.connected / o.attempted) + ' of attempted' : ''],
      ['Latency', ms(o.latency_ms), 'median, caller stops → bot speaks', 'Per turn: caller stops speaking to bot starts speaking. Recorded from 21 Oct 2026.'],
      ['Avg call duration', dur(o.avg_duration_s)], ['Total minutes', nf(o.total_minutes)], ['Short calls', nf(o.short_calls), 'under 10 seconds'],
    ]) + card(unit === 'pct' && isOut() ? 'Connected share by day' : 'Calls by day', '', daily)
      + `<div class="grid2">${card('Call outcomes', isOut() ? 'From the call record of each connected call' : 'Call disposition from each call record', donut(outcomes, nf(o.connected), 'connected calls'))}
         ${card('Top failure reasons', isOut() ? 'Attempts that did not connect' : '', failures.length ? hbars(failures, S(2)) : '<div class="empty">No failure reasons for this period</div>')}</div>`;
  }

  function connectivity() {
    const c = data.connectivity, res = Object.entries(c.results).map(([k, n]) => ({label: k === 'connected' ? 'answered' : words(k), n, color: RESULT_COLOR[k] || S(7)}));
    const total = res.reduce((s, e) => s + e.n, 0);
    const byHour = c.by_hour.filter(h => h.attempted);
    const hours = unit === 'pct'
      ? vbars(byHour.map(h => hourLabel(h.hour)), [{label: 'Connectivity', color: S(1), values: byHour.map(h => h.connected / h.attempted)}], {fmt: v => pct(v, 0), max: 1})
      : vbars(byHour.map(h => hourLabel(h.hour)), [{label: 'Attempted', color: S(1), values: byHour.map(h => h.attempted)}, {label: 'Connected', color: S(2), values: byHour.map(h => h.connected)}]);
    const a = c.agent;
    const agentTable = table(['Agent name', 'Volume', 'Connected', 'Connectivity', 'Unique recipients', 'Unique connects', 'Avg duration (s)'],
      [[esc(a.agent), nf(a.volume), nf(a.connected), pct(a.connectivity), nf(a.unique_recipients), nf(a.unique_connects), a.avg_duration_s ?? '—']]);
    const campTable = table(['Campaign name', 'Attempted', 'Connected', 'Connectivity', 'Unique recipients', 'Unique connects', 'Avg duration (s)'],
      c.campaigns.map(r => [esc(r.campaign), nf(r.attempted), nf(r.connected), pct(r.connectivity), nf(r.unique_recipients), nf(r.unique_connects), r.avg_duration_s ?? '—']));
    return tiles([['Attempts', nf(total)], ['Connected', nf(c.results.connected || 0)], ['Connectivity', pct(c.rate)],
                  ['Unique recipients', nf(a.unique_recipients)], ['Unique connects', nf(a.unique_connects)]])
      + `<div class="grid2">${card('Call attempts', isOut() ? 'Every attempt by result' : 'Every inbound call is answered', donut(res, nf(total), 'total attempts'))}
         ${card('Failure reasons', '', Object.keys(c.failure_reasons).length ? hbars(Object.entries(c.failure_reasons).map(([k, n]) => ({label: k, n})), S(2)) : '<div class="empty">No failure reasons for this period</div>')}</div>`
      + card(unit === 'pct' ? 'Connectivity by hour of day (India time)' : 'Attempts by hour of day (India time)', '', hours)
      + card('Agents overview', '', agentTable)
      + (isOut() ? card('Campaign overview', '', campTable) : '');
  }

  function engagement() {
    const e = data.engagement, days = e.series.map(s => dayLabel(s.date));
    const daily = unit === 'pct'
      ? vbars(days, [{label: 'Engaged of connected', color: S(1), values: e.series.map(s => s.connected ? s.engaged / s.connected : null)}], {fmt: v => pct(v, 0), max: 1})
      : vbars(days, [{label: 'Engaged', color: S(1), values: e.series.map(s => s.engaged)}]);
    const ends = Object.entries(e.end_reasons).map(([k, n]) => ({label: k, n, color: END_COLOR[k]}));
    let inner = '';
    if (engTab === 'hour') {
      const hs = e.by_hour.filter(h => h.calls);
      inner = vbars(hs.map(h => hourLabel(h.hour)), [{label: 'Engagement rate', color: S(1), values: hs.map(h => h.rate),
        tips: hs.map(h => `${pct(h.rate)} (${h.engaged} of ${h.calls})`)}], {fmt: v => pct(v, 0), max: 1});
    } else if (engTab === 'attempt') {
      inner = isOut() ? vbars(e.by_attempt.map(b => `Attempt ${b.attempt}`), [{label: 'Engagement rate', color: S(1), values: e.by_attempt.map(b => b.rate),
        tips: e.by_attempt.map(b => `${pct(b.rate)} (${b.engaged} of ${b.calls})`)}], {fmt: v => pct(v, 0), max: 1})
        : '<div class="empty">Inbound calls have one attempt each</div>';
    } else if (engTab === 'turns') {
      inner = hbars(e.turns.map(t => ({label: `${t.bucket} caller turns`, n: t.n})));
    } else {
      inner = hbars(e.durations.map(t => ({label: t.bucket, n: t.n})));
    }
    const funnel = e.funnel.map((f, i) => ({label: f.stage, n: f.n, tip: i ? `${pct(e.funnel[i - 1].n ? f.n / e.funnel[i - 1].n : null)} of ${e.funnel[i - 1].stage.toLowerCase()}` : ''}));
    return tiles([['Engaged', nf(e.engaged), `of ${nf(e.connected)} connected · ${pct(e.connected ? e.engaged / e.connected : null)}`, 'The caller spoke at least twice'],
                  ['Avg call duration', dur(e.avg_duration_s)], ['Avg attempts to engage', e.avg_attempts_to_engage ?? '—'],
                  ['End-to-end latency', ms(e.latency_ms), 'median per turn']])
      + card(unit === 'pct' ? 'Engaged share by day' : 'Engaged calls by day', '', daily)
      + `<div class="grid2">${card('How calls ended', 'Agent ends: ended from our side (goodbye, silence, time limit, voicemail, transfer). Unknown: calls from before 21 Oct.', donut(ends, nf(e.connected), 'calls'))}
         ${card('Full pipeline funnel', 'Hover a bar for the share kept from the step before', hbars(funnel, S(1), e.funnel[0].n))}</div>`
      + card('Engagement insights', '', `<div class="subtabs inner" id="aEngTabs">${[['hour', 'By hour of day'], ['attempt', 'By # of attempts'], ['turns', 'Turn distribution'], ['duration', 'Duration distribution']]
          .map(([k, l]) => `<button data-e="${k}" class="${k === engTab ? 'on' : ''}">${l}</button>`).join('')}</div>${inner}`);
  }

  function tools() {
    const t = data.tools, days = t.series.map(s => dayLabel(s.date));
    const daily = vbars(days, [{label: 'Tool calls', color: S(1), values: t.series.map(s => s.tool_calls)}, {label: 'Errors', color: S(8), values: t.series.map(s => s.tool_errors)}]);
    const rows = t.table.map(r => [`<b>${esc(r.name)}</b>`, nf(r.calls), nf(r.errors), pct(r.calls ? r.errors / r.calls : null),
      esc(Object.entries(r.statuses).map(([k, n]) => `${words(k)} ${n}`).join(' · ')), ms(r.median_ms), ms(r.p95_ms)]);
    return tiles([['Tool calls', nf(t.calls)], ['Unique tools', nf(t.unique)], ['Errors', nf(t.errors), 'tool returned an error'],
                  ['Error rate', pct(t.error_rate)], ['Median latency', ms(t.median_ms), `${nf(t.timed_calls)} timed calls`],
                  ['p95 latency', ms(t.p95_ms)]])
      + card('Tool calls by day', 'Latency is recorded for calls from 21 Oct 2026; older calls count but are not timed.', daily)
      + card('Tools called', 'A failed date of birth or faculty ID check is not an error: the tool worked and the caller gave the wrong detail.',
        table(['Tool', 'Calls', 'Errors', 'Error rate', 'Results', 'Median latency', 'p95 latency'], rows));
  }

  function goals() {
    const g = data.goals, st = (s) => `<div class="stat-list"><span>Calls</span><span>${nf(s.calls)}</span><span>Avg call duration</span><span>${dur(s.avg_duration_s)}</span>
        <span>Avg turns per call</span><span>${s.avg_turns ?? '—'}</span><span>Median latency</span><span>${ms(s.avg_latency_ms)}</span></div>`;
    const hs = g.by_hour.filter(h => h.eligible);
    const byHour = vbars(hs.map(h => hourLabel(h.hour)), [{label: 'Goal achievement rate', color: S(1), values: hs.map(h => h.rate),
      tips: hs.map(h => `${pct(h.rate)} (${h.achieved} of ${h.eligible})`)}], {fmt: v => pct(v, 0), max: 1});
    const peak = hs.length ? hs.reduce((a, b) => (b.rate > a.rate ? b : a)) : null, trough = hs.length ? hs.reduce((a, b) => (b.rate < a.rate ? b : a)) : null;
    return `<p class="note" style="margin:-6px 0 12px">Goal: ${esc(g.goal)}. Eligible: connected calls with a call record.</p>`
      + tiles([['Goal achievement rate', pct(g.rate), `${nf(g.achieved)} of ${nf(g.eligible)} eligible`], ['Avg turns to goal', g.avg_turns_to_goal ?? '—', `${g.avg_turns_not ?? '—'} turns when not achieved`]])
      + `<div class="grid2">${card('Goal achieved', '', st(g.achieved_stats))}${card('Goal not achieved', '', st(g.not_achieved_stats))}</div>`
      + card('Goal achievement by time of day', `Hour of day, India time${peak ? ` · peak ${hourLabel(peak.hour)} (${pct(peak.rate)}) · trough ${hourLabel(trough.hour)} (${pct(trough.rate)})` : ''}`, byHour)
      + `<div class="grid2">${card('Voice × goal rate', 'One voice for every call', table(['Voice', 'Calls', 'Goal rate'], g.voice.map(v => [esc(v.voice), nf(v.calls), pct(v.rate)])))}
         ${card('Intro message × goal rate', '', `<p class="intro-text">${esc(g.intro[0].text)}</p><p class="note" style="margin-top:8px">${pct(g.intro[0].rate)} · ${nf(g.intro[0].calls)} calls</p>`)}</div>`
      + `<div class="grid2">${card('Turn distribution by cohort', 'Share of each cohort', pairBars(g.turns_by_cohort, 'Achieved', 'Not achieved'))}
         ${card('Language split by cohort', 'Share of each cohort', pairBars(g.language_by_cohort, 'Achieved', 'Not achieved'))}</div>`;
  }

  /* Sentiment: the caller's side as judged by Sarvam 105B after the call (sentiment.py). Polarity
     colours: positive and negative from the palette, neutral in the muted grey midpoint. */
  const SENT_COLOR = {positive: S(3), neutral: 'var(--chart-muted)', negative: S(8)};
  let sentJob = null, sentTimer = null;
  /** The calls behind a clicked interest level or topic. */
  function filteredCalls(s) {
    if (!sentFilter) return '<p class="note" style="margin:0 0 12px">Click an admission-interest level or a topic to list those ' + unitWord() + 's.</p>';
    const [kind, key] = sentFilter.split(':');
    const hit = (s.items || []).filter(c => kind === 'interest' ? c.interest === key : (c.topics || []).includes(key));
    const view = id => isWeb() ? '' : `<button class="link" data-call="${esc(id)}">View</button>`;
    const title = kind === 'interest' ? `${isWeb() ? 'Conversations' : 'Calls'} with ${key} ${isOut() ? 'interest' : 'admission interest'}` : `${isWeb() ? 'Conversations' : 'Calls'} that raised "${key}"`;
    const who = c => isWeb() ? [] : [esc(c.lead_name || words(c.role) || '—')];
    const body = hit.length ? table(['Start time (IST)', isWeb() ? 'Channel' : 'Caller', ...(isWeb() ? [] : [isOut() ? 'Lead' : 'Role']), 'Language', 'Interest', 'Sentiment', 'Summary', ''],
      hit.map(c => [esc(fmtDate(c.started_at)), esc(isWeb() ? words(c.role) : (c.caller || '—')), ...who(c), esc(words(c.language)), esc(c.interest),
        `<span class="sent ${c.sentiment}">${c.sentiment}</span>`, esc(c.summary), view(c.id)])) : '<div class="empty">No ${unitWord()}s</div>';
    return `<div id="aSentList">${card(title, `${nf(hit.length)} ${unitWord()}${hit.length === 1 ? '' : 's'} · <button class="link" id="aSentClear">Clear</button>`, `<div class="wrap-cols">${body}</div>`)}</div>`;
  }
  function sentiment() {
    const s = data.sentiment, days = s.series.map(x => dayLabel(x.date)), n = s.analysed;
    const share = k => n ? pct(k / n, 0) + ' of analysed' : '';
    const daily = unit === 'pct'
      ? vbars(days, [{label: 'Negative share', color: SENT_COLOR.negative, values: s.series.map(x => { const t = x.positive + x.neutral + x.negative; return t ? x.negative / t : null; })}], {fmt: v => pct(v, 0), max: 1})
      : vbars(days, ['positive', 'neutral', 'negative'].map(k => ({label: k[0].toUpperCase() + k.slice(1), color: SENT_COLOR[k], values: s.series.map(x => x[k])})));
    const sent = ['positive', 'neutral', 'negative'].map(k => ({label: k, n: s.sentiment[k], color: SENT_COLOR[k]}));
    const trend = ['improved', 'steady', 'worsened'].map(k => ({label: k, n: s.trend[k] || 0,
      color: k === 'worsened' ? SENT_COLOR.negative : k === 'improved' ? SENT_COLOR.positive : 'var(--chart-muted)'}));
    const interest = ['high', 'medium', 'low', 'none'].map(k => ({label: k, n: s.interest[k] || 0, filter: 'interest:' + k}));
    const topics = Object.entries(s.topics).map(([k, v]) => ({label: k, n: v, filter: 'topic:' + k}));
    const split = rows => table(['', 'Calls', 'Positive', 'Neutral', 'Negative'],
      rows.map(r => [esc(words(r.label) || '—'), nf(r.calls), nf(r.positive), nf(r.neutral),
        `${nf(r.negative)}${r.negative ? ` <span class="dim">· ${pct(r.negative / r.calls, 0)}</span>` : ''}`]));
    const view = id => isWeb() ? '' : `<button class="link" data-call="${esc(id)}">View</button>`;
    const qs = table(['Question', 'Calls', ''], s.questions.map(q => [esc(q.question), nf(q.count), q.call_ids.slice(0, 3).map(view).join(' ')]));
    const flagged = table(['Start time (IST)', isOut() ? 'Lead' : 'Role', 'Language', 'Sentiment', 'Mood', 'Frustration', 'Summary', ''],
      s.flagged.map(f => [esc(fmtDate(f.started_at)), esc(f.lead_name || words(f.role) || '—'), esc(words(f.language)),
        `<span class="sent ${f.sentiment}">${f.sentiment}</span>`, esc(f.trend), f.frustrated ? esc(f.reason || 'yes') : '', esc(f.summary), view(f.id)]));
    const running = sentJob && sentJob.running;
    const job = running ? `Analysing… ${nf(sentJob.done)} of ${nf(sentJob.total)} conversations`
      : s.not_analysed ? `${nf(s.not_analysed)} ${unitWord()}${s.not_analysed === 1 ? '' : 's'} in this range not analysed yet` : `Every ${unitWord()} in this range is analysed`;
    return `<div class="sent-bar"><p class="note">Sarvam 105B's reading of the ${isWeb() ? "visitor's side of each website chat and voice conversation, made after it ends" : "caller's side of each call, made right after the call"}. Not yet checked against calls labelled by hand (English or Tamil): use it to pick calls to listen to, not as a score.</p>
        <div class="sent-actions"><span class="dim" id="aSentJob">${job}${sentJob && sentJob.error ? ` · ${esc(sentJob.error)}` : ''}</span>
        <button class="btn" id="aSentUpdate" ${running ? 'disabled' : ''} title="Analyse the calls not yet analysed and rebuild the full report (website and phone) for these dates">Update</button>
        <a class="btn" href="/test/sentiment-report" target="_blank" rel="noopener">Full report: website and phone</a></div></div>`
      + tiles([[isWeb() ? 'Conversations analysed' : 'Calls analysed', nf(n), `of ${nf(s.calls)} where the ${isWeb() ? 'visitor' : 'caller'} spoke`], ['Positive', nf(s.sentiment.positive), share(s.sentiment.positive)],
               ['Negative', nf(s.sentiment.negative), share(s.sentiment.negative)], ['Showed frustration', nf(s.frustrated), share(s.frustrated)],
               [isOut() ? 'High interest' : 'High admission interest', nf(s.interest.high || 0), share(s.interest.high || 0)],
               ['With an unanswered question', nf(s.with_unanswered), share(s.with_unanswered)]])
      + card(unit === 'pct' ? 'Negative share by day' : 'Sentiment by day', '', daily)
      + `<div class="grid2">${card('Overall sentiment', isWeb() ? 'By the end of the conversation' : 'By the end of the call', donut(sent, nf(n), unitWord() + 's analysed'))}
         ${card('Mood from start to end', '', hbars(trend, S(1), n))}</div>`
      + `<div class="grid2">${card(isOut() ? 'Interest in joining' : 'Admission interest', '', hbars(interest, S(1), n))}
         ${card('Topics raised', 'A call can raise several', hbars(topics, S(1), n))}</div>`
      + `<div class="grid2">${card(isOut() ? 'By lead' : isWeb() ? 'By channel' : 'By caller role', '', split(s.by_role))}${card('By language', '', split(s.by_language))}</div>`
      + filteredCalls(s)
      + card('Questions the bot could not answer', 'As judged from the transcript; it sometimes lists questions the bot did answer, so open the call to check.', `<div class="wrap-cols">${qs}</div>`)
      + card('Negative or frustrated calls', 'Newest first', `<div class="wrap-cols">${flagged}</div>`);
  }
  async function sentUpdate() {
    const p = new URLSearchParams({date_from: $('aFrom').value, date_to: $('aTo').value});
    try {
      const r = await fetch(`/test/api/sentiment/update?${p}`, {method: 'POST'}); sentJob = await r.json();
      if (!r.ok) sentJob = {error: sentJob.detail || 'failed'};
    } catch (e) { sentJob = {error: e.message}; }
    render(); sentPoll();
  }
  function sentPoll() {
    clearTimeout(sentTimer);
    if (!sentJob || !sentJob.running) return;
    sentTimer = setTimeout(async () => {
      try { sentJob = await (await fetch('/test/api/sentiment/status')).json(); } catch (e) { /* try again */ }
      if (sentJob.running) {
        const j = $('aSentJob'); if (j) j.textContent = `Analysing… ${nf(sentJob.done)} of ${nf(sentJob.total)} conversations`;
        sentPoll();
      } else load();
    }, 2000);
  }

  const DIMS = {language: 'Language', role: 'Caller role', campaign: 'Campaign', outcome: 'Outcome', ended_by: 'How it ended', day: 'Day', hour: 'Hour of day'};
  function group() {
    const key = r => groupBy === 'day' ? (r.started_at ? new Date(r.started_at).toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'}) : '—')
      : groupBy === 'hour' ? (r.started_at ? hourLabel(+new Date(r.started_at).toLocaleString('en-GB', {timeZone: 'Asia/Kolkata', hour: '2-digit', hour12: false})) : '—')
      : words(r[groupBy]) || '—';
    const groups = new Map();
    for (const r of data.calls) { const k = key(r); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
    const rows = [...groups.entries()].sort((a, b) => b[1].length - a[1].length).map(([k, rs]) => {
      const conn = rs.filter(r => r.connected), eng = conn.filter(r => r.turns >= 2), elig = conn.filter(r => r.goal != null), ach = elig.filter(r => r.goal);
      const durs = conn.map(r => r.duration_s).filter(x => x != null), lats = conn.map(r => r.latency_ms).filter(x => x != null).sort((a, b) => a - b);
      return [esc(k), nf(rs.length), nf(conn.length), pct(rs.length ? conn.length / rs.length : null), nf(eng.length),
        pct(elig.length ? ach.length / elig.length : null), dur(durs.length ? durs.reduce((a, b) => a + b, 0) / durs.length : null),
        ms(lats.length ? lats[Math.floor(lats.length / 2)] : null)];
    });
    return `<div class="group-ctl"><span class="dim">Group by</span><select id="aGroupBy">${Object.entries(DIMS).filter(([k]) => isOut() || k !== 'campaign')
      .map(([k, l]) => `<option value="${k}" ${k === groupBy ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`
      + card(`By ${DIMS[groupBy].toLowerCase()}`, '', table([DIMS[groupBy], isOut() ? 'Attempts' : 'Calls', 'Connected', 'Connectivity', 'Engaged', 'Goal rate', 'Avg duration', 'Median latency'], rows));
  }

  function logs() {
    const rows = data.calls.filter(r => logsAll || r.connected);
    const body = rows.slice(0, 300).map(r => [
      `<span class="call-kind ${r.connected ? '' : 'no'}">${r.connected ? 'Live call' : 'Not connected'}</span>`,
      `<span class="idcell"><span>${esc(r.id.replace('phone-test-', '').slice(0, 14))}…</span></span>`,
      esc((r.caller || '').replace(/^X+/, '••••••') || '—'), esc([r.lead_name, r.campaign].filter(Boolean).join(' · ') || words(r.role) || '—'),
      r.duration_s != null ? `${r.duration_s.toFixed ? r.duration_s.toFixed(1) : r.duration_s}` : '—', esc(fmtDate(r.started_at)),
      esc(words(r.outcome) || '—') + (r.failure_reason ? ` <span class="dim">· ${esc(r.failure_reason)}</span>` : ''),
      r.connected ? `<button class="link" data-call="${esc(r.id)}">View</button>` : '']);
    return `<div class="group-ctl"><div class="seg" id="aLogsMode"><button data-m="c" class="${logsAll ? '' : 'on'}">Connected</button><button data-m="a" class="${logsAll ? 'on' : ''}">All</button></div>
        <span class="spacer" style="flex:1"></span><button class="btn" id="aCsv">Download CSV</button></div>`
      + card(`${nf(rows.length)} ${logsAll ? 'calls and attempts' : 'connected calls'}`, rows.length > 300 ? 'Showing the newest 300; the CSV has all of them.' : '',
        table(['Call type', 'Interaction ID', 'User', 'Lead / role', 'Duration (s)', 'Start time (IST)', 'Outcome', ''], body));
  }

  /* ---------------------------------------------------------------- loading and filters */
  function render() {
    if (!data) return;
    const html = {overview, connectivity, engagement, tools, goals, sentiment, group, logs}[tab]();
    $('aBody').innerHTML = html;
    document.querySelectorAll('#aEngTabs button').forEach(b => b.onclick = () => { engTab = b.dataset.e; render(); });
    const gb = $('aGroupBy'); if (gb) gb.onchange = () => { groupBy = gb.value; render(); };
    document.querySelectorAll('#aLogsMode button').forEach(b => b.onclick = () => { logsAll = b.dataset.m === 'a'; render(); });
    document.querySelectorAll('#aBody [data-call]').forEach(b => b.onclick = () => openDetails(b.dataset.call));
    const csv = $('aCsv'); if (csv) csv.onclick = downloadCsv;
    document.querySelectorAll('#aBody .hbar.click').forEach(b => {
      const pick = () => { sentFilter = sentFilter === b.dataset.filter ? null : b.dataset.filter; render();
        const t = $('aSentList'); if (t) t.scrollIntoView({behavior: 'smooth', block: 'start'}); };
      b.onclick = pick; b.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
    });
    const sc = $('aSentClear'); if (sc) sc.onclick = () => { sentFilter = null; render(); };
    const su = $('aSentUpdate'); if (su) su.onclick = sentUpdate;
  }
  function downloadCsv() {
    const cols = ['kind', 'id', 'started_at', 'duration_s', 'caller', 'lead_name', 'campaign', 'language', 'role', 'outcome', 'goal', 'ended_by', 'turns', 'latency_ms', 'sentiment', 'failure_reason'];
    const q = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
    const rows = data.calls.filter(r => logsAll || r.connected).map(r => cols.map(c => q(r[c])).join(','));
    const a = el('a'); a.href = URL.createObjectURL(new Blob([[cols.join(','), ...rows].join('\n')], {type: 'text/csv'}));
    a.download = `call_logs_${data.agent}_${data.from}_to_${data.to}.csv`; a.click(); URL.revokeObjectURL(a.href);
  }
  function fillSelect(id, values, label, keep) {
    const s = $(id), cur = s.value; s.innerHTML = `<option value="">${label}</option>`;
    for (const v of values) { const [val, text] = Array.isArray(v) ? v : [v, words(v)]; s.appendChild(new Option(text, val)); }
    s.value = keep && [...s.options].some(o => o.value === cur) ? cur : '';
  }
  async function load() {
    const p = new URLSearchParams({agent: $('aAgent').value, date_from: $('aFrom').value, date_to: $('aTo').value,
      campaign: $('aCampaign').value, language: $('aLanguage').value, role: $('aRole').value});
    $('aBody').style.opacity = .5;
    try {
      const r = await fetch(`/test/api/analytics?${p}`); data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'failed');
    } catch (e) { $('aBody').innerHTML = `<div class="empty-row">Could not load analytics: ${esc(e.message)}</div>`; $('aBody').style.opacity = 1; return; }
    $('aBody').style.opacity = 1;
    if (!$('aFrom').value) $('aFrom').value = data.from;
    if (!$('aTo').value) $('aTo').value = data.to;
    fillSelect('aCampaign', data.filters.campaigns, 'All campaigns', true);
    fillSelect('aLanguage', data.filters.languages, 'All languages', true);
    fillSelect('aRole', data.filters.roles, isWeb() ? 'All channels' : 'All roles', true);
    $('aCampaign').hidden = !isOut(); $('aRole').hidden = isOut();
    document.querySelectorAll('#aTabs button').forEach(b => { b.hidden = isWeb() && b.dataset.t !== 'sentiment'; });
    if (isWeb() && tab !== 'sentiment') { tab = 'sentiment'; document.querySelectorAll('#aTabs button').forEach(x => x.classList.toggle('on', x.dataset.t === 'sentiment')); }
    render();
  }
  const istToday = () => new Date().toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'});
  document.querySelectorAll('#aRange button').forEach(b => b.onclick = () => {
    document.querySelectorAll('#aRange button').forEach(x => x.classList.toggle('on', x === b));
    const d = +b.dataset.d;
    $('aTo').value = d ? istToday() : '';
    $('aFrom').value = d ? new Date(Date.now() - (d - 1) * 86400000).toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'}) : '';
    load();
  });
  ['aFrom', 'aTo'].forEach(id => $(id).onchange = () => { document.querySelectorAll('#aRange button').forEach(x => x.classList.remove('on')); load(); });
  ['aCampaign', 'aLanguage', 'aRole'].forEach(id => $(id).onchange = load);
  $('aAgent').onchange = () => { $('aCampaign').value = $('aLanguage').value = $('aRole').value = ''; $('aFrom').value = $('aTo').value = '';
    document.querySelectorAll('#aRange button').forEach(x => x.classList.toggle('on', x.dataset.d === '0')); load(); };
  document.querySelectorAll('#aUnit button').forEach(b => b.onclick = () => {
    unit = b.dataset.u; document.querySelectorAll('#aUnit button').forEach(x => x.classList.toggle('on', x === b)); render();
  });
  document.querySelectorAll('#aTabs button').forEach(b => b.onclick = () => {
    tab = b.dataset.t; document.querySelectorAll('#aTabs button').forEach(x => x.classList.toggle('on', x === b)); render();
  });

  window.Analytics = {show: async () => {
    // A report started earlier (or from another window) may still be running: pick its progress up.
    try { sentJob = await (await fetch('/test/api/sentiment/status')).json(); } catch (e) { sentJob = null; }
    await load(); sentPoll();
  }};
})();
