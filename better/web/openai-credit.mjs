import {html,date} from './ui.mjs';

const usd=value=>value==null?'—':new Intl.NumberFormat('it-IT',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(Number(value));
export function creditPanel(balance){
 const b=balance || {};
 const labels={OK:'Credito disponibile stimato',LOW:'Credito basso: pianifica una ricarica',CRITICAL:'Credito quasi esaurito: verifica e ricarica',UNCERTAIN:'Consumi da riconciliare'};
 return html`<section class="panel"><span class="mini-label">Credito residuo OpenAI</span><h2>${b.amount==null?'Non disponibile via API':usd(b.amount)}</h2>
 ${b.amount!=null?html`<p><strong>${labels[b.level] || 'Saldo stimato'}</strong></p><p class="caption">Saldo dichiarato ${usd(b.reference_amount)} · ${date(b.reference_at)}.<br>Consumi successivi: contabilizzati ${usd(b.official_spend_since_reference)} · stima chiamate Bettor ${usd(b.local_estimated_spend_since_reference)}. Non vengono sommati due volte.</p>${b.unknown_cost_calls?html`<p class="error-banner">${b.unknown_cost_calls} chiamate senza costo attribuibile: il saldo potrebbe essere inferiore.</p>`:''}`:''}
 <p class="caption">${b.reason || 'Il costo del periodo non permette di ricavare il saldo del portafoglio.'}</p>
 <a class="btn small" href="${b.billing_url || 'https://platform.openai.com/settings/organization/billing/overview'}" target="_blank" rel="noopener noreferrer">Apri credito OpenAI</a>
 <details><summary>Aggiorna saldo e avvisi</summary><p>Dopo una ricarica inserisci il nuovo credito totale visualizzato su OpenAI, non il solo importo versato. Non modifica la Banca.</p>
 <label>Credito totale USD<input id="openai-credit-amount" type="number" min="0" step="0.01" placeholder="Saldo verificato su OpenAI"></label>
 <label>Avviso sotto USD<input id="openai-credit-warning" type="number" min="0" step="0.01" value="${b.warning_usd || '5.00'}"></label>
 <label>Critico sotto USD<input id="openai-credit-critical" type="number" min="0" step="0.01" value="${b.critical_usd || '2.00'}"></label>
 <button class="btn small" data-action="openai-credit-save">Registra saldo verificato</button></details></section>`;
}

export function exportImportPanel(){
 return html`<details class="panel"><summary>Importa export consumi OpenAI</summary><p>Importa i file JSON Costs e Completions Usage. I periodi sovrapposti sostituiscono quelli precedenti, senza duplicare la spesa.</p>
 <label>Costs JSON<input type="file" id="openai-costs-file" accept="application/json,.json"></label>
 <label>Completions Usage JSON<input type="file" id="openai-usage-file" accept="application/json,.json"></label>
 <button class="btn small" data-action="openai-exports-import">Importa consumi</button></details>`;
}
