import {test} from 'node:test';
import assert from 'node:assert/strict';
import {bettingStylesPanel} from '../../better/web/betting-styles.mjs';

test('style presets show budgets, selected inputs and separate confirmation',()=>{
 for(const mode of ['MULTIPLE','SYSTEM','SINGLES']){
  const markup=bettingStylesPanel({mode,budget:'50',selected:6}).value;
  assert.match(markup,/6 proposte selezionate/);
  assert.match(markup,/style-budget/);
  assert.match(markup,/Nessun addebito Banca/);
  assert.match(markup,/Conferma puntata restano azioni separate/);
 }
 const result=bettingStylesPanel({mode:'SINGLES',selected:3,preview:{ready:true,ticket_count:3,total_eur:'50',
  tickets:[{amount_eur:'16.67',combined_odds:'2',selections:[{match:'Casa - Ospiti',market:'1X2',selection:'HOME',odds:2,status:'INDICATIVE'}]}]}}).value;
 assert.match(result,/3 schedine/);
 assert.match(result,/Casa - Ospiti/);
 assert.match(result,/INDICATIVE/);
 assert.match(result,/Crea singole ripartite in Scommesse aperte/);
});

test('blocked or running styles cannot be created',()=>{
 const blocked=bettingStylesPanel({mode:'SYSTEM',selected:6,preview:{ready:false,reason_code:'SYSTEM_DIVERSIFICATION_INSUFFICIENT',message:'Alternative insufficienti'}}).value;
 assert.match(blocked,/Alternative insufficienti/);
 assert.match(blocked,/SYSTEM_DIVERSIFICATION_INSUFFICIENT/);
 assert.doesNotMatch(blocked,/data-action="style-create"/);
 const automatic=bettingStylesPanel({mode:'SYSTEM',selected:6,preview:{ready:false,selection_mode:'AUTO_FROM_ANALYSIS',reason_code:'SYSTEM_DIVERSIFICATION_INSUFFICIENT',message:'Non disponibili',missing_alternatives:[{match:'A - B',available_markets:['1X2']}]}}).value;
 assert.match(automatic,/Non devi cercare manualmente/);
 assert.match(automatic,/Ricerca alternative automatica/);
 assert.match(automatic,/Non selezionare altre righe/);
 assert.match(automatic,/A - B/);
 const running=bettingStylesPanel({mode:'SYSTEM',busy:true,selected:6}).value;
 assert.match(running,/Preparazione in corso/);
 assert.match(running,/data-action="style-preview" disabled/);
});

test('system budget preserves custom amounts instead of fixing the screenshot total',()=>{
 for(const budget of ['48','50','52']){
  const markup=bettingStylesPanel({mode:'SYSTEM',budget,selected:6}).value;
  assert.ok(markup.includes('value="'+budget+'"'));
  assert.doesNotMatch(markup,/Schema di riferimento: 52/);
 }
});
