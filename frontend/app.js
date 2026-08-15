const API = "/api";
let STATE = { coverage: null, matrix: null, catalog: null, ingestionLog: null, rfcMeta: null, artefacts: null, existingTests: null, library: null };
let charts = {};

function toast(msg, isError=false){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(()=> el.classList.remove('show'), 3200);
}

async function api(path, opts){
  const res = await fetch(API + path, opts);
  if(!res.ok){
    const body = await res.json().catch(()=>({error:res.statusText}));
    throw new Error(body.error || res.statusText);
  }
  return res.json();
}

// ---------- Nav ----------
document.querySelectorAll('#tabNav button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#tabNav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ---------- Boot ----------
async function boot(){
  try{
    const status = await api('/status');
    STATE.rfcMeta = status.rfc;
    renderHeader(status);
    await refreshAll();
  }catch(e){
    toast('Failed to reach backend: ' + e.message, true);
  }
}

async function refreshAll(){
  const [coverage, matrix, catalog, ingestionLog, artefacts, existingTests, library] = await Promise.all([
    api('/coverage'), api('/matrix'), api('/tests'), api('/ingestion-log'), api('/artefacts'), api('/existing-tests'),
    api('/knowledge-library')
  ]);
  STATE.coverage = coverage;
  STATE.matrix = matrix;
  STATE.catalog = catalog;
  STATE.ingestionLog = ingestionLog;
  STATE.artefacts = artefacts;
  STATE.existingTests = existingTests;
  STATE.library = library;

  renderStats(coverage);
  renderCharts(coverage, catalog);
  renderGenQuality(catalog);
  renderMatrix(matrix);
  renderCatalog();
  renderGaps(coverage);
  renderTimeline(ingestionLog);
  renderArtefacts(artefacts);
  renderExistingTests(existingTests);
  renderLibrary(library);
}

async function refreshGapsOnly(){
  const [coverage, existingTests] = await Promise.all([api('/coverage'), api('/existing-tests')]);
  STATE.coverage = coverage;
  STATE.existingTests = existingTests;
  renderGaps(coverage);
  renderExistingTests(existingTests);
}

function renderHeader(status){
  const meta = status.rfc;
  if(meta){
    document.getElementById('rfcHeading').textContent = `${meta.rfc_title} (RFC ${meta.rfc_number}) — Conformance Coverage`;
    const protocolLabel = meta.protocol_display_name || 'this protocol';
    document.getElementById('rfcSubhead').textContent = `Requirement-to-test traceability for ${protocolLabel} · target: Juniper vJunos-router / vMX`;
  }
  document.getElementById('kbBadgeText').textContent = 'Knowledge base persisted · RFC parsed once · live generation from database';
  const aiBadge = document.getElementById('aiBadge');
  if(status.ai && status.ai.ai_available){
    aiBadge.className = 'kb-badge';
    const backendLabel = status.ai.backend === 'claude-code-cli'
      ? 'via local Claude Code · no API key'
      : (status.ai.backend === 'anthropic-api' ? 'via Anthropic API key' : status.ai.backend);
    aiBadge.textContent = `AI reasoning active · ${status.ai.model} · ${backendLabel}`;
  } else {
    aiBadge.className = 'kb-badge warn';
    aiBadge.textContent = 'Heuristic mode · no AI backend available (run inside Claude Code, or set ANTHROPIC_API_KEY)';
  }
}

function renderStats(cov){
  const statRow = document.getElementById('statRow');
  const stats = [
    {num: cov.total_requirements, lbl: 'Requirements Extracted', cls:''},
    {num: cov.tests_generated, lbl: 'Tests Generated', cls:'accent-blue'},
    {num: cov.overall_coverage_pct + '%', lbl: 'Overall Coverage', cls:'accent-green'},
    {num: cov.automatable_coverage_pct + '%', lbl: 'Automatable Coverage', cls:'accent-green'},
    {num: cov.gap_count, lbl: 'Gaps Flagged', cls:'accent-amber'},
  ];
  statRow.innerHTML = stats.map(s => `<div class="stat ${s.cls}"><div class="num">${s.num}</div><div class="lbl">${s.lbl}</div></div>`).join('');
}

function renderCharts(cov, catalog){
  if(charts.category) charts.category.destroy();
  if(charts.type) charts.type.destroy();

  charts.category = new Chart(document.getElementById('chartCategory'), {
    type: 'bar',
    data: {
      labels: cov.category_breakdown.map(c=>c.category),
      datasets: [
        {label:'Covered', data: cov.category_breakdown.map(c=>c.covered), backgroundColor:'#38D69C'},
        {label:'Gap', data: cov.category_breakdown.map(c=>c.total-c.covered), backgroundColor:'#223050'},
      ]
    },
    options: {
      responsive:true,
      plugins:{legend:{labels:{color:'#8C9BB8', font:{family:'IBM Plex Mono', size:10.5}}}},
      scales:{
        x:{stacked:true, ticks:{color:'#8C9BB8', font:{family:'IBM Plex Mono', size:9.5}, maxRotation:45, minRotation:45}, grid:{color:'#1A263F'}},
        y:{stacked:true, ticks:{color:'#8C9BB8', font:{size:10}}, grid:{color:'#1A263F'}}
      }
    }
  });

  const typeCounts = {};
  catalog.forEach(t => { typeCounts[t.test_type] = (typeCounts[t.test_type]||0)+1; });
  const typeColors = {positive:'#38D69C', negative:'#F2586A', boundary:'#F0B84C', policy:'#5B9DF5', recovery:'#c78bf0'};
  charts.type = new Chart(document.getElementById('chartTestType'), {
    type: 'doughnut',
    data: {
      labels: Object.keys(typeCounts),
      datasets: [{data: Object.values(typeCounts), backgroundColor: Object.keys(typeCounts).map(k=>typeColors[k]||'#5C6A88'), borderColor:'#101A2E', borderWidth:2}]
    },
    options: { plugins:{legend:{position:'right', labels:{color:'#8C9BB8', font:{family:'IBM Plex Mono', size:11}}}} }
  });
}

function renderGenQuality(catalog){
  const row = document.getElementById('genQualityRow');
  const total = catalog.length;
  if(total === 0){
    row.innerHTML = '<div class="stat"><div class="num">—</div><div class="lbl">No tests generated yet</div></div>';
    return;
  }
  const highConf = catalog.filter(t => t.generation_mode === 'ai-high').length;
  const needsReview = catalog.filter(t => t.needs_review).length;
  const emulatorNeeded = catalog.filter(t => t.requires_peer_emulator).length;
  const heuristic = catalog.filter(t => !t.generation_mode || !t.generation_mode.startsWith('ai-')).length;
  const stats = [
    {num: total, lbl: 'Total Tests', cls:''},
    {num: `${highConf} (${Math.round(100*highConf/total)}%)`, lbl: 'High Confidence', cls:'accent-green'},
    {num: `${needsReview} (${Math.round(100*needsReview/total)}%)`, lbl: 'Needs Review', cls:'accent-amber'},
    {num: emulatorNeeded, lbl: 'Requires Peer Emulator', cls:'accent-amber'},
    {num: heuristic, lbl: 'Heuristic (no AI)', cls: heuristic > 0 ? 'accent-amber' : ''},
  ];
  row.innerHTML = stats.map(s => `<div class="stat ${s.cls}"><div class="num">${s.num}</div><div class="lbl">${s.lbl}</div></div>`).join('');
}

// ---------- Semantic search ----------
document.getElementById('searchBtn').addEventListener('click', runSearch);
document.getElementById('searchInput').addEventListener('keydown', e => { if(e.key==='Enter') runSearch(); });
async function runSearch(){
  const q = document.getElementById('searchInput').value.trim();
  if(!q) return;
  try{
    const results = await api('/search?q=' + encodeURIComponent(q) + '&k=8');
    const tbody = document.querySelector('#searchResults tbody');
    tbody.innerHTML = results.map(r => `
      <tr><td class="reqid">${r.similarity}</td><td class="reqid">${r.requirement_id}</td><td>${r.statement}</td></tr>
    `).join('') || '<tr><td colspan="3" style="color:var(--text-faint);">No matches.</td></tr>';
  }catch(e){ toast('Search failed: ' + e.message, true); }
}

// ---------- Matrix ----------
function renderMatrix(matrix){
  const reqs = matrix.requirements;
  const coveredIds = new Set(matrix.covered_ids);
  const categories = [...new Set(reqs.map(r=>r.category))].sort();
  const sections = [...new Map(reqs.map(r=>[r.section_id, r.section_title])).entries()]
    .sort((a,b)=>{
      const pa = a[0].split('.').map(Number), pb = b[0].split('.').map(Number);
      for (let i=0;i<Math.max(pa.length,pb.length);i++){ const x=pa[i]||0,y=pb[i]||0; if(x!==y) return x-y; }
      return 0;
    });

  function cellReqs(sectionId, category){ return reqs.filter(r=>r.section_id===sectionId && r.category===category); }
  function cellColor(total, covered){
    if (total===0) return null;
    const pct = covered/total;
    if (pct===0) return '#1A263F';
    if (pct<0.5) return '#4A3B1C';
    if (pct<1) return '#5c4d1e';
    return '#1E7A54';
  }

  const thead = '<tr><th class="row-hdr-th">Section</th>' + categories.map(c=>`<th>${c.replace(/_/g,' ')}</th>`).join('') + '</tr>';
  const rows = sections.map(([sid, title]) => {
    const cells = categories.map(cat => {
      const rs = cellReqs(sid, cat);
      if(rs.length===0) return '<td><div class="cell empty">·</div></td>';
      const covered = rs.filter(r=>coveredIds.has(r.requirement_id)).length;
      const color = cellColor(rs.length, covered);
      return `<td><div class="cell" style="background:${color};" data-section="${sid}" data-category="${cat}" title="${covered}/${rs.length} covered">${covered}/${rs.length}</div></td>`;
    }).join('');
    return `<tr><td class="row-hdr">${sid} ${title}</td>${cells}</tr>`;
  }).join('');
  document.getElementById('matrixTable').innerHTML = thead + rows;

  document.querySelectorAll('.cell[data-section]').forEach(cell => {
    cell.addEventListener('click', () => showMatrixDrilldown(cell.dataset.section, cell.dataset.category, reqs, coveredIds));
  });
}

let currentDrilldown = null;
function showMatrixDrilldown(sid, cat, reqs, coveredIds){
  const rs = reqs.filter(r=>r.section_id===sid && r.category===cat);
  currentDrilldown = rs;
  const wrap = document.getElementById('matrixDrilldown');
  wrap.style.display = 'block';
  document.getElementById('matrixDrilldownTitle').textContent = `Section ${sid} · ${cat.replace(/_/g,' ')}`;
  document.getElementById('matrixDrilldownDesc').textContent = `${rs.length} requirement(s) extracted for this section/category pair`;
  const tbody = document.querySelector('#matrixDrilldownTable tbody');
  tbody.innerHTML = rs.map(r => {
    const isCovered = coveredIds.has(r.requirement_id);
    return `<tr>
      <td class="reqid">${r.requirement_id}</td>
      <td><span class="pill" style="background:var(--panel-raised);color:var(--text-muted);">${r.keyword}</span></td>
      <td>${r.statement}</td>
      <td>${isCovered ? '<span style="color:var(--signal-green);">&#9679; covered</span>' : '<span style="color:var(--signal-amber);">&#9675; gap</span>'}</td>
    </tr>`;
  }).join('');
  wrap.scrollIntoView({behavior:'smooth', block:'nearest'});
}

document.getElementById('generateSelectedBtn').addEventListener('click', async () => {
  if(!currentDrilldown || currentDrilldown.length===0) return;
  const coveredIds = new Set(STATE.matrix.covered_ids);
  const uncoveredIds = currentDrilldown.filter(r=>!coveredIds.has(r.requirement_id)).map(r=>r.requirement_id);
  if(uncoveredIds.length===0){ toast('Everything in this cell is already covered.'); return; }
  try{
    const result = await api('/generate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({requirement_ids: uncoveredIds, label:'matrix-drilldown'})
    });
    toast(`Generated ${result.created.length} new test(s).`);
    await refreshAll();
  }catch(e){ toast('Generation failed: ' + e.message, true); }
});

