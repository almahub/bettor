import {html,date,shortId} from './ui.mjs';

export function pendingBetReminder(slips, now=Date.now()) {
  const pending=(slips || []).filter(s=>['DRAFT','READY'].includes(s.status));
  const rows=pending.map(s=>{
    const times=(s.selections || []).map(x=>Date.parse(x.kickoff));
    const kickoff=times.length && times.every(Number.isFinite)?Math.min(...times):null;
    return {id:s.id,kickoff,deadline:kickoff==null?null:kickoff-15*60000};
  });
  const future=rows.filter(r=>r.kickoff!=null && r.kickoff>now).sort((a,b)=>a.deadline-b.deadline);
  const started=rows.filter(r=>r.kickoff!=null && r.kickoff<=now).sort((a,b)=>b.kickoff-a.kickoff);
  const next=future[0] || started[0] || null;
  const remaining=next?next.deadline-now:null;
  const status=!pending.length?'EMPTY':!next?'UNKNOWN':next.kickoff<=now?'STARTED':remaining<=0?'OVERDUE':remaining<=15*60000?'URGENT':remaining<=60*60000?'SOON':'SCHEDULED';
  return {count:pending.length,started:started.length,unknown:rows.filter(r=>r.kickoff==null).length,next,remaining,status,
    tone:['STARTED','OVERDUE','URGENT'].includes(status)?'danger':status==='SOON'?'warning':'neutral'};
}

export function reminderDuration(milliseconds) {
  if(milliseconds==null)return '—';
  const seconds=Math.max(0,Math.ceil(milliseconds/1000));
  const hours=Math.floor(seconds/3600);
  return `${String(hours).padStart(2,'0')}:${String(Math.floor(seconds%3600/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
}

export function betReminderCard(slips,{now=Date.now(),loaded=true,error=false}={}) {
  const r=pendingBetReminder(slips,now);
  const labels={EMPTY:'Nessuna schedina da confermare',UNKNOWN:'Orario non disponibile',STARTED:'Partita iniziata',OVERDUE:'Termine consigliato superato',URGENT:'Scadenza vicina',SOON:'Da confermare entro un’ora',SCHEDULED:'Promemoria di conferma'};
  return html`<section class="panel bet-reminder ${error?'warning':r.tone}" aria-labelledby="bet-reminder-title"><div><h2 id="bet-reminder-title">Schedine da confermare</h2><strong>${!loaded?'Verifica SalaBet…':error?'Stato SalaBet da verificare':labels[r.status]}</strong>${loaded?html`<span class="secondary">${r.count} schedine DRAFT / READY in attesa${r.started?` · ${r.started} con partita già iniziata`:''}${r.unknown?` · ${r.unknown} senza orario verificabile`:''}</span>`:''}${error?html`<p>Dati non aggiornati. Apri SalaBet per verificare lo stato attuale.</p>`:''}${loaded&&r.next?html`<p>Schedina ${shortId(r.next.id)} · termine consigliato <strong>${date(new Date(r.next.deadline).toISOString())}</strong> · primo kickoff ${date(new Date(r.next.kickoff).toISOString())}</p>`:''}</div>${loaded&&r.next&&r.next.kickoff>now?html`<div class="reminder-countdown"><span>${r.remaining>0?'Tempo al termine consigliato':'Termine superato da'}</span><strong role="timer" aria-live="off">${reminderDuration(Math.abs(r.remaining))}</strong></div>`:''}<a class="btn" href="/salabet#open">Vai a Scommesse aperte</a><p class="reminder-note">Promemoria: primo kickoff meno 15 minuti. READY non significa giocata. Nessuna conferma automatica; funziona con Dashboard aperta.</p></section>`;
}
