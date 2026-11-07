async function nativeCommand(action, status) {
 const api=typeof window!=='undefined'?window.pywebview?.api:null;
 if(!api?.[action]){status.textContent='Riavvio integrato non disponibile in questa finestra/versione. Chiudi la finestra, usa dal menu Start "Arresta servizio Bettor", poi riapri Bettor. Chiudere soltanto la finestra non arresta il backend.';return;}
 status.textContent=action==='restart'?'Riavvio controllato in corso: attendo la chiusura del backend...':'Chiusura di Bettor, verifica del setup e installazione in corso...';
 const result=await api[action]();
 if(result?.status==='ERROR')status.textContent=result.message;
}

export async function backupControls(root, token){
 const panel=document.createElement('section');panel.className='panel maintenance-panel';panel.style.marginTop='24px';root.append(panel);
 const title=document.createElement('h2');title.textContent='Backup e ripristino';panel.append(title);
 const note=document.createElement('p');note.textContent='Backup completo dei dati locali, esclusi gli altri backup. Il ripristino sostituisce l’archivio corrente: non unisce due database. Prima della sostituzione viene creata una copia di sicurezza. Su un altro PC occorre reinserire le chiavi API.';panel.append(note);
 const folder=document.createElement('p');folder.className='secondary';panel.append(folder);
 const limit=document.createElement('p');limit.textContent='Ultimi 6 backup. I backup precedenti rimangono nella cartella dati anche scegliendo un nuovo archivio: non vengono cercati in tutto il PC.';panel.append(limit);
 const status=document.createElement('p');status.className='maintenance-status';status.setAttribute('role','status');panel.append(status);
 async function request(path,body){const response=await fetch('/system/settings'+path,{method:body===undefined?'GET':'POST',headers:{'X-Better-Settings-Token':token},body,cache:'no-store'});const value=await response.json();if(!response.ok)throw Error(typeof value.detail==='string'?value.detail:'Operazione non riuscita.');return value;}
 function button(label,action){const element=document.createElement('button');element.className='btn small';element.type='button';element.textContent=label;element.onclick=async()=>{element.disabled=true;try{await action();}catch(error){status.textContent=error.message;}finally{element.disabled=false;}};panel.append(element);return element;}
 const restart=button('Riavvia Bettor e applica ripristino',()=>nativeCommand('restart',status));restart.className='btn primary';restart.hidden=true;
 const instructions=document.createElement('p');instructions.className='secondary';instructions.textContent='Il ripristino si applica solo a backend fermo. Il pulsante chiude Bettor, arresta il servizio in sicurezza e riapre l’applicazione. Se il pulsante non è supportato dalla versione installata: chiudi la finestra > menu Start > Arresta servizio Bettor > riapri Bettor.';instructions.hidden=true;panel.append(instructions);
 const list=document.createElement('div');let records=[];
 function draw(){list.replaceChildren();for(const item of records.slice(0,6)){const row=document.createElement('p');row.className='backup-row';const caption=document.createElement('span');caption.textContent=item.name+' · '+(item.size/1024/1024).toFixed(1)+' MB ';const download=document.createElement('a');download.textContent='Scarica';download.href='/system/settings/backups/download/'+encodeURIComponent(item.name);const restore=document.createElement('button');restore.className='btn small';restore.textContent='Ripristina';restore.type='button';restore.onclick=async()=>{if(!confirm('Sostituire tutti i dati attuali con questo backup al prossimo riavvio? Verrà prima creata una copia di sicurezza.'))return;restore.disabled=true;try{const result=await request('/backups/restore/'+encodeURIComponent(item.name),'');await refresh();status.textContent=result.message;}catch(error){status.textContent=error.message;}finally{restore.disabled=false;}};row.append(caption,download,restore);list.append(row);}}
 async function refresh(){const value=await request('/backups');records=value.backups;draw();folder.textContent=value.data_directory?'Cartella dati e backup di questa installazione: '+value.data_directory:'';restart.hidden=!value.pending_restore;instructions.hidden=!value.pending_restore;if(value.pending_restore)status.textContent='Ripristino verificato e in attesa: i dati non sono ancora stati sostituiti. Riavvia Bettor con il pulsante qui sotto; riaprire soltanto la finestra non basta.';else if(value.last_restore)status.textContent=value.last_restore.message+(value.last_restore.credentials_restored===false?' Reinserisci le chiavi API in Impostazioni.':'');}
 button('Crea backup completo',async()=>{status.textContent='Creazione e verifica del backup in corso...';const result=await request('/backups','');await refresh();status.textContent='Backup verificato: '+result.name+'. Scaricalo e conservalo anche su un disco esterno.';});
 const file=document.createElement('input');file.type='file';file.accept='.zip';file.setAttribute('aria-label','Seleziona backup Bettor da importare');panel.append(file);
 button('Importa backup',async()=>{const selected=file.files[0];if(!selected){status.textContent='Seleziona un backup ZIP Bettor.';return;}if(!confirm('Importare questo backup e sostituire i dati attuali al prossimo riavvio?'))return;status.textContent='Caricamento e verifica...';const result=await request('/backups/import',selected);file.value='';await refresh();status.textContent=result.message;});
 const cancel=button('Annulla ripristino programmato',async()=>{await request('/backups/cancel','');await refresh();status.textContent='Ripristino annullato. Nessun dato sostituito.';});
 panel.append(list);try{await refresh();}catch(error){status.textContent=error.message;}
}