// ---------- Test Catalog ----------
function populateCatalogFilters(){
  const catFilter = document.getElementById('catalogCategoryFilter');
  const typeFilter = document.getElementById('catalogTypeFilter');
  const batchFilter = document.getElementById('catalogBatchFilter');
  const cats = [...new Set(STATE.catalog.map(t=>t.category))].sort();
  const types = [...new Set(STATE.catalog.map(t=>t.test_type))].sort();
  const batches = [...new Set(STATE.catalog.map(t=>t.batch_id))].sort((a,b)=>a-b);
  catFilter.innerHTML = '<option value="">All categories</option>' + cats.map(c=>`<option value="${c}">${c.replace(/_/g,' ')}</option>`).join('');
  typeFilter.innerHTML = '<option value="">All test types</option>' + types.map(t=>`<option value="${t}">${t}</option>`).join('');
  batchFilter.innerHTML = '<option value="">All batches</option>' + batches.map(b=>`<option value="${b}">Batch ${b}</option>`).join('');
}

function genModeBadge(t){
  let parts = [];
  if(t.generation_mode && t.generation_mode.startsWith('ai-')){
    const conf = t.generation_mode.replace('ai-','');
    const color = conf==='high' ? 'positive' : (conf==='medium' ? 'boundary' : 'negative');
    const backendTitle = t.ai_backend === 'claude-code-cli' ? 'via local Claude Code (no API key)'
      : (t.ai_backend === 'anthropic-api' ? 'via Anthropic API key' : '');
    parts.push(`<span class="pill ${color}" title="${backendTitle}">AI · ${conf}</span>`);
  } else {
    parts.push(`<span class="pill" style="background:var(--panel-raised);color:var(--text-muted);">heuristic</span>`);
  }
  if(t.needs_review){ parts.push(`<span class="pill negative" title="Review before trusting this assertion">review</span>`); }
  if(t.requires_peer_emulator){ parts.push(`<span class="pill policy" title="${t.emulator_tool} required">${t.emulator_tool||'emulator'}</span>`); }
  if(t.updated_at){ parts.push(`<span class="pill boundary" title="Regenerated ${t.updated_at} — new related knowledge was ingested since this test was first created">modified</span>`); }
  return parts.join(' ');
}

