import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pendingBetReminder,reminderDuration,betReminderCard} from '../../better/web/bet-reminder.mjs';
import {mount} from '../../better/web/ui.mjs';
const kickoff=Date.parse('2026-11-06T19:45:00Z');
const slip=(id,status='READY',times=[kickoff])=>({id,status,selections:times.map(t=>({kickoff:new Date(t).toISOString()}))});
test('reminder uses earliest leg and excludes every placed or closed status',()=>{
 const rows=[slip('draft','DRAFT',[kickoff+100000,kickoff]),slip('ready'),...['OPEN','PLACED','WON','LOST','VOID','PARTIAL_VOID','CANCELLED'].map(s=>slip(s,s))];
 const before=JSON.stringify(rows);
 const r=pendingBetReminder(rows,kickoff-2*3600000);
 assert.equal(r.count,2);assert.equal(r.next.deadline,kickoff-15*60000);assert.equal(r.status,'SCHEDULED');
 assert.equal(JSON.stringify(rows),before);
});
test('warning and urgency boundaries are based on kickoff minus fifteen minutes',()=>{
 const deadline=kickoff-15*60000;
 assert.equal(pendingBetReminder([slip('1')],deadline-60*60000).status,'SOON');
 assert.equal(pendingBetReminder([slip('1')],deadline-15*60000).status,'URGENT');
 assert.equal(pendingBetReminder([slip('1')],deadline).status,'OVERDUE');
 assert.equal(pendingBetReminder([slip('1')],kickoff).status,'STARTED');
 assert.equal(reminderDuration(25*3600000+61000),'25:01:01');
 assert.equal(reminderDuration(-1),'00:00:00');
});
test('old unplaced tickets do not hide the next future deadline and malformed times stay explicit',()=>{
 const r=pendingBetReminder([slip('past','DRAFT',[kickoff-86400000]),slip('future'),{id:'bad',status:'DRAFT',selections:[{kickoff:'bad'}]}],kickoff-3600000);
 assert.equal(r.next.id,'future');assert.equal(r.started,1);assert.equal(r.unknown,1);
 assert.equal(pendingBetReminder([],kickoff).status,'EMPTY');
 assert.equal(pendingBetReminder([{id:'bad',status:'DRAFT',selections:[]}],kickoff).status,'UNKNOWN');
});
test('card links to open tickets, displays timer and never claims a placed ticket or hides stale data',()=>{
 const node={innerHTML:''};
 mount(node,betReminderCard([slip('s1')],{now:kickoff-3600000}));
 assert.match(node.innerHTML,/href="\/salabet#open"/);assert.match(node.innerHTML,/role="timer" aria-live="off"/);
 assert.match(node.innerHTML,/00:45:00/);assert.match(node.innerHTML,/READY non significa giocata/);
 mount(node,betReminderCard([],{error:true,loaded:false}));
 assert.match(node.innerHTML,/Dati non aggiornati/);assert.doesNotMatch(node.innerHTML,/Nessuna schedina da confermare/);
});
