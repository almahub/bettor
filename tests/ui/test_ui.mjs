import {test} from 'node:test';
import assert from 'node:assert/strict';
import {html,mount,percent,number,points,money,safeURL,statuses,statusInfo,isItalianLeagueName,diagnosticCauses} from '../../better/web/ui.mjs';

test('local archive miss is distinct from provider failure and unpublished lineup',()=>{
  const causes=diagnosticCauses({input_manifest:{failures:{lineups:{source:'local_archive',code:'MISSING_DATA',message:'Archivio locale: dato assente o scaduto'}}}});
  assert.ok(causes.some(c=>c.code==='ARCHIVE_DATA_UNAVAILABLE' && c.label.includes('locale')));
  assert.ok(!causes.some(c=>['API_PROVIDER_ERROR','LINEUP_NOT_PUBLISHED_YET'].includes(c.code)));
});

test('research diagnostics separate rejected evidence, unpublished data and disconnected inputs',()=>{
  const causes=diagnosticCauses({
    input_manifest:{research_result:{findings:[
      {field:'injuries',status:'INSUFFICIENT_EVIDENCE'},
      {field:'confirmed_lineups',status:'NOT_FOUND',notes:'Lineups are not announced as of the cutoff'},
    ]}},
    results:{tactical_analyst:{status:'INSUFFICIENT_DATA',errors:[{message:'Event/video-derived traits unavailable; no invented matchup'}]}},
  });
  for(const code of ['EVIDENCE_REJECTED','LINEUP_NOT_PUBLISHED_YET','DATA_NOT_CONNECTED'])assert.ok(causes.some(c=>c.code===code),code);
  assert.ok(!causes.some(c=>c.code==='RESEARCH_FAILED'));
});

test('indicative input alone does not falsely imply insufficient validator sample',()=>{
  const run={forecast:{markets:[{method:'RAW_POISSON',operationally_eligible:false,reason:'MODEL_INPUT_SAMPLE_BELOW_OPERATIONAL_MINIMUM'}]},results:{model_validator:{status:'OK',payload:{validations:[{validation_status:'VALIDATED',sample_size:220}]}},lineup_player_impact:{status:'INSUFFICIENT_DATA'}}};
  let causes=diagnosticCauses(run);
  assert.ok(!causes.some(c=>['MODEL_VALIDATION_INSUFFICIENT','LINEUP_NOT_PUBLISHED_YET'].includes(c.code)));
  run.results.model_validator.payload.validations[0].validation_status='INSUFFICIENT_SAMPLE';
  causes=diagnosticCauses(run);
  assert.ok(causes.some(c=>c.code==='MODEL_VALIDATION_INSUFFICIENT'));
});

test('provider text and nested arrays cannot inject HTML or attributes',()=>{
  const node={innerHTML:''};
  mount(node,html`<p title="${'" onmouseover="alert(1)'}">${['<script>run()</script>',html`<strong>${'A & B'}</strong>`]}</p>`);
  assert.equal(node.innerHTML,'<p title="&quot; onmouseover=&quot;alert(1)">&lt;script&gt;run()&lt;/script&gt;<strong>A &amp; B</strong></p>');
});
test('missing values never appear as zero while actual zeros are retained',()=>{
  assert.equal(number(null),'—');
  assert.equal(percent(undefined),'—');
  assert.equal(percent(0),'0,0%');
  assert.equal(money(0),'0,00 u');
  assert.equal(points(0.015),'+1,50 pp');
});
test('source links allow HTTP(S) only',()=>{
  for(const url of ['javascript:alert(1)','data:text/html,attack','file:///C:/secret','//host/path','bad url'])assert.equal(safeURL(url),null);
  assert.equal(safeURL('https://example.com/source'),'https://example.com/source');
});
test('all six decision states remain distinct, unknown status never becomes BET',()=>{
  assert.equal(Object.keys(statuses).length,6);
  assert.equal(new Set(Object.values(statuses).map(s=>s.label)).size,6);
  assert.equal(statusInfo('unrecognized').tone,'neutral');
  assert.equal(statusInfo('NO_BET').short,'NO BET');
});
test('analysis competition filter recognizes supported Italian leagues only',()=>{
  for(const name of ['Serie A','Serie B','Serie C Girone A','Serie C - Girone B','serie c girone c'])assert.equal(isItalianLeagueName(name),true,name);
  for(const name of ['Brasileirao Serie A','Premier League','Coppa Italia','Serie A Women','Serie D'])assert.equal(isItalianLeagueName(name),false,name);
});
test('insufficient data diagnostics distinguish operational root causes',()=>{
  const run={
    input_manifest:{
      failures:{lineups:{message:'LINEUP_NOT_PUBLISHED_YET'},stats:{message:'IDENTITY_MAPPING_NOT_FOUND: nessuna corrispondenza'},news:{message:'OpenAI non disponibile: autenticazione fallita',source:'openai_research',code:'PERMANENT_PROVIDER_ERROR'}},
      resolved_config:{min_sample:200},
      external_research:{status:'EXTERNAL_RESEARCH_ERROR',provider:'openai_web_search',error:'OPENAI_API_KEY non valida o revocata'},
    },
    results:{
      football_odds_analyst:{status:'OK',payload:{quotes:[{odds:2}]}},
      lineup_player_impact:{status:'INSUFFICIENT_DATA',summary:'Official lineups unavailable'},
      model_validator:{status:'INSUFFICIENT_DATA',payload:{validations:[]}},
      predictive_model:{status:'PARTIAL',unknowns:['minimum_training_sample:7/10'],payload:{model_predictions:[]}},
    },
    forecast:{markets:[{admitted_for_decision:false}]},
  };
  const causes=diagnosticCauses(run);
  assert.deepEqual(causes.map(c=>c.code),['IDENTITY_MAPPING_MISSING','OPENAI_AUTH_ERROR','LINEUP_NOT_PUBLISHED_YET','MODEL_INPUT_INSUFFICIENT','MODEL_VALIDATION_INSUFFICIENT','MARKET_UNSUPPORTED','RESEARCH_FAILED']);
  assert.match(causes.find(c=>c.code==='LINEUP_NOT_PUBLISHED_YET').detail,/non sono ancora disponibili/);
  assert.match(causes.find(c=>c.code==='OPENAI_AUTH_ERROR').label,/autenticazione fallita/);
  assert.match(causes.find(c=>c.code==='MODEL_VALIDATION_INSUFFICIENT').detail,/0\/200/);
  assert.match(causes.find(c=>c.code==='MODEL_INPUT_INSUFFICIENT').detail,/7\/10/);
});

test('expired research credentials have a precise cause without a generic auth duplicate',()=>{
  const run={results:{},input_manifest:{failures:{news:{message:'OpenAI non disponibile: autenticazione fallita (OPENAI_KEY_EXPIRED)'}},research_result:{status:'EXTERNAL_RESEARCH_ERROR',error:'OPENAI_API_KEY scaduta (OPENAI_KEY_EXPIRED)'}}};
  const causes=diagnosticCauses(run);
  assert.ok(causes.some(c=>c.code==='OPENAI_KEY_EXPIRED'&&c.label==='Chiave OpenAI scaduta'));
  assert.ok(!causes.some(c=>c.code==='OPENAI_AUTH_ERROR'));
});
