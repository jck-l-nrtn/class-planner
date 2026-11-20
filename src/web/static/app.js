
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const state = {
  data: null,
  tab: 'today',
  hidden: new Set(),
  why: new Set(),
  form: null,
  menu: null,
  jobs: {},
  filters: { course: '', horizon: '14', sort: 'date' },
  eventsShown: 30,
  drawer: false,
};

function load(key, fallback) {
  try { const v = localStorage.getItem('planner.' + key); return v ? JSON.parse(v) : fallback; }
  catch (e) { return fallback; }
}
function save(key, v) {
  try { localStorage.setItem('planner.' + key, JSON.stringify(v)); } catch (e) { }
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const WDL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MO = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const fmtDay = d => `${WD[d.getDay()]} ${MO[d.getMonth()]} ${d.getDate()}`;
const fmtBand = d => `${WD[d.getDay()]}, ${MO[d.getMonth()]} ${d.getDate()}`;
const fmtMD = d => `${MO[d.getMonth()]} ${d.getDate()}`;
function fmtTime(d) {
  const h = d.getHours(), m = d.getMinutes();
  return `${h % 12 || 12}:${String(m).padStart(2, '0')} ${h < 12 ? 'am' : 'pm'}`;
}

const hasTime = d => !((d.getHours() === 0 && d.getMinutes() === 0) || (d.getHours() === 23 && d.getMinutes() === 59));
let tzFmt = null;
try { tzFmt = new Intl.DateTimeFormat('en-US', { timeZoneName: 'short' }); } catch (e) { }
function tzOf(d) {
  try { const p = tzFmt && tzFmt.formatToParts(d).find(x => x.type === 'timeZoneName'); return p ? p.value : ''; }
  catch (e) { return ''; }
}
function dayDiff(d) {
  const [y, m, dd] = state.data.today.split('-').map(Number);
  return Math.round((Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) - Date.UTC(y, m - 1, dd)) / 864e5);
}
function relDays(n) {
  if (n === 0) return 'today';
  if (n === 1) return 'tomorrow';
  if (n === -1) return 'yesterday';
  return n > 1 ? `in ${n} days` : `${-n} days ago`;
}
function fmtMin(m) {
  m = Math.round(m || 0);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h${m % 60 ? ' ' + (m % 60) + 'm' : ''}`;
}
const pctNum = p => (p >= 1 ? p.toFixed(1) : p.toFixed(2)).replace(/\.?0+$/, '');
const fmtPct = p => p ? `${pctNum(p)}% of grade` : 'no grade weight';
const safeLink = u => /^https?:\/\//i.test(u || '') ? u : '';
function fmtStamp(iso) {
  const d = new Date(iso);
  const n = dayDiff(d);
  const day = n === 0 ? 'Today' : n === -1 ? 'Yesterday' : fmtDay(d);
  return `${day}, ${fmtTime(d)}`;
}
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const todayDate = () => { const [y, m, dd] = state.data.today.split('-').map(Number); return new Date(y, m - 1, dd); };

function termLabel(d) {
  const m = d.getMonth();
  return `${m < 4 ? 'Winter' : m < 6 ? 'Spring' : m < 8 ? 'Summer' : 'Fall'} ${d.getFullYear()}`;
}

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  let j = {};
  try { j = await r.json(); } catch (e) { }
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

async function refresh() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.data = await r.json();
    state.hidden.clear();
    await loadRange();
    $('#offline').hidden = true;
  } catch (e) {
    $('#offline').hidden = false;
  }
  $('#loading').hidden = !!state.data || !$('#offline').hidden;
  render();
}

const findTask = uid => state.data.tasks.find(t => t.uid === uid) ||
  state.data.attention.missing.find(t => t.uid === uid);

async function taskAction(uid, action, extra = {}, opts = {}) {
  const t = findTask(uid) || { title: opts.title || 'task' };
  if (opts.hide) { state.hidden.add(uid); render(); }
  try {
    await post('/api/task', { uid, action, ...extra });
    if (opts.toast) toast(opts.toast, opts.undo ? () => undoMark(uid, t.title, opts.wasPlanned) : null);
  } catch (e) {
    state.hidden.delete(uid);
    toast(`Couldn't ${action} "${t.title}": ${e.message}`);
  }
  await refresh();
}

async function undoMark(uid, title, wasPlanned) {
  try {
    await post('/api/task', { uid, action: 'undone' });

    toast(wasPlanned ? `Restored "${title}". It's in Upcoming until the next replan.` : `Restored "${title}".`);
  } catch (e) {
    toast(`Undo failed for "${title}": ${e.message}`);
  }
  await refresh();
}

function markDone(uid, confirmed) {
  const t = findTask(uid);
  if (!t) return;
  if (t.confirm_done && !confirmed) { state.form = { uid, kind: 'confirm' }; state.menu = null; render(); focusForm(); return; }
  state.form = null;
  const wasPlanned = !!(state.data.plan && state.data.plan.ranked.some(r => r.uid === uid));
  taskAction(uid, 'done', {}, { hide: true, toast: `Marked "${t.title}" done.`, undo: true, wasPlanned });
}

