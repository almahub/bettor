import {test} from 'node:test';
import assert from 'node:assert/strict';
import {backupControls,updateControls} from '../../better/web/backup-settings.mjs';

function dom(){
 const nodes=[];
 const element=()=>{const n={children:[],style:{},hidden:false,textContent:'',append(...a){this.children.push(...a);},replaceChildren(...a){this.children=a;},setAttribute(){}};nodes.push(n);return n;};
 globalThis.document={createElement:element};
 return {nodes,root:element(),find:text=>nodes.find(n=>n.textContent===text)};
}
test('updates show running version immediately and install only after explicit verified download and confirmation',async()=>{
 const d=dom(), calls=[];let installs=0;
 globalThis.window={pywebview:{api:{install_update:async()=>{installs++;return {status:'STARTING'};}}}};
 globalThis.fetch=async(path,opts)=>{
  calls.push([path,opts]);
  const data=path.endsWith('/status')?{current:'0.2.8'}:path.endsWith('/download')?{status:'READY',version:'0.2.9'}:{status:'AVAILABLE',current:'0.2.8',latest:'0.2.9',url:'https://github.com/almahub/bettor/releases/tag/v0.2.9'};
  return {ok:true,json:async()=>data};
 };
 await updateControls(d.root,'token');
 assert.ok(d.find('Versione attuale: 0.2.8'));
 const download=d.find('Scarica e verifica aggiornamento'),install=d.find('Installa aggiornamento e riavvia Bettor');
 assert.equal(download.hidden,true);assert.equal(install.hidden,true);assert.equal(installs,0);
 await d.find('Controlla aggiornamenti').onclick();assert.equal(download.hidden,false);
 await download.onclick();assert.equal(install.hidden,false);
 assert.equal(calls.at(-1)[1].method,'POST');assert.equal(calls.at(-1)[1].headers['X-Better-Settings-Token'],'token');
 globalThis.confirm=()=>false;await install.onclick();assert.equal(installs,0);
 globalThis.confirm=()=>true;await install.onclick();assert.equal(installs,1);
 assert.equal(d.find('Note della release su GitHub').hidden,false);
});
test('pending restore has an explicit native restart and explains retained backups; opening the panel never restores',async()=>{
 const d=dom();let restarts=0;
 globalThis.window={pywebview:{api:{restart:async()=>{restarts++;return {status:'STARTING'};}}}};
 const calls=[];
 globalThis.fetch=async(path,opts)=>{calls.push([path,opts]);return {ok:true,json:async()=>({backups:[],pending_restore:true,data_directory:'C:/User/Better'})};};
 await backupControls(d.root,'token');
 const button=d.find('Riavvia Bettor e applica ripristino');assert.equal(button.hidden,false);
 assert.ok(d.nodes.some(n=>n.textContent.includes('non vengono cercati in tutto il PC')));
 assert.ok(d.nodes.some(n=>n.textContent.includes('C:/User/Better')));
 assert.equal(restarts,0);assert.equal(calls.length,1);assert.equal(calls[0][1].method,'GET');
 await button.onclick();assert.equal(restarts,1);
});
test('older browser-only windows receive working manual backend stop instructions',async()=>{
 const d=dom();globalThis.window={};
 globalThis.fetch=async()=>({ok:true,json:async()=>({backups:[],pending_restore:true})});
 await backupControls(d.root,'token');await d.find('Riavvia Bettor e applica ripristino').onclick();
 assert.ok(d.nodes.some(n=>n.textContent.includes('Arresta servizio Bettor')&&n.textContent.includes('non arresta il backend')));
});
