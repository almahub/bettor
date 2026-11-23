// Current errors are separate from the immutable history of failed calls.
export function activeResearchErrors(calls,now=Date.now()){
 const latest=new Map();
 for(const call of [...(calls||[])].sort((a,b)=>Date.parse(b.completed_at)-Date.parse(a.completed_at))){
  const at=Date.parse(call.completed_at);
  if(!Number.isFinite(at)||at>now||now-at>24*60*60*1000)continue;
  const key=call.agent_id||call.research_type||'research';
  if(!latest.has(key))latest.set(key,call);
 }
 return [...latest.values()].filter(call=>/ERROR|FAILED|TIMEOUT|UNAUTHORIZED|401|403|RATE_LIMIT/i.test(String(call.status)));
}