function renderCatalog(){
  populateCatalogFilters();
  applyCatalogFilters();
}

function applyCatalogFilters(){
  const q = document.getElementById('catalogSearch').value.toLowerCase();
  const cat = document.getElementById('catalogCategoryFilter').value;
  const type = document.getElementById('catalogTypeFilter').value;
  const batch = document.getElementById('catalogBatchFilter').value;
  const tbody = document.querySelector('#catalogTable tbody');
  const rows = STATE.catalog.filter(t => {
    if (cat && t.category!==cat) return false;
    if (type && t.test_type!==type) return false;
    if (batch && String(t.batch_id)!==batch) return false;
    if (q && !(t.requirement_id.toLowerCase().includes(q) || t.statement.toLowerCase().includes(q) || t.test_id.toLowerCase().includes(q))) return false;
    return true;
  });
  tbody.innerHTML = rows.map(t => `
    <tr class="clickable" data-testid="${t.test_id}">
      <td class="reqid">${t.test_id}</td>
      <td>${t.requirement_id}</td>
      <td>${t.category.replace(/_/g,' ')}</td>
      <td><span class="pill ${t.test_type}">${t.test_type}</span></td>
      <td class="risk-${t.risk}">${t.risk}</td>
      <td>${t.section_id}</td>
      <td>${t.batch_id}</td>
      <td>${genModeBadge(t)}</td>
    </tr>
  `).join('') || '<tr><td colspan="8" style="color:var(--text-faint);padding:20px;">No tests match these filters.</td></tr>';

  tbody.querySelectorAll('tr[data-testid]').forEach(tr => {
    tr.addEventListener('click', () => openModal(tr.dataset.testid));
  });
}
['catalogSearch','catalogCategoryFilter','catalogTypeFilter','catalogBatchFilter'].forEach(id=>{
  document.getElementById(id).addEventListener('input', applyCatalogFilters);
  document.getElementById(id).addEventListener('change', applyCatalogFilters);
});