function toast(msg, onUndo) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<span>${esc(msg)}</span>` +
    (onUndo ? '<button type="button" class="t-undo">Undo</button>' : '') +
    '<button type="button" class="t-x" aria-label="Dismiss">' + ICON_X + '</button>';
  const close = () => { clearTimeout(timer); el.remove(); };
  const timer = setTimeout(close, onUndo ? 10000 : 5000);
  el.querySelector('[aria-label=Dismiss]').onclick = close;
  if (onUndo) el.querySelector('.t-undo').onclick = () => { close(); onUndo(); };
  const box = $('#toasts');
  box.appendChild(el);
  while (box.children.length > 3) box.firstChild.remove();
}
const ICON_X = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M1.5 1.5l9 9m0-9l-9 9" stroke="currentColor" stroke-width="1.6"/></svg>';
const ICON_CHECK = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M2.5 7.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
const ICON_CARET = '<svg class="caret" width="8" height="5" viewBox="0 0 8 5" aria-hidden="true"><path d="M0 0h8L4 5z" fill="currentColor"/></svg>';

const JOB_LABEL = { sync: 'Sync', ls_refresh: 'Grade refresh', ls_login: 'LearningSuite sign-in', replan: 'Replan' };

const JOB_SECS = { sync: 20, ls_refresh: 25, replan: 90 };
const jobStart = {};
let jobTick = null;

function renderJobBar() {
  const el = $('#job-bar');
  const names = Object.keys(state.jobs).filter(n => JOB_SECS[n]);
  el.hidden = !names.length;
  el.querySelectorAll('[data-bar]').forEach(r => { if (!names.includes(r.dataset.bar)) r.remove(); });
  if (!names.length) { clearInterval(jobTick); jobTick = null; return; }
  for (const n of names) {
    let row = el.querySelector(`[data-bar="${n}"]`);
    if (!row) {
      el.insertAdjacentHTML('beforeend', `<div class="pbar-row" data-bar="${n}"><div class="pbar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-label="${JOB_LABEL[n]}"><i></i></div><span></span></div>`);
      row = el.lastElementChild;
    }
    const s = (Date.now() - jobStart[n]) / 1000;
    const pct = Math.round(Math.min(0.95, 1 - Math.exp(-1.6 * s / JOB_SECS[n])) * 100);
    row.firstChild.setAttribute('aria-valuenow', pct);
    row.firstChild.firstChild.style.width = pct + '%';
    row.lastChild.textContent = `${JOB_LABEL[n]} · ${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  }
  if (!jobTick) jobTick = setInterval(renderJobBar, 1000);
}

async function runJob(name) {
  if (state.jobs[name]) return;
  try {
    const { job } = await post('/api/job', { name });
    state.jobs[name] = job;
    jobStart[name] = Date.now();
    renderJobs();
    pollJob(name, job);
  } catch (e) {
    showJobError(name, e.message);
  }
}

async function pollJob(name, id) {
  let j;
  try {
    const r = await fetch('/api/job?id=' + encodeURIComponent(id), { cache: 'no-store' });
    j = await r.json();
  } catch (e) { j = { state: 'running' }; }
  if (j.state === 'running') { setTimeout(() => pollJob(name, id), 1500); return; }
  delete state.jobs[name];
  renderJobs();
  if (j.state === 'done') toast(`${JOB_LABEL[name]} finished.`);
  else showJobError(name, j.output || `job state: ${j.state}`);
  refresh();
}

function showJobError(name, output) {
  const tail = String(output).trim().split('\n').slice(-25).join('\n');
  const el = $('#job-error');
  el.innerHTML = `<div class="panel-head"><strong>${esc(JOB_LABEL[name] || name)} failed</strong>
    <button type="button" class="lnk" data-act="close-error">Dismiss</button></div><pre>${esc(tail)}</pre>`;
  el.hidden = false;
}

function renderJobs() {
  document.querySelectorAll('[data-job]').forEach(b => {
    const busy = !!state.jobs[b.dataset.job];
    b.classList.toggle('busy', busy);
    b.setAttribute('aria-busy', busy);
    b.disabled = busy;
  });
  renderJobBar();
}

function lastSynced() {
  const UNIT = { s: 1 / 60, m: 1, h: 60, d: 1440 };
  let best = null, bestMin = Infinity;
  for (const s of state.data.attention.sources) {
    const v = String(s.last_ok || '');
    if (!v || v === 'never') continue;
    const m = v.match(/(\d+(?:\.\d+)?)\s*([smhd])/i);
    const mins = /just now/i.test(v) ? 0 : m ? +m[1] * UNIT[m[2].toLowerCase()] : Infinity;
    if (mins < bestMin || best === null) { best = v; bestMin = mins; }
  }
  return best;
}

const absDate = d => fmtDay(d) + (hasTime(d) ? ', ' + fmtTime(d) : '');

function dueCell(t, banded) {
  if (!t.due) return '<span class="muted">No due date</span>';
  const d = new Date(t.due), n = dayDiff(d);
  const eod = d.getHours() === 23 && d.getMinutes() === 59;
  const tm = hasTime(d) || eod ? `${fmtTime(d)} ${tzOf(d)}`.trim() : '';
  let h = `<span class="d">${fmtMD(d)}</span>${tm ? ` <span class="tm">${esc(tm)}</span>` : ''}`;
  if (t.overdue) h += `<span class="rel red">${n === 0 ? 'earlier today' : relDays(n)}</span>`;
  else if (!banded) h += `<span class="rel">${relDays(n)}</span>`;
  return h;
}

const WEIGHT_TAG = {
  gradebook: ['exact', 'Weight from the course gradebook'],
  canvas: ['exact', 'Weight from Canvas'],
  manual: ['hand-set', 'Weight you entered by hand'],
  proxy: ['proxy', 'Stand-in weight: this prep work is not graded itself, so it borrows weight from what it prepares you for'],
  config: ['estimated', 'Estimated from the course configuration, not a real gradebook weight'],
};

function gradeCell(t) {
  if (!t.impact_pct) return '<span class="of" title="No grade weight">None</span>';
  const [tag, tip] = WEIGHT_TAG[t.weight_source] || [t.weight_label || t.weight_source, ''];
  return `<b>${pctNum(t.impact_pct)}%</b><span class="sr"> of grade</span>` +
    (tag ? ` <span class="of prov"${tip ? ` title="${esc(tip)}"` : ''}>${esc(tag)}</span>` : '');
}

function estCell(t, ctx) {
  if (ctx === 'today' && t.suggested_minutes) {
    const more = t.remaining > t.suggested_minutes + 1;
    return `<b>${fmtMin(t.suggested_minutes)}</b>${more ? ` <span class="of">/${fmtMin(t.remaining)}</span>` : ''}`;
  }
  if (t.spent) return `<b>${fmtMin(t.remaining)}</b> <span class="of">/${fmtMin(t.est)}</span>`;
  return `<b>${fmtMin(t.est)}</b>`;
}
function estTip(t, ctx) {
  if (ctx === 'today' && t.suggested_minutes) {
    return t.remaining > t.suggested_minutes + 1
      ? `${fmtMin(t.suggested_minutes)} today, of ${fmtMin(t.remaining)} left` : `${fmtMin(t.suggested_minutes)} today`;
  }
  return t.spent ? `${fmtMin(t.remaining)} left of a ${fmtMin(t.est)} estimate` : `Estimate ${fmtMin(t.est)}`;
}

function statusBits(t) {
  const s = (cls, txt, tip) => `<span class="st${cls ? ' ' + cls : ''}"${tip ? ` title="${esc(tip)}"` : ''}>${txt}</span>`;
  const out = [];
  if (t.due) {
    const n = dayDiff(new Date(t.due));
    if (t.overdue) out.push(s('red', 'Overdue'));
    else if (n === 0) out.push(s('strong', 'Due today'));
  }
  if (t.behind) out.push(s('red', t.slack_hours != null ? `Behind ${Math.abs(t.slack_hours).toFixed(1)}h` : 'Behind',
    'Not enough free time left before the due date for the remaining work'));
  if (t.in_progress) out.push(s('strong', 'In progress'));

  if (!t.hard_deadline && t.due) out.push(s('', 'Late OK', 'Late submissions are accepted'));
  if (t.prep_by) {
    const d = new Date(t.prep_by), n = dayDiff(d);
    const passed = d < new Date(state.data.now);
    out.push(s(passed ? 'red' : '', `Prep by ${esc(absDate(d))}`,
      `A soft earlier target, e.g. read before class. ${passed ? 'Passed ' + (n === 0 ? 'today' : relDays(n)) : relDays(n)}.`));
  }
  return out;
}

const EST_BY = { you: 'you', agent: 'the estimator', default: 'a default for this kind of work' };
function whyPanel(t) {
  const reasons = (t.reasons || []).map(r => `<li>${esc(r)}</li>`).join('');
  return `<div class="detail why" id="why-${esc(t.uid)}">
    ${reasons ? `<ul>${reasons}</ul>` : ''}
    <dl>
      ${t.note ? `<dt>Estimator</dt><dd>${esc(t.note)}</dd>` : ''}
      <dt>Estimate</dt><dd>${fmtMin(t.est)}, set by ${esc(EST_BY[t.est_by] || t.est_by)}. Spent ${fmtMin(t.spent)} so far.</dd>
      ${t.source_note ? `<dt>Source</dt><dd>${esc(t.source_note)}</dd>` : ''}
      ${t.missing_since ? `<dt>Missing</dt><dd>Not listed by its source since ${esc(t.missing_since)}.</dd>` : ''}
    </dl>
  </div>`;
}

function rowForm(t) {
  const f = state.form;
  if (!f || f.uid !== t.uid) return '';
  if (f.kind === 'confirm') {
    const why = t.kind === 'exams' ? 'This is an exam.' : `This is worth ${fmtPct(t.impact_pct)}.`;
    return `<div class="detail inline-form confirm" role="group" aria-label="Confirm done">
      <span>${esc(why)} Mark it done?</span>
      <span class="form-btns"><button type="button" class="btn go" data-act="done-yes" data-uid="${esc(t.uid)}">Mark done</button>
      <button type="button" class="lnk" data-act="cancel">Cancel</button></span></div>`;
  }
  const isLog = f.kind === 'log';
  return `<form class="detail inline-form" data-form="${f.kind}" data-uid="${esc(t.uid)}">
    <label>${isLog ? 'Minutes spent' : 'Total estimate (minutes)'}
      <input name="minutes" type="number" inputmode="numeric" required
        min="${isLog ? 1 : 5}" max="${isLog ? 1440 : 3000}" value="${isLog ? '' : esc(t.est)}"></label>
    <span class="form-btns"><button type="submit" class="btn go">${isLog ? 'Log' : 'Save'}</button>
    <button type="button" class="lnk" data-act="cancel">Cancel</button></span></form>`;
}

function moreMenu(t, ctx) {
  if (state.menu !== t.uid) return '';
  const u = esc(t.uid);
  const item = (act, label) => `<button type="button" role="menuitem" data-act="${act}" data-uid="${u}">${label}</button>`;
  return `<div class="menu" role="menu">
    ${t.in_progress ? item('stop', 'Not started') : item('start', 'Start')}
    ${item('estimate', 'Change estimate')}
    ${ctx === 'today' ? item('dismiss', 'Not today') : ''}
    ${item('drop', 'Drop (not required)')}
  </div>`;
}

function taskRow(t, ctx, banded) {
  if (state.hidden.has(t.uid)) return '';
  const u = esc(t.uid), link = safeLink(t.link), tt = esc(t.title);
  const title = link ? `<a class="title" href="${esc(link)}" target="_blank" rel="noopener">${tt}</a>` : `<span class="title">${tt}</span>`;
  const open = state.why.has(t.uid);
  const lnk = (act, label, extra = '') => `<button type="button" class="lnk" data-act="${act}" data-uid="${u}"${extra}>${label}</button>`;
  const why = `<button type="button" class="btn" data-act="why" data-uid="${u}" aria-expanded="${open}" aria-controls="why-${u}">Why?</button>`;
  const sub = ctx === 'missing'
    ? `<button type="button" class="btn go" data-act="keep" data-uid="${u}" aria-label="Keep ${tt}">Keep</button>`
    : `<button type="button" class="btn go" data-act="done" data-uid="${u}" aria-label="Mark ${tt} done">Done</button>`;
  const acts = ctx === 'missing'
    ? `${why}${lnk('done', 'Done', ` aria-label="Mark ${tt} done"`)}${lnk('drop', 'Drop')}`
    : `${why}${lnk('log', 'Log time')}<span class="more-wrap">${lnk('menu', `More${ICON_CARET}`,
        ` aria-haspopup="menu" aria-expanded="${state.menu === t.uid}" aria-label="More actions for ${tt}"`)}${moreMenu(t, ctx)}</span>`;
  const plan = ctx === 'today'
    ? `${t.action ? `<p class="action">${esc(t.action)}</p>` : ''}${t.urgency ? `<p class="urgency">${esc(t.urgency)}</p>` : ''}` : '';
  const bits = statusBits(t);
  return `<li class="row ${ctx}${t.overdue ? ' late' : ''}" data-row="${u}">
    <div class="c-title">${title}
      <div class="meta">${esc(t.course)} · ${esc(t.kind)}${bits.length ? ' · ' + bits.join(' · ') : ''}</div>${plan}</div>
    <div class="c-due"><span class="lbl">Due </span>${dueCell(t, banded)}</div>
    <div class="c-sub">${sub}</div>
    <div class="c-est" title="${esc(estTip(t, ctx))}"><span class="lbl">${ctx === 'today' ? 'Today ' : 'Est. '}</span>${estCell(t, ctx)}</div>
    <div class="c-grade"><span class="lbl">Grade </span>${gradeCell(t)}</div>
    <div class="c-act">${acts}</div>
    ${rowForm(t)}
    ${open ? whyPanel(t) : ''}
  </li>`;
}

const list = (rows, ctx, banded) => `<ul class="rows">${rows.map(t => taskRow(t, ctx, banded)).join('')}</ul>`;
const COLS = {
  today: ['Task', 'Due', 'Submission', 'Today', '% of Grade'],
  upcoming: ['Title', 'Due', 'Submission', 'Est.', '% of Grade', 'Details'],
  missing: ['Title', 'Due', 'Keep', 'Est.', '% of Grade', 'Details'],
};
const colhead = ctx => `<div class="colhead" aria-hidden="true">${COLS[ctx].map(c => `<span>${c}</span>`).join('')}</div>`;

const band = (id, label, est, pct, cls = '') =>
  `<h2 class="band${cls ? ' ' + cls : ''}"${id ? ` id="${id}"` : ''}><span class="b-l">${label}</span>` +
  `<span class="b-est">${est || ''}</span><span class="b-pct">${pct || ''}</span></h2>`;
const none = txt => `<p class="none">${txt}</p>`;
const sumPct = rows => { const p = rows.reduce((s, t) => s + (t.impact_pct || 0), 0); return p ? pctNum(p) + '%' : ''; };

const TIERS = [['must_today', 'Must do today', 'sec-must'], ['important', 'Should do today', 'sec-should'], ['if_time', "If there's time", 'sec-if']];
const liveRanked = () => state.data.plan ? state.data.plan.ranked.filter(r => !state.hidden.has(r.uid)) : [];

function freeChanged() {
  const d = state.data, p = d.plan;
  if (!p || !p.is_today || p.free_minutes == null || d.free_today == null) return '';
  if (Math.abs(p.free_minutes - d.free_today) < 15) return '';
  return `<div class="banner"><span>Free today: ${fmtMin(d.free_today)}. Plan used ${fmtMin(p.free_minutes)}.</span>${planBtn('Replan')}</div>`;
}

function renderToday() {
  const d = state.data, p = d.plan, td = todayDate();
  let m = `<h2 class="sec">Plan: ${WDL[td.getDay()]}, ${MO[td.getMonth()]} ${td.getDate()}</h2>` + freeChanged();
  if (!p) {
    m += `<div class="empty"><p class="empty-title">No plan for today yet.</p>${planBtn()}</div>`;
  } else {
    if (!p.is_today) {
      const pd = new Date(p.date + 'T12:00:00');
      m += `<div class="banner"><span>Showing the plan from ${esc(fmtDay(pd))}.</span>${planBtn()}</div>`;
    }
    if (p.day_summary) m += `<p class="summary">${esc(p.day_summary)}</p>`;
    const live = liveRanked();
    const total = live.reduce((s, r) => s + (r.suggested_minutes || 0), 0);
    if (p.budget_minutes) {
      const pct = Math.min(100, Math.round(100 * total / p.budget_minutes));
      m += `<div class="budget"><span><b>${fmtMin(total)}</b> planned of ${fmtMin(p.budget_minutes)} free</span>
        <span class="meter" role="img" aria-label="${pct}% of free time planned"><span style="width:${pct}%"></span></span></div>`;
    }
    if (live.length) {
      m += '<div class="tbl today">' + colhead('today');
      for (const [tier, label, id] of TIERS) {
        const rows = live.filter(r => r.tier === tier).sort((a, b) => a.rank - b.rank);
        if (!rows.length) continue;
        const mins = rows.reduce((s, r) => s + (r.suggested_minutes || 0), 0);
        m += band(id, label, fmtMin(mins), sumPct(rows), 'day') + list(rows, 'today', false);
      }
      m += '</div>';
    } else if (p.is_today) {
      m += `<div class="empty"><span class="empty-mark">${ICON_CHECK}</span><p class="empty-title">Everything in today's plan is done.</p></div>`;
    }
  }
  let s = '';
  if (p && p.later && p.later.length) {
    s += '<h2 class="sec" id="sec-later">Later this week</h2><ul class="later">' + p.later.map(l => {
      const t = d.tasks.find(x => x.uid === l.uid);
      return `<li class="ann${l.open ? '' : ' past'}">
        <h3 class="ann-t">${esc(l.title)}</h3>
        <div class="ann-meta">${t ? `<p class="b">${esc(t.course)}</p><p>${dueCell(t, false)}</p>` : ''}${l.open ? '' : '<p>Closed</p>'}</div>
        <p class="ann-body">${esc(l.why)}</p></li>`;
    }).join('') + '</ul>';
  }
  s += notesBox();
  return `<h1>Today</h1><div class="dash"><div class="dash-main">${m}</div><div class="dash-side">${s}</div></div>`;
}

