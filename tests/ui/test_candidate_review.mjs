import {test} from 'node:test';
import assert from 'node:assert/strict';

test('blocked candidates remain readable with precise cause and cannot be selected',async()=>{
 const nodes=new Map();
 const node=key=>{if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',hidden:true,addEventListener(){}});return nodes.get(key);};
 globalThis.document={querySelector:node,addEventListener(){},getElementById:id=>node('#'+id)};
 globalThis.location={hash:'#candidates'};
 const candidate={id:'c1',home_team:'Casa',away_team:'Ospiti',kickoff:'2026-11-08T14:00:00Z',competition_id:'serie-a',
  market:'1X2',selection:'HOME',prediction_status:'INDICATIVE',operationally_eligible:false,
  probability:.55,odds_at_analysis:2,fair_odds:1.82,validation_sample:3,validation_required:200,input_completeness:1,
  review:{blocked:true,reason_code:'STATS_STALE',message:'Statistiche scadute: aggiornamento necessario',analysis_id:'latest'}};
 globalThis.fetch=async path=>({ok:true,json:async()=>path==='/salabet/candidates'?[candidate]:path==='/bank'?{initialized:true,bets:[]}:path==='/salabet/stats'?{}:[]});
 await import('../../better/web/salabet.mjs');
 const markup=node('#app').innerHTML;
 assert.match(markup,/Casa/);assert.match(markup,/Ospiti/);
 assert.match(markup,/STATS_STALE/);assert.match(markup,/Statistiche scadute/);
 assert.match(markup,/data-candidate="c1" disabled/);
 assert.match(markup,/Ultima analisi/);
});