// ---------- Run tests (mocked PyEZ) ----------
document.getElementById('runTestsBtn').addEventListener('click', async () => {
  const btn = document.getElementById('runTestsBtn');
  const status = document.getElementById('runTestsStatus');
  btn.disabled = true; btn.textContent = 'Running…';
  status.textContent = 'Executing the deduplicated test catalog against mocked PyEZ…';
  try{
    const result = await api('/tests/run', { method: 'POST' });
    renderRunTestsResult(result);
    status.textContent = result.note || `Done — ${result.total} test(s) run.`;
    toast(result.total ? `${result.passed}/${result.total} test(s) passed.` : 'No deduplicated tests to run yet.');
  }catch(e){
    status.textContent = '';
    toast('Test run failed: ' + e.message, true);
  }finally{
    btn.disabled = false; btn.textContent = 'Run deduplicated tests';
  }
});

function renderRunTestsResult(result){
  const summaryEl = document.getElementById('runTestsSummary');
  const stats = [
    {num: result.total, lbl: 'Total', cls: ''},
    {num: result.passed, lbl: 'Passed', cls: 'accent-green'},
    {num: result.failed, lbl: 'Failed', cls: 'accent-amber'},
    {num: result.errored, lbl: 'Errored', cls: 'accent-amber'},
  ];
  summaryEl.innerHTML = stats.map(s => `<div class="stat ${s.cls}"><div class="num">${s.num}</div><div class="lbl">${s.lbl}</div></div>`).join('');

  const table = document.getElementById('runTestsTable');
  const tbody = table.querySelector('tbody');
  if(!result.tests || !result.tests.length){
    table.style.display = 'none';
    return;
  }
  table.style.display = '';
  const escapeHtml = s => (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  tbody.innerHTML = result.tests.map(t => {
    const color = t.outcome === 'passed' ? 'positive' : (t.outcome === 'skipped' ? 'boundary' : 'negative');
    return `<tr>
      <td class="reqid">${escapeHtml(t.test_id.split('::').pop())}</td>
      <td><span class="pill ${color}">${t.outcome}</span></td>
      <td>${t.duration}</td>
      <td style="white-space:pre-wrap;font-family:monospace;font-size:11px;">${escapeHtml(t.message)}</td>
    </tr>`;
  }).join('');
}

async function openModal(testId){
  try{
    const t = await api('/tests/' + encodeURIComponent(testId));
    document.getElementById('modalTestId').textContent = t.test_id + '  ·  ' + t.requirement_id;
    document.getElementById('modalDocContent').innerHTML = DOMPurify.sanitize(marked.parse(t.doc_content || ''));
    document.getElementById('modalPytestContent').textContent = t.pytest_content;
    document.getElementById('modalBackdrop').classList.add('open');
    document.querySelectorAll('.modal-tabs button').forEach(b=>b.classList.remove('active'));
    document.querySelector('.modal-tabs button[data-pane="doc"]').classList.add('active');
    document.querySelectorAll('.modal-pane').forEach(p=>p.classList.remove('active'));
    document.getElementById('pane-doc').classList.add('active');
  }catch(e){ toast('Could not load test detail: ' + e.message, true); }
}
document.getElementById('modalClose').addEventListener('click', ()=>document.getElementById('modalBackdrop').classList.remove('open'));
document.getElementById('modalBackdrop').addEventListener('click', (e)=>{ if(e.target.id==='modalBackdrop') e.currentTarget.classList.remove('open'); });
document.querySelectorAll('.modal-tabs button').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    document.querySelectorAll('.modal-tabs button').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.modal-pane').forEach(p=>p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('pane-'+btn.dataset.pane).classList.add('active');
  });
});

