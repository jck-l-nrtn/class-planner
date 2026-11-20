'use strict';

const HOUR_PX = 44, SNAP = 15;
const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const DAY_WORD = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' };
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const KIND_LABEL = { class: 'Class', work: 'Work', standing: 'Standing', busy: 'Yours', meal: 'Meal', travel: 'Travel' };
const PROVIDERS = {
  google: ['Google Calendar', 'Settings › your calendar › Integrate calendar › Secret address in iCal format'],
  outlook: ['Outlook', 'Settings › Calendar › Shared calendars › Publish a calendar › ICS link'],
  icloud: ['iCloud', 'Share Calendar › Public Calendar › copy the link'],
};
const cal = {
  view: ['day', 'week', 'month'].includes(load('calView', 'week')) ? load('calView', 'week') : 'week',
  anchor: null, range: null, pop: null, popN: 0, panel: null, provider: 'google', mini: null,
  hide: new Set(load('calHide', [])), busy: false,
};
const calNarrow = matchMedia('(max-width: 759.98px)');

const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const dayOf = iso => new Date(iso + 'T12:00:00');
const addDays = (iso, n) => { const d = dayOf(iso); d.setDate(d.getDate() + n); return isoDay(d); };
const mins = hm => { const [h, m] = hm.split(':').map(Number); return h * 60 + m; };
const hhmmOf = m => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
const clock = m => fmtTime(new Date(2000, 0, 1, Math.floor(m / 60), m % 60));
const spanText = (s, e) => `${clock(s)} – ${clock(e)}`;
const snap = m => Math.round(m / SNAP) * SNAP;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const anchorDay = () => cal.anchor || state.data.today;
const calView = () => (calNarrow.matches && cal.view === 'week' ? 'day' : cal.view);
const catOf = b => (b.kind === 'calendar' ? 'link:' + (b.from || '') : b.kind === 'travel' ? 'meal' : b.kind);
const shown = b => !cal.hide.has(catOf(b));
const repeatText = b => (!b.days ? '' : b.days.length === 7 ? 'Every day' : 'Every ' + b.days.map(x => DAY_WORD[x]).join(', '));

function seriesOf(b) {
  const c = state.data.calendar;
  if (b.id) { const o = c.blocks.find(x => x.id === b.id); return o ? { src: 'own', ...o } : null; }
  if (b.rec != null) { const w = (c.week || []).find(x => x.i === b.rec); return w ? { src: 'week', ...w } : null; }
  if (b.meal != null) { const m = (c.meals || []).find(x => x.i === b.meal); return m ? { src: 'meal', ...m, days: DAY_KEYS } : null; }
  return null;
}

function seriesAct(sr, kind, data = {}) {
  if (sr.src === 'own') {
    if (kind === 'all') return calAct({ action: 'update_block', id: sr.id, ...data }, 'Saved.');
    if (kind === 'one') return calAct({ action: 'detach_block', id: sr.id, ...data }, 'Changed.');
    if (kind === 'from') return calAct({ action: 'split_block', id: sr.id, ...data }, 'Changed.');
    if (kind === 'del-one') return calAct({ action: 'skip_block', id: sr.id, on: data.on }, 'Deleted.');
    if (kind === 'del-from') return calAct({ action: 'split_block', id: sr.id, on: data.on }, 'Deleted.');
    return calAct({ action: 'remove_block', id: sr.id }, 'Deleted.');
  }
  const action = { all: 'week_update', one: 'week_detach', from: 'week_split', 'del-one': 'week_skip', 'del-from': 'week_split', 'del-all': 'week_delete' }[kind];
  return calAct({ action, section: sr.src === 'meal' ? 'meals' : 'recurring', i: sr.i, was: { name: sr.name, start: sr.start, end: sr.end }, ...data },
    kind.startsWith('del') ? 'Deleted.' : 'Changed.', 'week');
}

function calSpan() {
  const a = anchorDay(), v = calView();
  if (v === 'day') return [a, 1];
  if (v === 'week') return [addDays(a, -dayOf(a).getDay()), 7];
  const first = a.slice(0, 8) + '01';
  return [addDays(first, -dayOf(first).getDay()), 42];
}

async function loadRange() {
  const c = state.data && state.data.calendar;
  if (!c || !c.started) return;
  const [start, days] = calSpan();
  try {
    const r = await fetch(`/api/calendar?start=${start}&days=${days}`, { cache: 'no-store' });
    if (r.ok) cal.range = await r.json();
  } catch (e) { }
}

async function calGo(change) {
  if (change) change();
  cal.pop = null;
  await loadRange();
  render();
}

