import {test} from 'node:test';
import assert from 'node:assert/strict';

test('SalaBet candidate shows paired checkpoint details without changing slips or bank',async()=>{
  const nodes=new Map();const listeners={};const requests=[];
  function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',hidden:true,addEventListener(){}});return nodes.get(id);}
  globalThis.document={querySelector:node,getElementById:node,addEventListener(name,fn){listeners[name]=fn;}};
  globalThis.location={hash:'#candidates'};
  const candidate={id:'c1',analysis_id:'a1',market:'1X2',selection:'HOME',home_team:'Casa',away_team:'Ospiti',competition_id:'SA',kickoff:'2026-11-08T18:00:00Z',model_name:'poisson',model_version:'1.0.0',validation_sample:3,validation_required:200,probability:.6,
    timing_comparison:{enabled:false,verdict:'CHANGED',reason:'Il modello indica una selezione diversa',checkpoints:{
      T24:{status:'COMPLETED',scheduled_at:'2026-11-07T18:00:00Z',capture:{analysis_id:'a1',as_of:'2026-11-07T18:00:01Z',lineup_status:'PROBABLE_LINEUP',calls:0,cost:0,cost_known:true,markets:[{model:'poisson',version:'1.0.0',market:'1X2',odds:2}]}},
      T30:{status:'DATA_MISSING',scheduled_at:'2026-11-08T17:30:00Z',capture:{analysis_id:'a2',as_of:'2026-11-08T17:31:00Z',lineup_status:'UNKNOWN',calls:0,cost:0,cost_known:true,markets:[]}}
    }}};
  globalThis.fetch=async(path,options)=>{
    requests.push([path,options]);
    const value=path==='/salabet/candidates'?[candidate]:path==='/bank'?{initialized:true,bets:[]}:path==='/salabet/stats'?{}:path==='/salabet/slips'||path.startsWith('/salabet/archive')?[]:undefined;
    assert.notEqual(value,undefined,`Unexpected request ${path}`);
    return {ok:true,json:async()=>value};
  };
  await import('../../better/web/salabet.mjs');
  const markup=node('#app').innerHTML;
  assert.match(markup,/<summary>Confronta analisi<\/summary>/);
  assert.match(markup,/T−24: Completato · T−30: Dati mancanti/);
  assert.match(markup,/Cambiata/);
  assert.match(markup,/Automazione disattivata/);
  assert.match(markup,/href="\/#analysis\/a1"/);
  assert.match(markup,/href="\/#analysis\/a2"/);
  assert.match(markup,/Le schedine esistenti non vengono modificate/);
  assert.equal(requests.some(([,options])=>options.method==='POST'),false);
});
