import {test} from 'node:test';
import assert from 'node:assert/strict';
import {activeResearchErrors} from '../../better/web/research-health.mjs';

test('historical errors clear after recovery and old failures are not current alerts',()=>{
 const now=Date.parse('2026-11-09T12:00:00Z');
 const row=(status,at,agent_id='a')=>({status,completed_at:at,agent_id});
 assert.equal(activeResearchErrors([row('EXTERNAL_RESEARCH_ERROR','2026-11-09T10:00:00Z'),row('OK','2026-11-09T11:00:00Z')],now).length,0);
 assert.equal(activeResearchErrors([row('EXTERNAL_RESEARCH_ERROR','2026-11-07T10:00:00Z')],now).length,0);
 assert.equal(activeResearchErrors([row('EXTERNAL_RESEARCH_ERROR','2026-11-09T11:00:00Z'),row('OK','2026-11-09T11:30:00Z','b')],now).length,1);
});