async function calAct(body, msg, undo = true) {
  const before = state.data.calendar.blocks;
  cal.pop = null; cal.busy = true;
  try {
    await post('/api/calendar', body);
    document.querySelectorAll('#toasts .toast').forEach(x => x.remove());
    const back = undo === 'week' ? { action: 'week_undo', blocks: before } : { action: 'set_blocks', blocks: before };
    if (msg) toast(msg, undo ? () => calAct(back, 'Undone.', false) : null);
  } catch (e) { toast(e.message); }
  cal.busy = false;
  await refresh();
}

function blockTimes(b, date) {
  const s = new Date(b.start), e = new Date(b.end);
  return [isoDay(s) < date ? 0 : s.getHours() * 60 + s.getMinutes(), isoDay(e) > date ? 1440 : e.getHours() * 60 + e.getMinutes()];
}

function setLanes(items) {
  let cluster = [], ends = [], until = -1;
  const flush = () => { cluster.forEach(x => { x.lanes = ends.length; }); cluster = []; ends = []; until = -1; };
  for (const it of items) {
    if (cluster.length && it.s >= until) flush();
    let i = ends.findIndex(e => e <= it.s);
    if (i < 0) i = ends.push(0) - 1;
    ends[i] = it.e; it.lane = i; cluster.push(it); until = Math.max(until, it.e);
  }
  flush();
}

function gridHours() {
  const c = state.data.calendar, from = Math.floor(mins(c.wake) / 60) * 60;
  const to = Math.ceil(mins(c.sleep) / 60) * 60;
  return [from, to <= from ? 1440 : to];
}

function timeGrid(r) {
  const [from, to] = gridHours();
  const px = m => (m - from) / 60 * HOUR_PX;
  const now = new Date(state.data.now), nowMin = now.getHours() * 60 + now.getMinutes();
  const draft = cal.pop && cal.pop.draft;
  let head = '<span></span>', cols = '';
  for (const d of r.days) {
    const dt = dayOf(d.date), today = d.date === state.data.today;
    head += `<button type="button" class="cg-dayhead${today ? ' today' : ''}" data-cal="day" data-date="${d.date}">
      <span class="cg-dow">${WD[dt.getDay()]}</span><span class="cg-dom">${dt.getDate()}</span>
      <span class="cg-free">${fmtMin(d.free_minutes)} free</span></button>`;
    const items = d.blocks.map((b, i) => { const [s, e] = blockTimes(b, d.date); return { b, i, s: Math.max(from, s), e: Math.min(to, e) }; })
      .filter(x => x.e > x.s && shown(x.b) && !(draft && draft.hide === `${d.date}:${x.i}`));
    if (draft && draft.date === d.date) items.push({ b: { title: draft.name || '(No title)', kind: 'busy', id: '' }, i: -1, s: draft.s, e: draft.e, draft: true });
    items.sort((a, b) => a.s - b.s || b.e - a.e);
    setLanes(items);
    cols += `<div class="cg-col${today ? ' today' : ''}" data-date="${d.date}" data-from="${from}" data-to="${to}" role="group" aria-label="${esc(fmtBand(dt))}">` +
      items.map(x => `<button type="button" class="cg-b k-${esc(x.b.kind)}${x.e - x.s < 40 ? ' short' : ''}${x.draft ? ' draft' : ''}${x.b.id || x.b.rec != null || x.b.meal != null ? ' own' : ''}"
          data-date="${d.date}" data-i="${x.i}" data-s="${x.s}" data-e="${x.e}"
          style="top:${px(x.s)}px;height:${Math.max(px(x.e) - px(x.s), 14)}px;left:${100 * x.lane / x.lanes}%;width:${100 / x.lanes}%">
          <span class="cg-bt">${esc(x.b.title)}</span><span class="cg-bm">${esc(spanText(x.s, x.e))}</span></button>`).join('') +
      (today && nowMin > from && nowMin < to ? `<span class="cg-now" style="top:${px(nowMin)}px"></span>` : '') + '</div>';
  }
  let times = '';
  for (let m = from; m < to; m += 60) times += `<span style="top:${px(m)}px">${clock(m).replace(':00', '')}</span>`;
  return `<div class="cg n${r.days.length}" style="--hh:${HOUR_PX}px;--hours:${(to - from) / 60};--days:${r.days.length}">
    <div class="cg-head">${head}</div>
    <div class="cg-body"><div class="cg-times" aria-hidden="true">${times}</div>${cols}</div></div>`;
}

