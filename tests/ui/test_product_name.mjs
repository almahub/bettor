import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('Bettor is the visible product name while technical compatibility is preserved',()=>{
 const read=p=>readFileSync(new URL('../../'+p,import.meta.url),'utf8');
 assert.match(read('better/desktop/window.py'),/create_window\("Bettor"/);
 assert.match(read('better/desktop/onboarding.py'),/Bettor — Configurazione iniziale/);
 assert.match(read('better/web/index.html'),/<title>Bettor/);
 assert.match(read('packaging/Better.iss'),/AppName=Bettor/);
 assert.match(read('better/web/dashboard.mjs'),/>bettor</);
 assert.match(read('better/desktop/launcher.py'),/X-Better-Settings-Token/);
 assert.match(read('packaging/Better.iss'),/Programs\\Better/);
});
