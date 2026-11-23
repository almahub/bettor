import {html,marketName,selectionName,number} from './ui.mjs';
const money=value=>`${number(value,2)} €`;

export function systemPreviewPanel(preview){
 if(!preview)return '';
 if(!preview.ready)return html`<div class="warning compact-warning"><strong>Sistema non preparabile</strong><p>${preview.message}</p><code>${preview.reason_code}</code>${preview.selection_mode==='AUTO_FROM_ANALYSIS'?html`<p><strong>Selezione alternative: automatica.</strong> Basta una proposta per partita.</p>`:''}${preview.missing_alternatives?.length?html`<details open><summary>Partite da aggiornare o sostituire</summary>${preview.missing_alternatives.map(row=>html`<p>${row.match || row.match_id}: trovati ${row.available_markets.map(marketName).join(', ') || 'nessun mercato utilizzabile'}</p>`)}</details>`:''}</div>`;
 return html`<details class="system-preview"><summary>Sistema diversificato: ${preview.ticket_count} combinazioni · ${money(preview.total_eur)}</summary><p>${preview.note}</p><p><strong>Alternative scelte automaticamente da Bettor:</strong> una sola selezione manuale per partita.</p>${preview.distribution.map(row=>html`<div class="compact-warning"><strong>${row.match || row.match_id}</strong><p>${row.appearances} presenze nelle schedine</p>${row.markets.map(m=>html`<p>${marketName(m.market)} · ${selectionName(m.selection)}: ${m.appearances} presenze · esposizione ${money(m.stake_exposure_eur)}</p>`)}</div>`)}<p>Anteprima: nessuna schedina creata e nessun addebito Banca.</p></details>`;
}