const planBtn = (label = 'Plan today') => `<button type="button" class="btn go" data-job="replan" ${state.jobs.replan ? 'disabled aria-busy="true"' : ''}>${label}</button>`;

function notesBox() {
  const notes = (state.data.notes || []).slice().reverse();
  return `<h2 class="sec" id="sec-notes">Notes for the planner</h2>
    <form class="note" data-form="note">
      <label class="sr" for="note-text">Note for the planner</label>
      <textarea id="note-text" name="text" rows="3" maxlength="2000" placeholder="What you've done, or anything the plan should know"></textarea>
      <div class="form-btns"><button type="submit" class="btn">Save</button>
        <button type="submit" class="btn go" data-replan="1">Save and replan</button></div>
    </form>
    ${notes.length ? '<ul class="notes">' + notes.map(n =>
      `<li><span class="when">${esc(fmtStamp(n.ts))}</span><span>${esc(n.text)}</span></li>`).join('') + '</ul>' : ''}`;
}

function horizonTasks() {
  const f = state.filters;
  let tasks = state.data.tasks.filter(t => !state.hidden.has(t.uid));
  if (f.horizon !== 'all') tasks = tasks.filter(t => !t.due || t.overdue || t.days_out <= +f.horizon);
  return tasks;
}

function renderUpcoming() {
  const d = state.data, f = state.filters;
  const tasks = horizonTasks().filter(t => !f.course || t.course === f.course);
  const opt = (name, val, label) => `<button type="button" class="opt" data-filter="${name}" data-value="${val}" aria-pressed="${f[name] === val}">${label}</button>`;
  let h = `<h1>${f.course ? esc(f.course) : 'Upcoming'}</h1>
  <div class="toolbar">
    <p class="count">${plural(tasks.length, 'open task')} · ${fmtMin(tasks.reduce((s, t) => s + t.remaining, 0))} of work</p>
    <div class="opts" role="group" aria-label="Horizon"><span class="opt-l">Show</span>${opt('horizon', '14', '2 weeks')}${opt('horizon', '31', 'Month')}${opt('horizon', 'all', 'All')}</div>
    <div class="opts" role="group" aria-label="Sort"><span class="opt-l">Sort by</span>${opt('sort', 'date', 'Date')}${opt('sort', 'priority', 'Priority')}</div>
  </div>`;
  if (!tasks.length) {
    return h + `<div class="empty"><p class="empty-title">Nothing open in this range.</p>
      ${f.horizon !== 'all' ? '<button type="button" class="btn" data-filter="horizon" data-value="all">Show all dates</button>' : ''}</div>`;
  }
  h += '<div class="tbl upcoming">' + colhead('upcoming');
  const sumMin = rows => fmtMin(rows.reduce((s, t) => s + t.remaining, 0));
  if (f.sort === 'priority') {
    h += band('', 'By priority', sumMin(tasks), sumPct(tasks)) + list(tasks, 'upcoming', false);
  } else {

    const groups = new Map();
    const add = (key, label, t, banded) => {
      if (!groups.has(key)) groups.set(key, { label, banded, rows: [] });
      groups.get(key).rows.push(t);
    };
    const sorted = tasks.slice().sort((a, b) => (a.due ? Date.parse(a.due) : Infinity) - (b.due ? Date.parse(b.due) : Infinity));
    for (const t of sorted) {
      if (!t.due) { add('~', 'No due date', t, false); continue; }
      const dt = new Date(t.due), n = dayDiff(dt);
      if (t.overdue) add('0', 'Overdue', t, false);
      else if (n <= 0) add('1', `Today <span class="band-sub">${fmtBand(dt)}</span>`, t, true);
      else if (n === 1) add('2', `Tomorrow <span class="band-sub">${fmtBand(dt)}</span>`, t, true);
      else add('3' + String(n).padStart(4, '0'), `${fmtBand(dt)} <span class="band-sub">in ${n} days</span>`, t, true);
    }
    for (const k of [...groups.keys()].sort()) {
      const g = groups.get(k);
      const label = g.label + (g.rows.length > 1 ? ` <span class="band-sub">${plural(g.rows.length, 'task')}</span>` : '');
      h += band('', label, sumMin(g.rows), sumPct(g.rows), k === '0' ? 'danger' : '') + list(g.rows, 'upcoming', g.banded);
    }
  }
  h += '</div>';
  if (d.hidden_attendance) h += `<p class="foot">${plural(d.hidden_attendance, 'attendance item')} not shown.</p>`;
  return h;
}