function monthGrid(r) {
  const month = anchorDay().slice(0, 7);
  let h = `<div class="mg"><div class="mg-head">${WD.map(w => `<span>${w}</span>`).join('')}</div><div class="mg-body">`;
  for (const d of r.days) {
    const rows = d.blocks.map((b, i) => ({ b, i })).filter(x => x.b.kind !== 'meal' && x.b.kind !== 'travel' && shown(x.b));
    h += `<div class="mg-cell${d.date.slice(0, 7) !== month ? ' out' : ''}${d.date === state.data.today ? ' today' : ''}" data-date="${d.date}">
      <div class="mg-top"><button type="button" class="mg-num" data-cal="day" data-date="${d.date}">${dayOf(d.date).getDate()}</button>
        <span class="mg-free">${fmtMin(d.free_minutes)}</span></div>
      ${rows.slice(0, 3).map(({ b, i }) => `<button type="button" class="mc k-${esc(b.kind)}" data-cal="chip" data-date="${d.date}" data-i="${i}">
        <span class="mc-t">${esc(clock(blockTimes(b, d.date)[0]).replace(':00', '').replace(' ', ''))}</span> ${esc(b.title)}</button>`).join('')}
      ${rows.length > 3 ? `<button type="button" class="mg-more" data-cal="day" data-date="${d.date}">${rows.length - 3} more</button>` : ''}</div>`;
  }
  return h + '</div></div>';
}

function popFields(p) {
  const k = `p${p.n}-`;
  const days = p.days || [];
  return `${p.src !== 'own' && p.kind === 'edit' ? `<h3>${esc(p.name)}</h3>`
    : `<input class="pop-title" name="name" data-keep="${k}name" maxlength="60" required placeholder="Add title" value="${esc(p.name || '')}">`}
    <div class="pop-row">
      ${p.kind === 'edit' && p.weekly ? '' : `<input name="date" data-keep="${k}date" type="date" value="${esc(p.date || '')}" aria-label="Date">`}
      <input name="start" data-keep="${k}start" type="time" required value="${hhmmOf(p.s)}" aria-label="Start">
      <span>–</span>
      <input name="end" data-keep="${k}end" type="time" required value="${hhmmOf(p.e)}" aria-label="End"></div>
    ${p.src === 'meal' ? '' : `<fieldset class="days"><legend>Repeat weekly</legend>${DAY_KEYS.map(d =>
      `<label><input type="checkbox" name="days" value="${d}" data-keep="${k}${d}"${days.includes(d) ? ' checked' : ''}>${DAY_WORD[d]}</label>`).join('')}</fieldset>`}`;
}

function popover() {
  const p = cal.pop;
  if (!p) return '';
  const close = `<button type="button" class="lnk pop-x" data-cal="close" aria-label="Close">${ICON_X}</button>`;
  const dateLine = (date, s, e) => `${esc(fmtBand(dayOf(date)))} · ${esc(spanText(s, e))}`;
  if (p.kind === 'new' || p.kind === 'edit') {
    const weekly = p.kind === 'edit' && p.weekly;
    return `<form class="cal-pop" id="cal-pop" data-cal-form="pop">${close}${popFields(p)}
      <div class="form-btns">${weekly ? `<button type="submit" class="btn go" data-scope="one">This day</button>
        <button type="submit" class="btn" data-scope="from">This and following</button>
        <button type="submit" class="btn" data-scope="all">All</button>`
        : `<button type="submit" class="btn go"${cal.busy ? ' disabled' : ''}>Save</button>`}
        <button type="button" class="lnk" data-cal="close">Cancel</button></div></form>`;
  }
  if (p.kind === 'apply') {
    return `<div class="cal-pop" id="cal-pop" role="dialog" aria-label="Change ${esc(p.draft.name)}">${close}
      <h3>${esc(p.draft.name)}</h3><p class="pop-when">${dateLine(p.draft.date, p.draft.s, p.draft.e)}</p>
      <div class="form-btns"><button type="button" class="btn go" data-cal="apply-one">This day</button>
        <button type="button" class="btn" data-cal="apply-from">This and following</button>
        <button type="button" class="btn" data-cal="apply-all">All</button>
        <button type="button" class="lnk" data-cal="close">Cancel</button></div></div>`;
  }
  const b = p.block, sr = seriesOf(b);
  const meta = sr ? [sr.src !== 'own' ? KIND_LABEL[b.kind] : '', repeatText(sr)].filter(Boolean).join(' · ')
    : b.kind === 'calendar' ? b.from : KIND_LABEL[b.kind] || '';
  let btns = '';
  if (sr && sr.days) {
    btns = cal.pop.deleting
      ? `<button type="button" class="btn go" data-cal="del-one">This day</button>
         <button type="button" class="btn" data-cal="del-from">This and following</button>
         <button type="button" class="btn" data-cal="del-all">All</button>
         <button type="button" class="lnk" data-cal="del-no">Cancel</button>`
      : `<button type="button" class="btn" data-cal="edit">Edit</button><button type="button" class="btn" data-cal="del">Delete</button>`;
  } else if (sr) {
    btns = `<button type="button" class="btn" data-cal="edit">Edit</button><button type="button" class="btn" data-cal="del-all">Delete</button>`;
  }
  return `<div class="cal-pop" id="cal-pop" role="dialog" aria-label="${esc(b.title)}">${close}
    <h3><span class="key k-${esc(b.kind)}"></span>${esc(b.title)}</h3>
    <p class="pop-when">${dateLine(p.date, p.s, p.e)}</p>
    ${meta ? `<p class="pop-meta">${esc(meta)}</p>` : ''}
    ${btns ? `<div class="form-btns">${btns}</div>` : ''}</div>`;
}

