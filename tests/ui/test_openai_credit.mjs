import {test} from 'node:test';
import assert from 'node:assert/strict';
import {creditPanel,exportImportPanel} from '../../better/web/openai-credit.mjs';
import {mount} from '../../better/web/ui.mjs';
const render=value=>{const node={innerHTML:''};mount(node,value);return node.innerHTML;};

test('credit reference and estimates are explicit; updating means the total after recharge',()=>{
 const markup=render(creditPanel({amount:'7.50',reference_amount:'7.50',reference_at:'2026-11-09T12:00:00Z',official_spend_since_reference:'0',local_estimated_spend_since_reference:'0',level:'OK',warning_usd:'5',critical_usd:'2',reason:'Saldo stimato'}));
 assert.match(markup,/7,50/);
 assert.match(markup,/Saldo dichiarato/);
 assert.match(markup,/Non vengono sommati due volte/);
 assert.match(markup,/nuovo credito totale/);
 assert.match(markup,/Non modifica la Banca/);
 assert.match(markup,/data-action="openai-credit-save"/);
 assert.match(markup,/id="openai-credit-amount"[^>]*placeholder=/);
 assert.doesNotMatch(markup,/id="openai-credit-amount"[^>]*value="7.50"/);
 assert.match(render(exportImportPanel()),/openai-costs-file/);
 assert.match(render(exportImportPanel()),/openai-usage-file/);
});

test('unknown costs and low credit do not appear as a verified live balance',()=>{
 assert.match(render(creditPanel({amount:'1.20',level:'CRITICAL',unknown_cost_calls:2})),/quasi esaurito/);
 assert.match(render(creditPanel({amount:'1.20',level:'CRITICAL',unknown_cost_calls:2})),/2 chiamate senza costo/);
 assert.match(render(creditPanel()),/Non disponibile via API/);
});
