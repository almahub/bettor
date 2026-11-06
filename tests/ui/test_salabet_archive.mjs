import {test} from 'node:test';
import assert from 'node:assert/strict';

test('SalaBet archive shows at most ten rows, resets filters and clamps after removals',async()=>{
  const nodes=new Map(),listeners={},requests=[];
  const slips=Array.from({length:23},(_,i)=>({id:`slip-${i}`,created_at:'2026-10-01T12:00:00Z',type:'SINGLE',bookmaker:`Book ${i}`,combined_odds:2,bank_stake:'10',bank_return:'0',profit_loss:'-10',status:'LOST',hidden:false,selections:[{analysis_id:`analysis-${i}`,selection:'HOME',market:'1X2',home_team:`Home ${i}`,away_team:`Away ${i}`,kickoff:'2026-10-01T12:00:00Z',match_status:'CONCLUSA'}]}));
  function node(selector){if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',value:'',textContent:'',hidden:true,addEventListener(){}});return nodes.get(selector);}
  globalThis.document={querySelector:node,getElementById:id=>node(`#${id}`),addEventListener(name,handler){listeners[name]=handler;}};
  globalThis.location={hash:'#archive'};
  globalThis.fetch=async(path,options)=>{
    requests.push([path,options]);
    if(options.method==='POST'){
      const slip=slips.find(s=>path.includes(`/slips/${s.id}/`));
      assert.ok(slip);
      slip.hidden=path.endsWith('/hide');
      return {ok:true,json:async()=>({hidden:slip.hidden})};
    }
    let data=[];
    if(path.startsWith('/salabet/archive')){
      const query=new URL(path,'http://local').searchParams;
      data=slips.filter(s=>query.get('include_hidden')==='true'||!s.hidden);
      if(query.get('bookmaker'))data=data.filter(s=>s.bookmaker===query.get('bookmaker'));
    }else if(path==='/salabet/stats')data={};
    else if(path==='/bank')data={available:'100',bets:[],transfers:[]};
    return {ok:true,json:async()=>data};
  };
  const markup=()=>node('#app').innerHTML;
  const click=dataset=>listeners.click({target:{closest:()=>({dataset})}});
  const rowCount=()=>[...markup().matchAll(/data-(?:hide|restore)-slip=/g)].length;
  await import('../../better/web/salabet.mjs');
  assert.equal(rowCount(),10);
  assert.match(markup(),/1-10 di 23/);
  assert.ok(!markup().includes('data-hide-slip="slip-10"'));
  await click({archiveStep:'1'});
  assert.equal(rowCount(),10);
  assert.match(markup(),/11-20 di 23/);
  await click({archiveStep:'1'});
  assert.equal(rowCount(),3);
  assert.match(markup(),/21-23 di 23/);
  for(const id of ['slip-20','slip-21','slip-22'])await click({hideSlip:id});
  assert.equal(rowCount(),10);
  assert.match(markup(),/11-20 di 20/);
  node('#filter-bookmaker').value='Book 0';
  await click({action:'filter'});
  assert.equal(rowCount(),1);
  assert.match(markup(),/1-1 di 1/);
  await listeners.change({target:{id:'show-hidden',checked:true}});
  assert.equal(rowCount(),10);
  assert.match(markup(),/1-10 di 23/);
  await click({archiveStep:'1'});await click({archiveStep:'1'});
  assert.equal(rowCount(),3);
  assert.match(markup(),/data-restore-slip="slip-20"/);
  await click({restoreSlip:'slip-20'});
  assert.match(markup(),/data-hide-slip="slip-20"/);
  for(const slip of slips)slip.hidden=true;
  await listeners.change({target:{id:'show-hidden',checked:false}});
  assert.equal(rowCount(),0);
  assert.match(markup(),/Archivio vuoto/);
  assert.ok(requests.every(([path,o])=>o.method!=='POST'||/\/(hide|restore)$/.test(path)));
});
