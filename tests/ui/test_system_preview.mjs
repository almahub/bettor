import {test} from 'node:test';
import assert from 'node:assert/strict';
import {systemPreviewPanel} from '../../better/web/system-preview.mjs';

test('system preview makes market concentration and missing alternatives visible',()=>{
 const blocked=systemPreviewPanel({ready:false,selection_mode:'AUTO_FROM_ANALYSIS',reason_code:'SYSTEM_DIVERSIFICATION_INSUFFICIENT',
  message:'Mancano alternative utilizzabili',missing_alternatives:[{match:'Casa - Ospiti',available_markets:['BTTS']}]}).value;
 assert.match(blocked,/Sistema non preparabile/);
 assert.match(blocked,/SYSTEM_DIVERSIFICATION_INSUFFICIENT/);
 assert.match(blocked,/Casa - Ospiti/);
 assert.match(blocked,/Selezione alternative: automatica/);
 assert.match(blocked,/Partite da aggiornare o sostituire/);
 const markup=systemPreviewPanel({ready:true,selection_mode:'AUTO_FROM_ANALYSIS',ticket_count:42,total_eur:'52',note:'Distribuzione verificata',
  distribution:[{match:'Casa - Ospiti',appearances:26,markets:[
   {market:'1X2',selection:'HOME',appearances:9,stake_exposure_eur:'11'},
   {market:'BTTS',selection:'YES',appearances:8,stake_exposure_eur:'10'}]}]}).value;
 assert.match(markup,/42 combinazioni/);
 assert.match(markup,/26 presenze/);
 assert.match(markup,/9 presenze/);
 assert.match(markup,/8 presenze/);
 assert.match(markup,/nessun addebito Banca/);
 assert.match(markup,/Alternative scelte automaticamente/);
});