function placePop() {
  const el = $('#cal-pop'), wrap = $('#cal-wrap');
  if (!el || !wrap || !cal.pop) return;
  const a = cal.pop.sel && wrap.querySelector(cal.pop.sel);
  const w = wrap.getBoundingClientRect(), pw = el.offsetWidth, ph = el.offsetHeight;
  let x = (w.width - pw) / 2, y = 8;
  if (a) {
    const r = a.getBoundingClientRect();
    x = r.right - w.left + 6;
    if (x + pw > w.width) x = r.left - w.left - pw - 6;
    if (x < 0) x = clamp(r.left - w.left, 0, Math.max(0, w.width - pw));
    y = r.top - w.top;
    if (x < r.right - w.left && x + pw > r.left - w.left) y = r.bottom - w.top + 4;
  }
  el.style.left = `${clamp(x, 0, Math.max(0, w.width - pw))}px`;
  el.style.top = `${clamp(y, 0, Math.max(0, w.height - ph))}px`;
  if (cal.pop.focus) { cal.pop.focus = false; const f = el.querySelector('input, .btn'); if (f) f.focus({ preventScroll: true }); }
}

function openPop(pop) {
  cal.pop = { ...pop, n: ++cal.popN, focus: true };
  cal.panel = null;
  render();
}

function openNew(date, s, e) {
  const [from, to] = gridHours();
  s = clamp(s, from, to - SNAP); e = clamp(e, s + SNAP, to);
  openPop({ kind: 'new', date, s, e, draft: { date, s, e }, sel: calView() === 'month' ? `.mg-cell[data-date="${date}"] .mg-top` : '.cg-b.draft' });
}

function openBlock(date, i, sel) {
  const day = cal.range.days.find(d => d.date === date), b = day && day.blocks[i];
  if (!b) return;
  const [s, e] = blockTimes(b, date);
  openPop({ kind: 'info', block: b, date, s, e, sel });
}

function connectPanel() {
  const who = PROVIDERS[cal.provider] ? cal.provider : 'google';
  return `<div class="detail cal-panel">
    <div class="opts" role="group" aria-label="Which calendar"><span class="opt-l">I use</span>${Object.keys(PROVIDERS).map(k =>
      `<button type="button" class="opt" data-cal="provider" data-id="${k}" aria-pressed="${k === who}">${PROVIDERS[k][0]}</button>`).join('')}</div>
    <p class="steps">${esc(PROVIDERS[who][1])}</p>
    <form class="inline-form" data-cal-form="link">
      <label>Name<input name="name" data-keep="l-name" maxlength="40" placeholder="${PROVIDERS[who][0]}"></label>
      <label class="grow">Link<input name="url" data-keep="l-url" type="url" required autocomplete="off" spellcheck="false" placeholder="https://…/basic.ics"></label>
      <span class="form-btns"><button type="submit" class="btn go"${cal.busy ? ' disabled aria-busy="true"' : ''}>Connect</button>
      <button type="button" class="lnk" data-cal="panel-close">Cancel</button></span></form>
  </div>`;
}

