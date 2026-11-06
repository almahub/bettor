import {test} from 'node:test';
import assert from 'node:assert/strict';

test('SalaBet owns initialization, deposits, withdrawals and safe retries',async()=>{
  const nodes=new Map(),listeners={},requests=[],applied=new Set();
  let bank={initialized:false,bets:[],transfers:[]};
  let loseDepositResponse=true;
  function node(selector){
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',value:'',hidden:true,addEventListener(){}});
    return nodes.get(selector);
  }
  globalThis.document={querySelector:node,getElementById:id=>node(`#${id}`),addEventListener(name,handler){listeners[name]=handler;}};
  globalThis.location={hash:'#bank'};
  globalThis.fetch=async(path,options)=>{
    requests.push([path,options]);
    if(options.method==='POST'){
      const body=JSON.parse(options.body),amount=Number(body.amount);
      if(path==='/bank/withdraw'&&amount>Number(bank.available))return {ok:false,status:409,json:async()=>({detail:'Saldo disponibile insufficiente per il prelievo'})};
      if(!applied.has(body.request_id)){
        applied.add(body.request_id);
        if(path==='/bank/initialize')bank={...bank,initialized:true,initial_budget:body.amount,available:body.amount,equity:body.amount,reserved:'0',profit:'0'};
        else {
          const deposit=path==='/bank/deposit';
          bank={...bank,available:String(Number(bank.available)+(deposit?amount:-amount)),equity:String(Number(bank.equity)+(deposit?amount:-amount)),transfers:[{kind:deposit?'DEPOSIT':'WITHDRAWAL',amount:body.amount,at:'2026-10-26T12:00:00Z'},...bank.transfers]};
        }
      }
      if(path==='/bank/deposit'&&loseDepositResponse){loseDepositResponse=false;throw new Error('Connection lost');}
      return {ok:true,json:async()=>bank};
    }
    const data=path==='/bank'?bank:path==='/salabet/stats'?{}:[];
    return {ok:true,json:async()=>data};
  };
  const click=action=>listeners.click({target:{closest:()=>({dataset:{action}})}});
  await import('../../better/web/salabet.mjs');
  assert.match(node('#app').innerHTML,/Imposta il fondo iniziale/);
  assert.doesNotMatch(node('#app').innerHTML,/#portfolio|Gestisci Banca in Better/);
  node('#bank-budget').value='100';
  await click('bank-initialize');
  assert.equal(bank.available,'100.00');
  assert.match(node('#app').innerHTML,/Versamenti e prelievi/);
  assert.match(node('#app').innerHTML,/data-action="bank-deposit"/);
  assert.match(node('#app').innerHTML,/data-action="bank-withdraw"/);
  assert.doesNotMatch(node('#app').innerHTML,/data-action="bank-initialize"/);
  node('#bank-transfer-amount').value='20';
  await click('bank-deposit');
  assert.equal(bank.available,'120');
  await click('bank-deposit');
  assert.equal(bank.available,'120','Retry after a lost response must not credit twice');
  const deposits=requests.filter(([path])=>path==='/bank/deposit').map(([,o])=>JSON.parse(o.body));
  assert.equal(deposits[0].request_id,deposits[1].request_id);
  assert.match(node('#app').innerHTML,/Versamento/);
  node('#bank-transfer-amount').value='10';
  await click('bank-withdraw');
  assert.equal(bank.available,'110');
  assert.match(node('#app').innerHTML,/Prelievo/);
  node('#bank-transfer-amount').value='200';
  await click('bank-withdraw');
  assert.equal(bank.available,'110');
  assert.match(node('#app').innerHTML,/Saldo disponibile insufficiente per il prelievo/);
  const count=requests.length;
  for(const amount of ['0','-1','1.234','NaN']){
    node('#bank-transfer-amount').value=amount;
    await click('bank-deposit');
  }
  assert.equal(requests.length,count,'Invalid amounts must not trigger a mutation');
  assert.equal(bank.profit,'0');
  assert.equal(bank.bets.length,0);
  assert.equal(requests.some(([path])=>path.includes('/place')),false);
});
