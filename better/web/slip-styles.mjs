import {html} from './ui.mjs';

export const slipStyleNames={MULTIPLE:'Multiple',SYSTEM:'Sistemi',SINGLES:'Singole'};
export function slipStyle(slip){
 return slip.preparation_style?.style || (legacySystemGroup(slip)?'SYSTEM':slip.type==='SINGLE'?'SINGLES':'MULTIPLE');
}
const legacySystemGroup=slip=>/^SISTEMA ([a-f0-9]{8}) - 42 combinazioni - /i.exec(slip.note || '')?.[1];
export function styleTabs(rows,active){
 return html`<nav class="toolbar" aria-label="Stile delle scommesse aperte">${Object.entries(slipStyleNames).map(([key,label])=>html`<button class="btn ${key===active?'primary':''}" data-slip-style="${key}" aria-pressed="${key===active}">${label} (${rows.filter(s=>slipStyle(s)===key).length})</button>`)}</nav>`;
}
export function styleGroups(rows){
 const groups=new Map();
 for(const slip of rows){
  const key=slip.preparation_style?.group_id || legacySystemGroup(slip) || slip.id;
  if(!groups.has(key))groups.set(key,[]);
  groups.get(key).push(slip);
 }
 return [...groups.entries()];
}
export function stylesLegend(){
 return html`<section class="panel"><h2>Legenda degli stili di giocata</h2><dl><dt><strong>Crea multipla</strong></dt><dd>Una schedina con 2–6 partite selezionate.</dd><dt><strong>Crea sistema</strong></dt><dd>Sei partite, 42 combinazioni e mercati diversificati; blocco se mancano alternative valide.</dd><dt><strong>Singole ripartite</strong></dt><dd>Una schedina per partita, dividendo il budget ai centesimi.</dd></dl></section>`;
}