const ORIGIN = { app: 'You', cli: 'You', sync: 'Sync', 'estimate-agent': 'Estimator' };
const STATUS_WORD = { submitted: 'done', dropped: 'dropped', in_progress: 'in progress', not_started: 'not started', graded: 'graded' };
const fmtDate = iso => { if (!iso) return 'none'; const d = new Date(iso.length === 10 ? iso + 'T12:00:00' : iso); return fmtDay(d) + (iso.length > 10 && hasTime(d) ? ', ' + fmtTime(d) : ''); };

function eventSentence(e) {
  const who = ORIGIN[e.origin] || e.origin, x = `"${e.title || e.uid}"`;
  const mine = who === 'You';
  switch (e.what) {
    case 'status':
      if (e.note === 'undo') return `${who} undid the ${STATUS_WORD[e.old] || e.old} mark on ${x}`;
      if (e.new === 'submitted') return `${who} marked ${x} done`;
      if (e.new === 'dropped') return `${who} dropped ${x}`;
      if (e.new === 'in_progress') return `${who} started ${x}`;
      return `${who} set ${x} to ${STATUS_WORD[e.new] || e.new}`;
    case 'graded': return `${who}: ${x} was graded ${e.new}${e.old != null ? ` (was ${e.old})` : ''}`;
    case 'due_changed': return `${who}: due date for ${x} moved from ${fmtDate(e.old)} to ${fmtDate(e.new)}`;
    case 'appeared': return `${who}: ${x} appeared${e.new ? `, due ${fmtDate(e.new)}` : ''}`;
    case 'closed_by_source': return `${who}: ${x} was closed by its source${e.note ? ` (${e.note})` : ''}`;
    case 'missing_from_source': return `${who}: ${x} is no longer listed by its source`;
    case 'back_in_source': return `${who}: ${x} is listed by its source again`;
    case 'superseded_placeholder': return e.note ? `${who}: ${e.note}` : `${who}: merged ${e.old} into ${x}`;
    case 'rehomed_orphan_override': return `${who}: moved your settings from an old entry onto ${x}`;
    case 'estimate': return `${mine ? who : who + ':'} set the estimate for ${x} to ${fmtMin(e.new)}${e.old != null ? ` (was ${fmtMin(e.old)})` : ''}`;
    case 'agent_estimate': return `${who}: ${x} estimated at ${fmtMin(e.new)}${e.old != null ? ` (was ${fmtMin(e.old)})` : ''}`;
    case 'kept_despite_missing': return `${who} kept ${x} although its source no longer lists it`;
    case 'time_logged': return `${who} logged ${fmtMin(e.new)} on ${x}`;
    default: return `${who}: ${x} ${String(e.what).replace(/_/g, ' ')}${e.new != null ? ` → ${e.new}` : ''}`;
  }
}