function linksPanel(c) {
  const rows = c.links.map(l => {
    const confirming = cal.unlink === l.id;
    const status = l.ok ? `updated ${esc(fmtStamp(l.last_ok))}`
      : `<span class="red">${esc(l.error || 'Not updating')}</span>${l.last_ok ? ` Last read ${esc(fmtStamp(l.last_ok))}.` : ''}`;
    return `<li class="line"><div class="line-main"><span class="title">${esc(l.name)}</span>
        <div class="meta">${esc(l.host)} · ${status}</div></div>
      <div class="form-btns">${confirming
        ? `<button type="button" class="btn go" data-cal="unlink-yes" data-id="${esc(l.id)}">Remove</button>
           <button type="button" class="lnk" data-cal="unlink-no">Cancel</button>`
        : `<button type="button" class="lnk" data-cal="unlink" data-id="${esc(l.id)}" aria-label="Remove ${esc(l.name)}">Remove</button>`}</div></li>`;
  }).join('');
  return (rows ? `<ul class="plain cal-links">${rows}</ul><p class="after"><button type="button" class="btn" data-cal="refresh"${cal.busy ? ' disabled' : ''}>Update now</button></p>` : '') + connectPanel();
}

function calChooser() {
  return `<h1>Calendar</h1>
    <div class="choices">
      <div class="choice"><h3>Connect a calendar</h3>
        <p>Google Calendar, Outlook or iCloud</p>
        <button type="button" class="btn go" data-cal="connect">Connect</button></div>
      <div class="choice"><h3>Built-in calendar</h3>
        <p>Add busy times by hand</p>
        <button type="button" class="btn go" data-cal="builtin">Use this</button></div>
    </div>`;
}

function rangeTitle(r) {
  const v = calView(), a = dayOf(anchorDay());
  if (v === 'month') return `${MONTHS[a.getMonth()]} ${a.getFullYear()}`;
  if (v === 'day') return `${WDL[a.getDay()]}, ${MONTHS[a.getMonth()]} ${a.getDate()}`;
  const f = dayOf(r.days[0].date), l = dayOf(r.days[r.days.length - 1].date);
  return f.getMonth() === l.getMonth() ? `${fmtMD(f)} – ${l.getDate()}, ${l.getFullYear()}` : `${fmtMD(f)} – ${fmtMD(l)}, ${l.getFullYear()}`;
}

function renderCalendar() {
  const c = state.data.calendar;
  if (!c) return '<h1>Calendar</h1><div class="empty"><p class="empty-title">Restart the planner to see this page.</p></div>';
  if (!c.started && cal.panel !== 'links') return calChooser();
  if (!c.started) return `<h1>Calendar</h1><h2 class="sec">Connect a calendar</h2>${connectPanel()}`;
  const r = cal.range;
  if (!r) return '<h1>Calendar</h1>';
  const v = calView();
  const opt = (k, label) => `<button type="button" class="opt" data-cal="view" data-id="${k}" aria-pressed="${v === k}">${label}</button>`;
  return `<h1>Calendar</h1>${freeChanged()}
    <div class="toolbar cal-bar">
      <span class="cal-nav"><button type="button" class="btn" data-cal="today">Today</button>
        <button type="button" class="btn" data-cal="prev" aria-label="Earlier">‹</button>
        <button type="button" class="btn" data-cal="next" aria-label="Later">›</button></span>
      <p class="count">${esc(rangeTitle(r))}</p>
      <div class="opts" role="group" aria-label="View">${opt('day', 'Day')}${opt('week', 'Week')}${opt('month', 'Month')}</div>
      <button type="button" class="btn go" data-cal="create">Create</button>
      <button type="button" class="btn" data-cal="links" aria-expanded="${cal.panel === 'links'}">Calendars${c.links.length ? ` (${c.links.length})` : ''}</button>
    </div>
    ${cal.panel === 'links' ? linksPanel(c) : ''}
    <div class="cal-wrap" id="cal-wrap">${v === 'month' ? monthGrid(r) : timeGrid(r)}${popover()}</div>`;
}

function miniMonth() {
  const month = cal.mini || anchorDay().slice(0, 7), first = month + '-01', a = dayOf(first);
  const start = addDays(first, -a.getDay());
  const lo = cal.range ? cal.range.days[0].date : '', hi = cal.range ? cal.range.days[cal.range.days.length - 1].date : '';
  let cells = WD.map(w => `<span>${w[0]}</span>`).join('');
  for (let i = 0; i < 42; i++) {
    const d = addDays(start, i);
    cells += `<button type="button" data-cal="goto" data-date="${d}" class="${d.slice(0, 7) !== month ? 'out' : ''}${d === state.data.today ? ' today' : ''}${calView() !== 'month' && d >= lo && d <= hi ? ' in' : ''}"
      aria-label="${esc(fmtBand(dayOf(d)))}">${dayOf(d).getDate()}</button>`;
  }
  return `<div class="mini"><div class="mini-head"><b>${MONTHS[a.getMonth()]} ${a.getFullYear()}</b>
      <span><button type="button" data-cal="mini-prev" aria-label="Earlier month">‹</button><button type="button" data-cal="mini-next" aria-label="Later month">›</button></span></div>
    <div class="mini-grid">${cells}</div></div>`;
}

