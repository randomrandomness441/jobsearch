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
// folder_path comes back from the DB as the real on-disk path under
// jobsearch/data/applications/ -- one place to strip that prefix down to
// what the /applications/ URL route expects, instead of the same literal
// string repeated at every call site (the kind of duplicated assumption
// that broke silently the last time this path moved).
function appDocUrl(folderPath, filename) {
  return `/applications/${encodeURIComponent(folderPath.replace('data/applications/', ''))}/${filename}`;
}
// missing: [{gap, why, example}, ...] -- same shape whether it came from a
// score-only pass (job.llm_missing) or a real generation
// (application.ats_estimate.missing), so one renderer covers both.
function _missingHtml(missing) {
  if (!missing || !missing.length) return '';
  return `<div style="margin-top:8px">${missing.map(m => `
      <div class="dim" style="font-size:12px;margin-top:4px">
        <b style="color:var(--warn)">${esc(m.gap || '')}</b>${m.why ? ' -- ' + esc(m.why) : ''}
        ${m.example ? `<br>example that would close it: ${esc(m.example)}` : ''}
      </div>`).join('')}</div>`;
}
// projects: [{row_id, name, worth_including, reason}, ...] -- advisory
// only, never auto-included in the resume. The button is the only way a
// project actually lands on the page (wired separately, see openJob()).
function _projectsHtml(projects) {
  if (!projects || !projects.length) return '';
  // One card per PROJECT, not per point-bank row -- a project with
  // several facts on file (e.g. 3 rows under "Kapitsa") used to render as
  // 3 separate "projects" with the same name, each with its own button.
  // Grouped by name here instead; the button carries every row id
  // belonging to that project, so "Add to resume" forces the whole
  // project in, not just whichever fact happened to list first.
  const byName = new Map();
  for (const p of projects) {
    if (!byName.has(p.name)) byName.set(p.name, []);
    byName.get(p.name).push(p);
  }
  const cards = [...byName.entries()].map(([name, rows]) => {
    const anyWorthIt = rows.some(p => p.worth_including);
    const rowIds = rows.map(p => p.row_id).join(',');
    const reasonLines = rows.map(p =>
      `<div>${p.worth_including ? '✓' : '–'} ${esc(p.reason || '')}</div>`).join('');
    return `
      <div class="dim" style="font-size:12px;margin-top:6px;display:flex;align-items:flex-start;gap:8px">
        <span style="flex:1">
          <b style="color:${anyWorthIt ? 'var(--ok)' : 'var(--dim)'}">${esc(name)}</b>
          ${reasonLines}
        </span>
        <button class="small jd-add-project" data-row-ids="${rowIds}">Add to resume</button>
      </div>`;
  }).join('');
  return `<div style="margin-top:8px">
    <div class="dim" style="font-size:12px;font-weight:600">Projects (not on the resume unless you add one)</div>
    ${cards}
  </div>`;
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
    // llm_score is the only score there is pre-generation -- regex only
    // classifies title/seniority/location, it doesn't rate fit. A posting
    // with no score yet just hasn't been through AI-judge.
    const isLlm = j.llm_score != null;
    const score = isLlm ? Math.round(j.llm_score) : '–';
    const tierNote = ['staff', 'senior_staff', 'manager'].includes(j.seniority_tier)
      ? `<span class="badge badge-tier">${j.seniority_tier.replace('_', ' ')}</span>` : '';
    const provenanceNote = isLlm
      ? `<span class="badge badge-llm" title="${esc(j.llm_reason || '')}">AI-judged</span>`
      : `<span class="badge badge-estimate" title="Not yet run through AI-judge">not judged</span>`;
    // Judged, but your master doc (point bank/skills/profile) changed since
    // -- this score no longer reflects your current profile. Only clears
    // when THIS posting is individually re-judged (AI-judge picks up every
    // stale posting automatically, no separate action needed).
    const staleNote = j.stale
      ? '<span class="badge badge-stale" title="Your master doc changed since this was judged -- re-run AI-judge to refresh">stale</span>' : '';
    row.innerHTML = `
      <div class="job-score ${scoreClass(j.llm_score)}" title="${isLlm ? esc(j.llm_reason || '') : 'Not yet judged'}">${score}</div>
      <div class="job-main">
        <div class="job-title">${esc(j.title)}</div>
        <div class="job-company">${esc(j.company)} · ${esc(j.source)} · ${fmtDate(j.fetched_ts)}</div>
      </div>
      <div class="job-badges">
        ${provenanceNote}
        ${staleNote}
        ${tierNote}
        ${j.applied ? '<span class="badge badge-applied">applied</span>' : ''}
        <span class="badge badge-${j.status}">${esc(j.status)}</span>
      </div>`;
    row.onclick = () => openJob(j.id);
    list.appendChild(row);
  }
  if (!data.jobs.length) list.innerHTML = '<p class="dim">No postings match these filters.</p>';

  document.getElementById('p-info').textContent = `page ${data.page} of ${data.total_pages}`;
  document.getElementById('p-prev').disabled = data.page <= 1;
  document.getElementById('p-next').disabled = data.page >= data.total_pages;
}