const loggedOutRe = /logged[_ ]?out|sign[ -]?in|log ?in/i;
const sourceRank = s => (!s.ok ? 0 : s.stale ? 1 : 2);

function renderAttention() {
  const a = state.data.attention;
  let h = '<h1>Needs attention</h1>';

  const stale = a.overdue_untouched || 0;
  if (stale >= 5) {
    const confirming = state.form && state.form.kind === 'catchup';
    h += `<h2 class="sec" id="sec-catchup">Old work</h2>
      <div class="line"><div class="line-main">${stale} open assignments were due before today and you have not touched them.
        They count against the time you have for current work.</div>
        <div class="form-btns">${confirming
          ? `<span>Mark all ${stale} as done? Each can be undone under Marked by you.</span>
             <button type="button" class="btn go" data-act="catchup-yes">Set them aside</button>
             <button type="button" class="lnk" data-act="cancel">Cancel</button>`
          : `<button type="button" class="btn" data-act="catchup">Set them aside</button>`}</div></div>`;
  }
  const missing = a.missing.filter(t => !state.hidden.has(t.uid));
  h += '<h2 class="sec" id="sec-missing">No longer listed by their source</h2>';
  h += missing.length ? '<div class="tbl missing">' + colhead('missing') + list(missing, 'missing', false) + '</div>' : none('None.');

  h += '<h2 class="sec" id="sec-dups">Possible duplicates</h2>';
  h += a.duplicates.length ? '<ul class="plain">' + a.duplicates.map(dup => {
    const confirming = state.form && state.form.kind === 'merge' && state.form.uid === dup.uid;
    return `<li class="line dup"><div class="line-main"><span class="title">${esc(dup.title)}</span> <span class="muted">(${esc(dup.course)}, entered by hand)</span>
      looks like <span class="title">${esc(dup.into_title)}</span>.</div>
      <div class="form-btns">${confirming
        ? `<span>Merge into the source's row? This can't be undone here.</span>
           <button type="button" class="btn go" data-act="merge-yes" data-uid="${esc(dup.uid)}" data-into="${esc(dup.into)}">Merge</button>
           <button type="button" class="lnk" data-act="cancel">Cancel</button>`
        : `<button type="button" class="btn" data-act="merge" data-uid="${esc(dup.uid)}">Merge</button>`}</div></li>`;
  }).join('') + '</ul>' : none('None.');

  const sources = a.sources.slice().sort((x, y) => sourceRank(x) - sourceRank(y));
  const anyLoggedOut = sources.some(s => loggedOutRe.test(`${s.detail || ''} ${s.fix || ''}`));
  h += `<h2 class="sec" id="sec-sources">Sources</h2>
    <table class="xt"><thead><tr><th scope="col">Source</th><th scope="col">Status</th><th scope="col">Last OK</th></tr></thead><tbody>` +
    sources.map(s => {
      const isToken = s.action === 'canvas_token';
      const status = isToken ? (s.ok ? 'Expiring' : 'Not working') : !s.ok ? 'Failing' : s.stale ? 'Stale' : 'OK';
      const loggedOut = loggedOutRe.test(`${s.detail || ''} ${s.fix || ''}`);
      return `<tr class="${!s.ok || s.stale ? 'bad' : ''}">
        <td class="src-main"><div class="src-label">${esc(s.label)}</div>
          ${s.detail ? `<div class="meta">${esc(s.detail)}</div>` : ''}
          ${s.fix ? `<div class="fix">${esc(s.fix)}</div>` : ''}
          ${loggedOut ? `<button type="button" class="btn go" data-job="ls_login">Sign in to LearningSuite</button>` : ''}
          ${s.action === 'canvas_token' ? `<a class="btn go" href="/setup#token">Renew Canvas token</a>` : ''}</td>
        <td class="src-st${!s.ok || s.stale ? ' red' : ''}"><span class="lbl">Status </span>${status}</td>
        <td class="src-when">${isToken ? '' : `<span class="lbl">Last OK </span>${esc(s.last_ok || 'never')}`}</td></tr>`;
    }).join('') + '</tbody></table>';
  if (!anyLoggedOut) h += '<p class="after"><button type="button" class="btn" data-job="ls_login">Sign in to LearningSuite</button></p>';

  const done = state.data.marked_done.filter(r => !state.hidden.has(r.uid));
  h += '<h2 class="sec" id="sec-marked">Marked by you</h2>';
  h += done.length ? '<ul class="plain marked">' + done.map(r => `<li class="line past">
      <div class="line-main"><span class="title">${esc(r.title)}</span>
        <div class="meta">${esc(r.course)}${r.due_label ? ' · due ' + esc(r.due_label) : ''}</div></div>
      <span class="st">${r.status === 'dropped' ? 'Dropped' : 'Completed'}</span>
      <button type="button" class="lnk" data-act="undo-mark" data-uid="${esc(r.uid)}" data-title="${esc(r.title)}">Undo</button></li>`).join('') + '</ul>'
    : none('Nothing marked by hand is waiting on a grade.');

  const ev = state.data.events;
  h += '<h2 class="sec" id="sec-activity">Activity</h2>';
  h += ev.length ? '<ul class="plain events">' + ev.slice(0, state.eventsShown).map(e =>
    `<li><span class="when">${esc(fmtStamp(e.ts))}</span><span>${esc(eventSentence(e))}</span></li>`).join('') + '</ul>' +
    (ev.length > state.eventsShown ? '<p class="after"><button type="button" class="lnk" data-act="more-events">Show more</button></p>' : '')
    : none('No activity recorded yet.');
  return h;
}

