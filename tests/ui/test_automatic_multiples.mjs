import {test} from 'node:test';
import assert from 'node:assert/strict';

test('one approval prepares ready tickets from six visible matches within 24h without placing',async()=>{
 const nodes=new Map(),listeners={},requests=[];
 const selected=Array.from({length:6},(_,i)=>({match:'Home '+i+' - Away '+i,kickoff:'2026-11-06T19:45:00Z',market:'1X2',selection:'HOME',status:'INDICATIVE',ranking_score:50,probability:.6,odds:2,reasons:['Validator insufficiente']}));
 const plan={plan_id:'reviewed-plan',turn:'week',budget_eur:'50.00',selected,considered:10,eligible_24h:8,note:'Solo prossime 24 ore'};
 let report={workflow_version:2,style:'MULTIPLE',preparation_enabled:false,preview:{tickets:[{amount_eur:'50',selections:[]}],total_eur:'50'},budget_eur:'50',proposal:plan,allocated_eur:'0',unallocated_eur:'50',preparations:[],note:'Conferma manuale; nessun movimento Banca.'};
 function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',value:'',hidden:true,addEventListener(){}});return nodes.get(id);}
 globalThis.document={querySelector:node,getElementById:id=>node('#'+id),addEventListener(name,fn){listeners[name]=fn;}};
 globalThis.location={hash:'#candidates'};
 let allow=false;globalThis.confirm=()=>allow;
 globalThis.fetch=async(path,options)=>{
  requests.push([path,options]);
  let value=path==='/bank'||path==='/salabet/stats'?{}:[];
  if(path==='/salabet/automatic-multiples/budget'){const body=JSON.parse(options.body);assert.equal(body.budget_eur,'75');report={...report,budget_eur:'75',unallocated_eur:'75',proposal:{...report.proposal,budget_eur:'75'}};value=report;}
  if(path==='/salabet/automatic-multiples'){
   if(options.method==='POST'){const body=JSON.parse(options.body);assert.equal(body.plan_id,plan.plan_id);assert.equal(body.enabled,true);report={...report,allocated_eur:'50',unallocated_eur:'0',preparations:[{slips:['s1','s2']}]};}
   value=report;
  }
  return {ok:true,json:async()=>value};
 };
 await import('../../better/web/salabet.mjs');
 assert.match(node('#app').innerHTML,/Automazioni delle giocate/);
 assert.match(node('#app').innerHTML,/id="automation-budget"/);
 assert.match(node('#app').innerHTML,/Budget massimo del turno/);
 assert.match(node('#app').innerHTML,/Nessuna conferma o movimento Banca/);
 assert.match(node('#app').innerHTML,/Prepara ora la proposta/);
 const click=()=>listeners.click({target:{closest(){return {dataset:{action:'multiple-automation'}};}}});
 await click();assert.equal(requests.filter(([,o])=>o.method==='POST').length,0);
 allow=true;await click();
 assert.match(node('#app').innerHTML,/Scommesse aperte/);
 assert.match(node('#toast').textContent,/Multiple preparate/);
 assert.deepEqual(requests.filter(([,o])=>o.method==='POST').map(([p])=>p),['/salabet/automatic-multiples']);
 await listeners.click({target:{closest(){return {dataset:{page:'candidates'}};}}});
 node('#automation-budget').value='75';node('#automation-style').value='SYSTEM';node('#automation-prepare').checked=true;
 await listeners.click({target:{closest(){return {dataset:{action:'automation-plan-save'}};}}});
 const saved=requests.find(([p])=>p==='/salabet/automatic-multiples/preparation');
 assert.equal(JSON.parse(saved[1].body).budget_eur,'75');
 assert.equal(JSON.parse(saved[1].body).style,'SYSTEM');
 report={...report,allocated_eur:'0',unallocated_eur:'50',proposal:{...plan,selected:[],eligible_24h:0}};
 await listeners.click({target:{closest(){return {dataset:{page:'candidates'}};}}});
 await listeners.click({target:{closest(){return {dataset:{action:'refresh'}};}}});
 assert.match(node('#app').innerHTML,/Stile automatico/);

});
