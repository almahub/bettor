import {loadSystemTools} from './backup-settings.mjs';
import {placementPreview,diagnosticsPanel} from './placement-review.mjs';
import {settingsPanel, loadSettings} from './settings.mjs';
import {betReminderCard} from './bet-reminder.mjs';
import {html, mount, number, percent, points, money, date, initials, progress, shortId, marketName, selectionName, competitionName, setCompetitionNames, isItalianLeagueName, statuses, statusInfo, agentNames, agentGroups, gateNames, gateExplanation, observedMetric, reassessmentText, scenarios, diagnosticCauses, icon, badge, empty, safeURL} from './ui.mjs';

const state = {page:'dashboard', tab:'overview', systemTab:'architecture', systemAgent:'football_data_analyst', aiAgents:null, aiAgentsBusy:false, apiControlTab:'openai', health:null, workspace:null, matches:[], history:[], models:[], metrics:null, bank:null, salabetCandidates:[], salabetSlips:[],salabetSlipsLoaded:false,salabetSlipsError:false, apiControl:null, apiControlBusy:false, apiFootballBusy:false, apiCallPage:0,apiTablePages:{activity:0,costs:0}, dataPipeline:null, dataPipelineBusy:false, run:null, shadow:[], selectedMatch:'', competition:'all', scenario:'bet', market:'all', busy:false, betBusy:false, batchBusy:false, batch:null, error:null, search:'', filter:'all', model:'poisson'};
state.journalSelection = new Set();
state.journalHasMore=false;state.journalBusy=false;
state.listViews={journal:{page:0},cohort:{mode:'active',search:'',page:0},maturity:{mode:'active',search:'',page:0}};
const listPageSize=12;
const pageRows=(rows,view)=>rows.slice(view.page*listPageSize,(view.page+1)*listPageSize);
const listPager=(rows,view,key)=>html`<div class="list-pager"><span>${rows.length?view.page*listPageSize+1:0}?${Math.min((view.page+1)*listPageSize,rows.length)} di ${rows.length}</span><button class="btn small" data-list-page="${key}" data-step="-1" ${view.page===0?'disabled':''}>Precedenti</button><button class="btn small" data-list-page="${key}" data-step="1" ${(view.page+1)*listPageSize>=rows.length?'disabled':''}>Successive</button></div>`;
const pipelineListControls=(key,view)=>html`<div class="table-toolbar list-controls"><label class="search-field">${icon('search')}<input data-list-search="${key}" value="${view.search}" placeholder="Cerca partita o ID" type="search"></label><div class="segmented">${[['active','Da seguire'],['completed','Concluse'],['all','Tutte']].map(([mode,label])=>html`<button class="btn small ${view.mode===mode?'active':''}" data-list-mode="${key}" data-mode="${mode}" aria-pressed="${view.mode===mode}">${label}</button>`)}</div></div>`;
state.batchSelection = new Set();
state.batchPickerOpen = false;
const journalItems = () => state.history.filter(r=>(state.filter==='all'||r.status===state.filter)&&`${r.home_team} ${r.away_team} ${r.analysis_id}`.toLowerCase().includes(state.search.toLowerCase()));
const pages = {dashboard:['Dashboard','grid'],analysis:['Analisi','chart'],journal:['Journal','document'],models:['Modelli e validazione','validation'],pipeline:['Data Pipeline','layers'],api_control:['API Control','plug'],system:['Sistema','settings'],agents:['Agenti','chip']};
const tabs = {overview:'Panoramica',evidence:'Evidenze',agents:'Agenti',markets:'Mercati'};
const app = document.querySelector('#app');
const dialog = document.querySelector('#evidence-dialog');
const payload = id => state.run?.results?.[id]?.payload;
const analysisMatches = () => state.workspace?.fixture_source==='888sport_it' ? state.matches.filter(m=>isItalianLeagueName(competitionName(m.competition))) : state.matches;
const visibleMatches = () => analysisMatches().filter(m=>state.competition==='all'||m.competition===state.competition);
const currentMatch = () => state.run?.snapshot?.match || state.matches.find(m=>m.match_id===state.selectedMatch);
const evidence = () => [...new Map(Object.values(state.run?.results || {}).flatMap(r=>r.evidence || []).map(e=>[e.evidence_id,e])).values()];
const demoRun = () => state.run && Object.values(state.run.results).some(r=>r.provenance.some(p=>p.source_id==='synthetic-fixture'));
const options = (items, selected) => items.map(([value,label])=> value===selected ? html`<option value="${value}" selected>${label}</option>` : html`<option value="${value}">${label}</option>`);
const describeItem = item => typeof item==='string' ? item : item.description || item.message || item.reason || (item.type==='ODDS'?`Quota almeno ${number(item.target)}.`:item.type==='LINEUP'?'Conferma delle formazioni ufficiali.':item.type==='CONFIDENCE'?`Confidenza almeno ${percent(item.target)}.`:item.type?`${item.type}: ${item.target ?? item.operator ?? ''}`:JSON.stringify(item));
const detailsList = items => items?.length ? html`<ul class="detail-list">${items.map(item=>html`<li>${describeItem(item)}</li>`)}</ul>` : '';

function degradationNotes(items) {
  return (items || []).map(d=>html`<details class="gate ${d.critical?'fail':''}"><summary>${icon(d.critical?'alert':'eye')}<span>${d.impact==='BLOCK'?'Limitazione critica':d.impact==='CONFIDENCE_CAP'?'Limite alla confidenza':'Limitazione non critica'}</span></summary><p><strong>${d.code}</strong><br>${d.reason}</p><p>Controlli mancanti: ${d.missing_checks.join(', ')}<br>Gate: ${d.affected_gates.map(g=>gateNames[g] || g).join(', ')}<br>${d.confidence_cap!=null?`Confidenza massima: ${percent(d.confidence_cap)}`:d.critical?'Impedisce BET nel proprio ambito.':'Nessuna penalità aggiuntiva.'}${d.applies_to_decision===false?' Non applicabile al mercato selezionato.':''}</p></details>`);
}

function confidenceDetails(decision) {
  const names={data_completeness:'Completezza dati',model_reliability:'Affidabilità modello',source_consistency:'Coerenza fonti',model_uncertainty:'Incertezza modello',lineup_certainty:'Certezza formazioni',market_liquidity:'Liquidità mercato',contrary_severity:'Criticità contrarie'};
  return html`<details class="gate"><summary>${icon('pulse')}Come si forma la confidenza</summary><p>È il minimo delle componenti operative. Non è una media degli agenti o la probabilità di vincita.</p><dl class="definition-list">${Object.entries(decision.confidence_components || {}).map(([key,value])=>html`<dt>${names[key] || key}</dt><dd>${percent(value,2)}${value===decision.confidence?' · minimo':''}</dd>`)}</dl></details>${degradationNotes(decision.degradations)}`;
}

