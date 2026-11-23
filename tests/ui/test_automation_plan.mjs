import {test} from 'node:test';
import assert from 'node:assert/strict';
import {automationPlan,preparationCard} from '../../better/web/automation-plan.mjs';
import {placementPreview} from '../../better/web/placement-review.mjs';
test('separate preparation and confirmation with explicit style and cumulative budget',()=>{
 const c={style:'SYSTEM',budget_eur:'50',preparation_enabled:true,confirmation_enabled:false,
  allocated_eur:'0',unallocated_eur:'50',preview:{tickets:[],total_eur:'0',message:'Alternative mancanti'}};
 const m=automationPlan(c).value;
 assert.match(m,/automation-style/);assert.match(m,/SYSTEM" selected/);
 assert.match(m,/Singole ripartite/);assert.match(m,/42 combinazioni/);
 assert.match(m,/settimanale/);assert.match(m,/Conferma con addebito Banca/);
 assert.match(m,/INDICATIVE restano DRAFT/);assert.match(m,/Alternative mancanti/);
 assert.match(preparationCard(c).value,/Nessun addebito/);
 const blocked=placementPreview({...c,workflow_version:2,preview:{tickets:[{}],operationally_admitted:false}},false).value;
 assert.match(blocked,/INDICATIVE_NOT_ADMITTED/);assert.doesNotMatch(blocked,/data-action="placement-approve"/);
});
