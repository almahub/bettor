import {test} from 'node:test';
import assert from 'node:assert/strict';

test('prepared ticket shows T30 warnings and links without mutating the ticket',async()=>{
 const nodes=new Map(),listeners={},requests=[];
 function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',value:'',hidden:true,addEventListener(){}});return nodes.get(id);}
 const slip={id:'s1',type:'SINGLE',status:'READY',stake:20,bookmaker:'888',combined_odds:2,potential_return:40,created_at:'2026-11-03T12:00:00Z',warnings:[],selections:[{bet_candidate_id:'c1',home_team:'Home',away_team:'Away',kickoff:'2026-11-06T19:45:00Z',market:'1X2',selection:'HOME',odds_at_analysis:2,odds_at_placement:2,match_status:'NON_INIZIATA'}],checkpoint_review:{status:'REVIEW_REQUIRED',note:'Quote accettate e Banca invariati.',selections:[{home_team:'Home',away_team:'Away',initial_analysis_id:'a1',analysis_id:'a2',status:'REVIEW_REQUIRED',checked_at:'2026-11-06T19:16:00Z',initial_odds:2,updated_odds:1.8,notices:[{code:'QUOTE_CHANGED',message:'Quota cambiata'},{code:'LINEUP_MATERIAL_CHANGE',message:'Formazione cambiata'}]}]}};
 const before=JSON.stringify(slip);
 globalThis.document={querySelector:node,getElementById:id=>node('#'+id),addEventListener(name,fn){listeners[name]=fn;}};
 globalThis.location={hash:'#open'};
 globalThis.fetch=async(path,options)=>{requests.push([path,options]);return {ok:true,json:async()=>path==='/salabet/slips'?[slip]:path==='/bank'||path==='/salabet/stats'?{}:[]};};
 await import('../../better/web/salabet.mjs');
 const markup=node('#app').innerHTML;
 assert.match(markup,/T-30: DA RIESAMINARE/);
 assert.match(markup,/QUOTE_CHANGED/);assert.match(markup,/LINEUP_MATERIAL_CHANGE/);
 assert.match(markup,/href="\/#analysis\/a1"/);assert.match(markup,/href="\/#analysis\/a2"/);
 assert.equal(JSON.stringify(slip),before);
 assert.equal(requests.some(([,o])=>o.method!=='GET'),false);
});
