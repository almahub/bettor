import {test} from 'node:test';
import assert from 'node:assert/strict';
import {slipStyle,styleTabs,styleGroups,stylesLegend} from '../../better/web/slip-styles.mjs';

test('styles separate systems from multiple slips and group only the same preparation',()=>{
 const rows=[{id:'a',type:'MULTIPLE'}, {id:'b',type:'SINGLE'},
  {id:'c',type:'MULTIPLE',preparation_style:{style:'SYSTEM',group_id:'group1'}},
  {id:'d',type:'MULTIPLE',preparation_style:{style:'SYSTEM',group_id:'group1'}},
  {id:'e',type:'MULTIPLE',preparation_style:{style:'SYSTEM',group_id:'group2'}}];
 assert.deepEqual(rows.map(slipStyle),['MULTIPLE','SINGLES','SYSTEM','SYSTEM','SYSTEM']);
 assert.deepEqual(styleGroups(rows.filter(s=>slipStyle(s)==='SYSTEM')).map(([,s])=>s.length),[2,1]);
 const tabs=styleTabs(rows,'SYSTEM').value;
 assert.match(tabs,/Multiple \(1\)/);assert.match(tabs,/Sistemi \(3\)/);assert.match(tabs,/Singole \(1\)/);
 assert.match(tabs,/data-slip-style="SYSTEM" aria-pressed="true"/);
});

test('existing explicitly labelled systems remain grouped without rewriting records',()=>{
 const rows=['a','b'].map(id=>({id,type:'MULTIPLE',note:'SISTEMA d5bde111 - 42 combinazioni - totale EUR 52 - esempio'}));
 const original=JSON.stringify(rows);
 assert.equal(slipStyle(rows[0]),'SYSTEM');assert.equal(styleGroups(rows).length,1);
 assert.equal(JSON.stringify(rows),original);
 assert.equal(slipStyle({id:'x',type:'MULTIPLE',note:'Idea sistema'}),'MULTIPLE');
});

test('candidate legend explains all three styles',()=>{
 const legend=stylesLegend().value;
 for(const text of ['Crea multipla','2–6','Crea sistema','42 combinazioni','alternative valide','Singole ripartite','centesimi'])assert.ok(legend.includes(text));
});
