import {test} from 'node:test';
import assert from 'node:assert/strict';
import {gateExplanation,observedMetric,reassessmentText} from '../../better/web/ui.mjs';

test('indicative gates explain dependencies without changing their status',()=>{
 const run={as_of:'2026-11-03T11:52:44Z',snapshot:{match:{kickoff:'2026-11-07T14:00:00Z'}},decision:{degradations:[{code:'INDICATIVE_PREDICTION_NOT_ADMITTED',applies_to_decision:true}],confidence_components:{model_reliability:0}},results:{risk_manager:{payload:{reasons:['MODEL_VALIDATION_INSUFFICIENT']}}}};
 for(const gate of ['MODEL_VALIDITY','MARKET','NUMERICAL_CONSISTENCY','PRICE_FRESHNESS','VALUE','RISK','CONFIDENCE','TIMING']){
  const record={gate,status:gate==='TIMING'?'WAIT':'FAIL',reason:'original'};
  assert.ok(gateExplanation(record,run).length>20);
  assert.equal(record.reason,'original');
  assert.equal(record.status,gate==='TIMING'?'WAIT':'FAIL');
 }
 assert.match(gateExplanation({gate:'RISK'},run),/validazione insufficiente/);
 assert.match(gateExplanation({gate:'TIMING'},run),/partita era futura/);
 assert.equal(gateExplanation({gate:'RISK',status:'PASS'},run),'');
 assert.doesNotMatch(gateExplanation({gate:'MARKET'},{decision:{}}),/solo indicativo/);
});
test('observed xG absence is explicit and never replaced by goals',()=>{
 assert.equal(observedMetric(null,'xg_per_match'),'Non disponibile dalla fonte');
 assert.equal(observedMetric(null,'xga_per_match'),'Non disponibile dalla fonte');
 assert.equal(observedMetric(0,'xg_per_match'),'0,00');
 assert.equal(observedMetric(1.75,'goals_for_per_match'),'1,75');
 assert.match(reassessmentText('Refresh price after TTL or material market movement'),/Aggiornare le quote/);
 assert.equal(reassessmentText('Custom condition'),'Custom condition');
});
