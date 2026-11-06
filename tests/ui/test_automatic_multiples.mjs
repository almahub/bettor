import {test} from 'node:test';
import assert from 'node:assert/strict';

test('one approval prepares ready tickets from six visible matches within 24h without placing',async()=>{
 const nodes=new Map(),listeners={},requests=[];
 const selected=Array.from({length:6},(_,i)=>({match:'Home '+i+' - Away '+i,kickoff:'2026-11-06T19:45:00Z',market:'1X2',selection:'HOME',status:'INDICATIVE',ranking_score:50,probability:.6,odds:2,reasons:['Validator insufficiente']}));
 const plan={plan_id:'reviewed-plan',turn:'week',budget_eur:'50.00',selected,considered:10,eligible_24h:8,note:'Solo prossime 24 ore'};
 let report={proposal:plan,allocated_eur:'0',unallocated_eur:'50',preparations:[],note:'Conferma manuale; nessun movimento Banca.'};
 function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',value:'',hidden:true,addEventListener(){}});return nodes.get(id);}
 globalThis.document={querySelector:node,getElementById:id=>node('#'+id),addEventListener(name,fn){listeners[name]=fn;}};
 globalThis.location={hash:'#candidates'};
 let allow=false;globalThis.confirm=()=>allow;
 globalThis.fetch=async(path,options)=>{
  requests.push([path,options]);
  let value=path==='/bank'||path==='/salabet/stats'?{}:[];
  if(path==='/salabet/automatic-multiples'){
   if(options.method==='POST'){const body=JSON.parse(options.body);assert.equal(body.plan_id,plan.plan_id);assert.equal(body.enabled,true);report={...report,allocated_eur:'50',unallocated_eur:'0',preparations:[{slips:['s1','s2']}]};}
   value=report;
  }
  return {ok:true,json:async()=>value};
 };
 await import('../../better/web/salabet.mjs');
 assert.match(node('#app').innerHTML,/6\/6 partite selezionate/);
 assert.doesNotMatch(node('#app').innerHTML,/raggruppamento|primo kickoff|schedine\/gruppo/);
 assert.match(node('#app').innerHTML,/Approva proposta e attiva preparazione/);
 const click=()=>listeners.click({target:{closest(){return {dataset:{action:'multiple-automation'}};}}});
 await click();assert.equal(requests.filter(([,o])=>o.method==='POST').length,0);
 allow=true;await click();
 assert.match(node('#app').innerHTML,/Scommesse aperte/);
 assert.match(node('#toast').textContent,/Multiple preparate/);
 assert.deepEqual(requests.filter(([,o])=>o.method==='POST').map(([p])=>p),['/salabet/automatic-multiples']);
 report={...report,allocated_eur:'0',unallocated_eur:'50',proposal:{...plan,selected:[],eligible_24h:0}};
 await listeners.click({target:{closest(){return {dataset:{page:'candidates'}};}}});
 await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
 assert.match(node('#app').innerHTML,/servono almeno 3 partite utilizzabili/);
 assert.match(node('#app').innerHTML,/Adesso: 0/);
 assert.match(node('#app').innerHTML,/aria-describedby="preparation-block-reason"/);

});