// ---------- Gap Analysis ----------
function renderGaps(cov){
  const realGaps = cov.gaps_after_existing_tests || cov.gaps;
  const gapCatFilter = document.getElementById('gapCategoryFilter');
  const cats = [...new Set(realGaps.map(g=>g.category))].sort();
  gapCatFilter.innerHTML = '<option value="">All categories</option>' + cats.map(c=>`<option value="${c}">${c.replace(/_/g,' ')}</option>`).join('');
  gapCatFilter.onchange = () => paintGaps(cov, gapCatFilter.value);
  paintGaps(cov, '');
  renderExistingCoverage(cov);
}

function paintGaps(cov, filterCat){
  const realGaps = cov.gaps_after_existing_tests || cov.gaps;
  const groups = {};
  realGaps.forEach(g => {
    if (filterCat && g.category!==filterCat) return;
    groups[g.category] = groups[g.category] || [];
    groups[g.category].push(g);
  });
  const container = document.getElementById('gapGroups');
  container.innerHTML = Object.entries(groups).sort((a,b)=>b[1].length-a[1].length).map(([cat, items], idx) => `
    <div class="gap-cat-group">
      <div class="gap-cat-hdr">
        <div class="name-wrap" data-idx="${idx}">
          <span class="name">${cat.replace(/_/g,' ')}</span>
          <span class="count">${items.length} gap(s)</span>
        </div>
        <button class="btn btn-small" data-gen-cat="${cat}">Generate 5 tests for this category</button>
      </div>
      <div class="gap-items" id="gap-items-${idx}">
        <div class="gap-item" style="border-left-color:var(--signal-blue);">
          <div class="rec"><span class="recommend-icon">&#9679;</span> Recommendation: ${items[0].recommendation}</div>
        </div>
        ${items.map(g => `
          <div class="gap-item">
            <div class="stmt"><span class="reqid">${g.requirement_id}</span> — ${g.statement}</div>
          </div>
        `).join('')}
      </div>
    </div>
  `).join('') || '<p class="muted-note">No gaps match this filter.</p>';

  container.querySelectorAll('.name-wrap').forEach(hdr => {
    hdr.addEventListener('click', () => document.getElementById('gap-items-'+hdr.dataset.idx).classList.toggle('open'));
  });
  container.querySelectorAll('button[data-gen-cat]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const category = btn.dataset.genCat;
      btn.disabled = true; btn.textContent = 'Generating…';
      try{
        const result = await api('/generate-by-category', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({category, count:5})
        });
        toast(`Generated ${result.created.length} test(s) for ${category.replace(/_/g,' ')}.`);
        await refreshAll();
      }catch(err){ toast('Generation failed: ' + err.message, true); btn.disabled=false; btn.textContent='Generate 5 tests for this category'; }
    });
  });
}