function sideCalendar() {
  const c = state.data.calendar;
  if (!c || !c.started) return '';
  const cats = [['class', 'Classes', 'class'], ['work', 'Work', 'work'], ['standing', 'Standing', 'standing'], ['busy', 'Yours', 'busy'],
    ...c.links.map(l => ['link:' + l.name, l.name, 'calendar']), ['meal', 'Meals and travel', 'meal']];
  return `<div class="cal-side">${miniMonth()}
    <ul class="cats">${cats.map(([key, label, kind]) => `<li><label><input type="checkbox" data-cal-cat="${esc(key)}"${cal.hide.has(key) ? '' : ' checked'}>
      <span class="key k-${kind}"></span>${esc(label)}</label></li>`).join('')}</ul></div>`;
}

function shiftAnchor(dir) {
  const v = calView(), a = anchorDay();
  if (v === 'month') { const d = dayOf(a.slice(0, 8) + '01'); d.setMonth(d.getMonth() + dir); cal.anchor = isoDay(d); }
  else cal.anchor = addDays(a, dir * (v === 'week' ? 7 : 1));
  cal.mini = null;
}

function startCreate() {
  const now = new Date(state.data.now), today = state.data.today;
  const inRange = cal.range && cal.range.days.some(d => d.date === today);
  const date = calView() === 'month' ? (inRange ? today : anchorDay()) : inRange ? today : cal.range.days[0].date;
  const s = date === today ? Math.ceil((now.getHours() * 60 + now.getMinutes() + 1) / 60) * 60 : 9 * 60;
  openNew(date, s, s + 60);
}

document.addEventListener('click', e => {
  if (state.tab !== 'calendar' || !state.data || !state.data.calendar) return;
  const el = e.target.closest('[data-cal]');
  if (!el) {
    const blk = e.target.closest('.cg-b');
    if (blk && e.detail === 0) return openBlock(blk.dataset.date, +blk.dataset.i, `.cg-b[data-date="${blk.dataset.date}"][data-i="${blk.dataset.i}"]`);
    const cell = !e.target.closest('.cal-pop') && e.target.closest('.mg-cell');
    if (cell) return openNew(cell.dataset.date, 12 * 60, 13 * 60);
    if (cal.pop && !e.target.closest('.cal-pop, .cg-body, .toast')) { cal.pop = null; render(); }
    return;
  }
  const id = el.dataset.id, date = el.dataset.date, p = cal.pop;
  switch (el.dataset.cal) {
    case 'close': cal.pop = null; return render();
    case 'today': return calGo(() => { cal.anchor = null; cal.mini = null; });
    case 'prev': return calGo(() => shiftAnchor(-1));
    case 'next': return calGo(() => shiftAnchor(1));
    case 'view': return calGo(() => { cal.view = id; save('calView', id); });
    case 'day': return calGo(() => { cal.anchor = date; cal.view = 'day'; cal.mini = null; });
    case 'goto': return calGo(() => { cal.anchor = date; cal.mini = null; });
    case 'mini-prev': case 'mini-next': {
      const d = dayOf((cal.mini || anchorDay().slice(0, 7)) + '-01');
      d.setMonth(d.getMonth() + (el.dataset.cal === 'mini-next' ? 1 : -1));
      cal.mini = isoDay(d).slice(0, 7);
      return render();
    }
    case 'chip': return openBlock(date, +el.dataset.i, `.mc[data-date="${date}"][data-i="${el.dataset.i}"]`);
    case 'create': return startCreate();
    case 'links': cal.panel = cal.panel === 'links' ? null : 'links'; cal.pop = null; return render();
    case 'panel-close': cal.panel = null; return render();
    case 'provider': cal.provider = id; return render();
    case 'connect': cal.panel = 'links'; render(); { const u = $('form[data-cal-form="link"] [name=url]'); if (u) u.focus(); } return;
    case 'builtin': return calAct({ action: 'start' }, '', false);
    case 'unlink': cal.unlink = id; return render();
    case 'unlink-no': cal.unlink = null; return render();
    case 'unlink-yes': cal.unlink = null; return calAct({ action: 'remove_link', id }, 'Calendar removed.', false);
    case 'refresh': return calAct({ action: 'refresh' }, 'Calendars updated.', false);
    case 'edit': {
      const sr = seriesOf(p.block);
      return openPop({ kind: 'edit', sr, src: sr.src, on: p.date, weekly: !!sr.days, name: sr.name, date: sr.date || p.date,
        days: sr.days || [], s: mins(sr.start), e: mins(sr.end), sel: p.sel });
    }
    case 'del': p.deleting = true; return render();
    case 'del-no': p.deleting = false; return render();
    case 'del-one': return seriesAct(seriesOf(p.block), 'del-one', { on: p.date });
    case 'del-from': return seriesAct(seriesOf(p.block), 'del-from', { on: p.date });
    case 'del-all': return seriesAct(seriesOf(p.block), 'del-all');
    case 'apply-one': return seriesAct(p.draft.sr, 'one', { on: p.draft.on, date: p.draft.date,
      start: hhmmOf(p.draft.s), end: hhmmOf(p.draft.e) });
    case 'apply-all': case 'apply-from': {
      const sr = p.draft.sr, was = DAY_KEYS[(dayOf(p.draft.on).getDay() + 6) % 7], now = DAY_KEYS[(dayOf(p.draft.date).getDay() + 6) % 7];
      const days = DAY_KEYS.filter(d => sr.days.map(x => (x === was ? now : x)).includes(d));
      const data = { start: hhmmOf(p.draft.s), end: hhmmOf(p.draft.e), days };
      return el.dataset.cal === 'apply-all' ? seriesAct(sr, 'all', data) : seriesAct(sr, 'from', { on: p.draft.date < p.draft.on ? p.draft.date : p.draft.on, ...data });
    }
  }
});

