import {test} from 'node:test';
import assert from 'node:assert/strict';

test('SalaBet shows rate-limit feedback beside a failed place attempt',async()=>{
  const nodes=new Map(); const listeners={}; const requests=[];
  let placeStatus=429;
  function node(selector){
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',hidden:true,addEventListener(){}});
    return nodes.get(selector);
  }
  globalThis.document={querySelector:node,addEventListener(name,handler){listeners[name]=handler;},getElementById(id){return node(`#${id}`);}};
  globalThis.location={hash:'#open'};
  const slip={
    id:'slip-1',type:'MULTIPLE',status:'READY',stake:'10.00',bookmaker:'888sport.it',
    combined_odds:2,potential_return:'20.00',created_at:'2026-10-09T10:00:00Z',
    warnings:[],note:null,selections:[{
      bet_candidate_id:'candidate-1',home_team:'Casa',away_team:'Ospiti',
      kickoff:'2026-10-12T18:45:00Z',market:'1X2',selection:'HOME',
      odds_at_analysis:2,odds_at_placement:2,match_status:'NON_INIZIATA',result:'UNRESOLVED',
    }],
  };
  globalThis.fetch=async(path,options)=>{
    requests.push([path,options]);
    if(path==='/salabet/slips/slip-1/place')return placeStatus===429
      ?{ok:false,status:429,json:async()=>({detail:'Analysis rate limit exceeded'})}
      :placeStatus===409
        ?{ok:false,status:409,json:async()=>({detail:'1 validation error for BankSlipBet: odds decimal_max_places'})}
        :{ok:true,status:200,json:async()=>({status:'OPEN'})};
    const data=path==='/salabet/slips'?[slip]:path==='/salabet/candidates'||path.startsWith('/salabet/archive')?[]:path==='/salabet/stats'?{}:path==='/bank'?{initialized:true,bets:[]}:undefined;
    assert.notEqual(data,undefined,`Unexpected API request ${path}`);
    return {ok:true,json:async()=>data};
  };
  await import('../../better/web/salabet.mjs');
  assert.match(node('#app').innerHTML,/CONFERMA PUNTATA/);
  await listeners.click({target:{closest(){return {dataset:{place:'slip-1'}};}}});
  assert.equal(requests.filter(([path])=>path==='/salabet/slips/slip-1/place').length,1);
  assert.match(node('#toast').textContent,/Attendi 60 secondi/);
  assert.match(node('#app').innerHTML,/Troppe operazioni ravvicinate/);
  assert.doesNotMatch(node('#toast').textContent,/Banca aggiornata/);
  placeStatus=409;
  await listeners.click({target:{closest(){return {dataset:{place:'slip-1'}};}}});
  assert.doesNotMatch(node('#toast').textContent,/Inserisci una quota valida/);
  placeStatus=200;
  await listeners.click({target:{closest(){return {dataset:{place:'slip-1'}};}}});
  assert.equal(requests.filter(([path])=>path==='/salabet/slips/slip-1/place').length,3);
  assert.match(node('#toast').textContent,/Banca aggiornata/);
  slip.status='OPEN';
  await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
  assert.match(node('#app').innerHTML,/<details class="ticket compact-ticket collapsible-ticket" data-ticket-id="slip-1"/);
  assert.match(node('#app').innerHTML,/Gestisci selezioni annullate dal bookmaker \(VOID\)/);
  assert.match(node('#app').innerHTML,/Segna annullata: Casa – Ospiti · Esito finale · Casa/);
  assert.doesNotMatch(node('#app').innerHTML,/Void: Casa/);
  listeners.toggle({target:{dataset:{ticketId:'slip-1'},open:true}});
  await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
  assert.match(node('#app').innerHTML,/data-ticket-id="slip-1" open/);
  slip.selections[0].match_status='CONCLUSA';
  slip.selections[0].final_score={home:1,away:0,source:'api_football',source_tier:'B'};
  await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
  assert.match(node('#app').innerHTML,/CONCLUSA/);
  assert.match(node('#app').innerHTML,/class="final-score"[^>]*>1–0<\/strong>/);
  slip.selections[0].result='WON';
  await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
  assert.match(node('#app').innerHTML,/class="status closed">CONCLUSA<\/span>/);
  slip.selections[0].result='LOST';
  await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
  assert.match(node('#app').innerHTML,/class="status negative">CONCLUSA<\/span>/);
  slip.selections[0].result='UNRESOLVED';
  await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
  assert.match(node('#app').innerHTML,/class="status ">CONCLUSA<\/span>/);
  slip.selections[0].match_status='RISULTATO_IN_ATTESA';
  slip.selections[0].final_score=null;
  await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
  assert.match(node('#app').innerHTML,/RISULTATO IN ATTESA/);
  assert.doesNotMatch(node('#app').innerHTML,/class="final-score"/);
});