const jump = (id, label, n, warn) =>
  `<li><a href="#${id}" data-jump>${esc(label)}${n != null ? ` <span class="n${warn ? ' warn' : ''}">${n}</span>` : ''}</a></li>`;

function sideToday() {
  const p = state.data.plan, live = liveRanked();
  let h = '';
  for (const [tier, label, id] of TIERS) {
    const n = live.filter(r => r.tier === tier).length;
    if (n) h += jump(id, label, n);
  }
  if (p && p.later && p.later.length) h += jump('sec-later', 'Later this week', p.later.filter(l => l.open).length);
  h += jump('sec-notes', 'Notes', (state.data.notes || []).length || null);
  return `<nav aria-label="Sections"><ul>${h}</ul></nav>`;
}

function sideUpcoming() {
  const tasks = horizonTasks(), f = state.filters;
  const item = (val, label) => {
    const n = val ? tasks.filter(t => t.course === val).length : tasks.length;
    return `<li><button type="button" data-filter="course" data-value="${esc(val)}" aria-pressed="${f.course === val}">${esc(label)} <span class="n">${n}</span></button></li>`;
  };
  return `<nav aria-label="Courses"><ul>${item('', 'All courses')}${state.data.courses.map(c => item(c, c)).join('')}</ul></nav>`;
}