document.addEventListener('change', e => {
  const key = e.target.dataset && e.target.dataset.calCat;
  if (!key) return;
  if (e.target.checked) cal.hide.delete(key); else cal.hide.add(key);
  save('calHide', [...cal.hide]);
  render();
});

document.addEventListener('submit', e => {
  const f = e.target.closest('form[data-cal-form]');
  if (!f) return;
  e.preventDefault();
  if (f.dataset.calForm === 'link') {
    const had = state.data.calendar.links.length;
    return calAct({ action: 'add_link', name: f.elements.name.value, url: f.elements.url.value }, 'Calendar connected.', false)
      .then(() => {
        if (state.data.calendar.links.length === had) return;
        const u = $('form[data-cal-form="link"]'); if (u) u.reset();
        cal.panel = null; render();
      });
  }
  const p = cal.pop, days = [...f.querySelectorAll('[name=days]:checked')].map(x => x.value);
  const body = { start: f.elements.start.value, end: f.elements.end.value };
  if (f.elements.name) body.name = f.elements.name.value;
  const date = f.elements.date ? f.elements.date.value : '';
  const scope = e.submitter && e.submitter.dataset.scope;
  if (scope === 'one') return seriesAct(p.sr, 'one', { on: p.on, ...body });
  if (p.src === 'meal') body.days = DAY_KEYS;
  else if (days.length) body.days = days; else if (date) body.date = date; else return toast('Pick a date or weekdays.');
  if (scope === 'from') return seriesAct(p.sr, 'from', { on: p.on, ...body });
  if (p.kind === 'new') return calAct({ action: 'add_block', ...body }, `Added "${body.name.trim()}".`);
  return seriesAct(p.sr, 'all', body);
});

document.addEventListener('keydown', e => {
  if (state.tab !== 'calendar' || !state.data || !state.data.calendar || !state.data.calendar.started) return;
  if (e.key === 'Escape' && (cal.pop || cal.panel)) { cal.pop = null; cal.panel = null; return render(); }
  if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  const k = e.key.toLowerCase();
  if (k === 't') calGo(() => { cal.anchor = null; cal.mini = null; });
  else if (k === 'j' || k === 'n') calGo(() => shiftAnchor(1));
  else if (k === 'k' || k === 'p') calGo(() => shiftAnchor(-1));
  else if (k === 'd' || k === 'w' || k === 'm') calGo(() => { cal.view = { d: 'day', w: 'week', m: 'month' }[k]; save('calView', cal.view); });
  else if (k === 'c') startCreate();
});

let drag = null;
const minuteAt = (col, y) => +col.dataset.from + (y - col.getBoundingClientRect().top) / HOUR_PX * 60;

