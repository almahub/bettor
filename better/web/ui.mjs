// Provider text is always escaped. Only template literals authored in this module
// and dashboard.mjs can produce markup; there is no untrusted HTML escape hatch.
class Markup { constructor(value) { this.value = value; } }
export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const content = value => value instanceof Markup ? value.value : Array.isArray(value) ? value.map(content).join('') : escapeHTML(value);
export function html(strings, ...values) { return new Markup(strings.reduce((out, part, i) => out + part + (i < values.length ? content(values[i]) : ''), '')); }
export function mount(element, markup) { if (element) element.innerHTML = content(markup); }
export const number = (value, digits=2) => value == null || !Number.isFinite(Number(value)) ? '—' : new Intl.NumberFormat('it-IT', {minimumFractionDigits:digits,maximumFractionDigits:digits}).format(Number(value));
export const percent = (value, digits=1) => value == null || !Number.isFinite(Number(value)) ? '—' : `${number(Number(value)*100,digits)}%`;
export const points = value => value == null ? '—' : `${Number(value)>0?'+':''}${number(Number(value)*100,2)} pp`;
export const money = value => value == null ? '—' : `${number(value)} u`;
export const date = (value, timeOnly=false) => value && Number.isFinite(Date.parse(value)) ? new Intl.DateTimeFormat('it-IT', timeOnly ? {hour:'2-digit',minute:'2-digit'} : {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(value)) : '—';
export const initials = name => String(name || '?').trim().split(/\s+/).slice(0,2).map(n=>n[0]).join('').toUpperCase();
export const progress = value => Math.max(0,Math.min(100,Number(value || 0)*100));
export const shortId = id => String(id || '').slice(0,8);
export const marketName = market => ({'1X2':'Esito finale','OVER_UNDER_2_5':'Over / Under 2,5','BTTS':'Entrambe a segno','DNB':'Draw no bet','ASIAN_HANDICAP':'Asian handicap','TEAM_TOTALS':'Gol squadra'}[typeof market==='object'?market?.type:market] || market || '—');
export const selectionName = selection => ({HOME:'Casa',DRAW:'Pareggio',AWAY:'Trasferta',OVER_2_5:'Over 2,5',UNDER_2_5:'Under 2,5',YES:'Sì',NO:'No'}[selection] || selection || '—');
let competitionNames = {};
export const setCompetitionNames = names => {competitionNames = names || {};};
export const competitionName = value => value === 'SYNTHETIC_LEAGUE' ? 'Campionato dimostrativo' : competitionNames[value] || value;
export const isItalianLeagueName = value => /^(serie a|serie b|serie c(?:\s*[-–—:]?\s*)girone\s+[a-c])$/i.test(String(value || '').trim());
export const statuses = {
  BET:{label:'Opportunità verificata',short:'BET',tone:'positive',icon:'check',message:'La proposta supera i controlli previsti. La decisione resta legata a questo snapshot.'},
  NO_BET:{label:'Nessuna giocata',short:'NO BET',tone:'negative',icon:'shield',message:'Almeno un controllo non è soddisfatto. Non è prevista alcuna esposizione.'},
  WAIT_FOR_LINEUPS:{label:'Attendi le formazioni',short:'ATTESA XI',tone:'waiting',icon:'clock',message:'La formazione ufficiale può cambiare materialmente l’analisi.'},
  WAIT_FOR_BETTER_ODDS:{label:'Attendi una quota migliore',short:'ATTESA QUOTA',tone:'waiting',icon:'clock',message:'Il prezzo attuale non raggiunge la quota minima richiesta.'},
  WATCHLIST:{label:'Da monitorare',short:'WATCHLIST',tone:'watch',icon:'eye',message:'Esiste una condizione osservabile da verificare prima di procedere.'},
  INSUFFICIENT_DATA:{label:'Dati insufficienti',short:'DATI MANCANTI',tone:'neutral',icon:'alert',message:'Mancano informazioni critiche per formulare una decisione utilizzabile.'},
};
export const statusInfo = status => statuses[status] || {label:'Analisi da avviare',short:'DA ANALIZZARE',tone:'neutral',icon:'circle',message:'Avvia l’analisi per verificare dati, modello, prezzo e rischio.'};
export const agentNames = {football_data_analyst:'Dati e statistiche',football_odds_analyst:'Mercato quote',news_intelligence:'News & intelligence',lineup_player_impact:'Formazioni e giocatori',tactical_analyst:'Analisi tattica',predictive_model:'Modello predittivo',value_bettor:'Value analysis',market_selection:'Selezione mercati',matched_betting:'Matched betting',model_validator:'Validazione modelli',risk_manager:'Rischio e bankroll',devils_advocate:'Devil’s Advocate',professional_bettor:'Decision Engine',betting_journal:'Betting Journal'};
export const agentGroups = [
  {name:'01 · Raccolta',ids:['football_data_analyst','football_odds_analyst','news_intelligence','lineup_player_impact']},
  {name:'02 · Analisi',ids:['tactical_analyst','predictive_model','market_selection','value_bettor']},
  {name:'03 · Controllo',ids:['model_validator','risk_manager','devils_advocate']},
  {name:'04 · Decisione e traccia',ids:['professional_bettor','betting_journal']},
];
export const gateNames = {CONTEXT_INTEGRITY:'Identità e riferimento temporale',DATA_QUALITY:'Qualità dei dati',EVIDENCE_PROVENANCE:'Provenienza delle evidenze',MODEL_VALIDITY:'Validità del modello',MARKET:'Mercato supportato',NUMERICAL_CONSISTENCY:'Coerenza dei calcoli',PRICE_FRESHNESS:'Freschezza della quota',VALUE:'Edge e valore atteso',CONTRARIAN:'Verifica contraria',RISK:'Rischio ed esposizione',TIMING:'Timing e formazioni',CONFIDENCE:'Affidabilità dell’analisi'};
export function gateExplanation(gate, run) {
  if(gate.status==='PASS')return '';
  const d=run?.decision || {};
  const blocked=(d.degradations || []).some(x=>x.code==='INDICATIVE_PREDICTION_NOT_ADMITTED' && x.applies_to_decision);
  const messages={
    MODEL_VALIDITY:'Il modello non ha ancora superato la validazione operativa. Le probabilità restano indicative; nessuno stake operativo.',
    MARKET:blocked?'Il mercato può essere calcolato, ma non è ammesso operativamente perché il modello è solo indicativo.':'Nessun mercato soddisfa i controlli operativi.',
    NUMERICAL_CONSISTENCY:blocked?'Manca una previsione ammessa nell’ensemble operativo: il collegamento modello/valore non è verificabile. Questo esito non dimostra un errore aritmetico.':'Collegamento tra previsione, quota e calcoli non verificato.',
    PRICE_FRESHNESS:blocked?'Il controllo completo quota/modello non è superato. Una quota visualizzata non implica che il controllo operativo sia valido; vedere timestamp e traccia tecnica.':'Quota assente oppure verifica di completezza, corrispondenza o freschezza non superata.',
    VALUE:'Il valore atteso conservativo o la quota minima richiesta non superano i limiti. Un EV indicativo positivo da solo non basta.',
    RISK:(run?.results?.risk_manager?.payload?.reasons || []).includes('MODEL_VALIDATION_INSUFFICIENT')?'Stake zero: rischio bloccato dalla validazione insufficiente del modello. Non indica necessariamente un problema della Banca.':'Il controllo su stake, rischio o disponibilità del portafoglio non è superato.',
    CONFIDENCE:`La confidenza operativa è limitata da: ${Object.entries(d.confidence_components || {}).filter(([,v])=>v===0).map(([k])=>k).join(', ') || 'componenti sotto soglia'}. Non è la probabilità del risultato.`,
  };
  if(gate.gate==='TIMING'){
    const kickoff=run?.snapshot?.match?.kickoff;
    return kickoff && run?.as_of && Date.parse(kickoff)>Date.parse(run.as_of)
      ? 'Al momento dello snapshot la partita era futura: il controllo su freschezza, ufficialità o sensibilità delle formazioni non è superato.'
      : 'Verificare kickoff e disponibilità delle formazioni ufficiali rispetto alla data dello snapshot.';
  }
  return messages[gate.gate] || '';
}
export const observedMetric = (value, key) => value==null && ['xg_per_match','xga_per_match'].includes(key) ? 'Non disponibile dalla fonte' : number(value,key==='sample_size'?0:2);
export const reassessmentText = value => ({
  'Refresh price after TTL or material market movement':'Aggiornare le quote dopo la scadenza o un movimento significativo del mercato.',
  'Re-run on lineup, injury, model validation or portfolio change':'Rivalutare quando cambiano formazioni, indisponibilità, validazione del modello o portafoglio.',
  'Decision valid only for recorded as_of snapshot':'La decisione vale soltanto per lo snapshot e l’orario registrati.',
}[value] || value);
export const scenarios = {bet:'Opportunità verificata',no_bet:'Nessuna giocata',wait_lineups:'Attesa formazioni',wait_odds:'Attesa quota',watchlist:'Watchlist',missing_data:'Dati mancanti',stale_odds:'Quote scadute',suspended_model:'Modello sospeso',source_conflict:'Conflitto tra fonti',risk_limit:'Limite di rischio',news_offline:'News non disponibili',odds_offline:'Provider quote offline'};
export function diagnosticCauses(run) {
  if (!run) return [];
  const causes=[];
  const add=(code,label,detail)=>{if(!causes.some(c=>c.code===code))causes.push({code,label,detail});};
  const failures=run.input_manifest?.failures || {};
  const results=run.results || {};
  const rawForecast=(run.forecast?.markets || []).find(item=>item.method==='RAW_POISSON');
  const validator=results.model_validator?.payload?.validations || [];
  const observed=rawForecast?.validation_sample_size ?? (validator.length?Math.max(...validator.map(v=>Number(v.sample_size || 0))):0);
  const required=rawForecast?.validation_minimum ?? Number(run.input_manifest?.resolved_config?.min_sample || 0);
  const known=new Set(run.decision?.reason_codes || []);
  const failureList=Object.entries(failures);
  if(failureList.some(([,failure])=>failure?.source==='local_archive'))known.add('ARCHIVE_DATA_UNAVAILABLE');
  const failureText=failureList.map(([,failure])=>failure?.message || '').join(' ').toLowerCase();
  if(failureText.includes('identity_mapping_') || failureText.includes('identita squadre'))known.add('IDENTITY_MAPPING_MISSING');
  if(failureList.some(([,failure])=>failure?.source==='api_football' && ['TRANSIENT_PROVIDER_ERROR','PERMANENT_PROVIDER_ERROR','RATE_LIMIT','TIMEOUT','SCHEMA_ERROR'].includes(failure?.code)))known.add('API_PROVIDER_ERROR');
  if(failureText.includes('openai non disponibile: autenticazione fallita') || failureText.includes('openai_api_key non valida'))known.add('OPENAI_AUTH_ERROR');
  if(failureText.includes('no news provider configured'))known.add('NEWS_PROVIDER_NOT_CONFIGURED');
  if(failureText.includes('lineup_not_published_yet'))known.add('LINEUP_NOT_PUBLISHED_YET');
  if(results.football_data_analyst?.status==='PARTIAL')known.add('STATS_PARTIAL');
  if(results.predictive_model?.status==='INSUFFICIENT_DATA' || !results.predictive_model?.payload?.model_predictions?.length || rawForecast?.reason?.startsWith('MODEL_INPUT_'))known.add('MODEL_INPUT_INSUFFICIENT');
  if(results.model_validator?.status==='INSUFFICIENT_DATA' || validator.some(v=>v.validation_status==='INSUFFICIENT_SAMPLE'))known.add('MODEL_VALIDATION_INSUFFICIENT');
  if(failures.odds || !(results.football_odds_analyst?.payload?.quotes || []).length)known.add('QUOTE_MISSING');
  const findings=Object.values(run.snapshot?.enriched?.fields || {});
  const recordedResearch=run.input_manifest?.research_result || run.snapshot?.enriched?.external_research;
  const qualitativeFindings=recordedResearch?.findings || findings;
  if(qualitativeFindings.some(f=>['INSUFFICIENT_EVIDENCE','CONFLICT','STALE'].includes(f.status)))known.add('EVIDENCE_REJECTED');
  if(qualitativeFindings.some(f=>f.field==='confirmed_lineups' && f.status==='NOT_FOUND' && /not (yet )?(published|announced|available)|non ancora|not released/i.test(f.notes || '')))known.add('LINEUP_NOT_PUBLISHED_YET');
  if(results.tactical_analyst?.status==='INSUFFICIENT_DATA' && (results.tactical_analyst.errors || []).some(e=>String(e.message).includes('traits unavailable')))known.add('DATA_NOT_CONNECTED');
  if(!(results.market_selection?.payload?.supported_markets || []).length)known.add('MARKET_UNSUPPORTED');
  for(const research of [run.input_manifest?.football_data_research,run.input_manifest?.external_research,run.input_manifest?.football_data_result,run.input_manifest?.research_result,run.football_data_result,run.research_result])if(research?.status?.endsWith('_ERROR')){known.add('RESEARCH_FAILED');if(String(research?.error || '').includes('OPENAI_API_KEY'))known.add('OPENAI_AUTH_ERROR');}
  const authDetails=failureText+' '+[run.input_manifest?.research_result,run.input_manifest?.football_data_result,run.input_manifest?.external_research,run.input_manifest?.football_data_research].map(r=>r?.error || '').join(' ').toLowerCase();
  if(authDetails.includes('openai_key_expired')){known.add('OPENAI_KEY_EXPIRED');known.delete('OPENAI_AUTH_ERROR');}
  if(authDetails.includes('openai_permission_error'))known.add('OPENAI_PERMISSION_ERROR');
  const training=String(results.predictive_model?.unknowns || '').match(/minimum_training_sample:(\d+\/\d+)/i)?.[1];
  const definitions={
    DATA_NOT_CONNECTED:['Dato non collegato','Il modulo richiede caratteristiche tattiche strutturate che le fonti attuali non forniscono allo snapshot. Non equivale a un errore API.'],
    EVIDENCE_REJECTED:['Evidenza non utilizzabile','La ricerca ha restituito evidenze insufficienti, discordanti o fuori data. Controllare fonti, date e motivi nei dettagli della ricerca.'],
    ARCHIVE_DATA_UNAVAILABLE:['Dato locale assente o scaduto','Attendere il prossimo aggiornamento del collector. Nessuna chiamata API-Football avviata dall’analisi.'],
    IDENTITY_MAPPING_MISSING:['Mapping API-Football mancante','L’identità 888 non ha una corrispondenza API-Football verificata e univoca.'],
    API_PROVIDER_ERROR:['API-Football non disponibile','Il provider ha restituito un errore tecnico, di quota o di schema.'],
    OPENAI_AUTH_ERROR:['OpenAI non disponibile: autenticazione fallita','I saved agent sono configurati, ma la chiave OpenAI è stata rifiutata.'],
    OPENAI_KEY_EXPIRED:['Chiave OpenAI scaduta','Sostituire OPENAI_API_KEY nello stesso progetto degli agenti e riavviare Bettor.'],
    OPENAI_PERMISSION_ERROR:['Permessi OpenAI insufficienti','Verificare accesso al progetto e permessi Agents API della chiave.'],
    NEWS_PROVIDER_NOT_CONFIGURED:['Provider news non configurato','Nessun provider news o servizio OpenAI Research è disponibile per questa analisi.'],
    LINEUP_NOT_PUBLISHED_YET:['Lineup non ancora pubblicate','Le formazioni richieste non sono ancora disponibili presso le fonti configurate.'],
    STATS_PARTIAL:['Statistiche parziali','Le statistiche sono disponibili, ma alcuni segmenti o campioni richiesti sono incompleti.'],
    MODEL_INPUT_INSUFFICIENT:['Input modello insufficienti',rawForecast?`Completezza input ${percent(rawForecast.input_completeness)}; la stima resta non operativa.`:training?`Campione input ${training}: il modello non è operativo.`:'Il modello non dispone degli input minimi per produrre una stima.'],
    MODEL_VALIDATION_INSUFFICIENT:['Validazione modello insufficiente',required?`Campione LIVE_FORWARD ${observed}/${required}: stima visibile, BET bloccato e stake 0.`:'La validazione indipendente non ammette il modello alla decisione.'],
    QUOTE_MISSING:['Quote mancanti',failures.odds?.message || 'Nessuna quota completa e simultanea utilizzabile nello snapshot.'],
    RESEARCH_FAILED:['Ricerca fallita','Un provider di ricerca non ha completato la richiesta.'],
    MARKET_UNSUPPORTED:['Mercato non supportato','Nessun mercato richiesto dispone di una stima supportata dalla configurazione corrente.'],
  };
  for(const code of known){const item=definitions[code];if(item)add(code,item[0],item[1]);}
  return causes;
}
const paths = {
  grid:html`<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>`,
  book:html`<path d="M4 4h6a3 3 0 0 1 2 1 3 3 0 0 1 2-1h6v16h-6a3 3 0 0 0-2 1 3 3 0 0 0-2-1H4zM12 5v16"/>`,
  layers:html`<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>`,
  wallet:html`<path d="M4 6V4h14v3M4 7h16v14H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Zm11 5h5v5h-5z"/>`,
  pulse:html`<path d="M2 12h5l3-8 4 16 3-8h5"/>`,
  arrow:html`<path d="M5 12h14m-5-5 5 5-5 5"/>`,
  chevron:html`<path d="m9 5 7 7-7 7"/>`,
  down:html`<path d="m6 9 6 6 6-6"/>`,
  check:html`<path d="m5 12 4 4L19 6"/>`,
  clock:html`<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>`,
  shield:html`<path d="m12 3 8 3v6c0 4-5 8-8 9-3-1-8-5-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>`,
  eye:html`<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>`,
  alert:html`<path d="M12 3 2 21h20L12 3ZM12 9v5m0 3v1"/>`,
  circle:html`<circle cx="12" cy="12" r="8"/>`,
  search:html`<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>`,
  refresh:html`<path d="M20 10a8 8 0 0 0-14-5L3 8m0-5v5h5M4 14a8 8 0 0 0 14 5l3-3m0 5v-5h-5"/>`,
  download:html`<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>`,
  external:html`<path d="M14 3h7v7m0-7L10 14M10 4H4v16h16v-6"/>`,
  link:html`<path d="m10 14 4-4m-6 7-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2-1 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0" transform="translate(1 0)"/>`,
  close:html`<path d="m6 6 12 12M6 18 18 6"/>`,
  menu:html`<path d="M4 6h16M4 12h16M4 18h16"/>`,
  ball:html`<circle cx="12" cy="12" r="9"/><path d="m12 8 4 3-1 5H9l-1-5 4-3Zm0-5v5m8 1-4 2m2 8-3-3m-9 3 3-3M4 9l4 2"/>`,
};
export const icon = name => html`<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.circle}</svg>`;
export const badge = status => {const info=statusInfo(status);return html`<span class="badge ${info.tone}">${icon(info.icon)}${info.short}</span>`;};
export const empty = (title, description, symbol='layers') => html`<div class="empty-state"><span class="empty-icon">${icon(symbol)}</span><h3>${title}</h3><p>${description}</p></div>`;
export const safeURL = value => {try {const url=new URL(value);return ['https:','http:'].includes(url.protocol)?url.href:null;}catch{return null;}};