function sideAttention() {
  const a = state.data.attention;
  const missing = a.missing.filter(t => !state.hidden.has(t.uid)).length;
  return `<nav aria-label="Sections"><ul>
    ${jump('sec-missing', 'Missing from source', missing, missing > 0)}
    ${jump('sec-dups', 'Possible duplicates', a.duplicates.length, a.duplicates.length > 0)}
    ${jump('sec-sources', 'Sources', a.problems ? `${a.problems} to fix` : a.sources.length, a.problems > 0)}
    ${jump('sec-marked', 'Marked by you', state.data.marked_done.filter(r => !state.hidden.has(r.uid)).length)}
    ${jump('sec-activity', 'Activity', null)}
  </ul></nav>`;
}

function spy() {
  const links = [...document.querySelectorAll('.side a[data-jump]')];
  if (!links.length) return;

  let cur = links[0], best = -Infinity;
  const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
  if (window.scrollY > 8) {
    for (const a of links) {
      const el = document.getElementById(a.getAttribute('href').slice(1));
      const top = el ? el.getBoundingClientRect().top : Infinity;
      if (top < 160 && top > best) { best = top; cur = a; }
    }
  }
  if (atBottom && window.scrollY > 0) cur = links[links.length - 1];
  links.forEach(a => a === cur ? a.setAttribute('aria-current', 'location') : a.removeAttribute('aria-current'));
}

const mqNarrow = matchMedia('(max-width: 1199.98px)');
function syncDrawer() {
  const narrow = mqNarrow.matches, open = narrow && state.drawer;
  const dr = $('#drawer');
  dr.classList.toggle('open', open);
  if (narrow && !open) dr.setAttribute('inert', ''); else dr.removeAttribute('inert');
  $('#scrim').hidden = !open;
  $('#burger').setAttribute('aria-expanded', open);
}
function setDrawer(open, refocus) {
  state.drawer = open;
  syncDrawer();
  if (open) { const t = $('#drawer [aria-selected=true]'); if (t) t.focus(); }
  else if (refocus) $('#burger').focus();
}
function closeDrawerAfterPick() { if (state.drawer) setDrawer(false, true); }
mqNarrow.addEventListener('change', () => { state.drawer = false; syncDrawer(); });

const VIEWS = { today: renderToday, upcoming: renderUpcoming, calendar: () => renderCalendar(), attention: renderAttention };
const SIDES = { today: sideToday, upcoming: sideUpcoming, calendar: () => sideCalendar(), attention: sideAttention };

function renderHeader() {
  const td = todayDate();
  $('#term').textContent = termLabel(td);
  const [view, more] = state.tab === 'today' ? ['Today', fmtBand(td)]
    : state.tab === 'upcoming' ? ['Upcoming', state.filters.course || 'All courses']
      : state.tab === 'calendar' ? ['Calendar', ''] : ['Needs attention', ''];
  $('#ctx-name').innerHTML = esc(view) + (more ? `<span class="ctx-more"> – ${esc(more)}</span>` : '');
  const synced = lastSynced();
  $('#synced').textContent = synced ? `Last synced ${synced}` : '';
}

function render() {
  document.querySelectorAll('[role=tab]').forEach(b => {
    const on = b.dataset.tab === state.tab;
    b.setAttribute('aria-selected', on);
    b.tabIndex = on ? 0 : -1;
  });

  for (const k of Object.keys(VIEWS)) {
    const v = $('#view-' + k);
    v.hidden = k !== state.tab;
    if (v.hidden) v.innerHTML = '';
  }
  renderJobs();
  if (!state.data) return;
  const n = state.data.attention.missing.length + state.data.attention.duplicates.length + state.data.attention.problems;
  const badge = $('#att-badge');
  badge.hidden = !n;
  badge.textContent = `(${n})`;
  badge.setAttribute('aria-label', `${n} to review`);
  renderHeader();
  $('#side').innerHTML = SIDES[state.tab]();
  keepFields(() => { $('#view-' + state.tab).innerHTML = VIEWS[state.tab](); });
  if (state.tab === 'calendar') placePop();
  renderJobs();
  spy();
}

