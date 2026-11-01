'use strict';

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function post(path, body) {
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
}
function esc(s) { return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function scoreClass(s) { return s == null ? 'score-lo' : s >= 80 ? 'score-hi' : s >= 60 ? 'score-mid' : 'score-lo'; }
function fmtDate(ts) { return ts ? new Date(ts * 1000).toLocaleDateString() : ''; }
// Files come from one route; the server finds the folder from the user's own
// analysis row, so the URL only names the job and which file.
function docUrl(jobId, kind) { return `/api/jobs/download?job_id=${jobId}&kind=${kind}`; }
const STATUS_TITLES = {
  none: 'No resume or cover letter yet',
  pending: 'Waiting in line',
  in_process: 'Being generated right now',
  retry: 'Failed an attempt, will try again',
  done: 'Resume and cover letter ready',
  failed: 'Gave up after 3 attempts',
  cleared: 'Cleared -- click Regenerate to make it again',
};
function statusChip(j) {
  const detail = j.status === 'failed' || j.status === 'retry' ? (j.error_msg || j.error_kind || '') : '';
  const attempts = (j.status === 'retry' || j.status === 'failed' || j.status === 'in_process') && j.attempts ? ` ${j.attempts}/3` : '';
  const title = [STATUS_TITLES[j.status] || '', detail].filter(Boolean).join(' -- ');
  return `<span class="badge badge-${j.status}" title="${esc(title)}">${esc(j.status.replace('_', ' '))}${attempts}</span>`;
}
// missing: [{gap, why, example}, ...] from the scoring step of an analysis.
function _missingHtml(missing) {
  if (!missing || !missing.length) return '';
  return `<div style="margin-top:8px">${missing.map(m => `
      <div class="dim" style="font-size:12px;margin-top:4px">
        <b style="color:var(--warn)">${esc(m.gap || '')}</b>${m.why ? ' -- ' + esc(m.why) : ''}
        ${m.example ? `<br>example that would close it: ${esc(m.example)}` : ''}
      </div>`).join('')}</div>`;
}
const STATUS_FLOW = ['phone_screen', 'interview', 'offer', 'rejected', 'ghosted', 'withdrawn'];
let currentPage = 1;
let currentCompany = null;      // set = flat list scoped to one company (via a card click)
let showingCompanyCards = false; // the "Group by company" toggle -- off by default, flat list is the landing view

function commonFilterParams() {
  const params = new URLSearchParams({
    eng_only: document.getElementById('f-eng').checked ? '1' : '0',
    min_score: document.getElementById('f-minscore').value || '0',
    level: document.getElementById('f-level').value,
  });
  const days = document.getElementById('f-date').value;
  if (days) params.set('days', days);
  const source = document.getElementById('f-source').value;
  if (source) params.set('source', source);
  const status = document.getElementById('f-status').value;
  if (status) params.set('status', status);
  const q = document.getElementById('f-search').value.trim();
  if (q) params.set('q', q);
  return params;
}

// ── company cards (optional, off by default) ─────────────────────────
async function loadCompanies() {
  const data = await api('/api/jobs/companies?' + commonFilterParams().toString());
  const el = document.getElementById('company-cards');
  el.innerHTML = '';
  for (const c of data.companies) {
    const card = document.createElement('div');
    card.className = 'company-card';
    const score = c.best_score == null ? '–' : Math.round(c.best_score);
    card.innerHTML = `
      <div class="cc-score ${scoreClass(c.best_score)}" style="display:inline-block;padding:2px 8px;border-radius:6px">${score}</div>
      <div class="cc-name">${esc(c.company)}</div>
      <div class="cc-meta">${c.count} matching posting${c.count === 1 ? '' : 's'}</div>
      <div class="cc-meta">${esc(c.best_title)}</div>`;
    card.onclick = () => drillIntoCompany(c.company);
    el.appendChild(card);
  }
  document.getElementById('f-count').textContent = `${data.companies.length} companies with a matching posting`;
  if (!data.companies.length) el.innerHTML = '<p class="dim">No companies match these filters.</p>';
}

function drillIntoCompany(company) {
  currentCompany = company;
  currentPage = 1;
  showingCompanyCards = false;
  document.getElementById('company-cards').hidden = true;
  document.getElementById('job-list').hidden = false;
  document.getElementById('pagination').hidden = false;
  showCompanyChip();
  loadJobs();
}

function showCompanyChip() {
  const chip = document.getElementById('company-scope-chip');
  chip.hidden = !currentCompany;
  if (currentCompany) document.getElementById('company-scope-name').textContent = currentCompany;
}

document.getElementById('btn-clear-company').onclick = () => {
  currentCompany = null;
  currentPage = 1;
  showCompanyChip();
  loadJobs();
};

document.getElementById('btn-group-by-company').onclick = () => {
  showingCompanyCards = !showingCompanyCards;
  document.getElementById('btn-group-by-company').textContent =
    showingCompanyCards ? 'Back to list' : 'Group by company';
  document.getElementById('company-cards').hidden = !showingCompanyCards;
  document.getElementById('job-list').hidden = showingCompanyCards;
  document.getElementById('pagination').hidden = showingCompanyCards;
  if (showingCompanyCards) loadCompanies(); else loadJobs();
};

// ── postings: flat list, the default view ────────────────────────────
async function loadJobs() {
  const params = commonFilterParams();
  params.set('page', currentPage);
  params.set('per_page', 10);
  if (currentCompany) params.set('company', currentCompany);

  const data = await api('/api/jobs/list?' + params.toString());
  const list = document.getElementById('job-list');
  document.getElementById('f-count').textContent =
    `${data.total_matched} matching · ${data.total_unfiltered} fetched postings total`;
  list.innerHTML = '';
  for (const j of data.jobs) {
    const row = document.createElement('div');
    row.className = 'job-row';
    const score = j.score != null ? Math.round(j.score) : '–';
    const tierNote = ['staff', 'senior_staff', 'manager'].includes(j.seniority_tier)
      ? `<span class="badge badge-tier">${j.seniority_tier.replace('_', ' ')}</span>` : '';
    row.innerHTML = `
      <div class="job-score ${scoreClass(j.score)}" title="${j.score != null ? esc(j.reason || '') : 'Not scored yet'}">${score}</div>
      <div class="job-main">
        <div class="job-title">${esc(j.title)}</div>
        <div class="job-company">${esc(j.company)} · ${esc(j.source)} · ${fmtDate(j.first_seen_ts)}</div>
      </div>
      <div class="job-badges">
        ${tierNote}
        ${statusChip(j)}
      </div>`;
    row.onclick = () => openJob(j.id);
    list.appendChild(row);
  }
  if (!data.jobs.length) list.innerHTML = '<p class="dim">No postings match these filters.</p>';

  document.getElementById('p-info').textContent = `page ${data.page} of ${data.total_pages}`;
  document.getElementById('p-prev').disabled = data.page <= 1;
  document.getElementById('p-next').disabled = data.page >= data.total_pages;
}

let jobPoll = null;
function fmtDur(ms) { return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms'; }

function _analysisHtml(job, a) {
  if (!a) return '<p class="dim" style="margin-top:8px">No resume or cover letter yet.</p>';
  const row = {status: a.status, attempts: a.attempts, error_kind: a.error_kind, error_msg: a.error_msg};
  const times = [a.started_ts && `started ${new Date(a.started_ts * 1000).toLocaleString()}`,
                 a.finished_ts && `finished ${new Date(a.finished_ts * 1000).toLocaleString()}`].filter(Boolean).join(' · ');
  let html = `<div class="app-entry"><div class="app-entry-meta">${statusChip(row)}
      ${a.score != null ? `<span class="badge badge-llm">fit ${Math.round(a.score)}</span>` : ''}
      ${a.provider ? `<span class="badge badge-new">${esc(a.provider)}</span>` : ''}
      <span class="dim">${esc(times)}</span></div>`;
  if (a.error_msg && (a.status === 'failed' || a.status === 'retry')) {
    html += `<p style="color:var(--bad);margin-top:6px">${esc(a.error_kind)}: ${esc(a.error_msg)}</p>`;
  }
  if (a.status === 'done') {
    html += `
      ${a.reason ? `<div style="font-size:13px;margin-top:4px">${esc(a.reason)}</div>` : ''}
      ${a.requirements.length ? `<div class="kw-list" style="margin-top:6px">${a.requirements.map(r => {
        const cls = r.presence === 'no' ? 'kw-no' : (r.depth === 'quantified' ? 'kw-yes' : 'kw-weak');
        const label = r.requirement + (r.importance === 'required' ? '' : ' (nice-to-have)') + ` (${r.earned}/${r.weight} pts)`;
        return `<span class="kw ${cls}" title="${esc(r.presence)}/${esc(r.depth)}">${esc(label)}</span>`;
      }).join('')}</div>` : ''}
      ${_missingHtml(a.gaps)}
      <div class="doc-links">
        <a class="doc-link" href="${docUrl(job.id, 'resume')}">Resume <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${docUrl(job.id, 'resume_docx')}">.docx</a>
        <a class="doc-link" href="${docUrl(job.id, 'cover')}">Cover letter <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${docUrl(job.id, 'cover_docx')}">.docx</a>
      </div>`;
  }
  if (a.steps.length) {
    html += `<details style="margin-top:8px"><summary class="dim" style="font-size:12px">timings (${a.steps.length} step${a.steps.length === 1 ? '' : 's'})</summary>
      <table style="font-size:12px;margin-top:4px">${a.steps.map(st => `<tr>
        <td>try ${st.attempt}</td><td>${esc(st.step)}</td><td>${fmtDur(st.duration_ms)}</td>
        <td>${st.prompt_tokens != null ? `${st.prompt_tokens} in / ${st.completion_tokens} out${st.cached_tokens ? ` / ${st.cached_tokens} cached` : ''}` : ''}</td>
        <td>${st.ok ? '' : esc(st.error || 'failed')}</td></tr>`).join('')}</table></details>`;
  }
  return html + '</div>';
}

async function openJob(id) {
  clearTimeout(jobPoll);
  const data = await api('/api/jobs/detail?id=' + id);
  const j = data.job, a = data.analysis, app = data.application;
  const panel = document.getElementById('job-detail');
  const status = a ? a.status : 'none';
  const active = status === 'pending' || status === 'in_process' || status === 'retry';
  const form = document.getElementById('jd-applied-form');
  const formOpen = form && !form.hidden && !panel.hidden;
  if (formOpen) return;            // a background refresh must not wipe a half-filled form

  panel.innerHTML = `
    <button class="close-x" onclick="closeJob()">✕</button>
    <div class="jd-title">${esc(j.title)}</div>
    <div class="jd-company">${esc(j.company)} ${j.url ? `· <a href="${esc(j.url)}" target="_blank">posting ↗</a>` : ''}</div>
    <div class="row" style="justify-content:flex-start;gap:8px;margin-top:10px">
      <button id="jd-regen" ${status === 'in_process' ? 'disabled' : ''} class="${status === 'none' || status === 'cleared' ? 'primary' : ''}">${
        status === 'none' ? 'Generate resume + cover letter' : status === 'cleared' ? 'Generate again' : 'Regenerate'}</button>
      <button id="jd-clear" ${status === 'in_process' || status === 'none' || status === 'cleared' ? 'disabled' : ''}>Clear</button>
      <button id="jd-mark-applied" ${app ? 'disabled' : ''} class="${app ? '' : 'primary'}">${app ? 'Applied ✓' : 'Mark as applied'}</button>
    </div>
    <div id="jd-result" style="margin-top:6px"></div>
    <div id="jd-applied-form" class="referral-form" hidden style="margin-top:10px">
      <select class="via-select">
        <option value="company_site">company site</option>
        <option value="linkedin">LinkedIn</option>
        <option value="referral">referral</option>
        <option value="recruiter">recruiter</option>
        <option value="other">other</option>
      </select>
      <input class="referral-name" type="text" placeholder="referral name (optional)">
      <input class="referral-note" type="text" placeholder="referral note (optional)">
      <button id="jd-confirm-applied" class="primary small">Confirm</button>
    </div>
    <h3 style="font-size:15px;margin-top:18px">Resume + cover letter</h3>
    ${_analysisHtml(j, a)}
    <h3 style="font-size:15px;margin-top:18px">Job description</h3>
    <div class="jd-body">${esc(j.description_raw)}</div>
  `;
  const act = (btnId, path) => {
    document.getElementById(btnId).onclick = async () => {
      const btn = document.getElementById(btnId);
      btn.disabled = true;
      try {
        await post(path, {job_id: j.id});
        openJob(id); loadJobs();
      } catch (e) {
        document.getElementById('jd-result').innerHTML = `<p style="color:var(--bad)">${esc(e.message)}</p>`;
        btn.disabled = false;
      }
    };
  };
  act('jd-regen', '/api/jobs/regenerate');
  act('jd-clear', '/api/jobs/clear');
  if (!app) {
    document.getElementById('jd-mark-applied').onclick = () => {
      document.getElementById('jd-applied-form').hidden = false;
    };
    document.getElementById('jd-confirm-applied').onclick = async () => {
      await post('/api/jobs/mark-applied', {
        job_id: j.id,
        via: panel.querySelector('.via-select').value,
        referral_name: panel.querySelector('.referral-name').value.trim(),
        referral_note: panel.querySelector('.referral-note').value.trim(),
      });
      closeJob();
      loadJobs();
      if (document.getElementById('view-applications').hidden === false) loadApplications();
    };
  }
  document.getElementById('overlay').hidden = false;
  panel.hidden = false;
  document.getElementById('overlay').onclick = closeJob;
  // The worker moves it along on its own; keep the panel and list in step.
  if (active) jobPoll = setTimeout(() => { if (!panel.hidden) { openJob(id); loadJobs(); } }, 3000);
}
function closeJob() {
  clearTimeout(jobPoll);
  document.getElementById('overlay').hidden = true;
  document.getElementById('job-detail').hidden = true;
}

// ── manual add ────────────────────────────────────────────────────────
function openManual() {
  document.getElementById('m-company').value = '';
  document.getElementById('m-url').value = '';
  document.getElementById('m-text').value = '';
  document.getElementById('m-title').value = '';
  document.getElementById('m-location').value = '';
  document.getElementById('m-result').innerHTML = '';
  document.getElementById('overlay').hidden = false;
  document.getElementById('manual-modal').hidden = false;
}
function closeManual() {
  document.getElementById('overlay').hidden = true;
  document.getElementById('manual-modal').hidden = true;
}

// parseJobPaste/MAX_TITLE_LEN live in jobparse.js (loaded before this file)
// -- pure text parsing, no DOM, so it's Node-require()-able for real tests
// without pulling in the rest of this file.

// Auto-fills Title/Location from the pasted text, but only while the user
// hasn't typed their own value into those fields yet -- once they've
// edited Title or Location directly, further edits to the paste never
// clobber what they typed.
document.getElementById('m-text').addEventListener('input', () => {
  const titleField = document.getElementById('m-title');
  const locationField = document.getElementById('m-location');
  if (titleField.dataset.userEdited || locationField.dataset.userEdited) return;
  const {title, location} = parseJobPaste(document.getElementById('m-text').value.trim());
  if (!titleField.dataset.userEdited) titleField.value = title;
  if (!locationField.dataset.userEdited) locationField.value = location;
});
for (const id of ['m-title', 'm-location']) {
  document.getElementById(id).addEventListener('input', (e) => { e.target.dataset.userEdited = '1'; });
}

async function submitManual() {
  const raw = document.getElementById('m-text').value.trim();
  const title = document.getElementById('m-title').value.trim();
  const location = document.getElementById('m-location').value.trim();
  const body = {company: document.getElementById('m-company').value.trim(), title, location,
                url: document.getElementById('m-url').value.trim(), text: raw};
  if (!body.company || !title || !raw) {
    document.getElementById('m-result').innerHTML = '<p style="color:var(--bad)">Company, title, and the pasted posting are all required.</p>';
    return;
  }
  let res;
  try {
    res = await post('/api/jobs/manual', body);
  } catch (e) {
    // post()/api() throws on any non-2xx response (e.g. the server's "title is
    // implausibly long" guard) instead of returning {error}. Without this catch,
    // that throw aborted the function right here -- before #m-result was ever
    // written -- so a rejected add looked like nothing happened at all: no error,
    // no job, modal just sitting there. Found live: exactly that silent failure.
    document.getElementById('m-result').innerHTML = `<p style="color:var(--bad)">${esc(e.message)}</p>`;
    return;
  }
  const note = !res.is_new ? 'Already in your Postings list'
    : res.queued ? 'Added -- resume + cover letter are queued and will be generated next'
    : 'Added';
  document.getElementById('m-result').innerHTML = `
    <div class="score-block">
      <div style="flex:1">
        <div style="font-size:12px;color:var(--ok);font-weight:600">✓ ${note} (job #${res.job_id})</div>
        <div class="dim" style="font-size:12px;margin-top:2px">title: ${esc(title)}${location ? ' &middot; location: ' + esc(location) : ' &middot; no location given'}</div>
      </div>
    </div>
    <div class="row" style="justify-content:flex-start;margin-top:10px">
      <button id="m-done" class="primary small">Close &amp; view in list</button>
    </div>`;
  document.getElementById('m-done').onclick = () => { closeManual(); loadJobs(); };
  loadJobs();
}

// ── applications view ────────────────────────────────────────────────
async function loadApplications() {
  const data = await api('/api/applications/list');
  const list = document.getElementById('app-list');
  list.innerHTML = '';
  if (!data.applications.length) {
    list.innerHTML = '<p class="dim">No applications tracked yet.</p>';
    return;
  }
  for (const a of data.applications) {
    const div = document.createElement('div');
    div.className = 'app-row';
    const hist = a.events.map(ev =>
      `<div>${new Date(ev.ts * 1000).toLocaleString()} — <b>${esc(ev.status)}</b> ${esc(ev.note || '')}</div>`).join('');
    div.innerHTML = `
      <div class="app-head">
        <div>
          <div class="job-title">${esc(a.title)}</div>
          <div class="job-company">${esc(a.company)}${a.score != null ? ' · fit ' + Math.round(a.score) : ''} · status: <b>${esc(a.status)}</b></div>
        </div>
        <span class="badge badge-done">${esc(a.status)}</span>
      </div>
      ${a.has_files ? `
      <div class="doc-links" style="margin-top:8px">
        <a class="doc-link" href="${docUrl(a.job_id, 'resume')}">Resume <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${docUrl(a.job_id, 'resume_docx')}">.docx</a>
        <a class="doc-link" href="${docUrl(a.job_id, 'cover')}">Cover letter <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${docUrl(a.job_id, 'cover_docx')}">.docx</a>
        <a class="doc-link alt" href="${docUrl(a.job_id, 'jd')}">Job description</a>
      </div>` : ''}
      <div class="status-track">
        ${STATUS_FLOW.map(s => `<button class="add-status" data-status="${s}">${s.replace('_',' ')}</button>`).join('')}
        <span class="dim" style="align-self:center;font-size:12px">via ${esc(a.applied_via || '?')}${a.referral_name ? ' · referral: ' + esc(a.referral_name) : ''}</span>
      </div>
      <div class="status-hist">${hist}</div>
    `;
    div.querySelectorAll('.add-status').forEach(btn => {
      btn.onclick = async () => {
        const note = prompt(`Note for "${btn.dataset.status}" (optional):`) || '';
        await post('/api/applications/add-status', {app_id: a.id, status: btn.dataset.status, note});
        loadApplications();
      };
    });
    list.appendChild(div);
  }
}

// ── gaps view ────────────────────────────────────────────────────────
// Distinct real gaps across every scored posting, clustered by meaning
// (one LLM call, run on demand), most-common first.
async function loadGaps(force) {
  const list = document.getElementById('gaps-list');
  const count = document.getElementById('gaps-count');
  // Plain tab-open reads the cached result (near-instant, no LLM call) --
  // only an explicit refresh (the button)
  // recomputes. Used to re-cluster with a real call every single time
  // this tab was opened, which is the actual reason it looked slow. A
  // plain open still shows SOME placeholder, not nothing -- the very
  // first-ever open (no cache yet) falls through to a real 20-40s
  // compute server-side with no way for this code to know that in
  // advance, so it can't promise "instant" here either.
  list.innerHTML = force
    ? '<p class="dim">Clustering gaps across your scored postings… (one real LLM call, can take 20-40s)</p>'
    : '<p class="dim">Loading…</p>';
  count.textContent = '';
  let data;
  try {
    data = await api(force ? '/api/jobs/missing-clusters?refresh=1' : '/api/jobs/missing-clusters');
  } catch (e) {
    // Used to just hang on "Clustering…" forever with no error shown if
    // this failed -- the exact "nothing is visible" symptom, since a
    // thrown fetch here was never caught anywhere. Real failure, real
    // retry button now, not a silent stuck state.
    list.innerHTML = `<p class="dim">Clustering failed: ${esc(e.message || 'unknown error')}. <button class="small" onclick="loadGaps(true)">Retry</button></p>`;
    return;
  }
  const computedNote = data.computed_ts ? ` · computed ${new Date(data.computed_ts * 1000).toLocaleString()}` : '';
  count.textContent = `${data.scored_count} posting(s) scored${computedNote}`;
  if (!data.clusters.length) {
    list.innerHTML = '<p class="dim">No gaps to show yet -- they appear as resumes are generated.</p>';
    return;
  }
  list.innerHTML = '';
  for (const c of data.clusters) {
    const row = document.createElement('div');
    row.className = 'gap-row';
    row.innerHTML = `
      <div class="gap-count">${c.count}</div>
      <div class="gap-label">${esc(c.label)}</div>`;
    list.appendChild(row);
  }
}
document.getElementById('btn-gaps-refresh').onclick = () => loadGaps(true);

// ── wiring ───────────────────────────────────────────────────────────
function showView(name) {
  document.getElementById('view-postings').hidden = name !== 'postings';
  document.getElementById('view-applications').hidden = name !== 'applications';
  document.getElementById('view-profile').hidden = name !== 'profile';
  document.getElementById('view-gaps').hidden = name !== 'gaps';
  document.getElementById('view-admin').hidden = name !== 'admin';
  document.getElementById('tab-postings').classList.toggle('active', name === 'postings');
  document.getElementById('tab-applications').classList.toggle('active', name === 'applications');
  document.getElementById('tab-profile').classList.toggle('active', name === 'profile');
  document.getElementById('tab-gaps').classList.toggle('active', name === 'gaps');
  document.getElementById('tab-admin').classList.toggle('active', name === 'admin');
  if (name === 'applications') loadApplications();
  if (name === 'gaps') loadGaps();
  if (name === 'admin') loadAdminUsers();
  if (name === 'profile') {
    loadTelegramConfig();
  }
}

document.getElementById('tab-postings').onclick = () => showView('postings');
document.getElementById('tab-applications').onclick = () => showView('applications');
document.getElementById('tab-profile').onclick = () => showView('profile');
document.getElementById('tab-admin').onclick = () => showView('admin');
document.getElementById('tab-gaps').onclick = () => showView('gaps');
document.getElementById('btn-manual').onclick = openManual;
document.getElementById('m-cancel').onclick = closeManual;
document.getElementById('m-submit').onclick = submitManual;
function flashCount(msg, title) {
  const el = document.getElementById('f-count');
  const prev = el.textContent;
  el.textContent = msg;
  // title is a real tooltip, not part of the 4s flash -- left in place after
  // the text reverts so "hover to see the last fetch's real breakdown" still
  // works later, instead of vanishing the one moment it'd actually get read.
  if (title) el.title = title;
  setTimeout(() => { if (el.textContent === msg) el.textContent = prev; }, 4000);
}
function reloadCurrentView() {
  if (showingCompanyCards) loadCompanies(); else loadJobs();
}

// Fetch only. Runs in the background (a real fetch across every board takes a
// minute or more), so start it, then poll the worker's status until it ends.
function openFetchResult(f) {
  const bits = [`${f.skipped_location} wrong location`, `${f.skipped_title} wrong title`, `${f.skipped_age} too old`];
  if (f.errors) bits.push(`${f.errors} source(s) errored`);
  document.getElementById('fr-body').innerHTML = `
    <div class="score-block" style="border:1px solid ${f.ok === false ? 'var(--bad)' : 'var(--accent)'}">
      <div><div class="num">${f.new}</div><div class="dim" style="font-size:13px">new just now</div></div>
      <div style="flex:1">
        ${f.ok === false ? `<div style="font-size:14px;color:var(--bad)">Fetch failed: ${esc(f.error)}</div>`
          : `<div style="font-size:14px">${f.seen} already tracked, ${f.rows_created} queued for generation · skipped: ${esc(bits.join(', '))}.</div>`}
      </div>
    </div>`;
  document.getElementById('overlay').hidden = false;
  document.getElementById('fetch-result-modal').hidden = false;
  document.getElementById('overlay').onclick = closeFetchResult;
}
function closeFetchResult() {
  document.getElementById('overlay').hidden = true;
  document.getElementById('fetch-result-modal').hidden = true;
}
async function pollFetchStatus(btn) {
  while (true) {
    await new Promise(r => setTimeout(r, 1500));
    const f = (await api('/api/jobs/fetch-status')).fetch;
    if (!f.running) {
      btn.disabled = false; btn.textContent = 'Fetch new';
      flashCount(f.ok === false ? 'Fetch failed -- check server logs.' : `${f.new} new posting(s) fetched.`);
      openFetchResult(f);
      reloadCurrentView();
      return;
    }
    btn.textContent = 'Fetching…';
  }
}
document.getElementById('btn-refetch').onclick = async () => {
  const btn = document.getElementById('btn-refetch');
  btn.disabled = true; btn.textContent = 'Fetching…';
  try {
    await post('/api/jobs/refetch');
  } catch (e) { flashCount(e.message); btn.disabled = false; btn.textContent = 'Fetch new'; return; }
  pollFetchStatus(btn);
};
// Queue depth, so "pending" has an answer to "how long?".
async function refreshQueue() {
  try {
    const st = await api('/api/jobs/fetch-status');
    document.getElementById('queue-tracker').textContent =
      st.current || st.queued ? `generating… ${st.queued} more queued` : '';
  } catch (e) { /* the tracker is a nicety, never an error */ }
}
refreshQueue();
setInterval(refreshQueue, 5000);
function resetAndLoad() {
  currentPage = 1;
  reloadCurrentView();
}
// Measured live: typing a search term fired one full /api/jobs/list round
// trip (~70-115ms server-side on 7681 rows) PER KEYSTROKE, no debounce at
// all -- a 10-character search was 10 sequential reloads, which is what
// made the list feel laggy/flickery while typing. 250ms of quiet collapses
// that to one real request per pause, with no perceptible added delay for
// a checkbox/dropdown change (a single click still fires once, just
// 250ms later instead of instantly -- not noticeable at that length).
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
const debouncedResetAndLoad = debounce(resetAndLoad, 250);
['f-search', 'f-minscore', 'f-eng', 'f-level', 'f-date', 'f-source', 'f-status'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('input', debouncedResetAndLoad);
  el.addEventListener('change', debouncedResetAndLoad);
});
document.getElementById('p-prev').onclick = () => { currentPage--; loadJobs(); };
document.getElementById('p-next').onclick = () => { currentPage++; loadJobs(); };

// ── profile view ─────────────────────────────────────────────────────
const FIELD_LABELS = {
  name: 'Name', title: 'Title', phone: 'Phone', email: 'Email', linkedin: 'LinkedIn',
  github: 'GitHub', portfolio: 'Portfolio site', education: 'Education',
  years_experience: 'Years experience', summary_current: 'Current summary',
};

async function loadProfile() {
  const [fieldsData, skillsData, pointsData] = await Promise.all([
    api('/api/profile/fields'),
    api('/api/profile/skills'),
    api('/api/profile/points?all=' + (document.getElementById('pf-show-retired').checked ? '1' : '0')),
  ]);
  renderFields(fieldsData.profile);
  renderSkills(skillsData.skills);
  renderPoints(pointsData.points);
}

function openManage() {
  document.getElementById('overlay').hidden = false;
  document.getElementById('manage-panel').hidden = false;
  loadProfile();
}
function closeManage() {
  document.getElementById('overlay').hidden = true;
  document.getElementById('manage-panel').hidden = true;
}
document.getElementById('btn-manage').onclick = openManage;

function renderFields(profile) {
  const el = document.getElementById('pf-fields');
  el.innerHTML = '';
  for (const key of Object.keys(FIELD_LABELS)) {
    const row = document.createElement('div');
    row.className = 'pf-field-row';
    const isLong = key === 'summary_current';
    row.innerHTML = `
      <label>${FIELD_LABELS[key]}</label>
      ${isLong ? `<textarea rows="3">${esc(profile[key] || '')}</textarea>`
               : `<input type="text" value="${esc(profile[key] || '')}">`}
      <button class="small">Save</button>`;
    const input = row.querySelector('input,textarea');
    row.querySelector('button').onclick = async () => {
      await post('/api/profile/fields/set', {key, value: input.value});
      flashCount(`Saved ${FIELD_LABELS[key]}.`);
    };
    el.appendChild(row);
  }
}

function renderSkills(skills) {
  const el = document.getElementById('pf-skills');
  const byCat = {};
  for (const s of skills) (byCat[s.category] ||= []).push(s.name);
  el.innerHTML = Object.keys(byCat).sort().map(cat => `
    <div class="pf-cat">
      <div class="pf-cat-name">${esc(cat)}</div>
      ${byCat[cat].sort().map(n => `<span class="pf-skill-chip">${esc(n)}</span>`).join('')}
    </div>`).join('');
}

function renderPoints(points) {
  const el = document.getElementById('pf-points');
  el.innerHTML = '';
  const roles = points.filter(p => p.entry_type !== 'project');
  const projects = points.filter(p => p.entry_type === 'project');
  _renderPointGroup(el, roles);
  if (projects.length) {
    const h = document.createElement('h4');
    h.style.cssText = 'font-size:12px;margin:14px 0 6px;color:var(--dim)';
    h.textContent = 'Projects';
    el.appendChild(h);
    _renderPointGroup(el, projects);
  }
  if (!points.length) el.innerHTML = '<p class="dim">No points yet.</p>';
}

function _renderPointGroup(el, points) {
  for (const p of points) {
    const row = document.createElement('div');
    row.className = 'pf-point-row' + (p.active ? '' : ' retired');
    // Keyword+Use+Result rubric, authoring-time half: visible on every real
    // point at a glance, not just the moment you happen to edit it -- so a
    // weak one doesn't sit unnoticed until it quietly gets cut from some
    // future resume. null quality = a bracket-flagged draft, not graded.
    const q = p.quality;
    const qualityNote = q && !q.ok
      ? `<div class="badge badge-stale" style="margin-top:4px">${q.weak_opener ? 'bare generic-verb opener, no brag' : 'no brag -- no number, award, or named result'}</div>`
      : '';
    row.innerHTML = `
      <div class="pf-point-head">
        <b>${esc(p.company)} / ${esc(p.role)}</b>
        <span class="meta">${esc(p.start_date)} &ndash; ${esc(p.end_date || 'present')}${p.active ? '' : ' &middot; RETIRED'}</span>
      </div>
      <textarea>${esc(p.text)}</textarea>
      ${qualityNote}
      <div class="pf-point-actions">
        <button class="small pf-save">Save</button>
        <button class="small pf-toggle">${p.active ? 'Retire' : 'Restore'}</button>
      </div>
      `;
    const textarea = row.querySelector('textarea');
    row.querySelector('.pf-save').onclick = async () => {
      try {
        const res = await post('/api/profile/points/update', {id: p.id, text: textarea.value});
        flashCount(res.quality_warning
          ? `Saved -- but ${res.quality_warning.weak_opener ? 'opens on a bare generic verb with no brag' : 'has no brag (no number, award, or named result)'}.`
          : 'Point saved.');
        loadProfile();
      } catch (e) { flashCount(e.message); }
    };
    row.querySelector('.pf-toggle').onclick = async () => {
      await post('/api/profile/points/toggle', {id: p.id, active: !p.active});
      loadProfile();
    };
    el.appendChild(row);
  }
}

document.getElementById('pf-show-retired').addEventListener('change', loadProfile);

document.getElementById('pf-skill-add').onclick = async () => {
  const name = document.getElementById('pf-skill-name').value.trim();
  const cat = document.getElementById('pf-skill-cat').value.trim() || 'Other';
  if (!name) return;
  await post('/api/profile/skills/add', {name, category: cat});
  document.getElementById('pf-skill-name').value = '';
  document.getElementById('pf-skill-cat').value = '';
  loadProfile();
};

// A point is added exactly as typed -- nothing rewrites it.
document.getElementById('pf-add-point').onclick = async () => {
  const val = id => document.getElementById(id).value.trim();
  const body = {entry_type: val('pf-np-type'), company: val('pf-np-company'), role: val('pf-np-role'),
                start_date: val('pf-np-start'), end_date: val('pf-np-end'), text: val('pf-np-text'),
                tags: val('pf-np-tags')};
  try {
    await post('/api/profile/points/add', body);
  } catch (e) { flashCount(e.message); return; }
  for (const id of ['pf-np-company', 'pf-np-role', 'pf-np-start', 'pf-np-end', 'pf-np-text', 'pf-np-tags']) {
    document.getElementById(id).value = '';
  }
  flashCount('Point added.');
  loadProfile();
};

// ── master document ─────────────────────────────────────────────────
// A read-only, single-page view of literally everything on file -- the
// point bank scattered across many small edit rows is good for editing,
// bad for "did I forget something" or "what was that number again."
// Groups every point (including retired and bracket-flagged ones, clearly
// marked) by company/role, plus the full skill list and profile fields.
async function openMasterDoc() {
  const [fieldsData, skillsData, pointsData] = await Promise.all([
    api('/api/profile/fields'),
    api('/api/profile/skills'),
    api('/api/profile/points?all=1'),
  ]);
  const profile = fieldsData.profile;
  const points = pointsData.points;

  // Master doc shows what's actually current -- a retired point is gone
  // from here the moment it's retired, not crossed out and left visible.
  // The point bank's own "show retired" toggle is the place to still see
  // retired entries when you need to.
  const activePoints = points.filter(p => p.active);

  // Grouped by COMPANY ONLY, same fix generate.py already uses for actual
  // resume rendering -- a title change/promotion at the same company
  // (different `role` values, same company) used to split into two
  // top-level blocks here, each repeating the company name. Projects get
  // the identical treatment: a project's tagline/stack line differing
  // slightly across its point-bank rows must not split it into two blocks.
  function groupByCompany(items) {
    const groups = [];
    const seen = new Map();
    for (const p of items) {
      if (!seen.has(p.company)) {
        seen.set(p.company, {company: p.company, rows: []});
        groups.push(seen.get(p.company));
      }
      seen.get(p.company).rows.push(p);
    }
    for (const g of groups) {
      g.rows.sort((a, b) => a.id - b.id);
      const starts = g.rows.map(p => p.start_date).filter(Boolean);
      g.start = starts.length ? starts.sort()[0] : '';
      g.end = g.rows.some(p => !p.end_date) ? null : g.rows.map(p => p.end_date).sort().slice(-1)[0];
      // Current title/tagline: prefer open-ended (still going) over closed;
      // among those, latest start_date; among ties, highest id.
      g.role = g.rows.slice().sort((a, b) => {
        const aOpen = a.end_date ? 0 : 1, bOpen = b.end_date ? 0 : 1;
        if (aOpen !== bOpen) return aOpen - bOpen;
        if ((a.start_date || '') !== (b.start_date || '')) return (a.start_date || '') < (b.start_date || '') ? -1 : 1;
        return a.id - b.id;
      }).slice(-1)[0].role;
    }
    return groups;
  }

  const roleGroups = groupByCompany(activePoints.filter(p => p.entry_type !== 'project'));
  const projectGroups = groupByCompany(activePoints.filter(p => p.entry_type === 'project'));

  const flaggedCount = activePoints.filter(p => p.text.trim().startsWith('[')).length;
  const retiredCount = points.filter(p => !p.active).length;

  const byCat = {};
  for (const s of skillsData.skills) (byCat[s.category] ||= []).push(s.name);

  function groupHtml(g) {
    return `
    <div class="md-role">
      <div class="md-role-head"><span>${esc(g.role)}</span><span class="dim">${esc(g.start)} &ndash; ${esc(g.end || 'present')}</span></div>
      <div class="md-role-company">${esc(g.company)}</div>
      ${g.rows.map(p => {
        const flagged = p.text.trim().startsWith('[');
        const note = flagged ? '<span class="md-flag-note">NEEDS YOUR REVIEW</span>' : '';
        return `<div class="md-bullet ${flagged ? 'flagged' : ''}">${esc(p.text)}${note}</div>`;
      }).join('')}
    </div>`;
  }

  const rolesHtml = roleGroups.map(groupHtml).join('');
  const projectsHtml = projectGroups.map(groupHtml).join('');

  const skillsHtml = Object.keys(byCat).sort().map(cat =>
    `<div class="md-skills-cat"><b>${esc(cat)}:</b> ${byCat[cat].sort().join(', ')}</div>`).join('');

  document.getElementById('masterdoc-body').innerHTML = `
    <div class="md-header">
      <h1>${esc(profile.name || '')}</h1>
      <div class="contact">${esc(profile.title || '')} &middot; ${esc(profile.phone || '')} &middot; ${esc(profile.email || '')}
        &middot; ${esc(profile.linkedin || '')} &middot; ${esc(profile.github || '')}</div>
    </div>
    <div class="md-summary">${esc(profile.summary_current || '')}</div>
    <p class="dim" style="font-size:12.5px">
      ${activePoints.length} point(s) shown &middot;
      ${retiredCount ? `${retiredCount} retired (hidden here -- see point bank) &middot; ` : ''}
      ${flaggedCount ? `<b style="color:var(--warn)">${flaggedCount} flagged, needs your review</b>` : 'nothing flagged'}
    </p>
    <h3 style="font-size:14px;margin-top:20px">EXPERIENCE</h3>
    ${rolesHtml}
    ${projectGroups.length ? `<h3 style="font-size:14px;margin-top:20px">PROJECTS</h3>${projectsHtml}` : ''}
    <h3 style="font-size:14px;margin-top:20px">SKILLS</h3>
    ${skillsHtml}
    <h3 style="font-size:14px;margin-top:20px">EDUCATION</h3>
    <div>${esc(profile.education || '')}</div>
  `;
  document.getElementById('overlay').hidden = false;
  document.getElementById('masterdoc-panel').hidden = false;
}
function closeMasterDoc() {
  document.getElementById('overlay').hidden = true;
  document.getElementById('masterdoc-panel').hidden = true;
}
document.getElementById('btn-masterdoc').onclick = openMasterDoc;

// ── Telegram notifications: one bot/chat per user ───────────────────────
async function loadTelegramConfig() {
  const status = document.getElementById('pf-telegram-status');
  const saveBtn = document.getElementById('pf-tg-save');
  document.getElementById('pf-tg-result').textContent = '';
  const cfg = await api('/api/telegram/config');
  if (cfg.configured) {
    status.textContent = `Connected -- bot token ending …${cfg.bot_token_last4}, chat id ${cfg.chat_id}`;
    saveBtn.textContent = 'Replace';
  } else {
    status.textContent = 'Not set up yet -- message your bot once on Telegram, then add its token and your chat id here.';
    saveBtn.textContent = 'Save';
  }
  document.getElementById('pf-tg-token').value = '';
  document.getElementById('pf-tg-chatid').value = '';
}

document.getElementById('pf-tg-save').onclick = async () => {
  const bot_token = document.getElementById('pf-tg-token').value.trim();
  const chat_id = document.getElementById('pf-tg-chatid').value.trim();
  const result = document.getElementById('pf-tg-result');
  if (!bot_token || !chat_id) {
    result.textContent = 'Bot token and chat id are both required.';
    return;
  }
  const btn = document.getElementById('pf-tg-save');
  btn.disabled = true;
  try {
    await post('/api/telegram/config', {bot_token, chat_id});
    result.textContent = 'Saved.';
    await loadTelegramConfig();
  } catch (e) {
    result.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
};

document.getElementById('pf-tg-test').onclick = async () => {
  const result = document.getElementById('pf-tg-result');
  const btn = document.getElementById('pf-tg-test');
  btn.disabled = true;
  result.textContent = 'Sending…';
  try {
    await post('/api/telegram/test', {});
    result.textContent = 'Test message sent -- check Telegram.';
  } catch (e) {
    result.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
};

// ── session / role: admin, whitelisted, or guest (read-only preview) ───
// Static action buttons that mutate something -- disabled outright for a
// guest, so there's nothing to click into a 403 for. The server enforces
// this independently either way (every mutating route checks role itself),
// this is just so the UI doesn't invite a guest to try.
const GUEST_DISABLED_BUTTON_IDS = [
  'btn-manual', 'btn-refetch',
  'pf-add-point', 'pf-tg-save', 'pf-tg-test',
];

async function loadSession() {
  const session = await api('/api/session');
  if (session.role === 'guest') {
    document.getElementById('guest-banner').hidden = false;
    for (const id of GUEST_DISABLED_BUTTON_IDS) {
      const el = document.getElementById(id);
      if (el) el.disabled = true;
    }
  }
  document.getElementById('tab-admin').hidden = session.role !== 'admin';
  return session;
}

async function loadAdminUsers() {
  const result = document.getElementById('admin-result');
  result.textContent = '';
  const {users} = await api('/api/admin/users');
  const list = document.getElementById('admin-users-list');
  list.innerHTML = users.map(u => `
    <div class="row row-start" style="margin-bottom:6px">
      <span style="flex:1">${esc(u.email)} <span class="dim">(${esc(u.role)})</span></span>
      ${u.role === 'admin' ? '' : `<button class="small admin-remove" data-email="${esc(u.email)}">Remove</button>`}
    </div>`).join('') || '<p class="dim">No one whitelisted yet.</p>';
  list.querySelectorAll('.admin-remove').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await post('/api/admin/users/remove', {email: btn.dataset.email});
        await loadAdminUsers();
      } catch (e) {
        result.textContent = 'Error: ' + e.message;
        btn.disabled = false;
      }
    };
  });
}

document.getElementById('admin-add-btn').onclick = async () => {
  const input = document.getElementById('admin-new-email');
  const result = document.getElementById('admin-result');
  const email = input.value.trim();
  if (!email) { result.textContent = 'Enter an email first.'; return; }
  const btn = document.getElementById('admin-add-btn');
  btn.disabled = true;
  try {
    await post('/api/admin/users/add', {email});
    input.value = '';
    result.textContent = 'Added.';
    await loadAdminUsers();
  } catch (e) {
    result.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
};

loadSession();
loadJobs();