document.getElementById('generateAllGapsBtn').addEventListener('click', async () => {
  const cov = STATE.coverage;
  const gapCount = (cov.gaps_after_existing_tests || cov.gaps || []).length;
  // gapCount==0 doesn't necessarily mean there's nothing to do -- a
  // knowledge-library ingest may have flagged existing tests context_stale
  // without opening any new automatable gap, so this still needs to run to
  // pick up that "modified tests" pass (see pipeline.regenerate_stale_tests).
  if(gapCount > 0 && !confirm(`Generate tests for all ${gapCount} remaining gap(s)? This calls the AI backend for each one and may take a while (roughly a few seconds per test, several running concurrently).`)) return;
  const btn = document.getElementById('generateAllGapsBtn');
  const status = document.getElementById('generateAllGapsStatus');
  btn.disabled = true; btn.textContent = 'Generating…';
  status.textContent = gapCount > 0 ? `Generating ${gapCount} test(s) — this can take several minutes for a large batch…`
                                     : 'Checking for tests to refresh from newly ingested knowledge…';
  try{
    const result = await api('/generate-all', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({}) });
    const modifiedCount = (result.modified || []).length;
    const modifiedNote = modifiedCount ? `, ${modifiedCount} modified` : '';
    status.textContent = `Done — ${result.created.length} test(s) created${modifiedNote}.`;
    toast(`Generated ${result.created.length} new test(s)${modifiedCount ? `, ${modifiedCount} modified due to newly ingested context` : ''}.`);
    await refreshAll();
  }catch(e){
    status.textContent = '';
    toast('Bulk generation failed: ' + e.message, true);
  }finally{
    btn.disabled = false; btn.textContent = 'Generate all remaining gaps';
  }
});

function confidencePill(conf){
  const color = conf==='high' ? 'positive' : (conf==='medium' ? 'boundary' : 'negative');
  return `<span class="pill ${color}">${conf} confidence</span>`;
}

