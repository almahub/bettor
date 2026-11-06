import {test} from 'node:test';
import assert from 'node:assert/strict';
import {placementPreview,diagnosticsPanel} from '../../better/web/placement-review.mjs';

test('preview shows exact stakes, selections and explicit opt-in for future proposals',()=>{
 const control={preview:{total_eur:'50',bank_available_eur:'100',bank_after_eur:'50',bank_sufficient:true,tickets:[{amount_eur:'50',combined_odds:'8',selections:[{match:'<script>unsafe</script>',kickoff:'2026-11-06T19:45:00Z',market:'BTTS',selection:'YES',odds:2,status:'INDICATIVE',analysis_id:'a1'}]}]}};
 const markup=placementPreview(control,false).value;
 assert.match(markup,/Anteprima delle giocate/);
 assert.match(markup,/Conferma soltanto queste multiple/);
 assert.match(markup,/Autorizzo anche composizioni future/);
 assert.match(markup,/bank|Banca/);
 assert.doesNotMatch(markup,/<script>/);
 assert.match(markup,/&lt;script&gt;/);
 assert.match(placementPreview(control,false,true).value,/Approva e attiva automazione/);
 assert.doesNotMatch(markup,/giocate di test/i);
});

test('model diagnostics show counts, calibration, Brier and separate sources without bankroll figures',()=>{
 const row={label:'60–70%',count:100,mean_probability:.6,accuracy:.6,calibration_gap:0,brier_score:.24};
 const source={summary:row,dimensions:{probability_band:[row],market:[{...row,label:'BTTS'}]}};
 const markup=diagnosticsPanel({note:'Campioni piccoli',sources:{LIVE_FORWARD:source,HISTORICAL_BACKTEST:source},duplicates_ignored:7,excluded:0}).value;
 assert.match(markup,/LIVE_FORWARD/);assert.match(markup,/HISTORICAL_BACKTEST/);
 assert.match(markup,/Brier/);assert.match(markup,/calibrazione/);assert.match(markup,/100/);
 assert.match(markup,/Duplicati ignorati: 7/);assert.doesNotMatch(markup,/saldo|stake|bankroll/i);
});
