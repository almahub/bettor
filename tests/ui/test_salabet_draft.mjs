import {test} from 'node:test';
import assert from 'node:assert/strict';

test('a new SalaBet draft names the missing stake and can then be prepared',async()=>{
  const nodes=new Map();const listeners={};const requests=[];
  function node(selector){
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',hidden:true,value:'',addEventListener(){}});
    return nodes.get(selector);
  }
  const slip={
    id:'slip-draft',type:'SINGLE',status:'DRAFT',stake:null,bookmaker:'888sport.it',
    combined_odds:2,potential_return:null,created_at:'2026-10-09T10:00:00Z',
    warnings:[],note:null,selections:[{
      bet_candidate_id:'candidate-1',home_team:'Casa',away_team:'Ospiti',
      kickoff:new Date(Date.now()+7*24*60*60*1000).toISOString(),market:'1X2',selection:'HOME',
      odds_at_analysis:2,odds_at_placement:2,match_status:'NON_INIZIATA',result:'UNRESOLVED',
    }],
  };
  globalThis.document={querySelector:node,addEventListener(name,handler){listeners[name]=handler;},getElementById(id){return node(`#${id}`);}};
  globalThis.location={hash:'#open'};
  globalThis.fetch=async(path,options)=>{
    requests.push([path,options]);
    if(path==='/salabet/slips/slip-draft'&&options.method==='PATCH'){
      slip.stake=JSON.parse(options.body).stake;
      return {ok:true,json:async()=>slip};
    }
    if(path==='/salabet/slips/slip-draft/confirm'){
      slip.status='READY';return {ok:true,json:async()=>slip};
    }
    const data=path==='/salabet/slips'?[slip]:path==='/salabet/candidates'||path.startsWith('/salabet/archive')?[]:path==='/salabet/stats'?{}:path==='/bank'?{initialized:true,bets:[]}:undefined;
    assert.notEqual(data,undefined,`Unexpected API request ${path}`);
    return {ok:true,json:async()=>data};
  };
  await import('../../better/web/salabet.mjs');
  assert.match(node('#app').innerHTML,/obbligatorio per preparare/);
  node('#bookmaker-slip-draft').value='888sport.it';
  node('#stake-slip-draft').value='';
  node('#odds-candidate-1').value='2';
  node('#note-slip-draft').value='';
  await listeners.click({target:{closest(){return {dataset:{prepare:'slip-draft'}};}}});
  assert.match(node('#toast').textContent,/Inserisci lo stake/);
  assert.equal(requests.filter(([path])=>path==='/salabet/slips/slip-draft').length,0);
  node('#stake-slip-draft').value='10';
  await listeners.click({target:{closest(){return {dataset:{prepare:'slip-draft'}};}}});
  assert.equal(requests.filter(([path])=>path==='/salabet/slips/slip-draft').length,1);
  assert.equal(requests.filter(([path])=>path==='/salabet/slips/slip-draft/confirm').length,1);
  assert.equal(requests.filter(([path])=>path.endsWith('/place')).length,0);
  assert.match(node('#app').innerHTML,/CONFERMA PUNTATA/);
});