async function api(path, body, signal) {
  const batchRequest=body&&(path.startsWith('/batch-analysis')||path==='/salabet/automatic-multiples/placement');
  const response = await fetch(path, {signal:signal || (batchRequest?undefined:AbortSignal.timeout(60000)), ...(body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})});
  if (!response.ok) {
    const data = await response.json().catch(()=>({}));
    const detail = data.error?.message || data.detail;
    if(path==='/salabet/automatic-multiples/placement'&&typeof detail==='string')throw new Error(detail);
    throw new Error(response.status===429 ? 'Troppe analisi in un minuto. Attendi e riprova.' : `${response.status}: ${typeof detail==='string'?detail:'Richiesta non riuscita'}`);
  }
  return response.json();
}
function announce(message) { document.querySelector('#announcer').textContent=message; }
let toastTimer;
function toast(message) { const element=document.querySelector('#toast'); element.textContent=message; element.hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>{element.hidden=true;},3500); announce(message); }
function render() {
  const toolsContent=state.page==='system'&&['backup','updates'].includes(state.systemTab)?document.querySelector('#system-tools'):null;
  const settingsContent=state.page==='system'&&state.systemTab==='settings'?document.querySelector('#credential-settings'):null;
  const active=document.activeElement; const focus=active?.id; const cursor=active?.selectionStart;
  mount(app,html`<div class="app-shell">
    <aside class="sidebar"><a class="brand" href="#dashboard" aria-label="Bettor, dashboard"><img class="brand-logo" src="/assets/brand/bettor-logo-dark.svg" alt="Bettor · Football Intelligence"></a><div class="edition">Football intelligence</div><div class="nav-label">WORKSPACE</div>
    <nav aria-label="Navigazione principale">${[['Operatività',['dashboard','analysis','journal']],['Dati e modelli',['models','pipeline']],['Sistema',['agents','system']]].map(([group,ids])=>html`<span class="nav-group-title">${group}</span>${ids.map(id=>{const [label,symbol]=pages[id];const active=state.page===id||(id==='agents'&&state.page==='api_control');return html`<button class="nav-item ${active?'active':''}" data-page="${id}" aria-label="${label}" aria-current="${active?'page':'false'}" title="${label}">${icon(symbol)}<span>${label}</span>${id==='journal'?html`<span class="nav-counter">${state.history.length}</span>`:''}</button>`;})}`)}<a class="nav-item salabet-link" href="/salabet" aria-label="Apri SalaBet" title="Apri SalaBet">${icon('ticket')}<span>SalaBet</span></a></nav>
    <div class="sidebar-bottom"><div class="architecture-note">${icon('shield')}<strong>Ogni decisione, una traccia.</strong><p>Dalle fonti ai controlli, il contesto resta sempre verificabile.</p></div><div class="sidebar-footer"><span class="dot ${state.health?'':'offline'}"></span><span>Bettor · v${state.health?.version || '—'}</span></div></div></aside>
    <div class="main-shell"><header class="topbar"><div class="breadcrumb">Workspace ${icon('chevron')}<strong>${pages[state.page][0]}</strong></div><a class="brand mobile-brand" href="#dashboard"><span class="brand-symbol">b</span><span>bettor.</span></a><div class="topbar-right"><span class="topdate">${new Intl.DateTimeFormat('it-IT',{weekday:'long',day:'numeric',month:'long'}).format(new Date())}</span><span class="system-chip"><i class="dot ${state.health?'':'offline'}"></i>${state.health?'Backend connesso':'Backend non disponibile'}</span><button class="icon-button" data-action="refresh" aria-label="Aggiorna workspace" title="Aggiorna workspace">${icon('refresh')}</button><span class="avatar" title="Workspace locale">B</span></div></header>
    <main class="main" id="main" tabindex="-1">${state.error?html`<div class="error-banner" role="alert">${icon('alert')}<span>${state.error}</span><button class="text-button" data-action="dismiss">Chiudi</button></div>`:''}
    ${state.workspace?.is_demo?html`<div class="demo-notice"><span><strong>DEMO</strong> Partite, quote e risultati sono sintetici. Esplora il processo con scenari riproducibili.</span>${icon('layers')}</div>`:demoRun()?html`<div class="demo-notice"><span><strong>ARCHIVIO DEMO</strong> Questa analisi salvata contiene dati sintetici.</span></div>`:''}
    ${state.page==='models'?matchResearchPanel():''}
    ${state.page==='dashboard'?dashboardPage():state.page==='analysis'?analysisPage():state.page==='journal'?journalPage():state.page==='models'?modelsPage():state.page==='pipeline'?dataPipelinePage():state.page==='api_control'?agentsPage():state.page==='agents'?agentsPage():systemPage()}
    <footer class="footer"><span>Bettor · Football decision support</span><span>${state.run?`Snapshot ${shortId(state.run.analysis_id)} · ${date(state.run.as_of)}`:'Le decisioni appariranno dopo l’analisi.'}</span></footer></main></div></div>`);
  if(settingsContent)document.querySelector('#credential-settings')?.replaceWith(settingsContent);
  if(toolsContent&&toolsContent.dataset?.kind===state.systemTab)document.querySelector('#system-tools')?.replaceWith(toolsContent);
  if (focus) {const next=document.getElementById(focus); next?.focus({preventScroll:true}); if (cursor!=null && next?.setSelectionRange) next.setSelectionRange(cursor,cursor);}
}
function heading(eyebrow,title,subtitle,action='') {return html`<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p class="subtext">${subtitle}</p></div>${action}</div>`;}
function dashboardCard(label,value,hint,page,symbol='arrow',kind='activity') {
  return html`<button class="overview-card ${kind}-card" data-page="${page}" aria-label="${label}: ${value}. Apri ${pages[page]?.[0] || page}"><span class="overview-card-icon">${icon(symbol)}</span><span class="metric-label">${label}</span><strong>${value}</strong><span>${hint}</span><i>${icon('arrow')}</i></button>`;
}
function automationCard(){
  const c=state.dataPipeline?.timing_comparison?.control;
  if(!c)return '';
  return html`<article class="overview-card status-card automation-card"><span class="metric-label">Automazione Serie A</span><strong>${c.enabled?'ATTIVA':'DISATTIVATA'}</strong><span>Analisi T−24 · candidate in Da valutare · nessuna puntata automatica</span><button class="btn small automation-switch" role="switch" aria-checked="${c.enabled}" aria-label="Automazione analisi Serie A" data-action="automation-toggle" ${state.automationBusy?'disabled':''}><span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span>${state.automationBusy?'Aggiornamento…':c.enabled?'Disattiva':'Attiva'}</button><a href="/salabet#bank">Massimali ricerche in Banca</a>${c.enabled&&!c.paid_research?html`<span>Ricerche a pagamento sospese: verificare massimali e cambio.</span>`:''}</article>`;
}
async function toggleAutomation(){
  if(state.automationBusy)return;
  const target=!state.dataPipeline.timing_comparison.control.enabled;
  if(state.automationDraft?.enabled!==target)state.automationDraft={enabled:target,request_id:crypto.randomUUID()};
  state.automationBusy=true;render();
  try{
    const c=await api('/automation',state.automationDraft);
    state.dataPipeline.timing_comparison.control=c;state.dataPipeline.timing_comparison.enabled=c.enabled;
    state.automationDraft=null;toast(c.enabled?'Automazione Serie A attivata.':'Automazione disattivata: nessuna nuova ricerca automatica.');
  }catch(error){state.error=error.message;toast(error.message);}
  finally{state.automationBusy=false;render();}
}
function placementAutomationCard(){
 const control=state.dataPipeline?.automatic_multiples;
 if(!control)return '';
 return html`<article class="overview-card status-card automation-card"><span class="metric-label">Automazione Piazza Scommessa</span><strong>${control.paused?'SOSPESA':control.enabled?'ATTIVA':'DISATTIVATA'}</strong><span>Prepara le multiple; verifica e conferma a T-15 dal primo kickoff di ciascuna. Banca addebitata solo alla conferma.</span><span>Approvazione Auto: ${control.control?.approval_mode==='FUTURE_PROPOSALS'?'ATTIVA':'solo proposta approvata'}</span><span>Budget turno ${money(control.budget_eur || 50)} · residuo ${money(control.unallocated_eur)}</span><button class="btn small automation-switch" role="switch" aria-label="Automazione Piazza Scommessa" aria-checked="${Boolean(control.enabled)}" data-action="placement-automation-toggle" ${state.placementBusy?'disabled':''}><span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span>${control.enabled?'Disattiva':'Attiva'}</button><a href="/salabet#candidates">Budget e candidate in Da valutare</a>${control.enabled||control.paused?html`<span>${control.readiness_reason || ''}</span>`:''}${control.approval_required?html`<button class="btn small" data-action="placement-review">Rivedi proposta</button>`:''}${state.placementReview?placementPreview(control,state.placementBusy,state.placementFuture):''}</article>`;
}
async function togglePlacementAutomation(reviewOnly=false){
 if(state.placementBusy)return;
 const control=state.dataPipeline?.automatic_multiples;if(!control)return;
 if(!control.enabled||reviewOnly){
  state.placementBusy=true;render();
  try{const report=await api('/salabet/automatic-multiples');state.dataPipeline.automatic_multiples=report;state.placementReview=report.preview.preview_id;state.placementFuture=false;}
  catch(error){state.error=error.message;}
  finally{state.placementBusy=false;render();}
  return;
 }
 await approvePlacement(false);
}
async function approvePlacement(enabled=true){
 if(state.placementBusy)return;
 const control=state.dataPipeline?.automatic_multiples;if(!control)return;
 const mode=state.placementFuture?'FUTURE_PROPOSALS':'CURRENT_PROPOSAL';
 if(enabled&&!confirm(state.placementFuture?`Autorizzare anche le composizioni future entro ${control.budget_eur} EUR per turno, con addebiti Banca? Nessun invio al bookmaker.`:`Confermare soltanto le multiple mostrate per ${control.preview.total_eur} EUR, addebitando la Banca?`))return;
 const body={request_id:crypto.randomUUID(),enabled,preview_id:enabled?state.placementReview:null,approval_mode:mode};
 state.placementBusy=true;render();
 try{const result=await api('/salabet/automatic-multiples/placement',body);state.dataPipeline.automatic_multiples=result;state.placementReview=null;await refresh();toast(result.execution?.created?.length?'Giocate registrate. Banca aggiornata.':enabled?'Autorizzazione salvata; verifica lo stato della proposta.':'Automazione disattivata.');if(result.execution?.diagnostics?.length)state.error=result.execution.diagnostics.join(' · ');}
 catch(error){state.error=error.message;}
 finally{state.placementBusy=false;render();}
}
async function loadModelDiagnostics(){
 if(state.modelDiagnosticsBusy)return;state.modelDiagnosticsBusy=true;
 try{state.modelDiagnostics=await api('/models/diagnostics');}
 catch(error){state.error=error.message;}
 state.modelDiagnosticsBusy=false;render();
}
function dashboardPage() {
  const pipeline=state.dataPipeline; const cohort=pipeline?.validation_cohort || {}; const groups=cohort.groups || [];
  const bestSample=groups.length?Math.max(...groups.map(group=>Number(group.sample_size || 0))):Number(cohort.valid_observations || 0);
  const minimum=Number(cohort.minimum_sample || 200); const forecasts=state.history.filter(item=>item.market).length;
  const openBets=state.salabetSlips.filter(item=>item.status==='OPEN').length;
  const collectorActive=Boolean(pipeline?.collector?.healthy); const collectorStatus=pipeline?.collector?.status || 'NON VERIFICATO'; const provider=state.workspace?.api_football; const account=provider?.account;
  const alerts=[]; const addAlert=(title,reason,severity,page)=>alerts.push({title,reason,severity,page});
  if(!collectorActive)addAlert('Collector da verificare',`Stato ${collectorStatus}: controlla ultimo ciclo ed errori.`,'critica','pipeline');
  if(pipeline&&pipeline.status!=='READY')addAlert('Pipeline non pronta',`Stato corrente: ${pipeline.status}.`,'critica','pipeline');
  if(state.apiControl?.calls?.some(call=>String(call.status).toUpperCase().includes('ERROR')))addAlert('Errore OpenAI','Una o più chiamate registrate risultano in errore.','alta','api_control');
  if(account?.daily_limit&&Number(account.requests_today)/Number(account.daily_limit)>=.85)addAlert('API-Football vicino al limite',`${number(account.requests_today,0)} richieste su ${number(account.daily_limit,0)}.`,'alta','api_control');
  if(provider?.configured&&(Number(provider.mapped_competitions || 0)===0||Number(provider.mapped_teams || 0)===0))addAlert('Mapping provider incompleto','Mancano associazioni esplicite per competizioni o squadre.','media','api_control');
  if(state.run?.decision?.status==='WAITING_INFO'&&diagnosticCauses(state.run).some(cause=>/lineup|formazion/i.test(`${cause.label} ${cause.detail}`)))addAlert('Lineup in attesa','L’analisi selezionata attende le formazioni.','media','analysis');
  if((pipeline?.settlement?.pending || []).length)addAlert('Settlement in attesa',`${pipeline.settlement.pending.length} partite concluse attendono un risultato.`,'alta','pipeline');
  if(pipeline?.storage?.within_limit===false)addAlert('Storage oltre limite','Lo spazio della Data Pipeline richiede attenzione.','alta','pipeline');
  if(Number(cohort.result_identity?.conflict || 0)>0)addAlert('Conflitto risultati',`${cohort.result_identity.conflict} identità risultato in conflitto.`,'critica','pipeline');
  return html`${heading('Quadro operativo','Dashboard','Come sta Bettor adesso: stato, attività e sole attenzioni operative.')}
  ${state.workspace?.installation?.issues?.length?html`<section class="panel"><h2>Configurazione da completare</h2><p>Alcune funzioni richiedono dati o impostazioni iniziali. Nessun fondo o schedina viene creato automaticamente.</p><a class="btn small" href="#system/settings">Vedi cosa manca (${state.workspace.installation.issues.length})</a></section>`:''}
  <nav class="quick-actions" aria-label="Azioni rapide"><button class="btn small" data-page="analysis">${icon('chart')}Analisi batch</button><a class="btn small" href="/salabet#candidates">${icon('ticket')}SalaBet</a><a class="btn small" href="/salabet#bank">${icon('wallet')}Banca</a><a class="btn small" href="#agents/api_control">${icon('plug')}API Control</a><a class="btn small" href="#system/backup">${icon('database')}Backup</a></nav><div id="pending-bet-reminder">${betReminderCard(state.salabetSlips,{loaded:state.salabetSlipsLoaded,error:state.salabetSlipsError})}</div>
  <section aria-labelledby="operational-status"><div class="dashboard-band-title"><h2 id="operational-status">Stato operativo</h2><span>A colpo d’occhio</span></div><div class="operational-grid">
    ${dashboardCard('Pipeline',pipeline?.status || 'NON DISPONIBILE','Stato Data Pipeline','pipeline','pulse','status')}
    ${dashboardCard('Collector',collectorActive?'ATTIVO':collectorStatus,collectorActive?`Ciclo ogni ${pipeline.policy.poll_seconds} secondi`:'Ultimo ciclo da verificare','pipeline','pulse','status')}
    ${dashboardCard('Model Validator',`${bestSample} / ${minimum}`,'Miglior progresso live forward','models','validation','status')}<a class="overview-card status-card" href="/salabet#bank"><span class="overview-card-icon">${icon('wallet')}</span><span class="metric-label">Bankroll</span><strong>${state.bank?.equity==null?'—':`${number(state.bank.equity,2)} €`}</strong><span>Capitale totale · disponibile ${state.bank?.available==null?'non disponibile':`${number(state.bank.available,2)} €`}</span></a></div><div class="automation-grid">${automationCard()}${placementAutomationCard()}
  </div></section>
  <section aria-labelledby="operational-activity"><div class="dashboard-band-title"><h2 id="operational-activity">Attività operativa</h2><span>Volumi essenziali</span></div><div class="activity-grid">
    ${dashboardCard('Partite disponibili',analysisMatches().length,'Pronte per la selezione','analysis','ball')}
    ${dashboardCard('Analisi recenti',state.history.length,'Ultime 50 nel Journal','journal','book')}
    ${dashboardCard('Pronostici indicativi',forecasts,'Analisi con mercato stimato','analysis','chart')}<a class="overview-card activity-card" href="/salabet#candidates"><span class="overview-card-icon">${icon('ticket')}</span><span class="metric-label">Candidate SalaBet</span><strong>${state.salabetCandidates.length}</strong><span>Da valutare</span></a><a class="overview-card activity-card" href="/salabet#bank"><span class="overview-card-icon">${icon('chart')}</span><span class="metric-label">Profit / Loss</span><strong>${state.bank?.profit==null?'—':`${number(state.bank.profit,2)} €`}</strong><span>Complessivo · giocate registrate</span></a>
    <a class="overview-card activity-card salabet-card" href="/salabet#open" aria-label="Scommesse aperte: ${openBets}. Apri SalaBet"><span class="overview-card-icon">${icon('ticket')}</span><span class="metric-label">Scommesse aperte</span><strong>${openBets}</strong><span>Piazzate e non liquidate</span><i>${icon('arrow')}</i></a>
  </div></section>
  <section class="panel attention-panel" aria-labelledby="attention-title"><div class="panel-title"><div><h2 id="attention-title">Attenzioni</h2><span class="caption">Solo condizioni che richiedono verifica</span></div><span class="badge ${alerts.length?'negative':'positive'}">${alerts.length?Math.min(alerts.length,5):'OK'}</span></div>${alerts.length?alerts.slice(0,5).map(alert=>html`<button class="attention-row ${alert.severity}" data-page="${alert.page}"><span class="attention-severity">${alert.severity}</span><span><strong>${alert.title}</strong><small>${alert.reason}</small></span>${icon('chevron')}</button>`):html`<div class="overview-clear">${icon('check')}<div><strong>Nessuna attenzione operativa</strong><span>Pipeline, collector e controlli principali non segnalano blocchi.</span></div></div>`}</section>`;
}
let researchTimer, researchController, researchGeneration=0;
function scheduleResearch() {
  clearTimeout(researchTimer);researchController?.abort();researchGeneration++;
  state.matchResearch=null;state.researchError=null;state.researchLoading=!!state.selectedMatch;
  if(!state.selectedMatch)return;
  const identifier=state.selectedMatch,generation=researchGeneration;
  researchTimer=setTimeout(()=>{void inspectSelectedMatch(identifier,generation);},400);
}
async function inspectSelectedMatch(identifier,generation) {
  const controller=new AbortController();researchController=controller;
  const timeout=setTimeout(()=>controller.abort(),60000);
  try{
    const result=await api(`/matches/${encodeURIComponent(identifier)}/research`,undefined,controller.signal);
    if(generation===researchGeneration&&identifier===state.selectedMatch)state.matchResearch=result;
  }catch(error){if(generation===researchGeneration)state.researchError=error.name==='AbortError'?'Consultazione interrotta o tempo scaduto.':error.message;}
  finally{
    clearTimeout(timeout);
    if(generation===researchGeneration){state.researchLoading=false;if(['analysis','models'].includes(state.page))render();}
  }
}
function matchResearchPanel() {
  const r=state.matchResearch;
  const labels={stats:'Statistiche',odds:'Quote',news:'Notizie',lineups:'Formazioni'};
  const availability=s=>!s.available&&s.research_found_fields?.length?`${s.research_found_fields.length} categorie trovate; non modellabili`:!s.available?'Non disponibili':s.complete?'Campi controllati presenti':'Parziale';
  const configurationErrors=[state.workspace?.football_data_research?.configuration_error,state.workspace?.external_research?.configuration_error].filter(Boolean);
  return html`<section class="panel mb-18"><div class="panel-title"><h2>Dati per la partita selezionata</h2><span class="caption">Consultazione automatica</span></div>
  ${configurationErrors.length?html`<div class="error-banner" role="alert">${icon('alert')}<span><strong>Ricerca non configurata.</strong> ${configurationErrors.join(' · ')}</span></div>`:''}
  <p>La scelta della partita consulta le fonti configurate e lo storico pertinente, senza avviare agenti o una decisione. L’analisi verifica nuovamente i dati e riusa le cache ancora valide.</p>
  ${state.researchLoading?html`<p role="status">Raccolta dati della partita selezionata…</p>`:state.researchError?html`<p role="alert">${state.researchError}</p>`:!r?html`<p>Seleziona una partita nel Centro analisi per consultare automaticamente le fonti.</p>`:html`
  <h3>${r.home_team} – ${r.away_team}</h3><p class="caption">Raccolta ${date(r.collected_at)} · verifica ${date(r.checked_at)}${r.cached?' · cache':''}</p>
  <div class="table-wrap"><table class="table"><thead><tr><th>Input normalizzati</th><th>Completezza</th><th>Freschezza</th><th>Affidabilità</th><th>Coerenza tra fonti</th></tr></thead><tbody>${r.sections.map(s=>html`<tr><td>${labels[s.kind]}</td><td>${availability(s)}</td><td>${s.fresh===null?'Non valutabile':s.fresh?'Entro TTL':'Scaduta'}</td><td>${s.reliability_tiers.length?`Tier ${s.reliability_tiers.join(', ')}`:'Non valutabile'}</td><td>${({NOT_VERIFIED:'Non verificata',CONSISTENT:'Confronto coerente',CONFLICT:'Conflitto'})[s.consistency]}</td></tr>`)}</tbody></table></div>
  ${r.sections.map(s=>html`<details class="gate"><summary>${labels[s.kind]} · dettagli e fonti</summary>${s.error?html`<p>${s.error.message}</p>`:''}${s.research_found_fields?.length?html`<p>Dati trovati dalla ricerca: ${s.research_found_fields.join(', ')}.</p>`:''}${s.normalization_reason?html`<p>${s.normalization_reason}</p>`:''}${s.missing.length?html`<p>Campi o controlli mancanti: ${s.missing.join(', ')}</p>`:''}<p>${s.consistency_reason}</p>${s.age_seconds!=null?html`<p>Età osservazione: ${number(s.age_seconds,0)} s · TTL: ${s.ttl_seconds} s</p>`:''}${s.data_cutoff?html`<p>Ultimi risultati inclusi: ${date(s.data_cutoff)}. Campioni casa/ospite: ${s.sample_sizes.home}/${s.sample_sizes.away}.</p>`:''}${s.sources.map(p=>html`<p>${p.source_id} · origine ${p.primary_source_id || 'non dichiarata'} · tier ${p.tier}${safeURL(p.url)?html` · <a href="${safeURL(p.url)}" target="_blank" rel="noopener noreferrer">Fonte</a>`:''}</p>`)}</details>`)}
  ${footballDataResearchPanel(r.football_data_research)}
  ${externalResearchPanel(r.external_research)}
  <p><strong>Storico pertinente al modello:</strong> ${r.history.settled_predictions} previsioni concluse. Soglia campione: ${r.history.minimum_sample} per gruppo modello/mercato. ${r.history.note}</p>`}
  <p class="caption">Tier A–D è la classificazione dichiarata della fonte, non una garanzia. Una sola fonte o due siti con la stessa origine non verificano la coerenza indipendente.</p></section>`;
}
function footballDataResearchPanel(research) {
  if(!research)return '';
  const enabled=!['DISABLED','OUT_OF_SCOPE'].includes(research.status);
  return html`<details class="gate" open><summary>Ricerca statistica · ${enabled?'dati correnti':'non disponibile'}</summary>
  <p><strong>Stato:</strong> ${research.status} · agente ${research.agent_id || '—'} · transport ${research.transport || '—'}${research.cached?' · cache':' · live'}</p>
  ${research.error?html`<p role="status">${research.error}</p>`:''}
  ${research.requested_fields?.length?html`<p>Metriche richieste: ${research.requested_fields.join(', ')}</p>`:''}
  ${(research.findings || []).map(f=>html`<div class="evidence-card"><strong>${f.field} · ${f.status}</strong><p>Confidenza ${percent(f.confidence)}${f.notes?` · ${f.notes}`:''}</p>${f.sources.map(s=>html`<p>Tier ${s.source_tier} · ${s.publisher}${safeURL(s.url)?html` · <a href="${safeURL(s.url)}" target="_blank" rel="noopener noreferrer">Fonte</a>`:''}<br><span class="caption">Stagione ${s.season} · campione ${s.sample_size} · recuperata ${date(s.retrieved_at)}</span></p>`)}</div>`) }
  <p class="caption">Costo stimato: ${research.estimated_cost==null?'non calcolabile':research.estimated_cost} · ${research.search_calls || 0} ricerche web / ${research.tool_calls || 0} tool call · ${research.input_tokens || 0}/${research.output_tokens || 0} token input/output · durata ${research.duration_ms==null?'—':number(research.duration_ms,0)+' ms'}.</p>
  <details><summary>Identificativi tecnici</summary><p class="monospace">Agent ${research.agent_id || '—'} · sessione ${research.session_id || '—'} · risposta ${research.response_id || '—'}</p></details></details>`;
}
function externalResearchPanel(research) {
  if(!research)return '';
  const enabled=!['DISABLED','NOT_REQUIRED'].includes(research.status);
  return html`<details class="gate" open><summary>Ricerca contestuale · ${enabled?'informazioni pre-match':'disattivata'}</summary>
  <p><strong>Stato:</strong> ${research.status} · provider ${research.provider || '—'} · transport ${research.transport || '—'} · modello ${research.model || 'definito dal saved agent'}${research.cached?' · cache':' · live'}</p>
  ${research.agent_id||research.session_id?html`<p>Agent ${research.agent_id || '—'} · sessione ${research.session_id || '—'} · risposta/turn ${research.response_id || '—'}</p>`:''}
  ${research.searched_fields?.length?html`<p>Campi cercati: ${research.searched_fields.join(', ')}</p>`:''}
  ${research.error?html`<p role="status">${research.error}</p>`:''}
  ${(research.findings || []).map(f=>html`<div class="evidence-card"><strong>${f.field} · ${f.status}</strong><p>Confidenza ${percent(f.confidence)}${f.notes?` · ${f.notes}`:''}</p>${f.sources.map(s=>html`<p>Tier ${s.reliability_tier} · ${s.publisher || s.source_type}${safeURL(s.url)?html` · <a href="${safeURL(s.url)}" target="_blank" rel="noopener noreferrer">${s.title || 'Fonte'}</a>`:''}<br><span class="caption">Pubblicata ${s.published_at?date(s.published_at):'timestamp non disponibile'} · recuperata ${date(s.retrieved_at)}</span></p>`)}</div>`)}
  ${research.coverage?html`<p>Campi mancanti: ${research.coverage.before} prima della ricerca, ${research.coverage.after} dopo.</p>`:''}
  <p class="caption">Costo stimato: ${research.estimated_cost==null?'non calcolabile':research.estimated_cost} · ${research.search_calls || 0} ricerche web / ${research.tool_calls || 0} tool call · ${research.input_tokens || 0}/${research.output_tokens || 0} token input/output · durata ${research.duration_ms==null?'—':number(research.duration_ms,0)+' ms'}.</p></details>`;
}
function analysisPage() {
  const match=currentMatch(); const d=state.run?.decision;
  return html`${heading('Il contesto prima della scelta','Centro analisi','Una partita, tutte le evidenze. Una decisione motivata.',state.run?html`<button class="btn" data-action="download" aria-label="Esporta analisi JSON">${icon('download')}<span>Esporta analisi</span></button>`:'')}
    ${state.workspace?.fixture_source==='888sport_it'?html`<div class="note-bar"><strong>888 Sport · ${analysisMatches().length} partite dei campionati italiani nei prossimi ${Math.round(state.workspace.catalog_hours/24)} giorni</strong><br>Il catalogo non avvia gli agenti. Le quote vengono richieste solo per la partita selezionata.${state.workspace.missing_sources?.length?html`<br>Fonti ancora da collegare: ${state.workspace.missing_sources.map(s=>({stats:'statistiche',news:'notizie',lineups:'formazioni'})[s] || s).join(', ')}. Una quota disponibile non significa che l’analisi abbia dati sufficienti.`:''}</div>`:''}
    ${batchPanel()}
    <div class="match-toolbar"><div class="field"><label for="competition-select">Competizione</label><select id="competition-select">${options([['all',state.workspace?.fixture_source==='888sport_it'?'Tutti i campionati italiani':'Tutte le competizioni'],...[...new Set(analysisMatches().map(m=>m.competition))].sort((a,b)=>competitionName(a).localeCompare(competitionName(b),'it')).map(c=>[c,competitionName(c)])],state.competition)}</select></div><div class="field grow"><label for="match-select">Partita</label><select id="match-select">${visibleMatches().length?options(visibleMatches().map(m=>[m.match_id,`${m.home_team} — ${m.away_team}`]),state.selectedMatch):html`<option value="">Nessuna partita disponibile</option>`}</select></div>
    ${state.workspace?.is_demo?html`<div class="field scenario-field"><label for="scenario-select">Scenario demo</label><select id="scenario-select">${options(state.workspace.demo_cases.map(s=>[s,scenarios[s] || s]),state.scenario)}</select></div>`:''}
    <div class="field market-field"><label for="market-select">Mercati</label><select id="market-select">${options([['all','Tutti i mercati'],...(state.workspace?.supported_markets || []).map(m=>[m,marketName(m)])],state.market)}</select></div>${analyzeButton()}</div>
    ${matchResearchPanel()}
    <div class="workspace-grid"><section class="analysis-column" aria-label="Contesto della partita">
    ${match?html`<div class="match-hero"><div class="pitch-lines" aria-hidden="true"></div><div class="hero-meta"><span>${icon('ball')}${competitionName(match.competition)}</span><span class="hero-chip">PRE-MATCH</span></div><div class="match-teams"><div class="team"><div class="team-crest" aria-hidden="true">${initials(match.home_team)}</div><div><h2 class="team-name">${match.home_team}</h2><p class="team-caption">CASA</p></div></div><div class="match-versus"><strong>${date(match.kickoff,true)}</strong><span>Kick-off</span></div><div class="team away"><div class="team-crest" aria-hidden="true">${initials(match.away_team)}</div><div><h2 class="team-name">${match.away_team}</h2><p class="team-caption">TRASFERTA</p></div></div></div><div class="hero-footer"><span>${icon('clock')}${date(match.kickoff)}</span><span class="snapshot-label">${state.run?`Snapshot · ${date(state.run.as_of)}`:'In attesa della prima analisi'}</span></div></div>`:html`<div class="panel">${empty('Scegli il prossimo contesto','Configura un provider di partite per iniziare. Le analisi archiviate restano disponibili nel Journal.','ball')}</div>`}
    <div class="metrics-strip">${metric('Quota attuale',number(d?.current_odds),'Prezzo nello snapshot')}${metric('Probabilità stimata',percent(d?.estimated_probability),'Stima del modello')}${metric('Edge',points(d?.estimated_edge),'Differenza di probabilità',d?.estimated_edge>0)}${metric('Qualità dati',percent(d?.data_quality?.score),'Valutazione deterministica')}</div>
    <div class="tabs" role="tablist" aria-label="Dettaglio analisi">${Object.entries(tabs).map(([id,label])=>html`<button class="tab ${state.tab===id?'active':''}" id="tab-${id}" data-tab="${id}" role="tab" aria-selected="${state.tab===id}" aria-controls="analysis-content" tabindex="${state.tab===id?0:-1}">${label}${id==='evidence'&&state.run?html`<span class="tab-count">${evidence().length}</span>`:''}</button>`)}</div>
    <div id="analysis-content" role="tabpanel" aria-labelledby="tab-${state.tab}" aria-busy="${state.busy}">${state.busy?html`<div class="panel busy-state"><span class="spinner" aria-hidden="true"></span><strong>Analisi in corso</strong><p>Raccolta delle fonti, elaborazione e controlli della decisione.</p></div>`:!state.run?html`<div class="panel">${empty('La prossima decisione parte da qui','Avvia la pipeline per collegare dati, quote, modelli e controlli di rischio.','ball')}<div class="empty-action">${analyzeButton()}</div><div class="pipeline-preview"><span><i>01</i> Fonti</span><span><i>02</i> Modelli</span><span><i>03</i> Controlli</span><span><i>04</i> Decisione</span></div></div>`:state.tab==='overview'?overview():state.tab==='evidence'?evidencePanel():state.tab==='agents'?agentsPanel():marketsPanel()}</div>
    ${state.run?html`<div class="mt-18">${analyzeButton()}</div>`:''}</section><aside class="decision-column" aria-label="Decisione e controlli">${decisionPanel()}</aside></div>`;
}
function batchPanel(){
  const matches=visibleMatches(); const report=state.batch;
  return html`<section class="panel batch-panel" aria-labelledby="batch-title"><div class="panel-title"><div><h2 id="batch-title">Analisi batch</h2><span class="caption">Screening deterministico prima delle ricerche OpenAI; massimo una proposta per partita.</span></div><div class="batch-actions"><button class="btn" id="batch-selection-button" data-action="batch-selection" ${state.batchBusy||!state.batchSelection.size?'disabled':''}>Analizza selezione (${state.batchSelection.size})</button><button class="btn primary" data-action="batch-all" ${state.batchBusy||!matches.length?'disabled':''}>${state.batchBusy?'Analisi in corso':'Analizza tutte'}</button></div></div>
  <details class="batch-picker" ${state.batchPickerOpen?'open':''}><summary>Seleziona partite (${matches.length})</summary><div class="batch-match-grid">${matches.map(match=>html`<label><input type="checkbox" data-batch-match="${match.match_id}" ${state.batchSelection.has(match.match_id)?'checked':''}> <span>${match.home_team} – ${match.away_team}</span><small>${date(match.kickoff)}</small></label>`)}</div></details>
  ${state.batchBusy?html`<div class="batch-progress" role="status"><span class="spinner busy-inline"></span><strong>${report?.total || matches.length} totali · analisi in corso</strong><span>Le partite escluse dallo screening non attivano i saved agent.</span></div>`:''}
  ${report?batchReport(report):''}</section>`;
}
function batchReport(report){
  const status=row=>row.status || row.exclusion || row.outcome;
  return html`<div class="batch-summary"><span>Totali <strong>${report.total}</strong></span><span>Analizzate <strong>${report.analyzed}</strong></span><span>Completate <strong>${report.completed}</strong></span><span>Escluse <strong>${report.excluded}</strong></span><span>Candidate <strong>${report.candidates}</strong></span><span>Trasferite <strong>${report.transferred}</strong></span></div>
  <div class="table-wrap"><table class="table batch-table"><thead><tr><th>Partita</th><th>Competizione</th><th>Kickoff</th><th>Mercato / esito</th><th>P / quota</th><th>Edge / EV</th><th>Qualità</th><th>Validator</th><th>Score</th><th>Esito</th></tr></thead><tbody>${(report.rows||[]).map(row=>html`<tr><td><strong>${row.match}</strong>${row.analysis_id?html`<button class="btn small" data-run="${row.analysis_id}">Apri analisi</button>`:''}${row.change?html`<span class="secondary">${row.change}</span>`:''}</td><td>${competitionName(row.competition)}</td><td>${date(row.kickoff)}</td><td>${row.market?`${marketName(row.market)} · ${selectionName(row.selection)}`:'—'}</td><td>${row.probability==null?'—':`${percent(row.probability)} · ${number(row.odds)}`}</td><td>${row.edge==null?'—':`${points(row.edge)} · ${percent(row.expected_value)}`}</td><td>${row.data_quality==null?'—':percent(row.data_quality)}</td><td>${row.validation_sample==null?'—':`${row.validation_sample}/${row.validation_required} · ${row.validation_status}`}</td><td>${row.ranking_score==null?'—':number(row.ranking_score,1)}</td><td><strong>${status(row)}</strong>${row.reasons?.length?html`<span class="secondary">${row.reasons.join(' · ')}</span>`:''}</td></tr>`)}</tbody></table></div>
  <div class="batch-footer"><span class="caption">Batch ${shortId(report.batch_id)} · ${date(report.completed_at)}</span>${report.transferred?html`<button class="btn small" data-action="batch-refresh">Rianalizza candidate</button>`:''}</div>`;
}
function analyzeButton() {return state.busy?html`<button class="btn primary" disabled><span class="spinner busy-inline"></span>Analisi in corso</button>`:state.selectedMatch?html`<button class="btn primary analyze-button" data-action="analyze">${icon('arrow')}${state.run?'Aggiorna analisi':'Avvia analisi'}</button>`:html`<button class="btn primary" disabled>Nessuna partita disponibile</button>`;}
function metric(label,value,hint,positive=false) {return html`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value ${positive?'positive-text':''}">${value}</div><div class="metric-hint">${hint}</div></div>`;}
function overview() {
  const prediction=payload('predictive_model'); const probabilities=state.run.forecast?.markets?.find(f=>f.market==='1X2'&&f.method==='RAW_POISSON')?.probabilities || state.run.forecast?.markets?.find(f=>f.market==='1X2')?.probabilities; const q=state.run.decision.data_quality;
  const lineup=payload('lineup_player_impact'); const news=payload('news_intelligence');
  return html`${forecastPanel()}<div class="split"><div class="panel estimated-probability-panel"><div class="panel-title"><h2>Probabilità stimate · 1X2</h2><span class="caption">STIMA DEL MODELLO</span></div>${probabilities?html`<div class="probability-bar" role="img" aria-label="Casa ${percent(probabilities.HOME)}, pareggio ${percent(probabilities.DRAW)}, trasferta ${percent(probabilities.AWAY)}">${['HOME','DRAW','AWAY'].map(s=>html`<span style="width:${progress(probabilities[s])}%"></span>`)}</div><div class="probability-legend">${['HOME','DRAW','AWAY'].map((s,i)=>html`<div><span><i class="legend-dot ${['','draw','away'][i]}"></i>${selectionName(s)}</span><strong>${percent(probabilities[s])}</strong></div>`)}</div><p class="panel-footnote">Probabilità del modello sullo snapshot. Non sono frequenze garantite.</p>`:empty('Stima non disponibile','Il modello non ha prodotto una distribuzione 1X2 utilizzabile.')}</div>
    <div class="panel observed-quality-panel"><div class="panel-title"><h2>Stato attuale dei dati</h2>${icon('shield')}</div>${[['completeness','Completezza'],['freshness','Freschezza'],['source_reliability','Affidabilità fonti'],['cross_source_consistency','Coerenza tra fonti']].map(([key,label])=>html`<div class="quality-item"><span>${label}</span><strong>${percent(q?.[key],0)}</strong><div class="thin-bar"><span style="width:${progress(q?.[key])}%"></span></div></div>`)}</div></div>
    <div class="panel mt-18"><div class="panel-title"><h2>Dentro la partita</h2><span class="caption">Contesto verificabile</span></div><div class="context-grid"><div class="context-cell">${icon('layers')}<h3>Formazioni</h3><p>${lineup?.official?'Ufficiali e confermate':lineup?.lineup?'Probabili, da confermare':'Non disponibili'}</p><p>Sensibilità: ${percent(lineup?.sensitivity)}</p></div><div class="context-cell">${icon('book')}<h3>News & fonti</h3><p>${news?`${news.events.length} elementi raccolti · ${news.conflicts.length} conflitti`:'Raccolta non disponibile'}</p><p>${state.run.results.news_intelligence?.status || 'Non eseguito'}</p></div><div class="context-cell">${icon('pulse')}<h3>Modello</h3><p>${prediction?.model_predictions?.map(m=>m.model).join(', ') || 'Non disponibile'}</p><p>${prediction?.method || 'Nessuna stima ammessa'}</p></div></div></div>
    <div class="panel"><div class="panel-title"><h2>Le evidenze in primo piano</h2><button class="text-button" data-tab="evidence">Vedi tutte ${icon('arrow')}</button></div>${evidenceRows(evidence().slice(0,3))}</div>${footballContext()}`;
}
function forecastPanel() {
  const forecasts=state.run?.forecast?.markets || [];
  if(!forecasts.length)return html`<div class="panel mb-18">${empty('Pronostico probabilistico non disponibile','Servono statistiche osservate per entrambe le squadre oppure quote complete e simultanee per un mercato.')}</div>`;
  const modelForecasts=forecasts.filter(f=>f.method==='RAW_POISSON');
  const quoteForecasts=forecasts.filter(f=>f.method==='MARKET_NO_VIG');
  const kickoff=currentMatch()?.kickoff;
  return html`<section class="analysis-forecast-section" aria-label="Probabilità stimate dal modello"><div class="analysis-section-heading"><span class="analysis-section-tag">STIMA DEL MODELLO</span><p>Percentuali sui possibili esiti, calcolate da Bettor sui dati raccolti.</p></div><div class="panel mb-18 estimated-forecast-panel"><div class="panel-title"><h2>Pronostico probabilistico indicativo</h2><span class="caption">Probabilità stimate</span></div>${modelForecasts.length?html`<div class="market-forecast-grid">${modelForecasts.map(f=>{
    const leading=Object.entries(f.probabilities).sort((a,b)=>b[1]-a[1])[0]?.[0];
    const prediction=leading?payload('predictive_model')?.model_predictions?.find(item=>item.probabilities?.[f.market]?.[leading]!=null):null;
    const sendable=Boolean(prediction&&f.sample_size>0&&!['MODEL_INPUT_INSUFFICIENT','MODEL_INPUT_FEATURES_INSUFFICIENT'].includes(f.reason)&&kickoff&&new Date(kickoff)>new Date());
    const valueReport=payload('value_bettor');
    const values=[...(valueReport?.candidates||[]),...(valueReport?.indicative_candidates||[])];
    return html`<article class="market-forecast" aria-label="${marketName(f.market)} · previsione del modello"><h3>${marketName(f.market)}</h3><div class="market-state"><span class="badge ${f.operationally_eligible?'positive':'warning'}">${f.prediction_status || 'INDICATIVE'}</span><span class="secondary">${f.operationally_eligible?'AMMESSO ALLA DECISIONE':'NON AMMESSO · STAKE 0'}</span></div><div class="forecast-outcomes">${Object.entries(f.probabilities).map(([selection,p])=>{
      const value=values.find(c=>c.market?.type===f.market&&c.market?.selection===selection);
      return html`<div class="forecast-outcome ${selection===leading?'leading':''}"><div><span>${selectionName(selection)}</span><strong>${percent(p)}</strong></div><div class="probability-track" aria-hidden="true"><i style="--probability:${progress(p)}%"></i></div><div class="outcome-quote"><span>Fair odds ${number(f.fair_odds?.[selection])}</span><span>Quota ${number(value?.current_odds)}</span></div><div class="outcome-quote"><span>Edge ${points(value?.edge)}</span><span>EV ${percent(value?.expected_value)}</span></div>${selection===leading?html`<small class="forecast-leading-label">Esito più probabile</small>`:''}</div>`;
    })}</div><details class="gate"><summary>Input e validazione · ${percent(f.input_completeness)}</summary><p>${f.source}${f.sample_size==null?'':` · campione input ${f.sample_size}`}</p><p>Completezza ${percent(f.input_completeness)}<br>Validator ${f.validation_sample_size ?? 0}/${f.validation_minimum ?? '—'} · ${f.validation_status || '—'}</p><p>${f.reason || ''}</p>${f.missing_features?.length?html`<p>Mancano: ${f.missing_features.join(', ')}</p>`:''}</details><button class="btn small forecast-send" data-action="send-indicative" data-prediction="${prediction?.prediction_id||''}" data-market="${f.market}" data-selection="${leading||''}" ${!sendable||state.betBusy?'disabled':''}>Invia a SalaBet</button></article>`;
  })}</div>`:empty('Stima del modello non disponibile','Le quote correnti sono mostrate separatamente qui sotto.') }<p class="panel-footnote">Invia a SalaBet crea soltanto una proposta in “Da valutare”. Non piazza puntate né muove la Banca. ${state.run.forecast.disclaimer}</p></div></section>
  ${quoteForecasts.length?html`<section class="analysis-market-section" aria-label="Percentuali ricavate dalle quote correnti"><div class="analysis-section-heading"><span class="analysis-section-tag">QUOTE ATTUALI</span><p>Percentuali implicite nelle quote 888 dopo la rimozione del margine. Descrivono il mercato, non il pronostico di Bettor.</p></div><div class="panel market-baseline-panel"><div class="panel-title"><h2>Fotografia delle quote attuali</h2><span class="caption">Riferimento di mercato</span></div><div class="table-wrap"><table class="table"><thead><tr><th>Mercato</th><th>Percentuali implicite nelle quote</th><th>Origine</th></tr></thead><tbody>${quoteForecasts.map(f=>html`<tr><td><strong>${marketName(f.market)}</strong></td><td>${Object.entries(f.probabilities).map(([selection,p])=>html`<div class="forecast-bet-row"><span>${selectionName(selection)}</span><strong>${percent(p)}</strong></div>`)}</td><td>${f.source}</td></tr>`)}</tbody></table></div></div></section>`:''}`;
}
function footballContext() {
  const stats=payload('football_data_analyst'); const profiles=stats?.team_profiles;
  const lineup=payload('lineup_player_impact')?.lineup; const news=payload('news_intelligence'); const tactical=payload('tactical_analyst');
  return html`${profiles?html`<div class="panel"><div class="panel-title"><h2>Il confronto delle squadre</h2><span class="caption">Dati osservati</span></div><div class="table-wrap"><table class="table"><thead><tr><th>Indicatore</th><th class="numeric">${currentMatch().home_team}</th><th class="numeric">${currentMatch().away_team}</th></tr></thead><tbody>${[['goals_for_per_match','Gol fatti / partita',2],['goals_against_per_match','Gol subiti / partita',2],['xg_per_match','xG / partita',2],['xga_per_match','xGA / partita',2],['sample_size','Partite nel campione',0]].map(([key,label,digits])=>html`<tr><td>${label}</td><td class="numeric"><strong>${observedMetric(profiles.home?.[key],key)}</strong></td><td class="numeric"><strong>${observedMetric(profiles.away?.[key],key)}</strong></td></tr>`)}</tbody></table></div><p class="panel-footnote">Dati disponibili fino al ${date(stats.data_cutoff)}. Il campione e la copertura sono verificati dalla pipeline. xG/xGA sono gol attesi creati/concessi, non reti effettive. Se assenti, il modello Poisson usa le medie dei gol fatti/subiti; eventuali valori disponibili sono usati per la rispettiva componente.</p>${detailsList(stats.sample_warnings)}</div>`:''}
  <div class="panel"><div class="panel-title"><h2>Formazioni & notizie</h2><span class="caption">${lineup?.status==='CONFIRMED_LINEUP'?'Formazioni ufficiali':'Conferma non disponibile'}</span></div>
  ${lineup?html`<details class="gate"><summary>${icon('layers')}Consulta i giocatori nello snapshot</summary><p><strong>${currentMatch().home_team}</strong><br>${lineup.home_players.join(', ') || 'Non disponibili'}</p><p><strong>${currentMatch().away_team}</strong><br>${lineup.away_players.join(', ') || 'Non disponibili'}</p>${lineup.unavailable.length?html`<p><strong>Indisponibili</strong><br>${lineup.unavailable.join(', ')}</p>`:''}</details>`:''}
  ${news?.events.length?html`<div class="evidence-list">${news.events.map(n=>html`<div class="evidence-row"><span class="evidence-symbol">${icon(n.verified?'check':'alert')}</span><div class="evidence-text"><strong>${n.headline}</strong><div class="evidence-meta"><span>${n.verified?'Verificata':'Da verificare'}</span><span>${n.provenance.source_id}</span><span>${date(n.provenance.retrieved_at)}</span></div></div></div>`)}</div>`:html`<p class="subtext mt-18">${news?'Nessun elemento informativo nel feed raccolto.':'Feed notizie non disponibile.'}</p>`}
  ${tactical?.matchups?.length?html`<details class="gate mt-18"><summary>${icon('ball')}Lettura tattica</summary>${tactical.matchups.map(m=>html`<p><strong>${m.home_trait} / ${m.away_trait}</strong><br>${m.interaction}<br>${m.possible_market_effect}</p>`)}</details>`:''}</div>`;
}
function evidenceRows(items) {return items.length?html`<div class="evidence-list">${items.map(e=>html`<div class="evidence-row"><span class="evidence-symbol">${icon(e.direction==='NEGATIVE'?'shield':'layers')}</span><div class="evidence-text"><strong>${e.claim}</strong><div class="evidence-meta"><span>${agentNames[e.agent_id] || e.agent_id}</span><span>·</span><span>${e.provenance.length} riferimenti</span><span>·</span><span>${e.direction}</span></div></div><button class="text-button" data-evidence="${e.evidence_id}" aria-label="Apri fonti: ${e.claim}">Fonti ${icon('chevron')}</button></div>`)}</div>`:empty('Nessuna evidenza disponibile','Consulta i risultati degli agenti per conoscere dati mancanti ed errori.');}
function evidencePanel() {return html`<div class="panel"><div class="panel-title"><h2>Catena delle evidenze</h2><span class="caption">Fonti e derivazioni</span></div>${evidenceRows(evidence())}<p class="panel-footnote">Ogni elemento conserva la propria provenienza. Il numero di evidenze o di agenti non determina la decisione.</p></div>`;}
function agentsPanel() {return html`${agentGroups.map(group=>html`<section class="agent-section"><h3>${group.name}</h3><div class="agent-grid">${group.ids.map(id=>{
  const r=state.run.results[id]; return html`<details class="agent-card"><summary>${icon(r?.status==='OK'?'check':r?'alert':'circle')}<strong>${agentNames[id]}</strong><span class="agent-status ${r?.status==='PARTIAL'?'partial':r&&r.status!=='OK'?'error':''}">${r?.status || 'NON ESEGUITO'}</span></summary><p>${r?.summary || 'Nessun risultato in questo workflow.'}</p>${r?html`<div class="agent-measures"><span>Qualità ${percent(r.data_quality?.score)}</span><span>Confidenza ${percent(r.signal?.confidence)}</span></div>${degradationNotes(r.degradations)}${detailsList(r.risks)}${detailsList(r.unknowns)}${detailsList(r.errors)}<p class="monospace">${id} · v${r.agent_version} · prompt ${r.prompt_version}</p>`:''}</details>`;
})}</div></section>`)}<div class="agent-note"><strong>14º agente · Matched betting</strong><br>Workflow separato, ${state.workspace?.agents.find(a=>a.id==='matched_betting')?.enabled?'abilitato':'disabilitato'} nella configurazione corrente. Non contribuisce con un voto alla decisione.</div>`;}
function marketsPanel() {
  const report=payload('value_bettor'); const operational=report?.candidates || []; const indicative=report?.indicative_candidates || []; const candidates=operational.length?operational:indicative;
  if (!candidates.length) return html`<div class="panel">${empty('Nessun confronto di valore disponibile','Servono una distribuzione supportata e quote complete e simultanee per calcolare fair odds, edge ed EV.')}</div>`;
  return html`<div class="panel"><div class="panel-title"><h2>Prezzo, probabilità e valore</h2><span class="caption">${operational.length?'Calcoli operativi del backend':'INDICATIVO · NON OPERATIVO'}</span></div>${!operational.length?html`<div class="note-bar"><strong>MODELLO NON ANCORA VALIDATO</strong> · Il confronto è teorico. BET bloccato e stake 0.</div>`:''}<div class="table-wrap"><table class="table"><thead><tr><th>Mercato / esito</th><th>Quota</th><th>P stimata</th><th>Fair</th><th>Edge</th><th>EV</th><th>Stato</th></tr></thead><tbody>${candidates.map(c=>html`<tr><td><strong>${selectionName(c.market.selection)}</strong><span class="secondary">${marketName(c.market)} · ${c.bookmaker}</span></td><td class="numeric">${number(c.current_odds)}</td><td class="numeric">${percent(c.model_probability)}</td><td class="numeric">${number(c.fair_odds)}</td><td class="numeric">${points(c.edge)}</td><td class="numeric">${percent(c.expected_value)}</td><td>${c.operationally_eligible?'OPERATIVO':'INDICATIVO'}<span class="secondary">${c.reason || ''}</span></td></tr>`)}</tbody></table></div><p class="panel-footnote">Edge in punti percentuali. EV per unità puntata. Solo i candidati operativi possono arrivare al Risk Manager con stake positivo.</p></div>`;
}
function decisionPanel() {
  const d=state.busy?null:state.run?.decision; const info=statusInfo(d?.status); const shadow=state.health?.system_one; const causes=diagnosticCauses(state.run);
  const prediction=d?.market?payload('predictive_model')?.model_predictions?.find(item=>item.probabilities?.[d.market.type]?.[d.market.selection]!=null):null;
  const forecast=d?.market?state.run?.forecast?.markets?.find(item=>item.market===d.market.type):null;
  const addBetEnabled=Boolean(d?.status==='BET'&&prediction&&forecast?.operationally_eligible);
  const addBetReason=addBetEnabled?'Crea una proposta in SalaBet senza piazzare la scommessa.':d?.status==='BET'?'Predizione operativa non disponibile per la selezione finale.':d?`${info.label}: la decisione finale non e trasferibile a SalaBet.`:'Completa prima l analisi.';
  return html`<div class="decision-card"><div class="decision-card-header ${info.tone}"><div class="decision-kicker">Decision Engine ${icon('shield')}</div><h2>${info.label}</h2>${badge(d?.status)}<p>${info.message}</p></div><div class="decision-body">${d?html`
  ${d.status==='INSUFFICIENT_DATA'&&causes.length?html`<div class="watch-condition"><strong>Cause operative</strong><ul class="detail-list">${causes.map(c=>html`<li><strong>${c.label}</strong> · <code>${c.code}</code><br>${c.detail}</li>`)}</ul></div>`:''}
  ${d.market?html`<div class="decision-market"><span class="mini-label">${marketName(d.market)}</span><strong>${selectionName(d.market.selection)}</strong></div>`:''}
  <div class="empty-action"><button class="btn primary" data-action="add-bet" data-prediction="${prediction?.prediction_id || ''}" data-market="${d.market?.type || ''}" data-selection="${d.market?.selection || ''}" ${!addBetEnabled||state.betBusy?'disabled':''}>ADD BET</button><span class="secondary">${addBetReason}</span></div>
  <div class="decision-values"><div><span>Quota disponibile</span><strong>${number(d.current_odds)}</strong></div><div><span>Quota minima accettabile</span><strong>${number(d.minimum_acceptable_odds)}</strong></div><div><span>Valore atteso · EV</span><strong>${percent(d.expected_value)}</strong></div><div><span>Stake proposto</span><strong class="big-amount">${money(d.stake?.amount)}</strong></div><div><span>Quota del bankroll</span><strong>${percent(d.stake?.bankroll_fraction,2)}</strong></div><div><span>Confidenza della decisione</span><strong>${percent(d.confidence,0)}</strong></div></div>
  ${confidenceDetails(d)}<div class="gate-heading">Controlli della decisione <span>${d.decision_trace.length} gate</span></div>${d.decision_trace.map(g=>html`<details class="gate ${g.status==='FAIL'?'fail':g.status==='WAIT'?'wait':''}"><summary>${icon(g.status==='PASS'?'check':g.status==='WAIT'?'clock':'alert')}<span>${gateNames[g.gate] || g.gate}</span><span class="gate-status">${g.status}</span></summary><p>${gateExplanation(g,state.run) || g.reason}</p>${gateExplanation(g,state.run)?html`<details><summary>Traccia tecnica ? ${g.gate}</summary><p>${g.reason}</p></details>`:''}</details>`)}
  ${d.watch_conditions?.length?html`<div class="watch-condition"><strong>Da verificare</strong>${detailsList(d.watch_conditions)}</div>`:''}${d.fatal_concerns?.length?html`<div class="watch-condition"><strong>Criticità bloccanti</strong>${detailsList(d.fatal_concerns)}</div>`:''}
  ${d.invalidation_conditions?.length?html`<details class="gate"><summary>${icon('clock')}Quando rivalutare</summary>${detailsList(d.invalidation_conditions.map(reassessmentText))}</details>`:''}<p class="decision-disclaimer">Stake espresso in unità di bankroll. Proposta relativa allo snapshot, senza esecuzione automatica.</p>`:html`<div class="empty-state"><span class="empty-icon">${icon('shield')}</span><p>La decisione apparirà al termine dei controlli su dati, valore, rischio e timing.</p></div>`}</div></div>
  <div class="side-meta">${icon('book')}<div><strong>${payload('betting_journal')?.recorded?'Traccia salvata nel Journal':'Una traccia per ogni analisi'}</strong><span>${state.run?`ID ${shortId(state.run.analysis_id)}`:'Snapshot ed evidenze consultabili'}</span></div><button class="text-button" data-page="journal" aria-label="Apri Journal">${icon('chevron')}</button></div>
  <div class="side-meta">${icon('layers')}<div><strong>System One · ${shadow?.enabled?'Shadow':'Disattivato'}</strong><span>${shadow?.enabled?'Osserva senza modificare la pipeline':'Pipeline deterministica indipendente'}</span></div><button class="text-button" data-page="system" aria-label="Apri stato sistema">${icon('chevron')}</button></div>`;
}
function stat(label,value,hint) {return html`<div class="panel large-stat"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-hint">${hint}</div></div>`;}
function journalPage() {
  const items=journalItems(); const view=state.listViews.journal; view.page=Math.min(view.page,Math.max(0,Math.ceil(items.length/listPageSize)-1));
  return html`${heading('Memoria del processo','Betting Journal','Riapri le analisi, le fonti e le ragioni di ogni decisione.')}<div class="view-grid">${stat('Analisi caricate',state.history.length,'Analisi caricate dallo storico')}${stat('Decisioni BET',state.history.filter(r=>r.status==='BET').length,'Proposte, non giocate eseguite')}${stat('Decisioni NO BET',state.history.filter(r=>r.status==='NO_BET').length,'Anche non giocare è una decisione')}</div><div class="panel"><div class="table-toolbar"><label class="search-field" for="history-search">${icon('search')}<input id="history-search" placeholder="Cerca partita o ID analisi" value="${state.search}" type="search"></label><label class="sr-only" for="history-filter">Filtra per decisione</label><select class="filter-input" id="history-filter">${options([['all','Tutte le decisioni'],...Object.entries(statuses).map(([id,s])=>[id,s.label])],state.filter)}</select></div><div class="table-toolbar"><button class="btn small" data-action="journal-select" ${state.journalBusy?'disabled':''}>Seleziona visibili</button><button class="btn small" data-action="journal-clear" ${state.journalBusy?'disabled':''}>Deseleziona</button><button class="btn small" data-action="journal-remove" ${!state.journalSelection.size||state.journalBusy?'disabled':''}>Rimuovi dall'elenco (${state.journalSelection.size})</button></div><p class="panel-footnote">La rimozione conserva gli snapshot per audit e replay e non modifica la Banca.</p>${items.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Seleziona</th><th>Partita</th><th>Snapshot</th><th>Decisione</th><th>Quota</th><th>Stake</th><th><span class="sr-only">Apri</span></th></tr></thead><tbody>${pageRows(items,view).map(r=>html`<tr><td><input type="checkbox" data-journal-id="${r.analysis_id}" aria-label="Seleziona ${r.home_team} - ${r.away_team}, ${shortId(r.analysis_id)}" ${state.journalSelection.has(r.analysis_id)?'checked':''} ${state.journalBusy?'disabled':''}></td><td><strong>${r.home_team} — ${r.away_team}</strong><span class="secondary">${competitionName(r.competition)}${r.is_demo?' · DEMO':''} · ${shortId(r.analysis_id)}</span></td><td>${date(r.as_of)}</td><td>${badge(r.status)}</td><td class="numeric">${number(r.current_odds)}</td><td class="numeric">${money(r.stake?.amount)}</td><td><button class="btn small" data-run="${r.analysis_id}">Apri ${icon('arrow')}</button></td></tr>`)}</tbody></table></div>`:empty('Nessuna analisi da mostrare','Avvia un’analisi oppure modifica i filtri di ricerca.','book')}${listPager(items,view,'journal')}${state.journalHasMore?html`<button class="btn small" data-action="journal-more" ${state.journalBusy?'disabled':''}>Carica analisi precedenti</button>`:''}<p class="panel-footnote">Le analisi sono snapshot conservati. Le decisioni non attestano l’esecuzione di una giocata o un risultato economico.</p></div>`;
}
function timingValidationPanel(){return '';}
function modelsPage() {
  const model=state.models.find(m=>m.model===state.model); const validations=(payload('model_validator')?.validations || []).filter(v=>v.model_id===state.model);
  return html`${heading('Misurare prima di fidarsi','Modelli & validazione','Stato operativo e valutazione indipendente delle stime.')}${timingValidationPanel()}${diagnosticsPanel(state.modelDiagnostics,state.diagnosticPages || {})}<div class="model-layout"><div class="model-menu">${state.models.map(m=>html`<button class="model-button ${m.model===state.model?'active':''}" data-model="${m.model}"><strong>${m.model.replaceAll('_',' ')}</strong><span>${m.operational?'Disponibile alla pipeline':'Baseline quantitativa'}</span></button>`)}</div><div class="stack"><div class="panel"><div class="panel-title"><h2>${model?.model || 'Nessun modello'}</h2><span class="badge neutral">${model?.operational?'OPERATIVO':'NON AMMESSO'}</span></div><p class="subtext">${model?.operational?'L’ammissione di ogni mercato dipende dalla validazione storica e dalla qualità dei dati.':'Baseline implementata, esclusa dalle decisioni operative fino a una validazione adeguata.'}</p><p class="panel-footnote">Il Model Validator lavora separatamente dal modello predittivo.</p></div><div class="panel"><div class="panel-title"><h2>Validazione nello snapshot</h2><span class="caption">${demoRun()?'Storico sintetico':'Analisi selezionata'}</span></div>${validations.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Mercato</th><th>Stato</th><th>Campione</th><th>Brier</th><th>Log loss</th></tr></thead><tbody>${validations.map(v=>html`<tr><td>${marketName(v.market)}</td><td>${v.validation_status}</td><td>${v.sample_size}</td><td>${number(v.brier_score,3)}</td><td>${number(v.log_loss,3)}</td></tr>`)}</tbody></table></div>${detailsList(validations.map(v=>v.reason))}`:empty('Nessuna validazione nello snapshot','Apri un’analisi che includa questo modello per consultare le metriche.')}</div></div></div>`;
}
function apiMoney(value,currency='USD') {
  if(value==null)return '—';
  return new Intl.NumberFormat('it-IT',{style:'currency',currency,minimumFractionDigits:2,maximumFractionDigits:6}).format(Number(value));
}
function bytes(value) {
  const amount=Number(value || 0); const units=['B','KB','MB','GB']; let index=0; let current=amount;
  while(current>=1024&&index<units.length-1){current/=1024;index++;}
  return `${number(current,index?2:0)} ${units[index]}`;
}
function multiplesPipelinePanel(report){
 const r=report.automatic_multiples;if(!r)return '';
 const refresh=(r.analysis_refresh_status||[]).length?html`<details class="gate"><summary>Aggiornamento candidate scadute</summary>${r.analysis_refresh_status.map(row=>html`<p><strong>${({CLAIM:'Avviato',DONE:'Completato',FAILED:'Fallito'})[row.kind]||row.kind}</strong> - ${row.match_id} - ${date(row.at)}<br>${row.reason||'ANALYSIS_REFRESH_IN_PROGRESS'}${row.analysis_id?html` - <a href="#analysis/${row.analysis_id}">Apri analisi aggiornata</a>`:''}</p>`)}</details>`:'';

 return html`<section class="panel"><h2>Giocate e preparazione automatiche</h2><p>${r.note}</p><p>Stato: ${r.paused?'SOSPESA':r.enabled?'ATTIVA':'DISATTIVATA'} · errori tecnici consecutivi ${r.execution_status?.consecutive_errors || 0}/3</p><p>Assegnato ${r.allocated_eur} EUR / ${r.budget_eur || '50.00'} - residuo ${r.unallocated_eur} EUR</p>${r.execution_status?.message?html`<p>${r.execution_status.message} · ${date(r.execution_status.at)}</p>`:''}${(r.errors||[]).map(e=>html`<p class="warning">${e.diagnostic?.code || ''} ${e.reason || e.diagnostic?.message || 'Errore registrato'}</p>`)}${refresh}<a href="/salabet#candidates">Scelta e preparazione in SalaBet</a></section>`;
}
function timingPipelinePanel(report){
  const t=report.timing_comparison;if(!t)return '';
  return html`<section class="panel"><div class="panel-title"><h2>Automazione Serie A · T−24</h2><span class="badge neutral">${t.enabled?'ATTIVI':'DISATTIVATI'}</span></div><p>T−24 · tolleranza 10 minuti · nessun recupero retroattivo. Ricerca OpenAI ${t.paid_research?'entro i massimali configurati':'solo da cache'}.</p><p>${t.captures} acquisizioni · ${t.calls} chiamate aggiuntive · ${t.cost} costo registrato</p>${t.failed.length?html`<ul class="detail-list">${t.failed.map(r=>html`<li>${r.match_id} · ${r.checkpoint} · ${r.diagnostic?.code}: ${r.diagnostic?.message}</li>`)}</ul>`:'Nessun errore registrato nelle analisi automatiche.'}</section>`;
}
function dataPipelinePage() {
  const report=state.dataPipeline;
  if(state.dataPipelineBusy&&!report)return html`${heading('Raccolta e memoria','Data pipeline','Controllo centrale di storico, checkpoint, copertura e spazio.')}<div class="panel busy-state"><span class="spinner" aria-hidden="true"></span><strong>Lettura della pipeline</strong></div>`;
  if(!report)return html`${heading('Raccolta e memoria','Data pipeline','Controllo centrale di storico, checkpoint, copertura e spazio.')}<div class="panel">${empty('Report non disponibile','Aggiorna lo stato della pipeline per leggere database e archivi.')}</div>`;
  const cohortView=state.listViews.cohort, maturityView=state.listViews.maturity;
  const cohortRows=(report.validation_cohort?.matches || []).filter(row=>{const done=['VALIDATION_INCLUDED','VALIDATION_EXCLUDED'].includes(row.validation_inclusion_status);return (cohortView.mode==='all'||(cohortView.mode==='completed'?done:!done))&&`${row.match} ${row.better_match_id} ${row.source_888_match_id} ${row.provider_match_id}`.toLowerCase().includes(cohortView.search.toLowerCase());});
  const maturityRows=(report.maturity?.rows || []).filter(row=>{const done=row.stages.includes('SETTLED');return (maturityView.mode==='all'||(maturityView.mode==='completed'?done:!done))&&`${row.match} ${row.match_id}`.toLowerCase().includes(maturityView.search.toLowerCase());});
  cohortView.page=Math.min(cohortView.page,Math.max(0,Math.ceil(cohortRows.length/listPageSize)-1));
  maturityView.page=Math.min(maturityView.page,Math.max(0,Math.ceil(maturityRows.length/listPageSize)-1));
  const storage=report.storage; const maturity=report.maturity; const agents=report.agent_coverage.agents || []; const pending=report.settlement?.pending || []; const measured=report.validation.measured_groups || []; const cohort=report.validation_cohort || {}; const cohortGroups=cohort.groups || [];
  return html`${heading('Raccolta e memoria','Data pipeline','Partite selezionate, storico point-in-time, validazione e limiti di archiviazione.',html`<div class="table-toolbar"><button class="btn" data-action="pipeline-collect" ${state.dataPipelineBusy?'disabled':''}>${icon('pulse')}Raccogli checkpoint dovuti</button><button class="btn" data-action="pipeline-refresh" ${state.dataPipelineBusy?'disabled':''}>${icon('refresh')}${state.dataPipelineBusy?'Aggiornamento…':'Aggiorna stato'}</button></div>`)}
  ${timingPipelinePanel(report)}${multiplesPipelinePanel(report)}<section class="panel"><div class="panel-title"><h2>Collector</h2><span class="badge ${report.collector?.healthy?'positive':'negative'}">${report.collector?.status || 'NON VERIFICATO'}</span></div><p>Ultimo avvio: ${date(report.collector?.started_at)} · Ultima conclusione: ${date(report.collector?.completed_at)} · Heartbeat: ${report.collector?.heartbeat_age_seconds==null?'non disponibile':number(report.collector.heartbeat_age_seconds,0)+' s fa'} · Errori: ${report.collector?.error_count ?? '—'}</p>${(report.collector?.errors||[]).slice(0,5).map(item=>{const d=item.diagnostic||item.error||item;return html`<p class="panel-footnote">${d.code||item.reason||'ERRORE'} · ${d.provider||'fonte non identificata'} · ${d.match_id||item.match_id||'ciclo'} · ${d.checkpoint||d.stage||'—'}: ${d.message||'Consulta il dettaglio della coorte.'}</p>`;})}</section>
  <div class="note-bar"><strong>${report.status}</strong> · ${report.selections.active} partite selezionate attive · raccolta automatica ${report.policy.auto_collect?'ogni '+report.policy.poll_seconds+' secondi':'disattivata'} · percorso ${storage.root} · generato ${date(report.generated_at)}. I record nel database sono append-only; la retention riguarda soltanto raw e cache.</div>
  <div class="view-grid">${stat('Partite nello storico',maturity.matches,`${maturity.validation_ready} pronte per validazione`)}${stat('Snapshot point-in-time',report.checkpoints.total,'Acquisizioni pre-match immutabili')}${stat('Spazio usato',bytes(storage.bytes),`limite ${number(report.policy.max_storage_gb,1)} GB`)}</div>
  <div class="panel mt-18"><div class="panel-title"><h2>Validation Cohort</h2><span class="caption">${cohort.source_type || 'LIVE_FORWARD_VALIDATION'} · checkpoint ${cohort.canonical_checkpoint || 'T6'}${cohort.fallback_checkpoint?' → fallback '+cohort.fallback_checkpoint:''}</span></div><div class="view-grid">${stat('Match iscritti',cohort.enrolled || 0,'Competizioni con ID verificato')}${stat('Prediction canoniche',cohort.canonical_predictions || 0,'Una distribuzione per match e mercato')}${stat('Risultati in attesa',cohort.results_pending || 0,`${cohort.results_verified || 0} verificati`)}${stat('Osservazioni valide',cohort.valid_observations || 0,'Solo live forward settled')}</div>${cohortGroups.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Modello / mercato</th><th>Progresso</th><th>Qualità modello</th><th>Performance betting</th></tr></thead><tbody>${cohortGroups.map(row=>html`<tr><td><strong>${row.model_name} ${row.model_version}</strong><span class="secondary">${marketName(row.market)} · feature ${row.feature_schema_version}</span></td><td><strong>${row.progress}</strong><span class="secondary">${row.progress_status} · ${row.validation_status}</span></td><td>Brier ${number(row.model_quality.brier_score,3)} · Log Loss ${number(row.model_quality.log_loss,3)}<span class="secondary">Calibrazione ${number(row.model_quality.calibration,3)}</span></td><td>ROI ${percent(row.betting_performance.roi)} · Yield ${percent(row.betting_performance.yield)}<span class="secondary">CLV ${percent(row.betting_performance.clv)} · Drawdown ${percent(row.betting_performance.max_drawdown)} · ${row.betting_performance.number_of_qualifying_bets} qualificanti</span></td></tr>`)}</tbody></table></div>`:empty('Campione live forward in raccolta','La coorte creerà prediction canoniche al checkpoint e le includerà dopo un risultato verificato.')}<p class="panel-footnote">Qualità probabilistica e performance betting restano metriche separate. A 199 osservazioni il Validator resta bloccato; il requisito è ${cohort.minimum_sample || 200} per modello, versione e mercato.</p></div>
  <div class="panel mt-18"><div class="panel-title"><h2>Identità e risultati</h2><span class="caption">Risoluzione deterministica · fonti Tier A/B</span></div><div class="view-grid">${stat('Identità risolte',cohort.result_identity?.resolved || 0,'Mapping verificati')}${stat('Ambigue',cohort.result_identity?.ambiguous || 0,'Nessun settlement automatico')}${stat('Non trovate',cohort.result_identity?.not_found || 0,'Restano in attesa')}${stat('Conflitti',cohort.result_identity?.conflict || 0,'Richiedono verifica')}</div></div>
  <div class="panel mt-18"><div class="panel-title"><h2>Dettaglio match della coorte</h2><span class="caption">${(cohort.matches || []).length} partite</span></div>${pipelineListControls('cohort',cohortView)}${cohortRows.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Partita</th><th>Identità risultato</th><th>Risultato</th><th>Settlement / validazione</th></tr></thead><tbody>${pageRows(cohortRows,cohortView).map(row=>html`<tr><td><strong>${row.match}</strong><span class="secondary">${row.competition} · Bettor ${row.better_match_id || row.match_id} · 888 ${row.source_888_match_id || '—'}</span></td><td>${row.provider_match_id || 'Non risolta'}<span class="secondary">${row.competition_mapping || '—'} · ${row.home_mapping || '—'} / ${row.away_mapping || '—'} · ${row.resolution_method || '—'} ${row.resolution_confidence ?? ''}</span></td><td>${row.result_status || 'PENDING'}<span class="secondary">${row.result_provider || '—'} · Tier ${row.result_source_tier || '—'} · ${row.regulation_score?.join('–') || '—'} · ${date(row.result_timestamp)}</span><span class="secondary">${row.result_source || 'fonte in attesa'}</span></td><td>${row.settlement_status || 'PENDING'}<span class="secondary">${row.validation_inclusion_status || 'PENDING'} · ${row.lifecycle_status}</span><span class="secondary">${row.reason}${row.diagnostic?` - ${row.diagnostic.provider || 'fonte non identificata'} - ${row.diagnostic.checkpoint || row.diagnostic.stage || ''}: ${row.diagnostic.message}`:''}</span></td></tr>`)}</tbody></table></div>`:empty('Nessun match nella coorte','Il collector iscriverà automaticamente le prossime partite delle competizioni verificate.')}${listPager(cohortRows,cohortView,'cohort')}</div>
  <div class="split"><div class="panel"><div class="panel-title"><h2>Checkpoint quote e dati</h2><span class="caption">Solo partite selezionate</span></div><dl class="definition-list">${report.policy.checkpoints.map(name=>html`<dt>${name}</dt><dd>${report.checkpoints.by_checkpoint[name] || 0}</dd>`)}</dl><p class="panel-footnote">EARLY, 24h, 6h, 1h e closing sono finestre temporali. LINEUP sarà conteggiato quando viene acquisito uno snapshot ufficiale dedicato.</p></div><div class="panel"><div class="panel-title"><h2>Storage control</h2><span class="badge ${storage.within_limit?'positive':'negative'}">${storage.within_limit?'ENTRO LIMITE':'LIMITE SUPERATO'}</span></div><dl class="definition-list">${Object.entries(storage.categories).map(([name,item])=>html`<dt>${name.replaceAll('_',' ')}</dt><dd>${bytes(item.bytes)} · ${item.files} file</dd>`)}</dl><p class="panel-footnote">Retention: raw ${report.policy.raw_retention_days} giorni · cache ${report.policy.cache_retention_days} giorni. Candidati attuali: ${storage.retention_candidates.files} file, ${bytes(storage.retention_candidates.bytes)}. Nessun database viene eliminato.</p></div></div>
  <div class="panel mt-18"><div class="panel-title"><h2>Maturità delle partite</h2><span class="caption">${maturity.validation_ready}/${maturity.matches} validation-ready</span></div>${pipelineListControls('maturity',maturityView)}${maturityRows.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Partita</th><th>Fasi realmente presenti</th><th>Validazione</th></tr></thead><tbody>${pageRows(maturityRows,maturityView).map(row=>html`<tr><td><strong>${row.match}</strong><span class="secondary">${row.match_id}</span></td><td>${row.stages.join(' → ')}</td><td><span class="badge ${row.validation_ready?'positive':'neutral'}">${row.validation_ready?'PRONTA':'IN RACCOLTA'}</span></td></tr>`)}</tbody></table></div>`:empty('Nessuna partita raccolta','Le partite analizzate inizieranno a popolare la pipeline.')}${listPager(maturityRows,maturityView,'maturity')}</div>
  <div class="panel mt-18"><div class="panel-title"><h2>Risultati da registrare</h2><span class="caption">${pending.length} partite concluse senza esito</span></div>${pending.length?html`<div class="bank-form"><label>Partita<select id="result-match">${pending.map(item=>html`<option value="${item.match_id}">${item.match}</option>`)}</select></label><label>Gol casa<input id="result-home" type="number" min="0" max="50" value="0"></label><label>Gol trasferta<input id="result-away" type="number" min="0" max="50" value="0"></label><label>Fonte ufficiale<input id="result-source" type="url" placeholder="https://…"></label><label>Editore<input id="result-publisher" value="Lega / competizione"></label><label>Tier<select id="result-tier"><option value="A">A · ufficiale</option><option value="B">B · affidabile</option></select></label><button class="btn primary" data-action="record-result" ${state.dataPipelineBusy?'disabled':''}>Registra risultato</button></div><p class="panel-footnote">Il risultato è append-only, deve provenire da una fonte A/B e viene accettato solo almeno 90 minuti dopo il kickoff. Correzioni incompatibili vengono rifiutate.</p>`:empty('Nessun risultato in attesa','Le partite concluse e analizzate compariranno qui.')}</div>
  <div class="panel mt-18"><div class="panel-title"><h2>Copertura dei 14 agenti</h2><span class="caption">${report.agent_coverage.analysis_runs} analisi</span></div><div class="table-wrap"><table class="table"><thead><tr><th>Agente</th><th class="numeric">Eseguito</th><th class="numeric">Output utilizzabile</th><th class="numeric">Copertura</th><th class="numeric">Evidenze</th></tr></thead><tbody>${agents.map(agent=>html`<tr><td><strong>${agentNames[agent.agent_id] || agent.agent_id}</strong><span class="secondary">${agent.agent_id}</span></td><td class="numeric">${agent.runs}</td><td class="numeric">${agent.usable}</td><td class="numeric">${percent(agent.coverage)}</td><td class="numeric">${agent.evidence}</td></tr>`)}</tbody></table></div><p class="panel-footnote">SUCCESS e PARTIAL indicano che l’agente ha prodotto un contratto leggibile. Non equivalgono a consenso né autorizzano una scommessa.</p></div>
  <div class="panel mt-18"><div class="panel-title"><h2>Validazione delle probabilità</h2><span class="caption">${report.validation.settled_predictions || 0} probabilità concluse</span></div>${measured.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Modello / mercato</th><th>Stato</th><th class="numeric">Campione</th><th class="numeric">Brier</th><th class="numeric">Log loss</th><th class="numeric">Calibrazione</th></tr></thead><tbody>${measured.map(row=>html`<tr><td><strong>${row.model_id}</strong><span class="secondary">${row.market} · ${row.model_version}</span></td><td>${row.status}</td><td class="numeric">${row.sample_size}</td><td class="numeric">${number(row.brier_score,3)}</td><td class="numeric">${number(row.log_loss,3)}</td><td class="numeric">${number(row.calibration_error,3)}</td></tr>`)}</tbody></table></div>`:empty('Nessuna validazione storica disponibile','Servono probabilità pre-match ed esiti acquisiti successivamente senza leakage.')}<p class="panel-footnote">${report.validation.note}</p></div>`;
}
function apiControlTabs() {
  return html`<div class="tabs provider-tabs" role="tablist" aria-label="Provider API">${[['openai','OpenAI'],['api_football','API-Football'],['football_data_org','football-data.org']].map(([id,label])=>html`<button class="tab ${state.apiControlTab===id?'active':''}" data-api-control-tab="${id}" role="tab" aria-selected="${state.apiControlTab===id}">${label}</button>`)}</div>`;
}
function footballDataOrgPanel() {
  const source=state.workspace?.football_data_org;
  const configured=source?.configured??state.workspace?.free_football?.football_data_org_configured;
  return html`<section class="panel"><div class="panel-title"><h2>football-data.org</h2><span class="badge neutral">${!configured?'CHIAVE MANCANTE':source?.acquired_at?'DATI ACQUISITI':'DA VERIFICARE'}</span></div><dl class="definition-list"><dt>Configurazione</dt><dd>${configured?'Chiave configurata':'Inserire FOOTBALL_DATA_ORG_API_KEY nel file .env'}</dd><dt>Competizione</dt><dd>Serie A · SA / 2019</dd><dt>Ultima acquisizione riuscita</dt><dd>${source?.acquired_at?date(source.acquired_at):'Non ancora registrata'}</dd><dt>Dati</dt><dd>Risultati finali e classifiche</dd><dt>Classifica locale</dt><dd>${source?.acquired_at?'Disponibile nel database':'Non ancora acquisita'}</dd><dt>Piano e richieste residue</dt><dd>Non rilevati</dd></dl><p class="panel-footnote">L’acquisizione registrata non certifica lo stato attuale delle credenziali. Questa schermata legge i dati locali senza consumare richieste API. Serie B e Serie C non verificate.</p></section>${freeFootballPanel()}`;
}
function apiFootballProviderError(source) {
  const failedRequest=(source?.recent_requests || []).some(item=>Number(item.http_status || 0)>=400);
  const providerFailure=/chiave|quota|rate limit|richiesta respinta|non raggiungibile|tempo di risposta|risposta .* non valida/i.test(source?.last_error || '');
  return failedRequest||providerFailure;
}
function providerSummary() {
  const openAIConfigurationErrors=[state.workspace?.football_data_research?.configuration_error,state.workspace?.external_research?.configuration_error].filter(Boolean);
  const openAILiveFailure=Boolean(state.apiControl?.refresh_error)||/failed|401|unauthorized|forbidden/i.test(state.apiControl?.warning || '');
  const openAIOk=Boolean(state.apiControl)&&openAIConfigurationErrors.length===0&&!openAILiveFailure;
  const football=state.workspace?.api_football; const footballOk=Boolean(football?.configured&&!apiFootballProviderError(football)&&football.account?.active!==false&&football.coverage?.accessible!==false);
  const footballLimited=football?.network_enabled!==false&&football?.account?.active===true&&football?.coverage?.accessible===false;
  const footballUnknown=football?.configured&&football?.network_enabled!==false&&!football?.account&&!(football?.recent_requests || []).some(r=>r.http_status===200);
  const data=state.workspace?.football_data_org;
  const dataConfigured=data?.configured??state.workspace?.free_football?.football_data_org_configured;
  const dataVerified=Boolean(dataConfigured&&data?.acquired_at);
  return html`<div class="provider-summary" aria-label="Stato provider"><div><span>OpenAI</span><strong class="${openAIOk?'provider-ok':'provider-error'}">${openAIOk?'OK':'ERROR'}</strong></div><div><span>API-Football</span><strong class="${football?.network_enabled===false||footballLimited||footballUnknown?'':footballOk?'provider-ok':'provider-error'}">${football?.network_enabled===false?'DISATTIVATA':footballLimited?'LIMITATA':footballUnknown?'DA VERIFICARE':footballOk?'OK':'ERROR'}</strong></div><div><span>football-data.org</span><strong class="${dataVerified?'provider-ok':dataConfigured?'':'provider-error'}" title="${dataVerified?`Ultima acquisizione riuscita: ${date(data.acquired_at)}. Non è un controllo live.`:'Nessuna acquisizione verificata'}">${dataVerified?'VERIFICATA':dataConfigured?'DA VERIFICARE':'NON CONFIGURATA'}</strong></div></div>`;
}
function freeFootballPanel() {
  const source=state.workspace?.free_football;
  if(!source)return '';
  const last=source.last_refresh;
  return html`<section class="panel"><div class="panel-title"><h2>Archivio e fonti gratuite</h2><span class="badge neutral">Serie A / Serie B</span></div><dl class="definition-list"><dt>Prima fonte</dt><dd>Archivio locale, con fonte e data di acquisizione</dd><dt>football-data.org Serie A</dt><dd>${source.football_data_org_configured?'Chiave configurata; esito verificabile nel collector':'Chiave mancante: FOOTBALL_DATA_ORG_API_KEY'}</dd><dt>Dati aperti automatici</dt><dd>OpenFootball CC0: ${source.openfootball_divisions.join(', ')}</dd><dt>CSV locali configurati</dt><dd>${source.csv_files}</dd><dt>Ultimo aggiornamento</dt><dd>${last?date(last.at):'Non ancora eseguito'}</dd><dt>Richieste ultimo ciclo</dt><dd>${last?.requests??'Non disponibili'}</dd></dl><p>Risultati automatici da fonte ufficiale o football-data.org. I dati della comunit? da soli non chiudono le schedine. Serie B: storico locale e import CSV; copertura automatica completa non disponibile.</p>${last?.issues?.length?html`<ul>${last.issues.map(i=>html`<li>${i.source}: ${i.status}</li>`)}</ul>`:''}<p class="panel-footnote">Serie C esclusa dalla raccolta operativa. OpenAI resta dedicata ai dati pre-match mancanti.</p></section>`;
}
function apiFootballControlPanel() {
  if(state.workspace?.api_football?.network_enabled===false)return html`${freeFootballPanel()}<div class="note-bar mt-18">API-Football: chiamate di rete disattivate. Lo storico acquisito resta disponibile localmente.</div>`;
  const source=state.workspace?.api_football; const account=source?.account; const coverage=source?.coverage; const requests=source?.recent_requests || [];
  const cacheHits=requests.filter(item=>item.cache_hit).length; const rateLimits=requests.filter(item=>item.http_status===429).length;
  const remaining=account?.daily_limit!=null&&account?.requests_today!=null?Math.max(0,Number(account.daily_limit)-Number(account.requests_today)):null;
  if(!source)return html`<div class="panel">${empty('API-Football non configurata','Il provider non è collegato al workspace corrente.')}</div>`;
  const successfulRequest=requests.some(item=>item.http_status===200||item.cache_hit); const providerError=apiFootballProviderError(source); const apiActive=account?.active===true||(account?.active!==false&&successfulRequest);
  return html`<div class="note-bar"><strong>${source.configured?'Provider configurato':'Chiave non configurata'}</strong> · stato API ${providerError?'ERRORE':apiActive?'ATTIVO':account?.active===false?'NON ATTIVO':'DA VERIFICARE'}${providerError?html`<br>${source.last_error}`:''}</div>
  <div class="view-grid">${stat('Piano',account?.plan || 'Non rilevato',account?.active===true?'Abbonamento attivo':'Eseguire la verifica accesso')}${stat('Limite giornaliero',account?.daily_limit==null?'—':number(account.daily_limit,0),account?.requests_today==null?'Utilizzo provider non rilevato':`${number(account.requests_today,0)} utilizzate · ${remaining==null?'—':number(remaining,0)} rimanenti`)}${stat('Richieste processo',number(source.requests_made_today,0),`Budget locale ${number(source.daily_request_budget,0)}`)}</div>
  <div class="split"><section class="panel"><div class="panel-title"><h2>Accesso e coverage</h2><button class="btn small" data-action="check-source" ${state.apiFootballBusy?'disabled':''}>${state.apiFootballBusy?'Verifica in corso…':'Aggiorna API-Football'}</button></div><dl class="definition-list"><dt>Season verificata</dt><dd>${coverage?.season || '—'}</dd><dt>Accesso season</dt><dd>${coverage?coverage.accessible?'ACCESSIBILE':'NON ACCESSIBILE':state.apiFootballBusy?'VERIFICA IN CORSO':'NON VERIFICATO'}</dd><dt>League verificata</dt><dd>${coverage?.league || '—'}</dd><dt>Fixture rilevate</dt><dd>${coverage?.fixture_count ?? '—'}</dd><dt>Competizioni configurate</dt><dd>${source.mapped_competitions ?? 0}</dd><dt>Squadre associate</dt><dd>${source.mapped_teams ?? 0}</dd><dt>Stagioni bloccate</dt><dd>${source.blocked_seasons?.length?source.blocked_seasons.join(', '):'Nessuna'}</dd><dt>Avviso dati / mapping</dt><dd>${!providerError&&source.last_error?source.last_error:'Nessuno'}</dd></dl><p class="panel-footnote">La copertura riflette esclusivamente i controlli realmente disponibili nel backend.</p></section>
  <section class="panel"><div class="panel-title"><h2>Cache e richieste</h2><span class="caption">Ultime ${requests.length}</span></div><dl class="definition-list"><dt>Cache hit</dt><dd>${cacheHits}</dd><dt>Richieste rete</dt><dd>${requests.length-cacheHits}</dd><dt>Rate limit</dt><dd>${rateLimits}</dd><dt>Ultimo errore provider</dt><dd>${providerError?source.last_error:'Nessuno'}</dd></dl>${requests.length?html`<div class="provider-request-list">${requests.slice(-6).reverse().map(item=>html`<div><strong>${item.endpoint}</strong><span>${item.cache_hit?'CACHE':`HTTP ${item.http_status ?? '—'}`} · ${item.result_count ?? '—'} risultati</span></div>`)}</div>`:''}</section></div>`;
}
function apiFiveRowPage(kind, source){
 const rows=[...(source || [])].sort((a,b)=>(Date.parse(b.date)||0)-(Date.parse(a.date)||0));
 const pages=Math.max(1,Math.ceil(rows.length/5));
 const page=Math.max(0,Math.min(state.apiTablePages[kind] || 0,pages-1));state.apiTablePages[kind]=page;
 const start=page*5;
 return {rows:rows.slice(start,start+5),caption:`${rows.length?start+1:0}-${Math.min(start+5,rows.length)} di ${rows.length} - massimo 5 per pagina`,pager:html`<div class="table-toolbar mt-18"><button class="btn small" data-action="api-table-prev" data-table="${kind}" ${page===0?'disabled':''}>Precedenti</button><span class="caption">Pagina ${page+1} di ${pages}</span><button class="btn small" data-action="api-table-next" data-table="${kind}" ${page>=pages-1?'disabled':''}>Successive</button></div>`};
}
function apiControlPage() {
  const top=html`${heading('Monitoraggio provider','API Control','Stato, copertura e consumo dei servizi esterni.')}${providerSummary()}${apiControlTabs()}`;
  if(state.apiControlTab==='api_football')return html`${top}${apiFootballControlPanel()}`;
  if(state.apiControlTab==='football_data_org')return html`${top}${footballDataOrgPanel()}`;
  const report=state.apiControl;
  if(state.apiControlBusy&&!report)return html`${top}<div class="panel busy-state"><span class="spinner" aria-hidden="true"></span><strong>Aggiornamento consumi OpenAI</strong></div>`;
  if(!report)return html`${top}<div class="panel">${empty('Dati OpenAI non disponibili','Premi Aggiorna dati per rileggere gli export o interrogare le API OpenAI.')}<div class="empty-action"><button class="btn primary" data-action="api-control-refresh">Aggiorna dati</button></div></div>`;
  const activityPage=apiFiveRowPage('activity',report.activity);const costsPage=apiFiveRowPage('costs',report.costs);
  const s=report.summary || {}; const callPageSize=10; const callCount=report.calls?.length || 0; const callPages=Math.max(1,Math.ceil(callCount/callPageSize)); state.apiCallPage=Math.min(state.apiCallPage,callPages-1); const callStart=state.apiCallPage*callPageSize; const visibleCalls=(report.calls || []).slice(callStart,callStart+callPageSize);
  const dataAgent=state.workspace?.football_data_research; const contextAgent=state.workspace?.external_research;
  const callErrors=(report.calls || []).filter(call=>/ERROR|FAILED|UNAUTHORIZED|401/i.test(String(call.status))).length;
  return html`${top}
  <div class="note-bar"><strong>${report.source==='OPENAI_LIVE'?'Dati live OpenAI':'Export locale'}</strong> · aggiornamento ${date(report.updated_at)} · periodo ${date(report.period_start)} – ${date(report.period_end)}.${report.warning?html`<br>${report.warning}`:''}</div>
  <div class="view-grid">${stat('Richieste',number(s.requests,0),'Chiamate modello e strumenti registrate')}${stat('Token totali',number(s.total_tokens,0),`${number(s.input_tokens,0)} input · ${number(s.output_tokens,0)} output`)}${stat('Costo del periodo',apiMoney(s.cost_eur,'EUR'),'Spesa del periodo indicato, convertita in euro')}<section class="panel"><span class="mini-label">Credito residuo OpenAI</span><h2>Non disponibile via API</h2><p class="caption">Il costo del periodo non permette di ricavare il saldo del portafoglio.</p><a class="btn small" href="https://platform.openai.com/settings/organization/billing/overview" target="_blank" rel="noopener noreferrer">Apri credito OpenAI</a></section></div>
  <div class="panel"><div class="panel-title"><h2>Saved agents</h2><button class="btn small" data-action="api-control-refresh" ${state.apiControlBusy?'disabled':''}>${icon('refresh')}Aggiorna OpenAI</button></div><dl class="definition-list"><dt>Football Data Research</dt><dd>${dataAgent?.agent_id || dataAgent?.provider || (dataAgent?.enabled?'Configurato':'Non configurato')}</dd><dt>External Research</dt><dd>${contextAgent?.agent_id || contextAgent?.provider || (contextAgent?.enabled?'Configurato':'Non configurato')}</dd><dt>Chiamate storiche</dt><dd>${callErrors?`${callErrors} errori registrati`:'Nessun errore registrato'}</dd><dt>Stato amministrativo</dt><dd>${report.refresh_error?report.refresh_error.code:report.admin_key_configured?'Chiave configurata':'Solo export locale'}</dd><dt>Budget / raccolta live</dt><dd>${report.refresh_error?'REFRESH FALLITO':report.live_enabled?'ATTIVA':'DISATTIVA'}</dd></dl></div>
  <div class="panel"><div class="panel-title"><h2>Attività per chiamata</h2><span class="caption">${callCount?`${callStart+1}–${Math.min(callStart+callPageSize,callCount)} di ${callCount}`:'0 chiamate'} · massimo 10 per pagina</span></div>${callCount?html`<div class="table-wrap"><table class="table"><thead><tr><th>Quando / partita</th><th>Ricerca</th><th>Sessione</th><th class="numeric">Web / tool</th><th class="numeric">Token</th><th class="numeric">Durata</th><th class="numeric">Costo €</th></tr></thead><tbody>${visibleCalls.map(call=>html`<tr><td><strong>${call.match}</strong><span class="secondary">${date(call.completed_at)} · ${call.competition || call.match_id}</span></td><td>${call.research_type==='FOOTBALL_DATA_RESEARCH'?'Dati calcistici':'Contesto pre-match'}<span class="secondary">${call.status} · ${(call.fields || []).join(', ') || 'campi non registrati'}</span></td><td><span class="monospace">${call.session_id || call.response_id || '—'}</span></td><td class="numeric">${call.search_calls} / ${call.tool_calls}</td><td class="numeric">${number(call.input_tokens+call.output_tokens,0)}</td><td class="numeric">${call.duration_ms==null?'—':number(call.duration_ms,0)+' ms'}</td><td class="numeric"><strong>${call.cost_eur==null?'Non attribuibile':apiMoney(call.cost_eur,'EUR')}</strong><span class="secondary">${call.cost_status==='ESTIMATED'?'stima Bettor':'usage assente nella risposta'}</span></td></tr>`)}</tbody></table></div><div class="table-toolbar mt-18"><button class="btn small" data-action="api-calls-prev" ${state.apiCallPage===0?'disabled':''}>Precedenti</button><span class="caption">Pagina ${state.apiCallPage+1} di ${callPages}</span><button class="btn small" data-action="api-calls-next" ${state.apiCallPage>=callPages-1?'disabled':''}>Successive</button></div>`:empty('Nessuna chiamata locale registrata','Le prossime ricerche per partita appariranno qui con sessione, utilizzo e costo disponibile.')}
  <p class="panel-footnote">Il costo per partita è mostrato solo quando la singola risposta API include usage/costo. Il totale OpenAI non viene ripartito artificialmente tra le partite.</p></div>
  <div class="panel"><div class="panel-title"><h2>Attività per modello</h2><span class="caption">${activityPage.caption}</span></div>${report.activity.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Data</th><th>Modello</th><th>Origine</th><th class="numeric">Richieste</th><th class="numeric">Input</th><th class="numeric">Cache letta</th><th class="numeric">Cache scritta</th><th class="numeric">Output</th><th class="numeric">Totale</th></tr></thead><tbody>${activityPage.rows.map(row=>html`<tr><td>${date(row.date)}</td><td><strong>${row.model}</strong><span class="secondary">${row.service_tier}</span></td><td>${row.api_source}</td><td class="numeric">${number(row.requests,0)}</td><td class="numeric">${number(row.input_tokens,0)}</td><td class="numeric">${number(row.cached_input_tokens,0)}</td><td class="numeric">${number(row.cache_write_tokens,0)}</td><td class="numeric">${number(row.output_tokens,0)}</td><td class="numeric"><strong>${number(row.total_tokens,0)}</strong></td></tr>`)}</tbody></table></div>${activityPage.pager}`:empty('Nessuna attività nel periodo','L’export o la risposta live non contiene chiamate completions/Agents.')}</div>
  <div class="panel mt-18"><div class="panel-title"><h2>Costi contabilizzati</h2><span class="caption">Fonte finanziaria - ${costsPage.caption}</span></div>${report.costs.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Data</th><th>Voce</th><th>Origine</th><th>Progetto</th><th class="numeric">Importo €</th></tr></thead><tbody>${costsPage.rows.map(row=>html`<tr><td>${date(row.date)}</td><td><strong>${row.line_item}</strong></td><td>${row.api_source}</td><td>${row.project}</td><td class="numeric"><strong>${apiMoney(row.amount_eur,'EUR')}</strong></td></tr>`)}</tbody></table></div>${costsPage.pager}`:empty('Nessun costo nel periodo','Il Costs endpoint/export non contiene importi.')}
  <p class="panel-footnote">Il totale costi proviene dai dati contabili OpenAI ed è convertito al cambio USD→EUR ${report.usd_eur_rate || 'non disponibile'} (${report.eur_rate_source}). Token e costi possono avere tempi di consolidamento diversi.</p></div>`;
}
function systemPage() {
  const tab=['settings','backup','updates'].includes(state.systemTab)?state.systemTab:'architecture';
  return html`${heading('Configurazione e trasparenza','Sistema','Architettura e impostazioni di Bettor.')}<div class="tabs" role="tablist" aria-label="Sezioni sistema"><button class="tab ${tab==='architecture'?'active':''}" data-system-tab="architecture">${icon('layers')}Architettura</button><button class="tab ${tab==='settings'?'active':''}" data-system-tab="settings">${icon('sliders')}Impostazioni</button><button class="tab ${tab==='backup'?'active':''}" data-system-tab="backup">${icon('database')}Backup</button><button class="tab ${tab==='updates'?'active':''}" data-system-tab="updates">${icon('download')}Aggiornamenti</button></div>${tab==='settings'?settingsPanel():['backup','updates'].includes(tab)?html`<div id="system-tools">Caricamento…</div>`:systemArchitecturePage()}`;
}
function agentsPage(){
  const tab=state.page==='api_control'?'api_control':state.systemTab==='ai'?'ai':'agents';
  return html`${heading('Motori di analisi','Agenti','Agenti deterministici e servizi AI di ricerca.')}<div class="tabs" role="tablist" aria-label="Sezioni agenti"><button class="tab ${tab==='agents'?'active':''}" data-system-tab="agents">${icon('chip')}14 agenti</button><button class="tab ${tab==='ai'?'active':''}" data-system-tab="ai">${icon('chip')}Agenti AI</button><button class="tab ${tab==='api_control'?'active':''}" data-system-tab="api_control">${icon('plug')}API Control</button></div>${tab==='api_control'?apiControlPage():tab==='ai'?systemAIAgentsPage():systemAgentsPage()}`;
}
async function loadAIAgents() {
  if(state.aiAgentsBusy)return;
  state.aiAgentsBusy=true;state.error=null;render();
  try{state.aiAgents=await api('/system/ai-agents');}
  catch(error){state.error=error.message;}
  finally{state.aiAgentsBusy=false;render();}
}
function systemAIAgentsPage() {
  const report=state.aiAgents;
  const labels={injuries:'Infortuni',suspensions:'Squalifiche',unavailable_players:'Indisponibili',doubtful_players:'Giocatori in dubbio',returned_players:'Rientri',probable_lineups:'Formazioni probabili',confirmed_lineups:'Formazioni ufficiali',squad_callups:'Convocati',manager_press_conferences:'Conferenze stampa',turnover:'Turnover',manager_changes:'Cambio allenatore',fixture_congestion:'Impegni ravvicinati',relevant_match_news:'Notizie sulla partita',travel_context:'Trasferte',weather:'Meteo',pitch_playing_conditions:'Condizioni del campo',contextual_information:'Contesto',league_table:'Classifica',recent_form:'Ultime partite',home_away_splits:'Rendimento casa / trasferta',shots:'Tiri',shots_on_target:'Tiri in porta',possession:'Possesso',clean_sheets:'Porte inviolate',btts:'Entrambe a segno',over_under_2_5:'Over / Under 2,5',current_player_stats:'Statistiche giocatori'};
  const cards=(report?.agents||[]).map(agent=>{
    const last=agent.latest;
    const failed=/ERROR|FAILED|TIMEOUT|RATE_LIMIT|BUDGET_EXCEEDED/.test(last?.status||'');
    const outcome=!last?'NESSUNA ESECUZIONE REGISTRATA':last.status==='OK'?'ULTIMO ESITO OK':last.status==='RESEARCH_NOT_FOUND'?'DATI NON TROVATI':failed?'ULTIMA RICERCA FALLITA':last.status;
    const fields=Array.isArray(agent.fields)?agent.fields:Object.entries(agent.fields||{}).filter(([,policy])=>policy.enabled).map(([field])=>field);
    return html`<section class="panel"><div class="panel-title"><h2>${agent.name}</h2><span class="badge ${agent.enabled&&!agent.configured?'negative':'neutral'}">${!agent.enabled?'DISABILITATO':agent.configured?'CONFIGURATO':'CONFIGURAZIONE INCOMPLETA'}</span></div><p>${agent.role}</p>
    <dl class="definition-list"><dt>Agent ID caricato</dt><dd class="monospace">${agent.agent_id||'Non configurato'}</dd><dt>Variabile .env</dt><dd>${agent.env_variable}</dd><dt>Collegamento</dt><dd>${agent.transport||'Non disponibile'}</dd><dt>Budget massimo per ricerca</dt><dd>${agent.budget_per_match_usd==null?'Non disponibile':apiMoney(agent.budget_per_match_usd,'USD')}</dd><dt>Ricerche registrate per questo ID</dt><dd>${agent.recorded_runs}</dd></dl>
    ${agent.configuration_error?html`<p class="error">${agent.configuration_error}</p>`:''}
    <div class="panel-title mt-18"><h3>Ultimo esito registrato</h3><span class="badge ${failed?'negative':last?.status==='OK'?'positive':'neutral'}">${outcome}</span></div>
    ${last?html`<dl class="definition-list"><dt>Quando</dt><dd>${date(last.completed_at)}</dd><dt>Partita</dt><dd>${last.match}</dd><dt>Esito tecnico</dt><dd>${last.status}</dd><dt>Campi con evidenze</dt><dd>${last.resolved_fields}</dd><dt>Origine</dt><dd>${last.cached?'Cache riutilizzata':'Ricerca registrata'}</dd><dt>Costo stimato della ricerca</dt><dd>${last.estimated_cost_usd==null?'Non disponibile':apiMoney(last.estimated_cost_usd,'USD')}</dd></dl>${last.error?html`<p class="error">${last.error}</p>`:''}${last.status==='RESEARCH_NOT_FOUND'?html`<p class="panel-footnote">La ricerca non ha fornito i dati richiesti. Questo esito, da solo, non indica un errore di autenticazione.</p>`:''}`:html`<p class="panel-footnote">La configurazione è presente solo se indicata sopra. Il funzionamento non è ancora verificato da una ricerca registrata per questo ID.</p>`}
    <details class="mt-18"><summary>Informazioni ricercate e cache</summary><ul class="detail-list">${fields.map(field=>html`<li>${labels[field]||field}${!Array.isArray(agent.fields)&&agent.fields[field]?.ttl_seconds?` · cache ${number(agent.fields[field].ttl_seconds/60,0)} min`:''}</li>`)}</ul>${agent.cache_ttl_seconds?html`<p>Cache forma recente: ${number(agent.cache_ttl_seconds/60,0)} minuti.</p>`:''}${agent.supported_competitions?.length?html`<p>Competizioni: ${agent.supported_competitions.join(', ')}.</p>`:''}</details>
    </section>`;
  });
  return html`<div class="panel"><div class="panel-title"><h2>Saved agent OpenAI di Bettor</h2><button class="btn small" data-action="ai-agents-refresh" ${state.aiAgentsBusy?'disabled':''}>${state.aiAgentsBusy?'Aggiornamento…':'Aggiorna stato'}</button></div><p>Fonti e date verificabili, rispetto del momento pre-match e riuso della cache. Le formazioni ufficiali richiedono una fonte ufficiale o equivalente. Probabilità, quote, EV e stake restano fuori dal compito di questi agenti.</p><p class="panel-footnote">Stato ricavato dalla configurazione e dagli esiti salvati; non è una verifica live delle credenziali. Aggiorna stato non avvia ricerche OpenAI. ${report?`Lettura registro: ${date(report.checked_at)}.`:''}</p><button class="btn small" data-page="api_control">Apri API Control</button></div>${report?html`<div class="split mt-18">${cards}</div>`:html`<div class="panel">${empty(state.aiAgentsBusy?'Lettura degli agenti AI':'Stato non disponibile','Configurazione e ricerche vengono lette dal backend locale.')}</div>`}`;
}
function agentSourceLinks(id) {
  const result=state.run?.results?.[id]; const sources=[];
  for(const provenance of result?.provenance || [])if(safeURL(provenance.url))sources.push({url:safeURL(provenance.url),label:provenance.source_id || provenance.url,tier:provenance.reliability_tier,field:provenance.field});
  for(const evidence of result?.evidence || [])for(const provenance of evidence.provenance || [])if(safeURL(provenance.url))sources.push({url:safeURL(provenance.url),label:provenance.source_id || provenance.url,tier:provenance.reliability_tier,field:provenance.field});
  return [...new Map(sources.map(source=>[source.url,source])).values()];
}
function systemAgentsPage() {
  const agents=state.workspace?.agents || []; const selected=agents.find(agent=>agent.id===state.systemAgent) || agents[0];
  if(!selected)return html`<div class="panel">${empty('Registro agenti non disponibile','Aggiorna il Workspace per caricare il catalogo.')}</div>`;
  const result=state.run?.results?.[selected.id]; const sources=agentSourceLinks(selected.id);
  return html`<div class="model-layout"><div class="model-menu">${agents.map(agent=>html`<button class="model-button ${agent.id===selected.id?'active':''}" data-system-agent="${agent.id}"><strong>${agentNames[agent.id] || agent.id}</strong><span>${agent.separate_workflow?'Workflow separato':agent.enabled?'Attivo':'Disabilitato'}</span></button>`)}</div><div class="stack">
  <div class="panel"><div class="panel-title"><div><span class="eyebrow">${selected.id}</span><h2>${agentNames[selected.id] || selected.id}</h2></div><span class="badge ${selected.enabled?'positive':'neutral'}">${selected.enabled?'ATTIVO':'DISABILITATO'}</span></div><p>${selected.responsibility}</p><dl class="definition-list mt-18"><dt>Ruolo nella pipeline</dt><dd>${selected.separate_workflow?'Separato dal pronostico':'Pipeline principale'}</dd><dt>Criticità</dt><dd>${selected.critical?'Critico':'Non critico'}</dd><dt>Dipendenze</dt><dd>${selected.dependencies.length?selected.dependencies.map(id=>agentNames[id] || id).join(', '):'Nessuna'}</dd><dt>Strumenti autorizzati</dt><dd>${selected.allowed_tools.length?selected.allowed_tools.join(', '):'Nessuno'}</dd><dt>Output</dt><dd>${selected.output}</dd></dl></div>
  <div class="panel"><div class="panel-title"><h2>Dati letti</h2><span class="caption">Contratto dell’agente</span></div><ul class="detail-list">${selected.data_inputs.map(item=>html`<li>${item}</li>`)}</ul><p class="panel-footnote">Gli agenti leggono snapshot normalizzati e risultati upstream. Non aprono liberamente siti né chiamano direttamente OpenAI.</p></div>
  <div class="panel"><div class="panel-title"><h2>Fonti effettive e link</h2><span class="caption">${state.run?'Analisi selezionata':'Nessuna analisi selezionata'}</span></div>${sources.length?html`<div class="evidence-list">${sources.map(source=>html`<div class="evidence-row"><div class="evidence-text"><strong>${source.label}</strong><div class="evidence-meta"><span>Tier ${source.tier || '—'}</span><span>${source.field || 'campo non dichiarato'}</span></div></div><a class="btn small" href="${source.url}" target="_blank" rel="noopener noreferrer">Apri fonte ${icon('external')}</a></div>`)}</div>`:empty('Nessun link nello snapshot corrente',state.run?'L’agente non ha ricevuto una fonte URL diretta oppure il dato è derivato da risultati upstream/locali.':'Apri un’analisi dal Journal per vedere le fonti realmente usate.')}</div>
  <div class="panel"><div class="panel-title"><h2>Ultimo risultato selezionato</h2><span class="caption">${result?.status || 'NON ESEGUITO'}</span></div>${result?html`<p>${result.summary}</p><dl class="definition-list mt-18"><dt>Analisi disponibile</dt><dd>${result.analysis_available?'SÌ':'NO'}</dd><dt>Idoneità operativa</dt><dd>${result.operationally_eligible?'SÌ':'NO'}</dd><dt>Qualità dati</dt><dd>${percent(result.data_quality?.score)}</dd><dt>Confidenza segnale</dt><dd>${percent(result.signal?.confidence)}</dd><dt>Evidenze</dt><dd>${result.evidence?.length || 0}</dd><dt>Limitazioni</dt><dd>${result.degradations?.length || 0}</dd></dl>${degradationNotes(result.degradations)}`:empty('Nessun output disponibile','Questo agente non è presente nell’analisi selezionata.')}</div></div></div>`;
}
function systemArchitecturePage() {
  const s=state.health?.system_one; const metrics=state.metrics?.pipeline || state.metrics || {};
  return html`<div class="system-grid"><div class="panel system-block"><div class="system-number">0</div><span class="eyebrow">Deterministico</span><h2>System Zero</h2><p>Probabilità implicite, de-vigging, fair odds, EV, Kelly, esposizioni e decision gates.</p><span class="badge positive">${icon('check')}MOTORE LOCALE</span></div><div class="panel system-block"><div class="system-number">1</div><span class="eyebrow">Osservazione semantica</span><h2>System One</h2><p>${s?.enabled?'Il provider osserva e suggerisce routing, consistenza ed escalation. Non modifica la pipeline.':'Disattivato. La pipeline continua a funzionare senza un provider System One.'}</p><span class="badge neutral">${s?.enabled?'SHADOW MODE':'DISATTIVATO'}</span></div><div class="panel system-block"><div class="system-number">2</div><span class="eyebrow">Reasoning complesso</span><h2>System Two</h2><p>Reasoning strutturato per approfondimenti. I calcoli e i controlli finanziari restano deterministici.</p><span class="badge neutral">${state.workspace?.system_two_enabled?'ABILITATO':'DISATTIVATO'}</span></div></div><div class="split mt-18"><div class="panel"><div class="panel-title"><h2>Configurazione corrente</h2>${icon('pulse')}</div><dl class="definition-list"><dt>Backend</dt><dd>${state.health?.status || 'Non disponibile'}</dd><dt>Versione</dt><dd>${state.health?.version || '—'}</dd><dt>Origine dati</dt><dd>${state.workspace?.is_demo?'Sintetica / demo':'Provider configurati'}</dd><dt>Agenti registrati</dt><dd>${state.workspace?.agents?.length ?? '—'}</dd><dt>Provider System One</dt><dd>${s?.provider || '—'}</dd><dt>Esecuzione scommesse</dt><dd>Non prevista</dd></dl></div><div class="panel"><div class="panel-title"><h2>Decisioni in questa sessione server</h2>${icon('layers')}</div>${Object.keys(metrics.decision_counts || {}).length?html`<dl class="definition-list">${Object.entries(metrics.decision_counts).map(([k,v])=>html`<dt>${statusInfo(k).label}</dt><dd>${v}</dd>`)}</dl>`:empty('Nessuna metrica di sessione','I contatori si popolano eseguendo la pipeline.')}</div></div><div class="panel mt-18"><div class="panel-title"><h2>Osservazioni shadow</h2><span class="caption">Analisi selezionata</span></div>${state.shadow.length?html`<div class="table-wrap"><table class="table"><thead><tr><th>Fase</th><th>Provider</th><th>Modello</th><th>Latenza</th><th>Errore</th></tr></thead><tbody>${state.shadow.map(r=>html`<tr><td>${r.stage}</td><td>${r.system_one_provider}</td><td>${r.model_id}</td><td>${number(r.latency_ms,0)} ms</td><td>${r.error || '—'}</td></tr>`)}</tbody></table></div>`:empty('Nessuna osservazione registrata',s?.enabled?'Seleziona un’analisi con osservazioni shadow.':'System One è disattivato nella configurazione corrente.','eye')}</div>`;
}
function openEvidence(id) {
  const e=evidence().find(item=>item.evidence_id===id); if (!e) return;
  mount(dialog,html`<div class="dialog-header"><div><span class="eyebrow">Evidenza e provenienza</span><h2 id="dialog-title">Dalla fonte alla decisione</h2></div><button class="icon-button" data-action="close-dialog" aria-label="Chiudi evidenza">${icon('close')}</button></div><p class="dialog-claim">${e.claim}</p><p class="caption mb-18">${agentNames[e.agent_id] || e.agent_id} · ${e.type} · confidenza ${percent(e.confidence)}</p>${e.provenance.map(p=>html`<article class="source-card"><h3>${p.source_id}</h3><dl><dt>Tipo / tier</dt><dd>${p.source_type} · ${p.reliability_tier}</dd><dt>Raccolta</dt><dd>${date(p.retrieved_at)}</dd><dt>Pubblicazione</dt><dd>${date(p.published_at)}</dd><dt>Campo</dt><dd>${p.field || '—'}</dd><dt>Fonte primaria</dt><dd>${p.primary_source_id || '—'}</dd><dt>Riferimento</dt><dd>${p.raw_reference || '—'}</dd><dt>Note</dt><dd>${p.notes || '—'}</dd></dl>${safeURL(p.url)?html`<a href="${safeURL(p.url)}" target="_blank" rel="noopener noreferrer">Apri fonte ${icon('external')}</a>`:''}</article>`)}${e.support?.length?html`<h3 class="mini-label mt-18">Supporto</h3>${detailsList(e.support)}`:''}${e.derived_from?.length?html`<h3 class="mini-label mt-18">Evidenze di origine</h3>${e.derived_from.map(id=>html`<p><button class="text-button" data-evidence="${id}">${id}</button></p>`)}`:''}<p class="monospace mt-18">${e.evidence_id}</p>`);
  if (!dialog.open) dialog.showModal();
}
async function loadApiControl() {
  if(state.apiControlBusy)return;
  state.apiControlBusy=true;state.error=null;render();
  try{state.apiControl=await api('/api-control');}
  catch(error){state.error=error.message;}
  finally{state.apiControlBusy=false;render();}
}
async function loadApiFootball(force=false) {
  if(state.apiFootballBusy)return;
  const current=state.workspace?.api_football;
  if(!force&&current?.account&&current?.coverage)return;
  state.apiFootballBusy=true;state.error=null;render();
  try{state.workspace.api_football=await api('/sources/api-football/check',{});}
  catch(error){state.error=error.message;}
  finally{state.apiFootballBusy=false;render();}
}
async function loadDataPipeline() {
  if(state.dataPipelineBusy)return;
  state.dataPipelineBusy=true;state.error=null;render();
  try{state.dataPipeline=await api('/data-pipeline');}
  catch(error){state.error=error.message;}
  finally{state.dataPipelineBusy=false;render();}
}
async function collectDuePipeline() {
  if(state.dataPipelineBusy)return;
  state.dataPipelineBusy=true;state.error=null;render();
  try{const result=await api('/data-pipeline/collect-due',{});toast(`Checkpoint raccolti: ${result.captured.length}; già presenti: ${result.skipped.length}.`);state.dataPipeline=await api('/data-pipeline');}
  catch(error){state.error=error.message;}
  finally{state.dataPipelineBusy=false;render();}
}
async function recordPipelineResult() {
  if(state.dataPipelineBusy)return;
  const match_id=document.getElementById('result-match')?.value; const home_score=Number(document.getElementById('result-home')?.value); const away_score=Number(document.getElementById('result-away')?.value); const source_url=document.getElementById('result-source')?.value?.trim(); const publisher=document.getElementById('result-publisher')?.value?.trim(); const source_tier=document.getElementById('result-tier')?.value;
  if(!match_id||!Number.isInteger(home_score)||!Number.isInteger(away_score)||!source_url||!publisher){state.error='Inserisci risultato, fonte URL ed editore.';render();return;}
  state.dataPipelineBusy=true;state.error=null;render();
  try{const result=await api('/data-pipeline/results',{match_id,home_score,away_score,source_url,publisher,source_tier});toast(`Risultato registrato · ${result.settled_predictions} probabilità concluse.`);state.dataPipeline=await api('/data-pipeline');}
  catch(error){state.error=error.message;}
  finally{state.dataPipelineBusy=false;render();}
}
async function refresh() {
  const endpoints={health:'/health',workspace:'/workspace',matches:'/matches',history:'/analyses?last=50',models:'/models',metrics:'/metrics',bank:'/bank',salabetCandidates:'/salabet/candidates',salabetSlips:'/salabet/slips',dataPipeline:'/data-pipeline'};
  const results=await Promise.allSettled(Object.values(endpoints).map(path=>api(path)));
  const failures=[];
  Object.keys(endpoints).forEach((key,i)=>{if(results[i].status==='fulfilled') state[key]=results[i].value; else {if(key==='health')state.health=null; failures.push(`${key}: ${results[i].reason.message}`);}});
  const slipResult=results[Object.keys(endpoints).indexOf('salabetSlips')];
  state.salabetSlipsLoaded=slipResult.status==='fulfilled' || state.salabetSlipsLoaded;
  state.salabetSlipsError=slipResult.status!=='fulfilled';
  if(state.workspace?.fixture_source==='888sport_it'){
    try{state.workspace=await api('/workspace');}catch(error){failures.push(error.message);}
  }
  state.journalHasMore=state.history.length===50;
  setCompetitionNames(state.workspace?.competition_names);
  state.error=failures.length?failures.join(' · '):null;
  const available=analysisMatches();
  if(!state.selectedMatch||(!state.run&&!available.some(match=>match.match_id===state.selectedMatch)))state.selectedMatch=available[0]?.match_id || '';
  render();
}
async function loadRun(id) {
  if (state.busy) return;
  state.busy=true; state.page='analysis'; state.error=null; render();
  try {const run=await api(`/analysis/${encodeURIComponent(id)}`); state.run=run; state.competition='all'; state.selectedMatch=run.match_id; clearTimeout(researchTimer);researchController?.abort();researchGeneration++;state.matchResearch=null;state.researchLoading=false;state.researchError=null; state.shadow=[]; state.shadow=await api(`/analysis/${encodeURIComponent(id)}/shadow`); location.hash=`analysis/${id}`; announce('Analisi salvata caricata.');}
  catch(error){state.error=error.message;}
  finally{state.busy=false;render();}
}
async function analyze() {
  if(state.busy || !state.selectedMatch)return;
  state.busy=true;state.run=null;state.shadow=[];state.error=null;render();announce('Analisi in corso.');
  try {
    const markets=state.market==='all'?state.workspace.supported_markets:[state.market];
    state.run=await api(state.workspace.is_demo?'/demo/analysis':'/analysis',state.workspace.is_demo?{scenario:state.scenario,markets}:{match_id:state.selectedMatch,markets});
    await refresh(); state.shadow=await api(`/analysis/${state.run.analysis_id}/shadow`);
    history.replaceState(null,'',`#analysis/${state.run.analysis_id}`); toast(`Analisi completata: ${statusInfo(state.run.decision.status).label}.`);
  } catch(error){state.error=error.message;announce(`Analisi non completata. ${error.message}`);}
  finally{state.busy=false;scheduleResearch();render();}
}
async function runBatch(matchIds,mode,refresh=false){
  if(state.batchBusy||!matchIds.length)return;
  state.batchBusy=true;state.error=null;state.batch={total:matchIds.length,analyzed:0,completed:0,excluded:0,candidates:0,transferred:0,rows:[]};render();announce('Analisi batch in corso.');
  try{
    state.batch=refresh?await api(`/batch-analysis/${encodeURIComponent(matchIds[0])}/refresh`,{}):await api('/batch-analysis',{mode,match_ids:matchIds,filter:{competition:state.competition}});
    state.salabetCandidates=await api('/salabet/candidates');
    toast(`Batch: ${state.batch.total} partite richieste, ${state.batch.completed} analisi completate, ${state.batch.failed || 0} errori. Candidate trasferite a SalaBet: ${state.batch.transferred}. Consulta gli esiti di ogni partita nel riepilogo.`);
  }catch(error){state.error=error.message;announce(`Batch non completato. ${error.message}`);}
  finally{state.batchBusy=false;render();}
}
document.addEventListener('click',async event=>{
  const target=event.target.closest('button'); if(!target)return;
  if(target.dataset.diagnosticPage){state.diagnosticPages ||= {};const key=target.dataset.diagnosticPage;state.diagnosticPages[key]=Math.max(0,(state.diagnosticPages[key]||0)+Number(target.dataset.step));render();return;}
  if(target.dataset.listMode){const view=state.listViews[target.dataset.listMode];view.mode=target.dataset.mode;view.page=0;render();return;}
  if(target.dataset.listPage){const view=state.listViews[target.dataset.listPage];view.page=Math.max(0,view.page+Number(target.dataset.step));render();return;}
  if(target.dataset.page==='portfolio'){location.href='/salabet#bank';return;}
  if(target.dataset.page){state.page=target.dataset.page;if(state.page==='system'&&!['architecture','settings','backup','updates'].includes(state.systemTab))state.systemTab='architecture';if(state.page==='agents'&&!['agents','ai'].includes(state.systemTab))state.systemTab='agents';location.hash=state.page;render();if(state.page==='api_control')await loadApiControl();if(state.page==='pipeline')await loadDataPipeline();if(state.page==='models')await loadModelDiagnostics();if(state.page==='system'&&['backup','updates'].includes(state.systemTab))await loadSystemTools(state.systemTab);if(state.page==='system'&&state.systemTab==='settings')await loadSettings();return;}
  if(target.dataset.apiControlTab){state.apiControlTab=target.dataset.apiControlTab;render();if(state.apiControlTab==='api_football')await loadApiFootball();return;}
  if(target.dataset.tab){state.tab=target.dataset.tab;render();return;}
  if(target.dataset.run){await loadRun(target.dataset.run);return;}
  if(target.dataset.evidence){openEvidence(target.dataset.evidence);return;}
  if(target.dataset.model){state.model=target.dataset.model;render();return;}
  if(target.dataset.systemTab){state.systemTab=target.dataset.systemTab;state.page=state.systemTab==='api_control'?'api_control':['agents','ai'].includes(state.systemTab)?'agents':'system';location.hash=(state.page==='api_control'?'agents':state.page)+'/'+state.systemTab;render();if(state.systemTab==='ai')await loadAIAgents();if(state.systemTab==='settings')await loadSettings();if(['backup','updates'].includes(state.systemTab))await loadSystemTools(state.systemTab);if(state.systemTab==='api_control')await loadApiControl();return;}
  if(target.dataset.systemAgent){state.systemAgent=target.dataset.systemAgent;render();return;}
  const action=target.dataset.action;
  if(action==='placement-review'){await togglePlacementAutomation(true);return;}
  if(action==='placement-approve'){await approvePlacement();return;}
  if(action==='placement-review-cancel'){state.placementReview=null;render();return;}
  if(action==='placement-automation-toggle'){await togglePlacementAutomation();return;}
  if(action==='automation-toggle'){await toggleAutomation();return;}
  if(action==='ai-agents-refresh'){await loadAIAgents();return;}
  if(action==='send-indicative'){
    if(state.betBusy||!state.run||!target.dataset.prediction)return;
    state.betBusy=true;state.error=null;render();
    try{
      await api('/salabet/candidates/indicative',{analysis_id:state.run.analysis_id,prediction_id:target.dataset.prediction,market:target.dataset.market,selection:target.dataset.selection});
      state.salabetCandidates=await api('/salabet/candidates');
      toast('Pronostico inviato a SalaBet · Da valutare. Nessuna puntata piazzata.');
      location.href='/salabet#candidates';
    }catch(error){state.error=error.message;}
    finally{state.betBusy=false;render();}
    return;
  }
  if(action==='add-bet'){
    if(state.betBusy||!state.run)return;
    state.betBusy=true;state.error=null;render();
    try{
      await api('/salabet/candidates',{analysis_id:state.run.analysis_id,prediction_id:target.dataset.prediction,market:target.dataset.market,selection:target.dataset.selection});
      state.salabetCandidates=await api('/salabet/candidates');
      toast('Proposta inviata a SalaBet. Nessuna puntata è stata piazzata.');location.href='/salabet#candidates';
    }catch(error){state.error=error.message;}
    finally{state.betBusy=false;render();}
    return;
  }
  if(['api-table-prev','api-table-next'].includes(action)){const key=target.dataset.table;if(['activity','costs'].includes(key)){state.apiTablePages[key]=Math.max(0,state.apiTablePages[key]+(action==='api-table-next'?1:-1));render();}return;}
  if(action==='api-calls-prev'){state.apiCallPage=Math.max(0,state.apiCallPage-1);render();return;}
  if(action==='api-calls-next'){state.apiCallPage++;render();return;}
  if(action==='batch-all'){await runBatch(visibleMatches().map(match=>match.match_id),'ALL');return;}
  if(action==='batch-selection'){await runBatch([...state.batchSelection],'SELECTION');return;}
  if(action==='batch-refresh'&&state.batch?.batch_id){await runBatch([state.batch.batch_id],'REFRESH',true);return;}
  if(action==='journal-more'){
    if(state.journalBusy||!state.journalHasMore)return;
    state.journalBusy=true;render();
    try{const older=await api(`/analyses?last=50&offset=${state.history.length}`);state.history.push(...older);state.journalHasMore=older.length===50;}
    catch(error){state.error=error.message;}
    finally{state.journalBusy=false;render();}
    return;
  }
  if(action?.startsWith('journal-')){
    if(state.journalBusy)return;
    if(action==='journal-select')pageRows(journalItems(),state.listViews.journal).forEach(r=>state.journalSelection.add(r.analysis_id));
    if(action==='journal-clear')state.journalSelection.clear();
    if(action==='journal-remove'&&state.journalSelection.size){
      state.journalBusy=true;state.error=null;render();
      try{
        const result=await api('/analyses/remove',{analysis_ids:[...state.journalSelection]});
        state.history=state.history.filter(r=>!result.removed.includes(r.analysis_id));
        state.journalSelection.clear();
        toast(`${result.removed.length} analisi rimosse dall'elenco.`);
        state.history=await api('/analyses?last=50');state.journalHasMore=state.history.length===50;
      }catch(error){state.error=error.message;}
      finally{state.journalBusy=false;}
    }
    render();return;
  }
  if(action==='check-source'){
    await loadApiFootball(true);
  }
  if(action==='analyze')await analyze();
  if(action==='api-control-refresh')await loadApiControl();
  if(action==='pipeline-refresh')await loadDataPipeline();
  if(action==='pipeline-collect')await collectDuePipeline();
  if(action==='record-result')await recordPipelineResult();
  if(action==='refresh'){await refresh();if(state.page==='api_control')await loadApiControl();if(state.page==='models')await loadModelDiagnostics();toast(state.error?'Aggiornamento parziale: consulta gli errori.':'Workspace aggiornato.');}
  if(action==='dismiss'){state.error=null;render();}
  if(action==='close-dialog')dialog.close();
  if(action==='download'&&state.run){const url=URL.createObjectURL(new Blob([JSON.stringify(state.run,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`better-${state.run.analysis_id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Analisi esportata in JSON.');}
});
document.addEventListener('toggle',event=>{
  if(event.target.matches?.('.batch-picker')&&event.target.isConnected!==false){
    state.batchPickerOpen=event.target.open;
  }
},true);

document.addEventListener('change',event=>{
  const {id,value}=event.target;
  if(id==='placement-future-approval'){state.placementFuture=event.target.checked;render();return;}
  if(event.target.dataset?.journalId){
    if(!state.journalBusy){const key=event.target.dataset.journalId;event.target.checked?state.journalSelection.add(key):state.journalSelection.delete(key);}
    render();return;
  }
  if(event.target.dataset?.batchMatch){
    const key=event.target.dataset.batchMatch;
    event.target.checked?state.batchSelection.add(key):state.batchSelection.delete(key);
    state.batchPickerOpen=true;
    const button=document.getElementById('batch-selection-button');
    button.textContent=`Analizza selezione (${state.batchSelection.size})`;
    button.disabled=state.batchBusy||!state.batchSelection.size;
    return;
  }
  if(state.busy&&['match-select','scenario-select','market-select','competition-select'].includes(id)){render();return;}
  if(id==='competition-select'){state.competition=value;state.selectedMatch=visibleMatches()[0]?.match_id || '';state.run=null;state.shadow=[];}
  if(id==='match-select'){state.selectedMatch=value;state.run=null;state.shadow=[];}
  if(id==='scenario-select'){state.scenario=value;state.run=null;state.shadow=[];}
  if(id==='market-select'){state.market=value;state.run=null;state.shadow=[];}
  if(id==='history-filter'){state.filter=value;state.listViews.journal.page=0;}
  if(['match-select','competition-select'].includes(id))scheduleResearch();
  render();
});
document.addEventListener('input',event=>{if(event.target.id==='history-search'){state.search=event.target.value;state.listViews.journal.page=0;render();return;}if(event.target.dataset?.listSearch){const view=state.listViews[event.target.dataset.listSearch];view.search=event.target.value;view.page=0;render();}});
document.addEventListener('keydown',event=>{
  if(!event.target.matches('[role="tab"]')||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const keys=Object.keys(tabs);const position=keys.indexOf(state.tab);
  state.tab=keys[event.key==='Home'?0:event.key==='End'?keys.length-1:(position+(event.key==='ArrowRight'?1:-1)+keys.length)%keys.length];render();document.querySelector(`#tab-${state.tab}`).focus();
});
dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}});
window.addEventListener('hashchange',()=>{const [page,id]=location.hash.slice(1).split('/');if(page==='portfolio'){location.href='/salabet#bank';return;}if(page==='analysis'&&id&&id!==state.run?.analysis_id){void loadRun(id);}else if(pages[page]){state.page=page;if(page==='system')state.systemTab=['settings','backup','updates'].includes(id)?id:'architecture';if(page==='agents'){state.systemTab=id==='ai'?'ai':'agents';if(id==='api_control')state.page='api_control';}render();if(page==='system'&&state.systemTab==='settings')void loadSettings();if(page==='agents'&&state.systemTab==='ai')void loadAIAgents();if(page==='system'&&['backup','updates'].includes(state.systemTab))void loadSystemTools(state.systemTab);if(state.page==='api_control')void loadApiControl();if(page==='models')void loadModelDiagnostics();}});
await refresh();
const [initialPage,initialId]=location.hash.slice(1).split('/');
if(initialPage==='portfolio')location.href='/salabet#bank';
else if(initialPage==='analysis'&&initialId)await loadRun(initialId);else if(pages[initialPage]){state.page=initialPage;if(initialPage==='system')state.systemTab=['settings','backup','updates'].includes(initialId)?initialId:'architecture';if(initialPage==='agents'){state.systemTab=initialId==='ai'?'ai':'agents';if(initialId==='api_control')state.page='api_control';}render();if(initialPage==='system'&&state.systemTab==='settings')await loadSettings();if(initialPage==='system'&&['backup','updates'].includes(state.systemTab))await loadSystemTools(state.systemTab);if(initialPage==='agents'&&state.systemTab==='ai')await loadAIAgents();if(state.page==='api_control')await loadApiControl();if(state.page==='pipeline')await loadDataPipeline();if(state.page==='models')await loadModelDiagnostics();}

// Local countdown only; refresh the local SalaBet ledger once/minute while visible.
let reminderPolling=false;
let reminderLastRead=Date.now();
function updateBetReminder(){
 if(state.page!=='dashboard' || document.hidden)return;
 const target=document.querySelector('#pending-bet-reminder');
 if(target && !target.contains?.(document.activeElement))mount(target,betReminderCard(state.salabetSlips,{loaded:state.salabetSlipsLoaded,error:state.salabetSlipsError}));
}
async function refreshBetReminder(){
 if(reminderPolling || state.page!=='dashboard' || document.hidden)return;
 reminderPolling=true;reminderLastRead=Date.now();
 try{state.salabetSlips=await api('/salabet/slips');state.salabetSlipsLoaded=true;state.salabetSlipsError=false;}
 catch{state.salabetSlipsError=true;}
 finally{reminderPolling=false;updateBetReminder();}
}
if(typeof window.setInterval==='function')window.setInterval(()=>{
 updateBetReminder();
 if(Date.now()-reminderLastRead>=60000)void refreshBetReminder();
},1000);
document.addEventListener('visibilitychange',()=>{updateBetReminder();if(!document.hidden)void refreshBetReminder();});
window.addEventListener('focus',()=>{updateBetReminder();if(Date.now()-reminderLastRead>=60000)void refreshBetReminder();});
