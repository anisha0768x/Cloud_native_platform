'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits = 1) => value === null || value === undefined ? '—' : Number(value).toLocaleString(undefined, {maximumFractionDigits:digits});
const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString() : 'Awaiting data';
const badge = value => `<span class="status ${esc(String(value).replace(/[^a-z]/g,''))}">${esc(value)}</span>`;
const button = (label, action, id = '', primary = false) => `<button class="${primary ? 'primary' : 'quiet-btn'} small-btn" data-action="${action}" data-id="${esc(id)}">${esc(label)}</button>`;
const empty = text => `<div class="empty">${esc(text)}</div>`;
const intro = (title, copy, actions = '') => `<div class="intro"><div><h1>${esc(title)}</h1><p>${esc(copy)}</p></div>${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
const card = (title, body, aside = '') => `<section class="card"><div class="card-head"><h3>${esc(title)}</h3>${aside ? `<span>${esc(aside)}</span>` : ''}</div>${body}</section>`;
const metric = (label, value, detail, accent = false) => `<article class="card metric"><span class="label">${esc(label)}</span><strong${accent ? ' class="accent"' : ''}>${esc(value)}</strong><span class="sub">${esc(detail)}</span></article>`;
const table = (headers, rows, message = 'No records yet.') => rows.length ? `<div class="table-wrap" tabindex="0" role="region" aria-label="${esc(headers.join(', '))}"><table><thead><tr>${headers.map(h=>`<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : empty(message);
const errorSlot = '<p class="form-error" role="alert"></p>';
const input = (name, label, value = '', options = '') => `<label>${label}<input name="${name}" value="${esc(value)}" ${options}></label>`;
const option = (value, label, selected) => `<option value="${esc(value)}"${value === selected ? ' selected' : ''}>${esc(label)}</option>`;
const selector = (name, label, items, selected = '') => `<label>${label}<select name="${name}">${items.map(x=>option(x.value ?? x, x.label ?? x, selected)).join('')}</select></label>`;
const permission = name => Boolean(state.user?.permissions.includes(name));
const restriction = text => `<p class="permission-note">${esc(text)}</p>`;
const state = {user:null, csrf:null, route:'overview', id:null, version:0, setup:false, filters:{service_id:'svc-worker',level:'',query:'',limit:'25'}};
const titles = {overview:'Command center',services:'Services',incidents:'Incidents',capacity:'Capacity & workloads',logs:'Log intelligence',predict:'Forecasts',notifications:'Notifications',storage:'Object storage',costs:'Cost & service levels',security:'Identity & security',network:'Connections',recovery:'Backup & recovery',audit:'Audit trail',settings:'Workspace'};
let toastTimer;

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').className = 'show' + (error ? ' error' : '');
  toastTimer = setTimeout(()=>$('#toast').className='', 6500);
}

async function api(path, method = 'GET', body, options = {}) {
  const headers = {};
  if (state.csrf) headers['X-CSRF-Token'] = state.csrf;
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {method, headers, credentials:'same-origin', signal:AbortSignal.timeout(options.timeout || 60000), body:body instanceof FormData ? body : body ? JSON.stringify(body) : undefined});
  const raw = await response.text();
  let result;
  try { result = raw ? JSON.parse(raw) : {}; } catch { throw new Error(`The server could not complete the request (${response.status}).`); }
  if (!response.ok) {
    if (response.status === 401 && state.user && !path.startsWith('/api/auth/')) showLogin('Your session expired. Please sign in again.');
    throw new Error(typeof result.detail === 'string' ? result.detail : `Request failed (${response.status}).`);
  }
  return result;
}

function showLogin(message = '') {
  state.user = null; state.csrf = null; state.version++;
  $('#dialog').close(); $('#app').hidden = true; $('#login').hidden = false;
  $('#login-form').hidden = state.setup; $('#setup-form').hidden = !state.setup;
  $('#boot-message').textContent = message;
}

function enter(user, csrf) {
  state.user = user; state.csrf = csrf;
  $('#login').hidden = true; $('#app').hidden = false;
  $('#account-name').textContent = user.name;
  $('#account-role').textContent = user.role;
  $('#avatar').textContent = user.name.split(/\s+/).map(x=>x[0]).slice(0,2).join('').toUpperCase();
  navigate();
}

async function boot() {
  try {
    const data = await api('/api/auth/setup-status');
    state.setup = data.required;
    if (state.setup) { showLogin('Choose a unique administrator password. No default account is created.'); return; }
    try { const session = await api('/api/auth/me'); enter(session.user, session.csrf); }
    catch { showLogin(); }
  } catch(error) { $('#boot-message').textContent = 'Unable to connect: ' + error.message; }
}

function routeParts() {
  const parts = location.hash.slice(1).split('/');
  return {page:titles[parts[0]] ? parts[0] : 'overview', id:parts[1] ? decodeURIComponent(parts[1]) : null};
}

function closeNavigation() {
  $('#sidebar').classList.remove('open'); $('#mobile-shade').hidden = true;
  $('#mobile-menu').setAttribute('aria-expanded','false');
}

async function navigate(quiet = false) {
  if (!state.user) return;
  const {page,id} = routeParts();
  state.route = page; state.id = id;
  const version = ++state.version;
  $('#page-title').textContent = titles[page]; document.title = titles[page] + ' · Helio Operations';
  document.querySelectorAll('nav a').forEach(a=>{a.classList.toggle('active',a.dataset.page===page); if(a.dataset.page===page)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  closeNavigation();
  if (!quiet) { $('#content').innerHTML = empty('Loading measured operations…'); $('#content').setAttribute('aria-busy','true'); }
  try {
    const html = await pages[page](id);
    if (version !== state.version || !state.user) return;
    $('#content').innerHTML = html; $('#content').setAttribute('aria-busy','false');
    $('#freshness').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(error) {
    if(version !== state.version)return;
    $('#content').innerHTML = `<div class="notice error" role="alert">${esc(error.message)}</div>${button('Try again','refresh')}`;
    $('#content').setAttribute('aria-busy','false');
  }
}

function chart(points, unit, forecastPoints = []) {
  const actual = points.slice(-100), future = forecastPoints;
  if (!actual.length) return empty('No measurements in this time range. Generate traffic or ingest metrics to populate this chart.');
  const all = [...actual,...future], left = 60, top = 20, width = 620, height = 195;
  const minTime = Math.min(...all.map(p=>p.occurred)), maxTime = Math.max(...all.map(p=>p.occurred));
  const maxValue = Math.max(1,...all.map(p=>p.value)) * 1.12;
  const x = p => left + (p.occurred - minTime) / Math.max(maxTime - minTime,1) * width;
  const y = p => top + height - p.value / maxValue * height;
  const line = series => series.map(p=>`${x(p).toFixed(1)},${y(p).toFixed(1)}`).join(' ');
  const ticks = [0,.5,1].map(t=>`<line class="gridline" x1="${left}" y1="${top+height*(1-t)}" x2="${left+width}" y2="${top+height*(1-t)}"/><text class="axis" text-anchor="end" x="50" y="${top+height*(1-t)+4}">${esc(number(maxValue*t,1))}</text>`).join('');
  return `<div class="chart"><div class="chart-caption"><span>${esc(unit)} · measured observations</span>${future.length?'<span>Orange dashed: forecast</span>':''}</div><svg viewBox="0 0 700 258" role="img" aria-label="${esc(unit)} over time. Exact values are available in the data table below.">${ticks}<polyline class="line" points="${line(actual)}"/>${future.length?`<polyline class="forecast-line" points="${line(future)}"/>`:''}${actual.map(p=>`<circle cx="${x(p)}" cy="${y(p)}" r="2.5" tabindex="0" aria-label="${esc(date(p.occurred)+': '+number(p.value,3)+' '+unit)}"><title>${esc(date(p.occurred)+': '+number(p.value,3)+' '+unit)}</title></circle>`).join('')}<text class="axis" x="60" y="242">${esc(new Date(minTime*1000).toLocaleTimeString())}</text><text class="axis" text-anchor="end" x="680" y="242">${esc(new Date(maxTime*1000).toLocaleTimeString())}</text></svg><details><summary>Show exact observations (${actual.length})</summary>${table(['Time',unit],actual.map(p=>[esc(date(p.occurred)),esc(number(p.value,3))]))}</details></div>`;
}

function serviceRows(services) {
  return services.map(s=>[`<a href="#services/${encodeURIComponent(s.id)}"><b>${esc(s.name)}</b></a><small>${esc(s.environment)} · ${esc(s.source)}</small>`,badge(s.status),number(s.latency)+' ms',s.success_percent===null?'Awaiting requests':number(s.success_percent,2)+'%',number(s.request_count,0),esc(date(s.last_seen))]);
}

function incidentRows(incidents) {
  return incidents.map(i=>[`<a href="#incidents/${encodeURIComponent(i.id)}"><b>${esc(i.title)}</b></a><small>${esc(i.service || i.service_id)}</small>`,badge(i.severity),badge(i.status),esc(i.owner_name || 'Unassigned'),esc(date(i.opened)),i.status==='open'&&permission('operate')?button('Acknowledge','ack',i.id):`<a href="#incidents/${encodeURIComponent(i.id)}">Open record →</a>`]);
}

async function overview() {
  const [d,m] = await Promise.all([api('/api/overview'),api('/api/v1/metrics/query?service_id=svc-worker&metric_name=request_rate')]);
  const healthy = d.services.filter(s=>s.status==='healthy').length;
  $('#runtime-label').textContent = d.runtime.backend + ' runtime';
  $('#system-status').textContent = d.active_incidents ? `${d.active_incidents} active incident${d.active_incidents===1?'':'s'}` : d.services.some(s=>s.status==='degraded')?'Service degradation detected':'No active incidents';
  return intro(`Welcome, ${state.user.name.split(' ')[0]}.`,'Your workloads, measured activity and incident history in one place.',`<a class="primary" href="#capacity">Run a workload test →</a>`)
    + (!d.runtime.connected?`<div class="notice warn">Workload runtime unavailable: ${esc(d.runtime.error || 'No ready replicas. Open Capacity to inspect the connection.')}</div>`:'')
    + (d.collector.error?`<div class="notice error">Collector needs attention: ${esc(d.collector.error)}</div>`:'')
    + `<div class="metric-grid">${metric('OBSERVED HEALTH',`${healthy} / ${d.services.length}`,'Services currently healthy',true)}${metric('ACTIVE INCIDENTS',d.active_incidents,`${d.open_alerts} awaiting acknowledgement`)}${metric('READY REPLICAS',d.runtime.observed,`${d.runtime.desired} requested · ${d.runtime.backend}`)}${metric('ACKNOWLEDGEMENT',d.median_ack_seconds===null?'—':number(d.median_ack_seconds)+'s','Median time across recorded incidents')}</div>`
    + `<div class="grid">${card('Workload request throughput',chart(m.points,'requests / second'),'LAST 15 MINUTES')}${card('Collection status',`<div class="card-pad"><span class="tag">MEASURED DATA</span><h3 style="margin-top:20px">${d.runtime.connected?'Workload runtime connected':'Connection required'}</h3><p>Last successful collection: <b>${esc(date(d.collector.last_success))}</b></p><p class="muted">Metrics are collected every two seconds. New services remain unknown until telemetry arrives. Acknowledging an incident assigns responsibility; resolution requires recovery evidence.</p><a href="#network">Inspect connections →</a></div>`)}</div>`
    + `<div class="stack">${card('Service health',table(['Service','Status','P95 latency','Request success','Requests','Last signal'],serviceRows(d.services)))}${card('Active incidents',table(['Incident','Severity','Status','Owner','Opened','Action'],incidentRows(d.incidents),'No active incidents. Alerts are generated from measured threshold breaches.'))}</div>`;
}

async function services(id) {
  if (id) {
    const [d,m] = await Promise.all([api('/api/services/'+encodeURIComponent(id)),api('/api/v1/metrics/query?service_id='+encodeURIComponent(id)+'&metric_name=latency_p95_ms')]);
    const s = d.service;
    return intro(s.name,`${s.environment} · ${s.owner_team} · ${s.namespace}`,`<a class="quiet-btn" href="#services">All services</a><a class="quiet-btn" href="#logs/${encodeURIComponent(id)}">Inspect logs</a>`)
      + `<div class="metric-grid">${metric('STATE',s.status,'Observed telemetry status',true)}${metric('P95 LATENCY',number(s.latency)+' ms','Measured requests · last 5 minutes')}${metric('REQUEST SUCCESS',s.success_percent===null?'—':number(s.success_percent,2)+'%','Successful requests / all requests')}${metric('WITHIN LATENCY SLO',s.slo_good_percent===null?'—':number(s.slo_good_percent,2)+'%','Successful requests below threshold')}</div>`
      + `<div class="stack">${card('Latency observations',chart(m.points,'milliseconds'))}${card('Service incidents',table(['Incident','Severity','Status','Owner','Opened','Action'],incidentRows(d.incidents)))}${card('Telemetry connection',`<div class="card-pad"><p>Service identifier: <code>${esc(s.id)}</code></p><p class="muted">Agents submit authenticated metrics to <code>/api/v1/metrics/ingest</code> and logs to <code>/api/logs/ingest</code>. Submitted observations feed charts and alert rules.</p>${permission('ingest')?`<form data-form="metric" data-id="${esc(id)}" class="form-grid">${selector('metric_name','Metric',d.metrics)}${input('value','Measured value','', 'type="number" min="0" step="any" required')}<button class="primary" type="submit">Submit observation</button>${errorSlot}</form>`:restriction('An operator can connect a telemetry agent.')}</div>`)}</div>`;
  }
  const d = await api('/api/services');
  return intro('Service catalog','Register workloads, inspect their evidence, and see when their signals were last observed.',permission('operate')?button('Register service','register','',true):'')
    + card('Registered workloads',table(['Service','Status','P95 latency','Request success','Requests / 5m','Last signal'],serviceRows(d.services)))
    + '<p class="permission-note">Registration does not imply availability. Unknown services have not supplied telemetry; stale services have not supplied a recent signal.</p>';
}

async function incidents(id) {
  if(id) {
    const d = await api('/api/alerts/'+encodeURIComponent(id)), i = d.incident, evidence = JSON.parse(i.evidence);
    const people = permission('operate')?(await api('/api/users')).users.filter(u=>u.active&&u.role!=='viewer'):[];
    const allowed = {open:['open','acknowledged'],acknowledged:['acknowledged','investigating'],investigating:['investigating','resolved'],resolved:[]}[i.status];
    return intro(i.title,'Assign responsibility, record the investigation, and resolve only after a fresh measurement confirms recovery.',`<a class="quiet-btn" href="#incidents">All incidents</a><a class="quiet-btn" href="#services/${encodeURIComponent(i.service_id)}">Affected service</a>`)
      + `<div class="grid">${card('Incident record',`<div class="card-pad"><div class="actions">${badge(i.status)}${badge(i.severity)}${badge(i.condition_active?'condition active':'recovery observed')}</div><p style="margin-top:18px">Opened ${esc(date(i.opened))}</p><p><b>${esc(evidence.metric)}</b>: ${number(evidence.value,3)} · threshold ${number(evidence.threshold,3)}</p><p class="muted">Evidence collected ${esc(date(evidence.collected_at))}</p>${i.resolution?`<p>Resolution: ${esc(i.resolution)}</p>`:''}${permission('operate')&&allowed.length?`<form data-form="incident" data-id="${esc(id)}">${selector('status','Status',allowed,i.status)}${selector('owner_id','Assigned operator',people.map(u=>({value:u.id,label:u.name+' · '+u.role})),i.owner_id||state.user.id)}<label>Investigation or resolution note<textarea name="note" required minlength="3" maxlength="2000"></textarea></label><button class="primary" type="submit">Save incident update</button>${errorSlot}</form>`:''}</div>`)}${card('Activity history',`<div class="card-pad"><ol class="timeline">${d.events.map(e=>`<li><b>${esc(e.kind.replaceAll('_',' '))}</b><small>${esc(e.actor)} · ${esc(date(e.created_at))}</small><p>${esc(e.note)}</p></li>`).join('')}</ol></div>`)}</div>`;
  }
  const d = await api('/api/alerts?status=all');
  return intro('Incident management','Open → Acknowledged → Investigating → Resolved. Each incident keeps one continuous activity record.')+card('Incident register',table(['Incident','Severity','Status','Owner','Opened','Action'],incidentRows(d.alerts),'No incidents recorded. Run a load test or ingest a threshold breach.'));
}

function autoscalePanel(policy) {
  if(!policy)return '';
  return card('Scaling & recovery policy',`<div class="card-pad"><p class="muted">Scaling uses measured request rate, consecutive evaluations and a cooldown. Recovery replaces workers after three failed health checks. Scale-down waits for active workload tests to finish.</p>${permission('admin')?`<form data-form="autoscale" class="form-grid"><label><input name="enabled" type="checkbox"${policy.enabled?' checked':''}> Enable automatic scaling</label><label><input name="self_heal" type="checkbox"${policy.self_heal?' checked':''}> Replace unhealthy workers</label>${input('min_replicas','Minimum replicas',policy.min_replicas,'type="number" min="1" max="8" required')}${input('max_replicas','Maximum replicas',policy.max_replicas,'type="number" min="1" max="8" required')}${input('target_rps','Target requests / second / replica',policy.target_rps,'type="number" min="0.1" max="1000" step="0.1" required')}${input('cooldown_seconds','Cooldown (seconds)',policy.cooldown_seconds,'type="number" min="5" max="600" required')}${input('consecutive_samples','Consecutive evaluations',policy.consecutive_samples,'type="number" min="2" max="10" required')}<button class="primary" type="submit">Save automation policy</button>${errorSlot}</form>`:`<p>Autoscaling: ${policy.enabled?'enabled':'disabled'} · Recovery: ${policy.self_heal?'enabled':'disabled'} · Bounds: ${policy.min_replicas}–${policy.max_replicas}</p>`}</div>`)+'<div style="height:20px"></div>';
}

function maintenancePanel(data) {
  return '<div style="height:20px"></div>'+card('Maintenance evidence',`<div class="card-pad"><p><b>Current workload health:</b> ${esc(data.current_health)}</p><p class="muted">${esc(data.reason)}</p><div class="actions">${badge(data.model_status)}<span>${data.labelled_samples} labelled observations · ${data.failure_samples} observed failures</span></div><p class="permission-note">Health checks and recovery are active independently of model training. No failure probability is fabricated.</p>${permission('operate')?`<form data-form="maintenance-outcome">${selector('observation_id','Observation to label',data.observations.map(o=>({value:o.id,label:date(o.occurred)+' · '+o.id.slice(-8)})))}${selector('failed','Failure within the next five minutes',[{value:'false',label:'No failure observed'},{value:'true',label:'Failure observed'}])}<label>Supporting evidence<textarea name="evidence" minlength="10" maxlength="2000" required></textarea></label><button class="quiet-btn" type="submit">Record observed outcome</button>${errorSlot}</form>`:''}</div>`);
}

async function capacity() {
  const [d,runs] = await Promise.all([api('/api/kubernetes'),api('/api/workload/runs')]);
  const r = d.runtime;
  return intro('Capacity & workloads','Scaling changes the connected runtime. Requested capacity and observed readiness are shown separately.')
    + (r.error?`<div class="notice error">${esc(r.error)}</div>`:'')
    + `<div class="metric-grid">${metric('RUNTIME',r.backend,r.connected?'Connected':'Connection required',true)}${metric('REQUESTED',r.desired,'Desired replicas')}${metric('OBSERVED',r.observed,'Ready workload replicas')}${metric('MANAGED WORKLOAD',r.deployment,r.namespace)}</div>`
    + `<div class="grid equal">${card('Scale the workload',`<div class="card-pad"><p class="muted">Changes are audited under your account. Allowed range: ${d.limits.min}–${d.limits.max}. Finish active load tests before scaling.</p>${permission('operate')?`<form data-form="scale" data-id="${esc(r.deployment)}">${input('replicas','Desired replicas',r.desired,`type="number" min="${d.limits.min}" max="${d.limits.max}" step="1" required`)}<button class="primary" type="submit">Apply and verify scaling</button>${errorSlot}</form>`:restriction('Your viewer role can inspect capacity but cannot change it.')}</div>`)}${card('Run a measured workload',`<div class="card-pad"><p class="muted">Sends real HTTP requests to the managed CPU workload. Compare identical tests before and after a capacity change.</p>${permission('operate')?`<form data-form="load" class="form-grid">${input('total','Requests',100,'type="number" min="10" max="500" required')}${input('concurrency','Concurrent requests',8,'type="number" min="1" max="24" required')}${input('work_ms','CPU work per request (ms)',50,'type="number" min="1" max="300" required')}<button class="primary" type="submit">Start workload test</button>${errorSlot}</form>`:restriction('An operator can run a workload test.')}</div>`)}</div>`
    + autoscalePanel(d.autoscale)
    + `<div class="stack">${card('Workload test results',table(['Started','State','Requests','Errors','P95 latency','Throughput'],runs.runs.map(r=>[esc(date(r.started)),badge(r.status),`${r.completed} / ${r.total}`,number(r.errors,0),number(r.p95)+' ms',number(r.rps,2)+' req/s']),'No workload tests yet.'))}${card('Scaling history',table(['Time','Actor','Runtime','Previous → desired','Observed','Result'],d.actions.map(a=>[esc(date(a.created_at)),esc(a.actor),esc(a.backend),`${a.previous} → ${a.desired}`,number(a.observed,0),badge(a.status)+(a.error?`<small>${esc(a.error)}</small>`:'')])))}${card('Connected replicas',table(['Replica','Endpoint'],r.workers.map(w=>[esc(w.id.slice(0,16)),esc(w.endpoint)]),r.backend==='kubernetes'?'Readiness is read from the configured Kubernetes deployment.':'No connected replicas.'))}</div>`;
}

async function logs(id) {
  if(id) state.filters.service_id = id;
  const f = state.filters;
  const params = new URLSearchParams(Object.fromEntries(Object.entries(f).filter(([,v])=>v)));
  const [d,s] = await Promise.all([api('/api/logs?'+params),api('/api/services')]);
  return intro('Log intelligence','Search persisted operational evidence. Diagnosis uses explicit rules and links to the observations behind each finding.')
    + `<form data-form="log-filters" class="filters">${selector('service_id','Service',s.services.map(x=>({value:x.id,label:x.name})),f.service_id)}${selector('level','Level',[{value:'',label:'All levels'},'INFO','WARN','ERROR'],f.level)}${input('query','Message contains',f.query,'maxlength="200"')}${selector('limit','Latest records',['25','100','500'],f.limit)}<button class="quiet-btn" type="submit">Filter logs</button>${permission('operate')?button('Analyze evidence','analyze',f.service_id):''}</form>`
    + `<div class="grid">${card('Persisted log stream',d.entries.length?d.entries.map(l=>`<article class="list-row"><div class="actions">${badge(l.level.toLowerCase())}<small>${esc(date(l.occurred))}</small></div><b>${esc(l.service)}</b><p class="log-message">${esc(l.message)}</p><span class="log-id">${esc(l.id)}${l.trace_id?' · '+esc(l.trace_id):''}</span></article>`).join(''):empty('No matching logs. Generate a workload or submit a log below.'))}${card('Submit an operational log',`<div class="card-pad"><p class="muted">Use this form for a collected log message. The source is recorded as your authenticated account. Known email and credential patterns are redacted.</p>${permission('ingest')?`<form data-form="log-ingest">${selector('service_id','Service',s.services.map(x=>({value:x.id,label:x.name})),f.service_id)}${selector('level','Level',['INFO','WARN','ERROR'])}<label>Log message<textarea name="message" required maxlength="10000"></textarea></label><button class="primary" type="submit">Store log</button>${errorSlot}</form>`:restriction('An operator can submit logs.')}</div>`)}</div>`;
}

async function predict() {
  const [d,maintenance] = await Promise.all([api('/api/predictions'),api('/api/maintenance')]);
  const evidence=maintenancePanel(maintenance);
  if(d.status!=='ready')return intro('Traffic forecasting','A fitted model using collected request-rate history, evaluated against actual subsequent observations.')+`<div class="notice">${esc(d.message)} ${d.required?`${d.samples} / ${d.required} observations available.`:''}</div>`+card('Collected history',chart(d.points,'requests / second'))+evidence;
  return intro('Traffic forecasting',`${d.model} · ${d.samples} measured observations · next ${d.horizon_seconds} seconds.`)
    + `<div class="metric-grid">${metric('FORECAST',number(d.expected,2)+' req/s','Fitted trend projection',true)}${metric('MODEL MAE',number(d.mae,3),'Walk-forward absolute error · req/s')}${metric('BASELINE MAE',number(d.baseline_mae,3),'Previous-observation baseline · req/s')}${metric('MAPE',d.mape===null?'Unavailable':number(d.mape,2)+'%',`${d.mape_samples} non-zero actual observations`)}</div>`
    + card('Measured history and future projection',chart(d.points,'requests / second',d.forecast))
    + `<div class="notice" style="margin-top:20px">${esc(d.validation)}. ${esc(d.band_description)} Forecasts do not automatically execute scaling.</div>`
    + card('Evaluation evidence',table(['Observed time','Actual req/s','Predicted req/s','Baseline req/s'],d.evaluation.map(p=>[esc(date(p.time)),number(p.actual,3),number(p.predicted,3),number(p.baseline,3)])))+evidence;
}

async function notifications() {
  const [d,a] = await Promise.all([api('/api/v1/notifications'),api('/api/alerts?status=all')]);
  const configured = d.channels.filter(c=>c.configured);
  return intro('Notification delivery','Workspace inbox entries are stored immediately. External channels are marked delivered only after the receiver accepts the request.')
    + `<div class="grid">${card('Delivery connections',d.channels.map(c=>`<div class="list-row"><div class="actions"><b>${esc(c.name)}</b>${badge(c.configured?'configured':'disabled')}</div><p>${esc(c.description)}</p></div>`).join(''))}${card('Send an incident notification',`<div class="card-pad">${permission('operate')&&a.alerts.length?`<form data-form="notify">${selector('incident_id','Incident',a.alerts.map(x=>({value:x.id,label:x.title})))}${selector('channel','Channel',configured.map(x=>x.name))}<button class="primary" type="submit">Request delivery</button>${errorSlot}</form><p class="permission-note">Duplicate requests for the same incident and channel reuse its existing delivery record.</p>`:empty('An incident must exist before it can be sent.')}</div>`)}</div>`
    + card('Delivery history',table(['Incident','Channel','Status','Attempts','Time','Action'],d.notifications.map(n=>[`<a href="#incidents/${encodeURIComponent(n.incident_id)}">${esc(n.title)}</a>`,esc(n.channel),badge(n.status)+(n.error?`<small>${esc(n.error)}</small>`:''),number(n.attempts,0),esc(date(n.delivered_at||n.created_at)),n.status==='failed'&&permission('operate')?button('Retry delivery','retry-notice',n.id):!n.read_at?button('Mark read','read-notice',n.id):'Read'])));
}

async function storage() {
  const d = await api('/api/cloud/storage');
  return intro('Encrypted object storage','Upload real files, preserve versions, verify downloaded bytes, and enforce per-object retention.')
    + `<div class="notice">${esc(d.backend)} · ${esc(d.encryption)} authenticated encryption. Maximum upload: 10 MB. Encryption covers file contents; database metadata is not encrypted.</div>`
    + (permission('storage')?card('Upload a file',`<div class="card-pad"><form data-form="upload" class="form-grid"><label>File<input name="file" type="file" required></label>${input('retention_days','Minimum retention (days)',0,'type="number" min="0" max="3650" required')}<button class="primary" type="submit">Upload and encrypt</button>${errorSlot}</form></div>`):'')
    + `<div style="margin-top:20px">${card('Stored versions',table(['File','Version','Size','Retention until','SHA-256','Actions'],d.objects.map(o=>[`<b>${esc(o.name)}</b><small>${esc(date(o.created_at))}</small>`,number(o.version,0),number(o.size/1024,2)+' KB',esc(date(o.retention_until)),`<code title="${esc(o.checksum)}">${esc(o.checksum.slice(0,16))}…</code>`,`<div class="actions"><a class="quiet-btn small-btn" href="/api/cloud/storage/${encodeURIComponent(o.id)}/download">Download</a>${permission('storage')?button('Delete','delete-object',o.id):''}</div>`]),'No files uploaded yet.'))}</div>`;
}

async function costs() {
  const [d,s] = await Promise.all([api('/api/cloud/costs'),api('/api/services')]);
  const r = d.rates;
  return intro('Cost & service levels','Transparent cost assumptions and request-based service performance.')
    + `<div class="notice">${esc(d.mode)}. ${esc(d.basis)}</div><div class="metric-grid">${metric('ESTIMATED TOTAL',d.currency+' '+number(d.total,6),'Sum of all displayed line items',true)}${metric('PLANNING BUDGET',d.currency+' '+number(d.budget,2),'Administrator-configured budget')}${metric('SERVICES OBSERVED',s.services.filter(x=>x.request_count>0).length,'With requests in the last five minutes')}${metric('DATA BASIS','Measured','Capacity and stored byte counts')}</div>`
    + card('Cost calculation',table(['Resource','Quantity','Rate','Cost'],d.line_items.map(x=>[esc(x.name),number(x.quantity,9)+' '+esc(x.unit),number(x.rate,6),number(x.cost,6)+' '+esc(d.currency)])))
    + `<div class="grid equal" style="margin-top:20px">${card('Request service levels',table(['Service','Requests / 5m','Success','Within latency objective'],s.services.map(x=>[esc(x.name),number(x.request_count,0),x.success_percent===null?'—':number(x.success_percent,2)+'%',x.slo_good_percent===null?'—':number(x.slo_good_percent,2)+'%'])))}${card('Rate assumptions',`<div class="card-pad">${permission('admin')?`<form data-form="costs" class="form-grid">${input('worker_hour','Cost / replica-hour',r.worker_hour,'type="number" min="0" max="1000" step="any" required')}${input('storage_gb_month','Object cost / GB-month',r.storage_gb_month,'type="number" min="0" max="1000" step="any" required')}${input('database_gb_month','Database cost / GB-month',r.database_gb_month,'type="number" min="0" max="1000" step="any" required')}${input('budget','Monthly planning budget',r.budget,'type="number" min="0.01" step="any" required')}${selector('currency','Currency',['USD','INR'],r.currency)}<button class="primary" type="submit">Save rates</button>${errorSlot}</form>`:restriction('An administrator can update pricing assumptions.')}</div>`)}</div>`;
}

async function security() {
  const d = await api('/api/cloud/iam');
  const users = permission('operate')?(await api('/api/users')).users:[];
  return intro('Identity & security','Access is enforced by the API. Every control below reports its actual configuration.',permission('admin')?button('Create user','create-user','',true):'')
    + `<div class="grid equal">${card('Security controls',d.controls.map(c=>`<div class="list-row"><b>${esc(c.name)}</b><p>${esc(c.status)}</p></div>`).join(''))}${card('Your authentication',`<div class="card-pad"><h3>${esc(state.user.name)}</h3><p>${esc(state.user.email)} · ${esc(state.user.role)}</p><p>MFA: <b>${state.user.mfa_enabled?'Enabled':'Not enrolled'}</b></p><div class="actions">${!state.user.mfa_enabled?button('Enroll authenticator','mfa','',true):''}${button('Change password','password')}</div>${permission('admin')?`<form data-form="security-policy" style="margin-top:25px"><label><input name="require_privileged_mfa" type="checkbox"${d.policy.require_privileged_mfa?' checked':''}> Require MFA for privileged actions</label><button class="quiet-btn" type="submit">Save security policy</button>${errorSlot}</form>`:''}</div>`)}</div>`
    + card('Role membership',table(['Role','Active members','MFA enrolled'],d.roles.map(r=>[esc(r.role),number(r.members,0),number(r.mfa_members,0)])))
    + (users.length?`<div style="margin-top:20px">${card('Workspace users',table(['Name','Email','Role','Status','MFA','Action'],users.map(u=>[esc(u.name),esc(u.email),esc(u.role),badge(u.active?'active':'disabled'),u.mfa_enabled?'Enabled':'Not enrolled',permission('admin')?button('Edit access','edit-user',u.id):'—'])))}</div>`:'');
}

async function network() {
  const d = await api('/api/cloud/network');
  const failures=permission('admin')?(await api('/api/events/failures')).events:[];
  return intro('Runtime connections','A map of the connections used by this installation.')
    + `<div class="notice">${esc(d.note)}</div>`
    + card('Service readiness & event queues',table(['Service','Readiness','Pending events','Failed events','Last publish'],d.services.map(s=>[esc(s.service),badge(s.status),number(s.outbox_pending,0),number(s.dead_letters,0),esc(date(s.last_published))])))
    + (failures.length?card('Events needing attention',table(['Service','Topic','Failure','Action'],failures.map(e=>[esc(e.service),esc(e.topic),esc(e.error),button('Retry event','retry-event',e.service+'/'+e.id)]))):'')
    + card('Observed architecture',`<div class="card-pad stack">${d.flows.map(f=>`<div class="flow"><b>${esc(f.from)}</b><span aria-hidden="true">→</span><b>${esc(f.to)}</b><span>${esc(f.protocol)}</span>${badge(f.status)}</div>`).join('')}</div>`)
    + `<div class="grid equal" style="margin-top:20px">${card('Runtime scope',`<div class="card-pad"><p>Backend: <b>${esc(d.backend)}</b></p><p>Managed namespace: <b>${esc(d.runtime.namespace)}</b></p><p>Managed deployment: <b>${esc(d.runtime.deployment)}</b></p><p>Gateway transport: <b>${esc(d.transport)}</b></p></div>`)}${card('Connection evidence',`<div class="card-pad"><p>${d.runtime.connected?'The runtime is connected.':'The runtime is unavailable.'}</p><p class="muted">${esc(d.runtime.error||'Workload readiness and scaling results are returned by the selected runtime adapter.')}</p><a href="#capacity">Inspect managed replicas →</a></div>`)}</div>`;
}

async function recovery() {
  const d = await api('/api/cloud/resilience');
  return intro('Backup & recovery','Create encrypted snapshots and prove that their database and file contents can be restored.',permission('admin')?button('Create backup','backup','',true):'')
    + `<div class="notice">${esc(d.method)}. Schedule: ${esc(d.schedule)}. Keep the encryption key separately backed up; an archive cannot be recovered without it.</div>`
    + card('Backup inventory',table(['Created','Size','Restore verification','Measured duration','Actions'],d.backups.map(b=>[esc(date(b.created_at)),number(b.size/1024,2)+' KB',b.verified_at?esc(date(b.verified_at)):'Not yet verified',b.verify_seconds===null?'—':number(b.verify_seconds,3)+' s',permission('admin')?`<div class="actions">${button('Verify restore','verify-backup',b.id)}${button('Restore workspace','restore-backup',b.id)}</div>`:'Administrator required']),'No backups exist yet.'))
    + `<div class="grid equal" style="margin-top:20px">${card('Recovery point',`<div class="card-pad"><p>${esc(d.rpo)}</p></div>`)}${card('Recovery time',`<div class="card-pad"><p>${esc(d.rto)}</p></div>`)}</div>`;
}

async function audit() {
  if(!permission('audit'))return intro('Audit trail','Access to operational audit records is restricted.')+restriction('Your viewer role cannot read audit records. Ask an administrator for operator access.');
  const d = await api('/api/v1/audit');
  return intro('Audit trail','Authenticated actors, recorded changes, and verifiable outcomes. Latest 200 events.')+card('Activity register',table(['Time','Actor','Action','Resource','Detail'],d.events.map(e=>[esc(date(e.created_at)),esc(e.actor),esc(e.action),`<span class="break">${esc(e.resource)}</span>`,`<details><summary>Details</summary><pre class="code">${esc(JSON.stringify(JSON.parse(e.detail),null,2))}</pre></details>`])));
}

async function settings() {
  const d = await api('/api/settings'), t = d.thresholds;
  return intro('Workspace configuration','Thresholds, retention, and the policy governing operations.')
    + `<div class="grid equal">${card('Alert thresholds',`<div class="card-pad"><p class="muted">The collector evaluates a rolling window every two seconds. Consecutive breaches create one incident per service and rule.</p>${permission('admin')?`<form data-form="thresholds" class="form-grid">${input('latency_ms','P95 latency threshold (ms)',t.latency_ms,'type="number" min="1" max="30000" step="any" required')}${input('error_percent','Error threshold (%)',t.error_percent,'type="number" min="0" max="100" step="any" required')}${input('consecutive_windows','Consecutive collections',t.consecutive_windows,'type="number" min="1" max="10" required')}${input('window_seconds','Rolling window (seconds)',t.window_seconds,'type="number" min="10" max="300" required')}<button class="primary" type="submit">Save thresholds</button>${errorSlot}</form>`:`<p>Latency: ${t.latency_ms} ms · Errors: ${t.error_percent}%</p>${restriction('Only administrators can change alert policies.')}`}</div>`)}${card('Data retention',`<div class="card-pad"><p><b>Telemetry:</b> ${d.retention.telemetry_days} days; background cleanup removes expired samples.</p><p><b>Audit records:</b> ${esc(d.retention.audit)}</p><p><b>Objects:</b> ${esc(d.retention.objects)}</p><p><b>Session expiry:</b> 30 minutes idle; eight hours maximum.</p><a href="#recovery">Create a recoverable snapshot →</a></div>`)}</div>`;
}

const pages = {overview,services,incidents,capacity,logs,predict,notifications,storage,costs,security,network,recovery,audit,settings};

function dialog(title, html) {
  const element = $('#dialog');
  if(element.open)element.close();
  $('#dialog-title').textContent = title; $('#dialog-body').innerHTML = html;
  element.showModal();
  setTimeout(()=>$('#dialog-body input:not([type=hidden]), #dialog-body select, #dialog-body button')?.focus(),0);
}

async function searchDialog() {
  dialog('Search services and incidents','<label>Search<input id="search-input" type="search" placeholder="Service name or incident title" autocomplete="off"></label><div id="search-results">Loading inventory…</div>');
  const [s,a] = await Promise.all([api('/api/services'),api('/api/alerts?status=all')]);
  const items = [...s.services.map(x=>({label:x.name,detail:x.status+' · '+x.environment,href:'#services/'+encodeURIComponent(x.id)})),...a.alerts.map(x=>({label:x.title,detail:x.service+' · '+x.status,href:'#incidents/'+encodeURIComponent(x.id)}))];
  const field = $('#search-input'); if(!field)return;
  const update=()=>{const value=field.value.toLowerCase();$('#search-results').innerHTML=items.filter(x=>(x.label+' '+x.detail).toLowerCase().includes(value)).map(x=>`<a class="search-result" href="${x.href}"><b>${esc(x.label)}</b><small>${esc(x.detail)}</small></a>`).join('')||empty('No matches.');};
  field.addEventListener('input',update);update();field.focus();
}

async function action(name, id) {
  if(name==='retry-event'){const [service,event]=id.split('/');await api('/api/events/'+encodeURIComponent(service)+'/'+encodeURIComponent(event)+'/retry','POST');toast('Event processed successfully.');await navigate(true);return;}
  if(name==='refresh'){await navigate();return;}
  if(name==='ack'){await api('/api/alerts/'+encodeURIComponent(id)+'/acknowledge','POST');toast('Incident acknowledged and assigned.');await navigate(true);return;}
  if(name==='register'){dialog('Register a service',`<form data-form="register" class="form-grid">${input('name','Service name','','required minlength="3" maxlength="64" pattern="[a-z][a-z0-9-]+" placeholder="payments-api"')}${selector('environment','Environment',['development','staging','production','local'])}${selector('kind','Workload type',['api','worker','cron'])}${input('namespace','Namespace','platform','required minlength="2" maxlength="63"')}${input('owner_team','Owner team','operations','required minlength="2" maxlength="64"')}<button class="primary" type="submit">Register service</button>${errorSlot}</form>`);return;}
  if(name==='create-user'){dialog('Create a workspace user',`<form data-form="create-user">${input('name','Name','','required minlength="2" maxlength="80"')}${input('email','Email','','type="email" required autocomplete="off"')}${input('password','Initial password (12+ characters)','','type="password" required minlength="12" maxlength="256" autocomplete="new-password"')}${selector('role','Role',['viewer','operator','admin'])}<button class="primary" type="submit">Create user</button>${errorSlot}</form>`);return;}
  if(name==='edit-user'){const d=await api('/api/users'),u=d.users.find(x=>x.id===id);dialog('Edit '+u.name,`<form data-form="edit-user" data-id="${esc(id)}">${selector('role','Role',['viewer','operator','admin'],u.role)}<label><input name="active" type="checkbox"${u.active?' checked':''}> Account active</label><p class="muted">Saving revokes this user's current sessions.</p><button class="primary" type="submit">Update access</button>${errorSlot}</form>`);return;}
  if(name==='mfa'){const d=await api('/api/auth/mfa/enroll','POST');dialog('Enroll an authenticator',`<p>Add a time-based account in your authenticator app using this setup key.</p><div class="code">${esc(d.secret)}</div><p class="muted">Issuer: Helio Operations · Account: ${esc(state.user.email)} · 6 digits · 30 seconds.</p><form data-form="mfa-confirm">${input('code','Current authenticator code','','required inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code"')}<button class="primary" type="submit">Verify and enable MFA</button>${errorSlot}</form>`);return;}
  if(name==='password'){dialog('Change your password',`<form data-form="password">${input('current_password','Current password','','type="password" required autocomplete="current-password"')}${input('new_password','New password (12+ characters)','','type="password" minlength="12" maxlength="256" required autocomplete="new-password"')}<button class="primary" type="submit">Change password and sign out</button>${errorSlot}</form>`);return;}
  if(name==='analyze'){const d=await api('/api/logs/analyze','POST',{service_id:id,query:state.filters.query});dialog('Evidence analysis',`<p class="eyelabel">${esc(d.method)} · ${d.sources} evidence records</p><h3>${esc(d.summary)}</h3>${d.findings.map(f=>`<div class="notice"><b>${esc(f.title)}</b><p>${esc(f.recommendation)}</p><small>Supporting logs: ${esc(f.log_ids.join(', ')||'Measured metric evidence')}</small></div>`).join('')}<h3>Supporting observations</h3>${d.logs.map(l=>`<p class="log-message">${esc(l.message)}<span class="log-id">${esc(l.id)}</span></p>`).join('')}${table(['Metric','Value','Collected'],d.metrics.map(m=>[esc(m.name),number(m.value,3),esc(date(m.occurred))]))}<p class="permission-note">${esc(d.redaction)}. Findings are hypotheses for investigation; no confidence percentage is invented.</p>`);return;}
  if(name==='delete-object'){dialog('Delete this object version?',`<p>Deletion is allowed only after its retention period expires.</p><form data-form="delete-object" data-id="${esc(id)}"><button class="danger-btn" type="submit">Delete object version</button>${errorSlot}</form>`);return;}
  if(name==='backup'){await api('/api/backups','POST',undefined,{timeout:180000});toast('Encrypted snapshot created. Verify its restore next.');await navigate(true);return;}
  if(name==='verify-backup'){const d=await api('/api/backups/'+encodeURIComponent(id)+'/verify','POST',undefined,{timeout:180000});toast(`Restore verified in ${d.seconds}s; ${d.restored_rows.objects} objects passed integrity checks.`);await navigate(true);return;}
  if(name==='restore-backup'){dialog('Restore the workspace',`<div class="notice warn">This replaces operational records with the selected snapshot and signs everyone out. A safety backup is created first.</div><p>Backup ID:</p><div class="code">${esc(id)}</div><form data-form="restore" data-id="${esc(id)}">${input('confirmation','Type the backup ID','','required autocomplete="off"')}${input('password','Your administrator password','','type="password" required autocomplete="current-password"')}<button class="danger-btn" type="submit">Restore workspace</button>${errorSlot}</form>`);return;}
  if(name==='read-notice'){await api('/api/notifications/'+encodeURIComponent(id)+'/read','POST');await navigate(true);return;}
  if(name==='retry-notice'){await api('/api/notifications/'+encodeURIComponent(id)+'/retry','POST');toast('Delivery queued for retry.');await navigate(true);}
}

async function submit(form) {
  const kind = form.dataset.form || form.id, values=Object.fromEntries(new FormData(form)), id=form.dataset.id;
  if(kind==='setup-form'){await api('/api/auth/setup','POST',values);state.setup=false;showLogin('Administrator created. Sign in with your new credentials.');$('#login-form [name=email]').value=values.email;return;}
  if(kind==='login-form'){const d=await api('/api/auth/login','POST',values);form.reset();enter(d.user,d.csrf);return;}
  if(kind==='register'){await api('/api/services','POST',values);toast('Service registered; awaiting telemetry.');}
  if(kind==='metric'){values.value=Number(values.value);await api('/api/v1/metrics/ingest','POST',{...values,service_id:id});toast('Observation persisted.');}
  if(kind==='maintenance-outcome'){values.failed=values.failed==='true';await api('/api/maintenance/outcomes','POST',values);toast('Outcome recorded with evidence.');}
  if(kind==='incident'){await api('/api/alerts/'+encodeURIComponent(id),'PATCH',values);toast('Incident updated.');}
  if(kind==='scale'){const d=await api('/api/kubernetes/scale','POST',{deployment:id,replicas:Number(values.replicas)});toast(d.status==='verified'?`${d.observed} replicas verified in ${d.backend}.`:'Scaling requested; waiting for observed readiness.');}
  if(kind==='autoscale'){values.enabled=!!form.elements.enabled.checked;values.self_heal=!!form.elements.self_heal.checked;for(const k of ['min_replicas','max_replicas','target_rps','cooldown_seconds','consecutive_samples'])values[k]=Number(values[k]);await api('/api/kubernetes/autoscale','PUT',values);toast('Autoscaling and recovery policy saved.');}
  if(kind==='load'){Object.keys(values).forEach(k=>values[k]=Number(values[k]));await api('/api/workload/load','POST',values);toast('Real workload test started. Results update below.');}
  if(kind==='log-filters'){state.filters=values;await navigate(true);return;}
  if(kind==='log-ingest'){await api('/api/logs/ingest','POST',values);toast('Log stored with source attribution.');}
  if(kind==='notify'){const d=await api('/api/v1/notifications/send','POST',values);toast('Notification status: '+d.status+'.');}
  if(kind==='upload'){const file=new FormData(form).get('file');if(file.size>10*1024*1024)throw new Error('Choose a file up to 10 MB.');await api('/api/cloud/storage','POST',new FormData(form));toast('File encrypted and stored; checksum recorded.');}
  if(kind==='delete-object'){await api('/api/cloud/storage/'+encodeURIComponent(id),'DELETE');toast('Object version deleted.');}
  if(kind==='costs'){for(const key of ['worker_hour','storage_gb_month','database_gb_month','budget'])values[key]=Number(values[key]);await api('/api/settings/costs','PUT',values);toast('Cost assumptions saved.');}
  if(kind==='create-user'){await api('/api/users','POST',values);toast('User created with enforced role permissions.');}
  if(kind==='edit-user'){values.active=!!form.elements.active.checked;await api('/api/users/'+encodeURIComponent(id),'PATCH',values);if(id===state.user.id){showLogin('Your access was updated. Sign in again.');return;}toast('Access updated and sessions revoked.');}
  if(kind==='security-policy'){await api('/api/settings/security','PUT',{require_privileged_mfa:form.elements.require_privileged_mfa.checked});toast('Security policy saved.');}
  if(kind==='mfa-confirm'){await api('/api/auth/mfa/confirm','POST',values);state.user.mfa_enabled=true;toast('Authenticator verified; MFA is enabled.');}
  if(kind==='password'){await api('/api/auth/password','POST',values);showLogin('Password changed. Sign in again.');return;}
  if(kind==='thresholds'){Object.keys(values).forEach(k=>values[k]=Number(values[k]));await api('/api/settings/thresholds','PUT',values);toast('Alert thresholds saved.');}
  if(kind==='restore'){await api('/api/backups/'+encodeURIComponent(id)+'/restore','POST',values,{timeout:180000});showLogin('Workspace restored. Sign in again.');return;}
  $('#dialog').close();await navigate(true);
}

document.addEventListener('submit',async event=>{
  const form=event.target;if(!(form instanceof HTMLFormElement))return;
  event.preventDefault();if(form.dataset.busy)return;
  form.dataset.busy='true';const error=$('.form-error',form);if(error)error.textContent='';
  const buttons=[...form.querySelectorAll('button[type=submit]')];buttons.forEach(b=>b.disabled=true);
  try{await submit(form);}catch(e){if(error)error.textContent=e.message;else toast(e.message,true);}finally{delete form.dataset.busy;buttons.forEach(b=>b.disabled=false);}
});
document.addEventListener('click',async event=>{
  const target=event.target.closest('[data-action]');if(target){target.disabled=true;try{await action(target.dataset.action,target.dataset.id);}catch(e){toast(e.message,true);}finally{target.disabled=false;}}
  if(event.target.closest('.search-result'))$('#dialog').close();
});
$('#dialog-close').addEventListener('click',()=>$('#dialog').close());
$('#logout').addEventListener('click',async()=>{try{await api('/api/auth/logout','POST');showLogin('Signed out.');}catch(e){toast(e.message,true);}});
$('#refresh').addEventListener('click',()=>navigate());
$('#mobile-menu').addEventListener('click',()=>{const open=$('#sidebar').classList.toggle('open');$('#mobile-shade').hidden=!open;$('#mobile-menu').setAttribute('aria-expanded',String(open));});
$('#mobile-shade').addEventListener('click',closeNavigation);
$('#search').addEventListener('click',()=>searchDialog().catch(e=>toast(e.message,true)));
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'){closeNavigation();if($('#dialog').open){event.preventDefault();$('#dialog').close();}}
  if(event.key==='Tab'&&$('#dialog').open){
    const controls=[...$('#dialog').querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary,[tabindex="0"]')].filter(e=>e.getClientRects().length);
    const first=controls[0],last=controls.at(-1),active=document.activeElement;
    if(first&&((event.shiftKey&&(active===first||!$('#dialog').contains(active)))||(!event.shiftKey&&(active===last||!$('#dialog').contains(active))))){event.preventDefault();(event.shiftKey?last:first).focus();}
  }
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'&&state.user){event.preventDefault();searchDialog().catch(e=>toast(e.message,true));}
});
window.addEventListener('hashchange',()=>navigate());
async function poll(){
  try{
    if(state.user&&!document.hidden){
      const d=await api('/api/v1/notifications');$('#notice-count').textContent=d.notifications.filter(n=>!n.read_at).length;
      const editing=document.activeElement?.closest('form')||document.querySelector('form[data-busy]')||$('#dialog').open;
      if(!editing&&['overview','services','incidents','capacity','notifications'].includes(state.route))await navigate(true);
    }
  }catch(e){$('#freshness').textContent='Connection interrupted';}finally{setTimeout(poll,5000);}
}
boot();setTimeout(poll,5000);
