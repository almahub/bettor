import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {icon} from '../../better/web/ui.mjs';

const read=path=>readFileSync(new URL('../../'+path,import.meta.url),'utf8');
test('Precision palette, offline Inter and accessible interaction states are shared by both workspaces',()=>{
 const css=read('better/web/design-system.css');
 for(const color of ['101923','1B2935','253644','46D6B2','E9F0F5','9BAFBD','E8AF54','E56F76','A995ED'])assert.ok(css.includes('#'+color));
 assert.match(css,/@font-face[^}]*InterVariable\.woff2/);
 assert.match(css,/font-variant-numeric:tabular-nums/);
 assert.match(css,/:focus-visible/);
 assert.match(css,/\[hidden\]\{display:none!important\}/);
 assert.match(css,/prefers-reduced-motion/);
 assert.match(css,/body\[data-workspace=salabet\]/);
 assert.match(css,/body\[data-workspace=salabet\]\{--background:#10251D;--surface:#19362A;--elevated:#1F3D30/);
 assert.match(css,/@media\(max-width:640px\)/);
 for(const page of ['index','salabet'])assert.match(read(`better/web/${page}.html`),/design-system\.css/);
 assert.match(read('better/web/fonts/OFL.txt'),/SIL OPEN FONT LICENSE/);
});
test('Primary navigation uses a single outline icon family',()=>{
 for(const name of ['grid','chart','ticket','wallet','layers','plug','chip','settings','database','sliders','document','validation','archive']){
  const markup=String(icon(name).value);
  assert.match(markup,/<svg/);assert.match(markup,/stroke="currentColor"/);
  assert.doesNotMatch(markup,/<circle cx="12" cy="12" r="9"\/>/);
 }
});
test('Brand variants are vector outlines and native packaging references the same icon',()=>{
 for(const theme of ['dark','light'])for(const type of ['symbol','logo']){
  const svg=read(`better/web/brand/bettor-${type}-${theme}.svg`);
  assert.match(svg,/<path/);assert.match(svg,/viewBox=/);assert.doesNotMatch(svg,/<image|<text|data:image/);
 }
 assert.match(read('packaging/Better.spec'),/icon=str\(root \/ 'better\/web\/brand\/bettor\.ico'\)/);
 assert.match(read('packaging/Better.iss'),/SetupIconFile=.*bettor\.ico/);
 assert.match(read('better/desktop/window.py'),/icon=str\(resource_root\(\)/);
 assert.match(read('better/desktop/onboarding.py'),/root\.iconbitmap/);
});