async function openJob(id) {
  const data = await api('/api/jobs/detail?id=' + id);
  const j = data.job, apps = data.applications;
  const panel = document.getElementById('job-detail');
  const alreadyApplied = apps.some(a => a.status !== 'generated');
  // Two shapes of application row: real generated materials (resume/cover
  // letter links + ATS estimate), or a quick "I already applied elsewhere"
  // row (empty folder_path -- no materials came out of this tool for it).
  const appsHtml = apps.length ? apps.map(a => a.folder_path ? `
    <div class="app-entry">
      <div class="app-entry-meta">
        <span class="badge badge-llm" title="${esc(a.ats_estimate.reason || '')}">ATS ${a.ats_estimate.score}</span>
        ${a.ats_estimate.rendered_pages ? `<span class="badge badge-new">${a.ats_estimate.rendered_pages}p</span>` : ''}
        <span class="dim">generated ${fmtDate(a.generated_ts)}</span>
        <span class="badge badge-${a.status === 'generated' ? 'new' : 'generated'}">${esc(a.status)}</span>
      </div>
      ${a.ats_estimate.reason ? `<div style="font-size:13px;margin-top:4px">${esc(a.ats_estimate.reason)}</div>` : ''}
      ${(a.ats_estimate.requirements || []).length ? `
      <div class="kw-list" style="margin-top:6px">
        ${a.ats_estimate.requirements.map(r => {
          const cls = r.presence === 'no' ? 'kw-no' : (r.depth === 'quantified' ? 'kw-yes' : 'kw-weak');
          const pts = (r.weight != null && r.earned != null) ? ` (${r.earned}/${r.weight} pts)` : '';
          const label = r.requirement + (r.importance === 'required' ? '' : ' (nice-to-have)') + pts;
          return `<span class="kw ${cls}" title="${esc(r.presence)}/${esc(r.depth)}">${esc(label)}</span>`;
        }).join('')}
      </div>` : ''}
      ${_missingHtml(a.ats_estimate.missing)}
      <div class="doc-links">
        <a class="doc-link" href="${appDocUrl(a.folder_path, 'resume.pdf')}">Resume <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${appDocUrl(a.folder_path, 'resume.docx')}">.docx</a>
        <a class="doc-link" href="${appDocUrl(a.folder_path, 'cover_letter.pdf')}">Cover letter <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${appDocUrl(a.folder_path, 'cover_letter.docx')}">.docx</a>
      </div>
    </div>` : `
    <div class="app-entry-applied">
      Marked applied ${fmtDate(a.applied_ts)} via ${esc(a.applied_via || '?')}${a.referral_name ? ' · referral: ' + esc(a.referral_name) : ''}
      — no materials generated through this tool — status: <b>${esc(a.status)}</b>
    </div>`).join('') : '<p class="dim" style="margin-top:8px">Not generated yet.</p>';

  panel.innerHTML = `
    <button class="close-x" onclick="closeJob()">✕</button>
    <div class="jd-title">${esc(j.title)}</div>
    <div class="jd-company">${esc(j.company)} ${j.url ? `· <a href="${esc(j.url)}" target="_blank">posting ↗</a>` : ''}</div>
    ${j.llm_score != null ? `
    <div class="score-block" style="border:1px solid var(--accent)">
      <div><div class="num">${Math.round(j.llm_score)}</div><div class="dim" style="font-size:13px">AI fit</div></div>
      <div style="flex:1">
        ${j.stale ? '<div class="badge badge-stale" style="margin-bottom:4px">stale -- your master doc changed since this was judged, rescore to refresh</div>' : ''}
        <div style="font-size:14px">${esc(j.llm_reason || '')}</div>
        ${_missingHtml(j.llm_missing)}
        ${_projectsHtml(j.llm_projects)}
      </div>
    </div>` : `
    <div class="score-block">
      <div class="dim" style="font-size:14px">Not yet AI-judged -- run "AI-judge", or rescore this one posting, to get a fit score.</div>
    </div>`}
    <div class="row" style="justify-content:flex-start;gap:8px">
      <button id="jd-rescore" class="small">${j.llm_score != null ? 'Rescore this posting' : 'Score this posting'}</button>
      <button id="jd-queue">${j.status === 'queued' ? 'Retry generating' : (j.status === 'generated' ? 'Regenerate resume + cover letter' : 'Queue for resume + cover letter')}</button>
      <button id="jd-mark-applied" ${alreadyApplied ? 'disabled' : ''} class="${alreadyApplied ? '' : 'primary'}">${alreadyApplied ? 'Applied ✓' : 'Mark as applied'}</button>
    </div>
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
    <h3 style="font-size:15px;margin-top:18px">Generated materials</h3>
    ${appsHtml}
    <h3 style="font-size:15px;margin-top:18px">Job description</h3>
    <div class="jd-body">${esc(j.description_raw)}</div>
  `;
  document.getElementById('jd-rescore').onclick = async () => {
    const btn = document.getElementById('jd-rescore');
    btn.disabled = true; btn.textContent = 'Scoring…';
    const res = await post('/api/jobs/rescore', {job_id: j.id});
    if (res.error) { flashCount(res.error); btn.disabled = false; btn.textContent = 'Rescore this posting'; return; }
    openJob(id); loadJobs();
  };
  // Delegated -- one "Add to resume" button per project, rendered fresh
  // each time by _projectsHtml. Forces this project into a full
  // regenerate (fresh summary + cover letter + content selection) via
  // /api/jobs/generate-with-project; the project is advisory-only
  // otherwise, this is the only thing that actually puts it on the page.
  panel.querySelectorAll('.jd-add-project').forEach(btn => {
    btn.onclick = async () => {
      const rowIds = btn.dataset.rowIds.split(',').map(Number);
      btn.disabled = true; btn.textContent = 'Adding…';
      const res = await post('/api/jobs/generate-with-project', {job_id: j.id, project_ids: rowIds});
      if (res.error) { flashCount(res.error); btn.disabled = false; btn.textContent = 'Add to resume'; return; }
      // Async now -- the server enqueues the real regenerate (3 LLM calls plus
      // rendering, can take a couple of minutes) onto one shared worker instead of
      // blocking this request, so the response here is just "started"/"already_running",
      // not the finished result. Poll the same way the "Generate queued" batch flow
      // already does, and just show the files once the page refreshes -- no separate
      // notification needed here, the person's already looking at this page.
      while (true) {
        await new Promise(r => setTimeout(r, 1500));
        const st = await api(`/api/jobs/generate-status?job_id=${j.id}`);
        if (st.state === 'done') {
          flashCount('Project added -- resume regenerated.');
          break;
        }
        if (st.state === 'failed') {
          flashCount(st.error || 'Generation failed -- see server logs');
          break;
        }
        btn.textContent = st.state === 'running' ? 'Generating…' : 'Queued…';
      }
      btn.disabled = false; btn.textContent = 'Add to resume';
      openJob(id); loadJobs();
      if (document.getElementById('view-applications').hidden === false) loadApplications();
    };
  });
  document.getElementById('jd-queue').onclick = async () => {
    const btn = document.getElementById('jd-queue');
    await post('/api/jobs/queue', {job_id: j.id});
    // Queuing alone used to be the whole action -- a flag with nothing
    // watching it, so nothing ever actually got generated until you asked
    // separately. Now it immediately kicks off the real draft for this job
    // (plus anything else already queued), same async batch "Generate
    // queued" uses, so there's real progress to see instead of a static
    // "queued" label with nothing happening.
    btn.disabled = true; btn.textContent = 'Starting…';
    const res = await post('/api/jobs/generate-queued', {});
    if (res.error || !res.candidates) {
      openJob(id); loadJobs();
      return;
    }
    while (true) {
      await new Promise(r => setTimeout(r, 1500));
      const st = await api('/api/jobs/generate-queued/status');
      if (!st.running) {
        flashCount(`Generated ${st.generated} of ${st.total} queued job(s).`);
        openJob(id); loadJobs();
        if (document.getElementById('view-applications').hidden === false) loadApplications();
        return;
      }
      btn.textContent = `Generating… ${st.done}/${st.total}`;
    }
  };
  if (!alreadyApplied) {
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
}
function closeJob() {
  document.getElementById('overlay').hidden = true;
  document.getElementById('job-detail').hidden = true;
}

// ── manual add ────────────────────────────────────────────────────────
function openManual() {
  document.getElementById('m-company').value = '';
  document.getElementById('m-url').value = '';
  document.getElementById('m-text').value = '';
  document.getElementById('m-result').innerHTML = '';
  document.getElementById('overlay').hidden = false;
  document.getElementById('manual-modal').hidden = false;
}
function closeManual() {
  document.getElementById('overlay').hidden = true;
  document.getElementById('manual-modal').hidden = true;
}
// First non-empty line is the title; the next one is the location IF it
// looks like one (short, and either "Remote"/"Hybrid"/India-ish, or a
// plain "City, Country" with no sentence punctuation) -- otherwise it's
// already prose and there's no separate location line to pull out.
function parseJobPaste(raw) {
  const lines = raw.split('\n').map(l => l.trim());
  let i = 0;
  while (i < lines.length && !lines[i]) i++;
  const title = lines[i] || '';
  let j = i + 1;
  while (j < lines.length && !lines[j]) j++;
  const candidate = lines[j] || '';
  const looksLikeLocation = candidate && candidate.length <= 60 &&
    (/remote|hybrid|on-?site|india|bangalore|bengaluru/i.test(candidate) ||
     (candidate.includes(',') && !candidate.endsWith('.')));
  return {title, location: looksLikeLocation ? candidate : ''};
}
async function submitManual() {
  const raw = document.getElementById('m-text').value.trim();
  const {title, location} = parseJobPaste(raw);
  const body = {company: document.getElementById('m-company').value.trim(), title, location,
                url: document.getElementById('m-url').value.trim(), text: raw};
  if (!body.company || !title || !raw) {
    document.getElementById('m-result').innerHTML = '<p style="color:var(--bad)">Company and the pasted posting are required.</p>';
    return;
  }
  const res = await post('/api/jobs/manual', body);
  document.getElementById('m-result').innerHTML = `
    <div class="score-block">
      <div style="flex:1">
        <div style="font-size:12px;color:var(--ok);font-weight:600">${res.is_new ? '✓ Added to your Postings list -- close this to see it (job #' + res.job_id + ')' : '✓ Already in your Postings list as job #' + res.job_id}</div>
        <div class="dim" style="font-size:12px;margin-top:2px">title: ${esc(title)}${location ? ' &middot; location: ' + esc(location) : ' &middot; no location line detected'}</div>
        <div class="dim" style="font-size:11.5px;margin-top:4px">Not scored yet -- run "AI-judge" to get a fit score for this posting.</div>
      </div>
    </div>
    <div class="row" style="justify-content:flex-start;margin-top:10px">
      <button id="m-queue" class="primary small">Queue for resume + cover letter</button>
      <button id="m-done" class="small">Close &amp; view in list</button>
    </div>`;
  document.getElementById('m-queue').onclick = async () => {
    await post('/api/jobs/queue', {job_id: res.job_id});
    const btn = document.getElementById('m-queue');
    btn.textContent = 'Queued ✓'; btn.disabled = true;
    loadJobs();
  };
  document.getElementById('m-done').onclick = () => { closeManual(); loadJobs(); };
  loadJobs();
  if (document.getElementById('view-applications').hidden === false) loadApplications();
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
    const hist = (a.status_history || []).map(ev =>
      `<div>${new Date(ev.ts * 1000).toLocaleString()} — <b>${esc(ev.status)}</b> ${esc(ev.note || '')}</div>`).join('');
    // Quick-marked ("applied elsewhere, never generated through this tool")
    // rows have an empty folder_path and {} ats_estimate -- no docs to link,
    // no ATS score to show, so that whole block is skipped for those.
    div.innerHTML = `
      <div class="app-head">
        <div>
          <div class="job-title">${esc(a.title)}</div>
          <div class="job-company">${esc(a.company)}${a.folder_path ? ' · ATS ' + a.ats_estimate.score + (a.ats_estimate.rendered_pages ? ` (${a.ats_estimate.rendered_pages}p)` : '') : ' · applied directly, no materials generated here'} · status: <b>${esc(a.status)}</b></div>
        </div>
        <span class="badge badge-${a.status === 'generated' ? 'new' : 'generated'}">${esc(a.status)}</span>
      </div>
      ${a.folder_path ? `
      <div class="doc-links" style="margin-top:8px">
        <a class="doc-link" href="${appDocUrl(a.folder_path, 'resume.pdf')}">Resume <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${appDocUrl(a.folder_path, 'resume.docx')}">.docx</a>
        <a class="doc-link" href="${appDocUrl(a.folder_path, 'cover_letter.pdf')}">Cover letter <span class="ext">PDF</span></a>
        <a class="doc-link alt" href="${appDocUrl(a.folder_path, 'cover_letter.docx')}">.docx</a>
        <a class="doc-link alt" href="${appDocUrl(a.folder_path, 'job_description.md')}">Job description</a>
      </div>` : ''}
      ${a.status === 'generated' ? `
      <div class="referral-form">
        <select class="via-select">
          <option value="company_site">company site</option>
          <option value="linkedin">LinkedIn</option>
          <option value="referral">referral</option>
          <option value="recruiter">recruiter</option>
          <option value="other">other</option>
        </select>
        <input class="referral-name" type="text" placeholder="referral name (optional)">
        <input class="referral-note" type="text" placeholder="referral note (optional)">
        <button class="primary small mark-applied">Mark applied</button>
      </div>` : `
      <div class="status-track">
        ${STATUS_FLOW.map(s => `<button class="add-status" data-status="${s}">${s.replace('_',' ')}</button>`).join('')}
        <span class="dim" style="align-self:center;font-size:12px">via ${esc(a.applied_via || '?')}${a.referral_name ? ' · referral: ' + esc(a.referral_name) : ''}</span>
      </div>`}
      <div class="status-hist">${hist}</div>
    `;
    const markBtn = div.querySelector('.mark-applied');
    if (markBtn) markBtn.onclick = async () => {
      await post('/api/applications/mark-applied', {
        app_id: a.id,
        via: div.querySelector('.via-select').value,
        referral_name: div.querySelector('.referral-name').value.trim(),
        referral_note: div.querySelector('.referral-note').value.trim(),
      });
      loadApplications();
      loadJobs();
    };
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
// (one LLM call, run on demand -- see _cluster_missing_gaps server-side),
// most-common first. Each cluster carries the real source job_ids it was
// built from, so "rejudge" targets exactly those postings against
// whatever the master doc looks like right now -- not a blanket re-score.
async function loadGaps(force) {
  const list = document.getElementById('gaps-list');
  const count = document.getElementById('gaps-count');
  // Plain tab-open reads the cached result (near-instant, no LLM call) --
  // only an explicit refresh (the button, or a rejudge just finishing)
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
    list.innerHTML = '<p class="dim">No gaps to show -- score some postings first (AI-judge on the Postings tab), or none of your scored postings have a real gap flagged.</p>';
    return;
  }
  list.innerHTML = '';
  for (const c of data.clusters) {
    const row = document.createElement('div');
    row.className = 'gap-row';
    row.innerHTML = `
      <div class="gap-count">${c.count}</div>
      <div class="gap-label">${esc(c.label)}</div>
      <button class="small gap-rejudge">Rejudge these ${c.count}</button>`;
    row.querySelector('.gap-rejudge').onclick = async () => {
      const btn = row.querySelector('.gap-rejudge');
      btn.disabled = true; btn.textContent = 'Rejudging…';
      const res = await post('/api/jobs/rejudge-cluster', {job_ids: c.job_ids});
      if (res.error) { flashCount(res.error); btn.disabled = false; btn.textContent = `Rejudge these ${c.count}`; return; }
      while (true) {
        await new Promise(r => setTimeout(r, 1500));
        const st = await api('/api/jobs/llm-backfill/status');
        if (!st.running) {
          flashCount(`Rejudged ${st.judged} of ${st.total} affected posting(s).`);
          loadGaps(true);   // force: re-cluster from the fresh scores, not the stale cache
          return;
        }
        btn.textContent = `Rejudging… ${st.done}/${st.total}`;
      }
    };
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
  document.getElementById('tab-postings').classList.toggle('active', name === 'postings');
  document.getElementById('tab-applications').classList.toggle('active', name === 'applications');
  document.getElementById('tab-profile').classList.toggle('active', name === 'profile');
  document.getElementById('tab-gaps').classList.toggle('active', name === 'gaps');
  if (name === 'applications') loadApplications();
  if (name === 'gaps') loadGaps();
  if (name === 'profile') {
    // reset to a clean landing every time you switch in -- upload box +
    // two buttons only, no stale proposal from a previous visit
    document.getElementById('pf-proposal-wrap').hidden = true;
    document.getElementById('pf-proposal').innerHTML = '';
    document.getElementById('pf-upload-status').textContent = '';
    document.getElementById('pf-upload').value = '';
    loadTelegramConfig();
  }
}

document.getElementById('tab-postings').onclick = () => showView('postings');
document.getElementById('tab-applications').onclick = () => showView('applications');
document.getElementById('tab-profile').onclick = () => showView('profile');
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

// Fetch only -- no LLM call here, ever. AI judging is the separate button
// below, triggered explicitly. Runs async server-side (a real fetch across
// the whole company list takes well over a minute of real HTTP calls to
// each board, too long to hold the request open for) -- start it, then
// poll /api/jobs/refetch/status the same way AI-judge polling works, so a
// reload (or just waiting) shows the real result once it lands, not a
// browser tab hung on an open connection with no feedback.
// "0 new postings" and "every source failed" used to read identically in
// the UI -- fetch_ats.py already prints the real breakdown (new / already-
// tracked / outside India / too old / errored) to its own stdout, captured
// in st.log, just never shown. Pulling the real "total: ..." line out of it
// so a genuinely empty cycle doesn't read as "is this broken?" anymore.
function _fetchSummary(st) {
  const re = /total: (\d+) new job rows added, (\d+) already-tracked duplicates skipped, (\d+) skipped \(outside India[^)]*\), (\d+) skipped \(older than[^)]*\), (\d+) sources errored/;
  const m = (st.log || '').match(re);
  if (!m) return {short: st.ok ? `${st.new_postings} new posting(s) fetched.` : 'Fetch failed -- check server logs.', full: st.log || '', add: st.new_postings || 0, bits: []};
  const [, add, dup, loc, age, err] = m;
  const bits = [`${dup} already tracked`, `${loc} outside India`, `${age} too old`];
  if (+err) bits.push(`${err} source(s) errored`);
  return {short: `${add} new (${bits.join(', ')}).`, full: st.log || '', add: +add, bits};
}
// Dialog version of the same result flashCount() already shows as a tooltip --
// the number matters enough on a fetch (potentially minutes of waiting) to
// surface prominently instead of only in a hover title that's easy to miss.
function openFetchResult(st, summary) {
  const bitsHtml = summary.bits.length
    ? `<div class="kw-list">${summary.bits.map(b => `<span class="kw kw-weak">${esc(b)}</span>`).join('')}</div>`
    : `<div style="font-size:14px">${esc(summary.short)}</div>`;
  const todayLine = st.added_today != null
    ? `<div class="dim" style="font-size:12.5px;margin-top:8px">${st.added_today} added today in total</div>` : '';
  document.getElementById('fr-body').innerHTML = `
    <div class="score-block" style="border:1px solid ${st.ok === false ? 'var(--bad)' : 'var(--accent)'}">
      <div><div class="num">${summary.add}</div><div class="dim" style="font-size:13px">new just now</div></div>
      <div style="flex:1">
        ${st.ok === false ? `<div style="font-size:14px;color:var(--bad)">${esc(summary.short)}</div>` : bitsHtml}
        ${todayLine}
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
    const st = await api('/api/jobs/refetch/status');
    if (!st.running) {
      btn.disabled = false; btn.textContent = 'Fetch new';
      const summary = _fetchSummary(st);
      flashCount(summary.short, summary.full);
      openFetchResult(st, summary);
      reloadCurrentView();
      return;
    }
    btn.textContent = 'Fetching…';
  }
}
document.getElementById('btn-refetch').onclick = async () => {
  const btn = document.getElementById('btn-refetch');
  btn.disabled = true; btn.textContent = 'Fetching…';
  const res = await post('/api/jobs/refetch');
  if (res.error) { flashCount(res.error); btn.disabled = false; btn.textContent = 'Fetch new'; return; }
  pollFetchStatus(btn);
};
// The only place a pre-generation AI judgment happens -- judges target-
// title India/Bangalore postings that are new, or stale (point bank
// changed since they were last judged). Can take a while for a large
// batch; button stays disabled so it's clear something's happening.
// AI-judge runs async server-side (a real "all of them" batch is too many
// LLM calls to hold one HTTP request open for) -- start it, then poll
// /api/jobs/llm-backfill/status for real progress (each score lands in the
// db the moment that one call finishes, so "done"/"total" are real counts,
// not a guess) until the background batch reports done.
// Persistent, survives a refresh -- unlike flashCount's 4s fade, this is
// the durable record of "is something running right now, and what did the
// last run actually do", read from the server's real _judge_progress
// state, not from anything stored in this one browser tab. A refresh used
// to lose all of this: the button just reset to "AI-judge" whether a batch
// was still running in the background or had finished minutes ago, with
// zero way to tell which.
function renderJudgeTracker(st) {
  const el = document.getElementById('judge-tracker');
  if (!st || st.started_ts == null) { el.textContent = ''; el.title = ''; return; }
  if (st.running) {
    el.textContent = `AI-judging… ${st.done}/${st.total} (${st.judged} judged so far)`;
    el.title = '';
    return;
  }
  const failed = st.failed || [];
  const prefix = st.interrupted ? `Interrupted by a restart at ${st.done}/${st.total}` : `Last AI-judge: ${st.judged} of ${st.total} judged`;
  el.textContent = failed.length ? `${prefix}, ${failed.length} failed` : prefix;
  el.title = failed.length ? failed.map(f => `${f.company} -- ${f.title}: ${f.reason}`).join('\n') : '';
}
// btn is optional -- null when this poll loop was started on page load
// (an already-running batch picked up after a refresh), not from a click
// in this tab. Either way the tracker updates every tick; the button only
// updates when one's actually in play.
async function pollJudgeStatus(btn, defaultLabel) {
  while (true) {
    await new Promise(r => setTimeout(r, 1500));
    const st = await api('/api/jobs/llm-backfill/status');
    renderJudgeTracker(st);
    if (!st.running) {
      if (btn) { btn.disabled = false; btn.textContent = defaultLabel; }
      const failed = st.failed || [];
      // "judged X of Y" alone looked identical whether everything else
      // was skipped (already judged, nothing new) or every single call
      // actually failed -- same gap the fetch-button dialog fixed earlier,
      // same fix: surface the real reasons, not just the count.
      const short = failed.length
        ? `AI-judged ${st.judged} of ${st.total} -- ${failed.length} failed.`
        : `AI-judged ${st.judged} of ${st.total} posting(s).`;
      const full = failed.length
        ? failed.map(f => `${f.company} -- ${f.title}: ${f.reason}`).join('\n')
        : '';
      flashCount(short, full);
      reloadCurrentView();
      return;
    }
    if (btn) btn.textContent = `AI-judging… ${st.done}/${st.total}`;
  }
}
// On load -- not just on click -- so a refresh mid-batch resumes live
// tracking instead of silently losing it, and a refresh after a batch
// finished still shows what it actually did, not a blank slate.
(async () => {
  const st = await api('/api/jobs/llm-backfill/status');
  renderJudgeTracker(st);
  if (st.running) {
    const btn = document.getElementById('btn-llm-backfill');
    btn.disabled = true; btn.textContent = `AI-judging… ${st.done}/${st.total}`;
    pollJudgeStatus(btn, 'AI-judge');
  }
})();
document.getElementById('btn-llm-backfill').onclick = async () => {
  const btn = document.getElementById('btn-llm-backfill');
  btn.disabled = true; btn.textContent = 'Starting…';
  // Its own date box (f-judge-days), separate from the list's display
  // filter (f-date) -- the two used to share one input, which meant
  // narrowing the list to browse also silently narrowed what AI-judge would
  // score. Never changes the server's own "not yet judged, or stale"
  // default -- the date cutoff is an extra AND on top of that, not a
  // replacement for it.
  const days = document.getElementById('f-judge-days').value;
  const res = await post('/api/jobs/llm-backfill', {days});
  if (res.error) { flashCount(res.error); btn.disabled = false; btn.textContent = 'AI-judge'; return; }
  if (!res.candidates) { flashCount('Nothing new or stale to judge.'); btn.disabled = false; btn.textContent = 'AI-judge'; return; }
  pollJudgeStatus(btn, 'AI-judge');
};
// Clear only -- it used to also force an immediate re-judge pass in the
// same click (and called itself "...all", even though it was already
// scoped to a date filter, which was the actually-confusing part). Simpler
// now: this button only wipes scores, in its own date window (f-clear-days,
// independent of the list filter and of AI-judge's own day count). A
// cleared posting has no score, which already satisfies AI-judge's normal
// "not yet judged" default -- so re-judging it is just clicking AI-judge
// next, same as for any other unjudged posting, not a special combined flow.
document.getElementById('btn-clear-llm').onclick = async () => {
  document.getElementById('more-actions').open = false;
  const days = document.getElementById('f-clear-days').value;
  const scopeLabel = days ? `the last ${days} day(s)` : 'ALL TIME';
  if (!confirm(`Clear AI scores for postings fetched in ${scopeLabel}? `
    + '(change the day count above first to widen or narrow this) '
    + "You'll need to click AI-judge afterward to re-score them -- real LLM cost per posting.")) return;
  const btn = document.getElementById('btn-clear-llm');
  btn.disabled = true;
  btn.textContent = 'Clearing…';
  const cleared = await post('/api/jobs/clear-llm-scores', {days});
  btn.disabled = false;
  btn.textContent = 'Clear scores';
  flashCount(`Cleared ${cleared.cleared}.`);
  reloadCurrentView();
};
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

// ── Tidy master doc: one company's full point list, cleaned up ─────────
// Same /api/profile/points/tidy endpoint the add-point flow uses below --
// here called with no company (checks every company, returns only the
// ones that actually changed).
function closeTidy() {
  document.getElementById('overlay').hidden = true;
  document.getElementById('tidy-panel').hidden = true;
}
document.getElementById('pf-consolidate').onclick = async () => {
  const btn = document.getElementById('pf-consolidate');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try {
    const res = await post('/api/profile/points/tidy', {});
    renderTidyAll(res);
    document.getElementById('overlay').hidden = false;
    document.getElementById('tidy-panel').hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Tidy master doc';
  }
};
function renderTidyAll(res) {
  const body = document.getElementById('tidy-body');
  if (!res.companies.length) {
    body.innerHTML = `<p class="dim">Checked ${res.checked_points} point(s) -- nothing needs combining right now.</p>`;
    document.getElementById('tidy-commit').hidden = true;
    return;
  }
  document.getElementById('tidy-commit').hidden = false;
  body.innerHTML = `<p class="dim">Checked ${res.checked_points} point(s) across all companies, ${res.companies.length} found something to combine.</p>`;
  res.companies.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'pf-proposal-row';
    row.dataset.company = c.company;
    row.dataset.role = c.role;
    row.innerHTML = `
      <input type="checkbox" checked class="tidy-check">
      <div style="flex:1">
        <b>${esc(c.company)}</b>
        <textarea class="tidy-text" rows="${Math.max(3, c.proposed_texts.length * 2)}">${esc(c.proposed_texts.join('\n\n'))}</textarea>
        ${c.notes.length ? `<p class="dim" style="font-size:11.5px">${c.notes.map(esc).join(' -- ')}</p>` : ''}
      </div>`;
    body.appendChild(row);
  });
}
document.getElementById('tidy-commit').onclick = async () => {
  const btn = document.getElementById('tidy-commit');
  btn.disabled = true;
  try {
    const companies = [];
    document.querySelectorAll('#tidy-body .pf-proposal-row').forEach(row => {
      if (!row.querySelector('.tidy-check').checked) return;
      const texts = row.querySelector('.tidy-text').value.split('\n\n').map(t => t.trim()).filter(Boolean);
      companies.push({company: row.dataset.company, role: row.dataset.role, texts});
    });
    const res = await post('/api/profile/points/tidy-commit', {companies});
    flashCount(`Tidied ${res.companies_updated} company/companies.`);
    loadProfile();
    closeTidy();
  } finally {
    btn.disabled = false;
  }
};

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

// Keyword+Use+Result rubric, LLM rewrite-or-refuse: runs BEFORE a point-bank
// save, never invents a fact. Either proposes a rewrite (same facts, better
// shape) or says exactly what's missing so he can recall the real fact --
// shared by both the add-point modal and the Manage Entries edit flow below.
async function runRewriteCheck(text, company, role) {
  return await post('/api/profile/points/rewrite-check', {text, company, role});
}

function renderRewriteReview(panel, result, handlers) {
  panel.hidden = false;
  const body = panel.querySelector('.rewrite-body');
  const actions = panel.querySelector('.rewrite-actions');
  actions.innerHTML = '';
  const addBtn = (label, fn, primary) => {
    const btn = document.createElement('button');
    btn.className = 'small' + (primary ? ' primary' : '');
    btn.textContent = label;
    btn.onclick = fn;
    actions.appendChild(btn);
  };
  // Full response shown verbatim -- no trimming -- since the whole point of
  // asking Zai is to tell him exactly what's missing or exactly what changed.
  if (result.status === 'rewritten') {
    body.innerHTML = `<b>Proposed rewrite</b> (same facts, reshaped -- nothing added):<p class="rewrite-text">${esc(result.text)}</p>`;
    addBtn('Use this version', () => handlers.useRewritten(result.text), true);
    addBtn('Keep my original text', handlers.keepOriginal);
  } else if (result.status === 'missing_info') {
    body.innerHTML = `<b>Missing:</b> ${esc(result.explanation)}<br><b>To recall it:</b> ${esc(result.question)}`;
    addBtn('Save as written anyway', handlers.saveAnyway);
    addBtn('Let me edit it', handlers.dismiss);
  } else {
    body.innerHTML = esc(result.message || 'Check failed.');
    addBtn('Retry check', handlers.retry);
    addBtn('Save as written anyway', handlers.saveAnyway);
  }
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
        <button class="small pf-save">Check &amp; save</button>
        <button class="small pf-toggle">${p.active ? 'Retire' : 'Restore'}</button>
      </div>
      <div class="pf-rewrite-panel rewrite-panel" hidden>
        <div class="rewrite-body"></div>
        <div class="rewrite-actions"></div>
      </div>`;
    const textarea = row.querySelector('textarea');
    const rewritePanel = row.querySelector('.pf-rewrite-panel');
    async function finishSavePoint(text) {
      rewritePanel.hidden = true;
      const res = await post('/api/profile/points/update', {id: p.id, text});
      flashCount(res.quality_warning
        ? `Saved -- but ${res.quality_warning.weak_opener ? 'opens on a bare generic verb with no brag' : 'has no brag (no number, award, or named result)'}.`
        : 'Point updated.');
      loadProfile();
    }
    row.querySelector('.pf-save').onclick = async () => {
      const text = textarea.value.trim();
      const result = await runRewriteCheck(text, p.company, p.role);
      renderRewriteReview(rewritePanel, result, {
        useRewritten: (newText) => { textarea.value = newText; finishSavePoint(newText); },
        keepOriginal: () => finishSavePoint(text),
        saveAnyway: () => finishSavePoint(text),
        retry: () => row.querySelector('.pf-save').click(),
        dismiss: () => { rewritePanel.hidden = true; },
      });
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

// ── upload + extract ─────────────────────────────────────────────────
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function runExtract(body, triggerBtn) {
  const status = document.getElementById('pf-upload-status');
  status.textContent = 'Extracting… (calls the LLM, can take a few seconds)';
  triggerBtn.disabled = true;
  try {
    const res = await post('/api/profile/extract', body);
    if (res.error) { status.textContent = 'Error: ' + res.error; return; }
    const dupeCount = res.proposal.points.filter(p => p.likely_duplicate).length;
    status.textContent = `Found ${res.proposal.points.length} point(s), ${res.proposal.skills.length} skill(s)`
      + (dupeCount ? ` (${dupeCount} look like duplicates already on file -- unchecked below).` : '.');
    document.getElementById('pf-proposal-wrap').hidden = false;
    renderProposal(res.proposal);
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    triggerBtn.disabled = false;
  }
}

document.getElementById('pf-upload-btn').onclick = async () => {
  const file = document.getElementById('pf-upload').files[0];
  const btn = document.getElementById('pf-upload-btn');
  if (!file) { document.getElementById('pf-upload-status').textContent = 'Choose a file first.'; return; }
  const content_base64 = await fileToBase64(file);
  await runExtract({filename: file.name, content_base64}, btn);
};

document.getElementById('pf-paste-toggle').onclick = () => {
  const box = document.getElementById('pf-paste-box');
  box.hidden = !box.hidden;
  document.getElementById('pf-paste-toggle').textContent = box.hidden ? 'or paste text instead →' : '← or upload a file instead';
};
document.getElementById('pf-paste-btn').onclick = async () => {
  const text = document.getElementById('pf-paste-text').value.trim();
  const btn = document.getElementById('pf-paste-btn');
  if (!text) { document.getElementById('pf-upload-status').textContent = 'Paste some text first.'; return; }
  await runExtract({text}, btn);
};

function renderProposal(proposal) {
  const el = document.getElementById('pf-proposal');
  el.innerHTML = `
    <h3 style="font-size:13px;margin-top:16px">Proposed points (uncheck to skip)</h3>
    <div id="pf-proposal-points"></div>
    <h3 style="font-size:13px;margin-top:16px">Proposed skills (uncheck to skip)</h3>
    <div id="pf-proposal-skills"></div>
    <button id="pf-commit" class="primary" style="margin-top:12px">Add to master doc</button>
  `;
  const pointsEl = document.getElementById('pf-proposal-points');
  proposal.points.forEach((p, i) => {
    const row = document.createElement('div');
    row.className = 'pf-proposal-row';
    // likely_duplicate comes from the server (difflib against what's
    // already in the point bank) -- default the checkbox OFF for those so
    // committing a near-duplicate takes a deliberate re-check, not a miss.
    const dupeBadge = p.likely_duplicate
      ? `<span class="dupe-badge" title="Close match: ${esc(p.duplicate_of_text || '')}">possible duplicate</span><br>` : '';
    // needs_review comes from the LLM itself: it found no real number/
    // outcome/named result anywhere in the source text for this point, so
    // it extracted it as written rather than inventing one -- same refuse-
    // and-ask discipline as the single-bullet rewrite check, surfaced here
    // instead of silently saving a weak bullet.
    const reviewBadge = p.needs_review
      ? `<span class="badge badge-stale" title="${esc(p.review_note || '')}">needs a real result -- ${esc(p.review_note || '')}</span><br>` : '';
    row.innerHTML = `
      <input type="checkbox" ${p.likely_duplicate ? '' : 'checked'} data-idx="${i}" class="prop-point-check">
      <div style="flex:1">
        ${dupeBadge}
        ${reviewBadge}
        <div class="dim" style="font-size:11.5px">${esc(p.company)} / ${esc(p.role)} &middot; ${esc(p.start_date || '?')}&ndash;${esc(p.end_date || 'present')}</div>
        <textarea data-idx="${i}" class="prop-point-text">${esc(p.text)}</textarea>
      </div>`;
    pointsEl.appendChild(row);
  });
  const skillsEl = document.getElementById('pf-proposal-skills');
  proposal.skills.forEach((s, i) => {
    const row = document.createElement('label');
    row.style.cssText = 'display:inline-flex;align-items:center;gap:5px;margin:2px 10px 2px 0;font-size:12.5px';
    row.innerHTML = `<input type="checkbox" checked data-idx="${i}" class="prop-skill-check"> ${esc(s.name)} <span class="dim">(${esc(s.category)})</span>`;
    skillsEl.appendChild(row);
  });

  document.getElementById('pf-commit').onclick = async () => {
    const points = [];
    document.querySelectorAll('.prop-point-check').forEach(cb => {
      if (cb.checked) {
        const i = cb.dataset.idx;
        const text = document.querySelector(`.prop-point-text[data-idx="${i}"]`).value;
        points.push({...proposal.points[i], text});
      }
    });
    const skills = [];
    document.querySelectorAll('.prop-skill-check').forEach(cb => {
      if (cb.checked) skills.push(proposal.skills[cb.dataset.idx]);
    });
    const res = await post('/api/profile/extract/commit', {points, skills});
    document.getElementById('pf-upload-status').textContent =
      `Added ${res.added_points} point(s), ${res.added_skills} skill(s) to the master doc`
      + (res.skipped_dupes ? ` (${res.skipped_dupes} skipped as duplicates).` : '.');
    document.getElementById('pf-proposal-wrap').hidden = true;
    document.getElementById('pf-proposal').innerHTML = '';
    document.getElementById('pf-upload').value = '';
  };
}

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

// The "More" menu is a native <details>, which only closes on its own
// summary click -- clicking anywhere else on the page left it hanging open.
document.addEventListener('click', (e) => {
  const more = document.getElementById('more-actions');
  if (more.open && !more.contains(e.target)) more.open = false;
});

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

loadJobs();
