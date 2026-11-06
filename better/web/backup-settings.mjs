export async function backupControls(root, token){
 const panel=document.createElement('section');panel.className='panel';panel.style.marginTop='24px';root.append(panel);
 const title=document.createElement('h2');title.textContent='Backup e ripristino';panel.append(title);
 const note=document.createElement('p');note.textContent='Backup completo dei dati locali, esclusi gli altri backup. Il ripristino sostituisce l’archivio corrente: non unisce due database. Prima della sostituzione viene creata una copia di sicurezza. Su un altro PC occorre reinserire le chiavi API.';panel.append(note);const limit=document.createElement('p');limit.textContent='Ultimi 6 backup. Le copie precedenti restano conservate sul disco.';panel.append(limit);
 const status=document.createElement('p');status.setAttribute('role','status');panel.append(status);
 async function request(path,body){const response=await fetch('/system/settings'+path,{method:body===undefined?'GET':'POST',headers:{'X-Better-Settings-Token':token},body,cache:'no-store'});const value=await response.json();if(!response.ok)throw Error(typeof value.detail==='string'?value.detail:'Operazione non riuscita.');return value;}
 function button(label,action){const element=document.createElement('button');element.className='btn small';element.type='button';element.textContent=label;element.onclick=async()=>{element.disabled=true;try{await action();}catch(error){status.textContent=error.message;}finally{element.disabled=false;}};panel.append(element);return element;}
 const list=document.createElement('div');let records=[];
 function draw(){list.replaceChildren();for(const item of records.slice(0,6)){const row=document.createElement('p');const caption=document.createElement('span');caption.textContent=item.name+' · '+(item.size/1024/1024).toFixed(1)+' MB ';const download=document.createElement('a');download.textContent='Scarica';download.href='/system/settings/backups/download/'+encodeURIComponent(item.name);const restore=document.createElement('button');restore.className='btn small';restore.textContent='Ripristina';restore.type='button';restore.onclick=async()=>{if(!confirm('Sostituire tutti i dati attuali con questo backup al prossimo riavvio? Verrà prima creata una copia di sicurezza.'))return;restore.disabled=true;try{const result=await request('/backups/restore/'+encodeURIComponent(item.name),'');status.textContent=result.message;}catch(error){status.textContent=error.message;}finally{restore.disabled=false;}};row.append(caption,download,restore);list.append(row);}}
 async function refresh(){const value=await request('/backups');records=value.backups.slice(0,6);draw();if(value.pending_restore)status.textContent='Ripristino programmato: arresta il backend e riavvia Bettor. Collector e modifiche sono sospesi fino al riavvio o all’annullamento.';else if(value.last_restore)status.textContent=value.last_restore.message;}
 button('Crea backup completo',async()=>{status.textContent='Creazione e verifica del backup in corso…';const result=await request('/backups','');await refresh();status.textContent='Backup verificato: '+result.name+'. Scaricalo e conservalo anche su un disco esterno.';});
 const file=document.createElement('input');file.type='file';file.accept='.zip';file.setAttribute('aria-label','Seleziona backup Bettor da importare');panel.append(file);
 button('Importa backup',async()=>{const selected=file.files[0];if(!selected){status.textContent='Seleziona un backup ZIP Bettor.';return;}if(!confirm('Importare questo backup e sostituire i dati attuali al prossimo riavvio?'))return;status.textContent='Caricamento e verifica…';const result=await request('/backups/import',selected);file.value='';status.textContent=result.message;});
 button('Annulla ripristino programmato',async()=>{await request('/backups/cancel','');status.textContent='Ripristino annullato. Nessun dato sostituito.';});

 panel.append(list);try{await refresh();}catch(error){status.textContent=error.message;}
}

export async function loadSystemTools(kind){
 const root=document.querySelector('#system-tools');if(!root)return;
 root.dataset.kind=kind;
 try{const response=await fetch('/system/settings',{cache:'no-store'});if(!response.ok)throw Error('Impostazioni non disponibili.');const data=await response.json();root.replaceChildren();if(kind==='backup'){await backupControls(root,data.token);return;}await updateControls(root);}catch(error){root.textContent=error.message;}
}

export async function updateControls(root){
 const panel=document.createElement('section');panel.className='panel';root.append(panel);
 const title=document.createElement('h2');title.textContent='Aggiornamenti';panel.append(title);
 const status=document.createElement('p');status.setAttribute('role','status');panel.append(status);
 async function request(path){const response=await fetch('/system/settings'+path,{cache:'no-store'});if(!response.ok)throw Error('Verifica aggiornamenti non disponibile.');return response.json();}
 function button(label,action){const b=document.createElement('button');b.className='btn small';b.textContent=label;b.onclick=async()=>{b.disabled=true;try{await action();}catch(error){status.textContent=error.message;}finally{b.disabled=false;}};panel.append(b);}
 button('Controlla aggiornamenti',async()=>{const result=await request('/updates');status.textContent=result.status==='AVAILABLE'?'Disponibile Bettor '+result.latest+'. Scarica il setup e il checksum; installa con Bettor arrestato.':result.status==='CURRENT'?'Bettor è aggiornato ('+result.current+').':result.status==='NO_RELEASE'?'Nessuna versione pubblicata disponibile.':'Verifica GitHub non disponibile: riprova più tardi.';if(result.url){const a=document.createElement('a');a.href=result.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=' Apri release GitHub';status.append(a);}});
}
