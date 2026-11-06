import {test} from 'node:test';
import assert from 'node:assert/strict';

test('SalaBet Banca edits research limits without changing bankroll or creating slips',async()=>{
  const nodes=new Map(),listeners={},requests=[];
  const policy={enabled:false,per_match_eur:'0.25',per_day_eur:'5.00',per_week_eur:'25.00',day_committed_eur:'0',week_committed_eur:'0',usd_eur_rate:'.9',usd_safety_caps:{per_match:.25,per_day:5,per_week:25}};
  function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',value:'',hidden:true,addEventListener(){}});return nodes.get(id);}
  globalThis.document={querySelector:node,getElementById:id=>node('#'+id),addEventListener(name,fn){listeners[name]=fn;}};
  globalThis.location={hash:'#bank'};
  globalThis.fetch=async(path,options)=>{
    requests.push([path,options]);
    const value=path==='/bank/research-budget'?{...policy,...JSON.parse(options.body)}:path==='/bank'?{initialized:true,available:'100',equity:'100',reserved:'0',research_budget:policy}:path==='/salabet/stats'?{}:path==='/salabet/slips'||path==='/salabet/candidates'||path.startsWith('/salabet/archive')?[]:undefined;
    assert.notEqual(value,undefined,`Unexpected request ${path}`);
    return {ok:true,json:async()=>value};
  };
  await import('../../better/web/salabet.mjs');
  assert.match(node('#app').innerHTML,/Massimali ricerche automatiche/);
  assert.match(node('#app').innerHTML,/Spesa reale OpenAI, separata dal saldo delle puntate/);
  node('#research-per_match_eur').value='-1';
  node('#research-per_day_eur').value='5';node('#research-per_week_eur').value='25';
  const click=()=>listeners.click({target:{closest(){return {dataset:{action:'research-budget-save'}};}}});
  await click();
  assert.equal(requests.filter(([p])=>p==='/bank/research-budget').length,0);
  node('#research-per_match_eur').value='0,40';
  await click();
  const saved=requests.find(([p])=>p==='/bank/research-budget');
  assert.equal(JSON.parse(saved[1].body).per_match_eur,'0.40');
  assert.match(node('#toast').textContent,/Saldo Banca invariato/);
  assert.equal(requests.filter(([,options])=>options.method==='POST').length,1);
});