export async function loadSystemTools(kind){
 const root=document.querySelector('#system-tools');if(!root)return;
 root.dataset.kind=kind;
 try{const response=await fetch('/system/settings',{cache:'no-store'});if(!response.ok)throw Error('Impostazioni non disponibili.');const data=await response.json();root.replaceChildren();if(kind==='backup'){await backupControls(root,data.token);return;}await updateControls(root,data.token);}catch(error){root.textContent=error.message;}
}

export async function updateControls(root, token){
 const panel=document.createElement('section');panel.className='panel maintenance-panel';root.append(panel);
 const title=document.createElement('h2');title.textContent='Aggiornamenti';panel.append(title);
 const version=document.createElement('p');version.className='installed-version';version.textContent='Versione attuale: verifica in corso...';panel.append(version);
 const status=document.createElement('p');status.className='maintenance-status';status.setAttribute('role','status');panel.append(status);
 async function request(path,write=false){const response=await fetch('/system/settings'+path,{method:write?'POST':'GET',headers:write?{'X-Better-Settings-Token':token}:{},cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'Verifica aggiornamenti non disponibile.');return data;}
 function button(label,action){const b=document.createElement('button');b.className='btn small';b.type='button';b.textContent=label;b.onclick=async()=>{b.disabled=true;try{await action();}catch(error){status.textContent=error.message;}finally{b.disabled=false;}};panel.append(b);return b;}
 const install=button('Installa aggiornamento e riavvia Bettor',async()=>{if(!confirm('Bettor verr? chiuso e aggiornato, poi riaperto. Il setup creer? prima un backup dei dati. Continuare?'))return;await nativeCommand('install_update',status);});install.className='btn primary';install.hidden=true;
 const download=button('Scarica e verifica aggiornamento',async()=>{status.textContent='Download del setup e verifica SHA256 in corso...';const result=await request('/updates/download',true);status.textContent='Bettor '+result.version+' scaricato e verificato. Puoi installarlo e riavviare.';install.hidden=false;download.hidden=true;});download.hidden=true;
 const release=document.createElement('a');release.target='_blank';release.rel='noopener noreferrer';release.textContent='Note della release su GitHub';release.hidden=true;
 button('Controlla aggiornamenti',async()=>{const result=await request('/updates');version.textContent='Versione attuale: '+(result.current||'non rilevata');status.textContent=result.status==='AVAILABLE'?'Disponibile Bettor '+result.latest+'. Scarica e verifica il setup direttamente da qui.':result.status==='CURRENT'?'Bettor è aggiornato.':result.status==='NO_RELEASE'?'Nessuna versione pubblicata disponibile.':'Verifica GitHub non disponibile: riprova più tardi.';download.hidden=result.status!=='AVAILABLE';if(result.url){release.href=result.url;release.hidden=false;}});
 panel.append(release);
 const note=document.createElement('p');note.className='secondary';note.textContent='L’aggiornamento richiede una tua conferma. Non reinstalla i dati iniziali e non azzera Banca o schedine. Analisi e checkpoint in corso bloccano l’arresto. Nessuna installazione da file privi di checksum verificato.';panel.append(note);
 try{const info=await request('/updates/status');version.textContent='Versione attuale: '+(info.current||'non rilevata');if(info.prepared){install.hidden=false;status.textContent='Aggiornamento '+info.prepared.version+' già scaricato e verificato.';}if(info.pending_restore){install.hidden=true;status.textContent='Completa o annulla il ripristino programmato prima di aggiornare.';}}catch(error){version.textContent='Versione attuale: non rilevata';status.textContent=error.message;}
}