function renderExistingCoverage(cov){
  const panel = document.getElementById('existingCoveragePanel');
  const list = document.getElementById('existingCoverageList');
  const items = cov.existing_test_coverage || [];
  if(!items.length){ panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  list.innerHTML = items.map(g => `
    <div class="gap-item" style="border-left-color:var(--signal-green);">
      <div class="stmt"><span class="reqid">${g.requirement_id}</span> — ${g.statement}</div>
      ${g.matched_by.map(m => `
        <div class="rec" style="margin-top:6px;">
          <span class="recommend-icon" style="color:var(--signal-green);">&#9679;</span>
          Matched by <code>${m.filename}</code> ${confidencePill(m.confidence)} — ${m.rationale}
        </div>
      `).join('')}
    </div>
  `).join('');
}

// ---------- Existing test suite uploads (AI-reviewed coverage) ----------
function renderExistingTests(tests){
  const tbody = document.querySelector('#existingTestsTable tbody');
  tbody.innerHTML = tests.map(t => {
    let statusLbl;
    if (!t.analyzed) statusLbl = '<span style="color:var(--signal-amber);">not yet analyzed</span>';
    else if (t.analysis_mode.startsWith('ai-')) statusLbl = '<span style="color:var(--signal-green);">AI-reviewed</span>';
    else if (t.analysis_mode.startsWith('heuristic-fallback')) statusLbl = `<span style="color:var(--signal-amber);" title="${t.analysis_mode}">heuristic (no AI)</span>`;
    else statusLbl = `<span style="color:var(--text-faint);">${t.analysis_mode || t.notes}</span>`;
    const analyzeLabel = t.analyzed ? 'Re-analyze' : 'Analyze';
    return `<tr data-id="${t.id}">
      <td>${t.filename}</td>
      <td>${t.char_count.toLocaleString()}</td>
      <td>${t.uploaded_at}</td>
      <td>${statusLbl}${t.notes && !t.analyzed ? ` — ${t.notes}` : ''}</td>
      <td>${t.matched_requirement_count}</td>
      <td style="white-space:nowrap;">
        <button class="btn btn-small" data-analyze="${t.id}">${analyzeLabel}</button>
        <button class="btn btn-small" data-del-test="${t.id}">Remove</button>
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="color:var(--text-faint);padding:16px;">No existing tests uploaded yet.</td></tr>';

  tbody.querySelectorAll('button[data-analyze]').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.textContent = 'Analyzing…';
      try{
        await api('/existing-tests/' + btn.dataset.analyze + '/analyze', { method:'POST' });
        toast('Existing test analyzed against RFC requirements.');
        await refreshGapsOnly();
      }catch(e){ toast('Analysis failed: ' + e.message, true); btn.disabled=false; }
    });
  });
  tbody.querySelectorAll('button[data-del-test]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if(!confirm('Remove this uploaded test and its coverage matches?')) return;
      try{
        await api('/existing-tests/' + btn.dataset.delTest, { method:'DELETE' });
        toast('Existing test removed.');
        await refreshGapsOnly();
      }catch(e){ toast('Could not remove: ' + e.message, true); }
    });
  });
}

document.getElementById('existingTestUploadBtn').addEventListener('click', async () => {
  const file = document.getElementById('existingTestFile').files[0];
  if(!file){ toast('Choose a file to upload.', true); return; }
  const status = document.getElementById('existingTestStatus');
  status.textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append('file', file);
  try{
    const result = await api('/existing-tests/upload', { method:'POST', body: form });
    status.textContent = result.notes ? `Uploaded — ${result.notes}` : `Uploaded — ${result.char_count.toLocaleString()} chars. Click Analyze to review it.`;
    toast(`${file.name} uploaded.`);
    document.getElementById('existingTestFile').value = '';
    await refreshGapsOnly();
  }catch(e){
    status.textContent = '';
    toast('Upload failed: ' + e.message, true);
  }
});

document.getElementById('existingTestAnalyzeAllBtn').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true; btn.textContent = 'Analyzing…';
  try{
    const result = await api('/existing-tests/analyze-all', { method:'POST' });
    toast(`Analyzed ${result.analyzed.length} existing test(s).`);
    await refreshGapsOnly();
  }catch(e){ toast('Bulk analysis failed: ' + e.message, true); }
  finally{ btn.disabled = false; btn.textContent = 'Analyze all pending'; }
});

// ---------- Knowledge Base tab ----------

// Knowledge library: files kept separate from the app code
// (backend/kb/rfc_library/), ingested additively via /api/knowledge-library
// rather than replacing the whole knowledge base like the paste/upload form
// below does.
function renderLibrary(files){
  const tbody = document.querySelector('#libraryTable tbody');
  tbody.innerHTML = files.map(f => {
    const statusLbl = f.ingested
      ? `<span style="color:var(--signal-green);">ingested ${f.ingested_at}</span>`
      : `<span style="color:var(--text-faint);">not ingested</span>`;
    const rfcLbl = f.rfc_number ? `RFC ${f.rfc_number}` : '—';
    const addedLbl = f.requirements_added != null ? f.requirements_added : '—';
    const action = f.ingested ? '' : `<button class="btn btn-small" data-ingest="${f.filename}">Ingest</button>`;
    return `<tr data-filename="${f.filename}">
      <td>${f.filename}</td>
      <td>${rfcLbl}</td>
      <td>${addedLbl}</td>
      <td>${statusLbl}</td>
      <td>${action}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="5" style="color:var(--text-faint);padding:16px;">No files in kb/rfc_library/.</td></tr>';

  tbody.querySelectorAll('button[data-ingest]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const filename = btn.dataset.ingest;
      btn.disabled = true; btn.textContent = 'Ingesting…';
      try{
        const result = await api('/knowledge-library/' + encodeURIComponent(filename) + '/ingest', { method:'POST' });
        toast(`Ingested ${filename}: ${result.requirement_count_added} new requirement(s)`
          + (result.flagged_stale_test_ids.length ? `, ${result.flagged_stale_test_ids.length} existing test(s) flagged for a refresh` : '') + '.');
        const newStatus = await api('/status');
        renderHeader(newStatus);
        await refreshAll();
      }catch(e){
        toast('Ingest failed: ' + e.message, true);
        btn.disabled = false; btn.textContent = 'Ingest';
      }
    });
  });
}

function renderTimeline(log){
  document.getElementById('ingestionTimeline').innerHTML = log.map(e => `
    <div class="timeline-item">
      <div class="t-event">${e.event}</div>
      <div class="t-source">${e.source}</div>
      <div class="t-time">${e.timestamp}</div>
    </div>
  `).join('');
}

