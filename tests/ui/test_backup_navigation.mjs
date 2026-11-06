import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {backupControls,updateControls} from '../../better/web/backup-settings.mjs';

test('backup displays only the latest six without deleting files; updates are separate',async()=>{
 const nodes=[];
 function element(){const node={children:[],style:{},textContent:'',append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(){}};nodes.push(node);return node;}
 globalThis.document={createElement:element};
 const calls=[];
 globalThis.fetch=async(path,options)=>{calls.push([path,options]);return {ok:true,json:async()=>({backups:Array.from({length:9},(_,i)=>({name:`Better-Backup-${9-i}.zip`,size:1024}))})};};
 const root=element();await backupControls(root,'token');
 assert.equal(nodes.filter(n=>n.textContent==='Ripristina').length,6);
 assert.equal(nodes.filter(n=>n.textContent==='Controlla aggiornamenti').length,0);
 assert.ok(!nodes.some(n=>n.textContent.startsWith('Better-Backup-3.zip')));
 assert.equal(calls.length,1);assert.equal(calls[0][1].method,'GET');
 await updateControls(element());
 assert.equal(nodes.filter(n=>n.textContent==='Controlla aggiornamenti').length,1);
 const settings=readFileSync(new URL('../../better/web/settings.mjs',import.meta.url),'utf8');
 assert.doesNotMatch(settings,/backupControls/);
});