document.addEventListener('pointerdown', e => {
  if (state.tab !== 'calendar' || e.button !== 0 || e.target.closest('.cal-pop')) return;
  const col = e.target.closest('.cg-col');
  if (!col) return;
  const blk = e.target.closest('.cg-b'), from = +col.dataset.from, to = +col.dataset.to;
  const base = { col, from, to, x0: e.clientX, y0: e.clientY, moved: false, touch: e.pointerType === 'touch' };
  if (blk && blk.classList.contains('draft')) return;
  if (blk) {
    const r = blk.getBoundingClientRect(), own = blk.classList.contains('own') && !base.touch;
    const mode = !own ? 'tap' : e.clientY > r.bottom - 7 ? 'end' : e.clientY < r.top + 5 && r.height > 28 ? 'start' : 'move';
    drag = { ...base, mode, blk, s0: +blk.dataset.s, e0: +blk.dataset.e, s: +blk.dataset.s, e: +blk.dataset.e, date: col.dataset.date };
  } else {
    const a = clamp(Math.floor(minuteAt(col, e.clientY) / SNAP) * SNAP, from, to - SNAP);
    drag = { ...base, mode: 'new', a, s: a, e: a + 60, date: col.dataset.date };
  }
});

document.addEventListener('pointermove', e => {
  const d = drag;
  if (!d || d.mode === 'tap' || d.touch) return;
  if (!d.moved && Math.hypot(e.clientX - d.x0, e.clientY - d.y0) < 5) return;
  if (!d.moved) {
    d.moved = true;
    const pop = $('#cal-pop'); if (pop) pop.remove();
    cal.pop = null;
    document.body.classList.add('cal-dragging');
    if (d.mode === 'new') {
      d.blk = document.createElement('div');
      d.blk.className = 'cg-b k-busy draft';
      d.blk.innerHTML = '<span class="cg-bm"></span>';
      d.col.appendChild(d.blk);
    } else {
      d.blk.classList.add('dragging');
      d.blk.style.left = '0'; d.blk.style.width = '100%';
    }
  }
  const dm = snap((e.clientY - d.y0) / HOUR_PX * 60);
  if (d.mode === 'new') {
    const cur = clamp(snap(minuteAt(d.col, e.clientY)), d.from, d.to);
    d.s = Math.min(d.a, cur); d.e = Math.max(d.a + SNAP, cur);
    if (cur < d.a) d.e = d.a + SNAP;
  } else if (d.mode === 'move') {
    const len = d.e0 - d.s0;
    d.s = clamp(d.s0 + dm, d.from, d.to - len); d.e = d.s + len;
    const over = [...document.querySelectorAll('.cg-col')].find(c => { const r = c.getBoundingClientRect(); return e.clientX >= r.left && e.clientX < r.right; });
    if (over && over !== d.blk.parentElement) { over.appendChild(d.blk); d.date = over.dataset.date; }
  } else if (d.mode === 'end') d.e = clamp(d.e0 + dm, d.s0 + SNAP, d.to);
  else d.s = clamp(d.s0 + dm, d.from, d.e0 - SNAP);
  d.blk.style.top = `${(d.s - d.from) / 60 * HOUR_PX}px`;
  d.blk.style.height = `${Math.max((d.e - d.s) / 60 * HOUR_PX, 14)}px`;
  const label = d.blk.querySelector('.cg-bm');
  if (label) label.textContent = spanText(d.s, d.e);
});

function endDrag(d) {
  document.body.classList.remove('cal-dragging');
  if (!d.moved) {
    if (d.blk) return openBlock(d.blk.dataset.date, +d.blk.dataset.i, `.cg-b[data-date="${d.blk.dataset.date}"][data-i="${d.blk.dataset.i}"]`);
    return openNew(d.date, d.a, d.a + 60);
  }
  if (d.mode === 'new') return openNew(d.date, d.s, d.e);
  const on = d.blk.dataset.date, day = cal.range.days.find(x => x.date === on);
  const sr = day && day.blocks[+d.blk.dataset.i] && seriesOf(day.blocks[+d.blk.dataset.i]);
  if (!sr || (d.s === d.s0 && d.e === d.e0 && d.date === on)) return render();
  if (sr.days) {
    return openPop({ kind: 'apply', sel: '.cg-b.draft',
      draft: { sr, hide: `${on}:${d.blk.dataset.i}`, on, date: d.date, s: d.s, e: d.e, name: sr.name } });
  }
  seriesAct(sr, 'all', { date: d.date, start: hhmmOf(d.s), end: hhmmOf(d.e) });
}

document.addEventListener('pointerup', () => {
  const d = drag;
  drag = null;
  if (d) setTimeout(() => endDrag(d), 0);
});
document.addEventListener('pointercancel', () => {
  if (drag && drag.moved) { document.body.classList.remove('cal-dragging'); render(); }
  drag = null;
});
calNarrow.addEventListener('change', () => { if (state.tab === 'calendar') calGo(); });