function keepFields(draw) {
  const was = {}, at = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.keep : null;
  const each = fn => document.querySelectorAll('[data-keep]').forEach(fn);
  each(el => { was[el.dataset.keep] = el.type === 'checkbox' ? el.checked : el.value; });
  draw();
  each(el => {
    if (!(el.dataset.keep in was)) return;
    if (el.type === 'checkbox') el.checked = was[el.dataset.keep]; else el.value = was[el.dataset.keep];
    if (el.dataset.keep === at) el.focus();
  });
}

function focusForm() {
  const el = $('.inline-form input, .inline-form .go');
  if (el) el.focus();
}

function setTab(tab) {
  state.tab = tab; state.form = null; state.menu = null;
  save('tab', tab);
  render();
  window.scrollTo(0, 0);
}

document.addEventListener('click', e => {
  if (e.target.closest('[data-drawer]')) return setDrawer(!state.drawer, true);
  const tab = e.target.closest('[data-tab]');
  if (tab) { setTab(tab.dataset.tab); return closeDrawerAfterPick(); }
  const job = e.target.closest('[data-job]');
  if (job) return runJob(job.dataset.job);
  const flt = e.target.closest('button[data-filter]');
  if (flt) {
    state.filters[flt.dataset.filter] = flt.dataset.value; save('filters', state.filters); render();
    if (flt.dataset.filter === 'course') { window.scrollTo(0, 0); closeDrawerAfterPick(); }
    return;
  }
  const jmp = e.target.closest('a[data-jump]');
  if (jmp) {
    const el = document.getElementById(jmp.getAttribute('href').slice(1));
    if (el) {
      e.preventDefault();
      closeDrawerAfterPick();
      el.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      document.querySelectorAll('.side a[data-jump]').forEach(a => a === jmp ? a.setAttribute('aria-current', 'location') : a.removeAttribute('aria-current'));
    }
    return;
  }

  const b = e.target.closest('[data-act]');
  if (!b) { if (state.menu && !e.target.closest('.menu')) { state.menu = null; render(); } return; }
  const uid = b.dataset.uid, act = b.dataset.act;
  if (act !== 'menu') state.menu = null;
  switch (act) {
    case 'retry': return refresh();
    case 'close-error': $('#job-error').hidden = true; return;
    case 'done': return markDone(uid, false);
    case 'done-yes': return markDone(uid, true);
    case 'cancel': state.form = null; return render();
    case 'why': state.why.has(uid) ? state.why.delete(uid) : state.why.add(uid); return render();
    case 'menu': state.menu = state.menu === uid ? null : uid; render();
      { const m = $('.menu button'); if (m) m.focus(); } return;
    case 'log': case 'estimate': state.form = { uid, kind: act }; render(); return focusForm();
    case 'merge': state.form = { uid, kind: 'merge' }; return render();
    case 'catchup': state.form = { kind: 'catchup' }; return render();
    case 'catchup-yes': state.form = null;
      return post('/api/task', { action: 'catch_up' })
        .then(r => { toast(`Set aside ${r.count} assignments.`); return refresh(); })
        .catch(err => { toast(`Couldn't do that: ${err.message}`); return refresh(); });
    case 'merge-yes': state.form = null;
      return taskAction(uid, 'merge', { into: b.dataset.into }, { hide: true, toast: 'Merged.' });
    case 'start': return taskAction(uid, 'start', {}, { toast: 'Started.' });
    case 'stop': return taskAction(uid, 'undone', {}, { toast: 'Marked not started.' });
    case 'keep': return taskAction(uid, 'keep', {}, { hide: true, toast: 'Kept.' });
    case 'dismiss': return taskAction(uid, 'dismiss', {}, { hide: true, toast: "Hidden from today's plan." });
    case 'drop': {
      const t = findTask(uid);
      return taskAction(uid, 'drop', {}, { hide: true, toast: `Dropped "${t ? t.title : ''}".`, undo: true });
    }
    case 'undo-mark': state.hidden.add(uid); render(); return undoMark(uid, b.dataset.title, false);
    case 'more-events': state.eventsShown += 60; return render();
  }
});

document.addEventListener('submit', async e => {
  const f = e.target.closest('form[data-form]');
  if (!f) return;
  e.preventDefault();
  const kind = f.dataset.form;
  if (kind === 'note') {
    const text = f.elements.text.value.trim();
    if (!text) return f.elements.text.focus();
    const replan = e.submitter && e.submitter.dataset.replan;
    try {
      await post('/api/note', { text });
      f.elements.text.value = '';
      toast('Note saved.');
      if (replan) runJob('replan');
    } catch (err) { toast(`Couldn't save the note: ${err.message}`); }
    return refresh();
  }
  const minutes = parseInt(f.elements.minutes.value, 10);
  const uid = f.dataset.uid;
  state.form = null;
  taskAction(uid, kind, { minutes }, { toast: kind === 'log' ? `Logged ${fmtMin(minutes)}.` : `Estimate set to ${fmtMin(minutes)}.` });
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && (state.menu || state.form)) { state.menu = null; state.form = null; render(); return; }
  if (e.key === 'Escape' && state.drawer) { setDrawer(false, true); return; }

  const tab = e.target.closest && e.target.closest('[role=tab]');
  if (tab && (e.key === 'ArrowRight' || e.key === 'ArrowLeft' || e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    e.preventDefault();
    const tabs = [...document.querySelectorAll('[role=tab]')];
    const fwd = e.key === 'ArrowRight' || e.key === 'ArrowDown';
    const next = tabs[(tabs.indexOf(tab) + (fwd ? 1 : tabs.length - 1)) % tabs.length];
    setTab(next.dataset.tab); next.focus();
  }
});

document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
let spyQueued = false;
window.addEventListener('scroll', () => {
  if (spyQueued) return;
  spyQueued = true;
  requestAnimationFrame(() => { spyQueued = false; spy(); });
}, { passive: true });

state.tab = VIEWS[load('tab', 'today')] ? load('tab', 'today') : 'today';
Object.assign(state.filters, load('filters', {}));
syncDrawer();
render();
refresh();