document.getElementById('ingestBtn').addEventListener('click', async () => {
  const rfc_number = document.getElementById('ingestNumber').value.trim();
  const rfc_title = document.getElementById('ingestTitle').value.trim();
  const raw_text = document.getElementById('ingestText').value;
  const protocol = document.getElementById('ingestProtocol').value;
  if(!rfc_number || !raw_text){ toast('RFC number and text are required.', true); return; }
  if(!confirm('This replaces the current knowledge base and clears all generated tests. Continue?')) return;
  const status = document.getElementById('ingestStatus');
  status.textContent = 'Parsing and extracting requirements…';
  try{
    const result = await api('/ingest', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rfc_number, rfc_title, raw_text, protocol})
    });
    status.textContent = `Done — ${result.requirement_count} requirements extracted.`;
    toast(`Re-ingested RFC ${rfc_number}: ${result.requirement_count} requirements extracted.`);
    const newStatus = await api('/status');
    renderHeader(newStatus);
    await refreshAll();
  }catch(e){
    status.textContent = '';
    toast('Ingestion failed: ' + e.message, true);
  }
});

document.getElementById('ingestFileBtn').addEventListener('click', async () => {
  const rfc_number = document.getElementById('ingestNumber').value.trim();
  const rfc_title = document.getElementById('ingestTitle').value.trim();
  const protocol = document.getElementById('ingestProtocol').value;
  const file = document.getElementById('ingestFile').files[0];
  if(!rfc_number || !file){ toast('RFC number and a file are required.', true); return; }
  if(!confirm('This replaces the current knowledge base and clears all generated tests. Continue?')) return;
  const status = document.getElementById('ingestStatus');
  status.textContent = `Uploading ${file.name}, extracting text, parsing requirements…`;
  const form = new FormData();
  form.append('rfc_number', rfc_number);
  form.append('rfc_title', rfc_title);
  form.append('protocol', protocol);
  form.append('rfc_file', file);
  try{
    const result = await api('/ingest/upload', { method:'POST', body: form });
    status.textContent = `Done — ${result.requirement_count} requirements extracted from ${file.name}.`;
    toast(`Re-ingested RFC ${rfc_number} from ${file.name}: ${result.requirement_count} requirements extracted.`);
    const newStatus = await api('/status');
    renderHeader(newStatus);
    await refreshAll();
  }catch(e){
    status.textContent = '';
    toast('Upload/ingestion failed: ' + e.message, true);
  }
});

// ---------- Supporting artefacts (product specs / other context) ----------
function renderArtefacts(artefacts){
  const tbody = document.querySelector('#artefactsTable tbody');
  tbody.innerHTML = artefacts.map(a => {
    const typeLbl = a.artefact_type === 'product_spec' ? 'Product spec' : 'Other';
    const statusLbl = a.notes ? `<span style="color:var(--signal-amber);">${a.notes}</span>` : '<span style="color:var(--signal-green);">used in AI context</span>';
    return `<tr data-id="${a.id}">
      <td><span class="pill" style="background:var(--panel-raised);color:var(--text-muted);">${typeLbl}</span></td>
      <td>${a.filename}</td>
      <td>${a.char_count.toLocaleString()}</td>
      <td>${a.uploaded_at}</td>
      <td>${statusLbl}</td>
      <td><button class="btn btn-small" data-del="${a.id}">Remove</button></td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="color:var(--text-faint);padding:16px;">No artefacts uploaded yet.</td></tr>';

  tbody.querySelectorAll('button[data-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if(!confirm('Remove this artefact? It will no longer be used as AI grounding context.')) return;
      try{
        await api('/artefacts/' + btn.dataset.del, { method:'DELETE' });
        const artefacts = await api('/artefacts');
        STATE.artefacts = artefacts;
        renderArtefacts(artefacts);
        toast('Artefact removed.');
      }catch(e){ toast('Could not remove artefact: ' + e.message, true); }
    });
  });
}

document.getElementById('artefactUploadBtn').addEventListener('click', async () => {
  const file = document.getElementById('artefactFile').files[0];
  const artefact_type = document.getElementById('artefactType').value;
  if(!file){ toast('Choose a file to upload.', true); return; }
  const status = document.getElementById('artefactStatus');
  status.textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append('artefact_type', artefact_type);
  form.append('file', file);
  try{
    const result = await api('/artefacts/upload', { method:'POST', body: form });
    status.textContent = result.notes ? `Uploaded — ${result.notes}` : `Uploaded — ${result.char_count.toLocaleString()} chars extracted.`;
    toast(`Artefact ${file.name} uploaded.`);
    document.getElementById('artefactFile').value = '';
    const artefacts = await api('/artefacts');
    STATE.artefacts = artefacts;
    renderArtefacts(artefacts);
  }catch(e){
    status.textContent = '';
    toast('Artefact upload failed: ' + e.message, true);
  }
});

boot();
