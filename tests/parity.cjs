const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
if(!process.argv[2]){
 require('node:child_process').execFileSync('python',['-B','-X','utf8',path.join(__dirname,'test_engine.py'),'--parity-only'],{stdio:'inherit'});
 process.exit(0);
}
const bundle=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const root=path.dirname(__dirname);
const ctx=vm.createContext({bundle,RAW:{schemas:bundle.raw.schemas},URL});
function run(code){return vm.runInContext(code,ctx);}
const html=path.join(root,'rotation_quant_lab_v4_0_offline.html');
if(fs.existsSync(html)){
 const script=[...fs.readFileSync(html,'utf8').matchAll(/<script>([\s\S]*?)<\/script>/g)];
 assert.equal(script.length,7);
 run(script.slice(1,4).map(x=>x[1]).join('\n'));
}else run(['engine','input','analytics'].map(n=>fs.readFileSync(path.join(root,'offline',n+'.js'),'utf8')).join('\n'));
function compare(actual,target,label){
 if(actual===target)return;
 if(typeof actual==='number'&&typeof target==='number'){
  assert.ok(Math.abs(actual-target)<=1e-7+Math.abs(target)*5e-15,label+': '+actual+' vs '+target);return;
 }
 if(actual&&target&&typeof actual==='object'&&typeof target==='object'){
  const keys=Object.keys(actual).sort();
  assert.deepEqual(keys,Object.keys(target).sort(),label);
  for(const k of keys)compare(actual[k],target[k],label+'.'+k);
  return;
 }
 assert.equal(actual,target,label);
}
const d=run('const d=prepare(bundle.raw,bundle.raw.policy.freeze,bundle.fixture!==false);d');
compare(d,bundle.d,'validate');
const s=run('const s=scan(d);s');
compare(s,bundle.states,'scans');
let orders=0,rows=0;
for(let i=0;i<bundle.paths.length;i++){
 const v=run('replay(d,s,...bundle.paths['+i+'].key)'),ref=bundle.paths[i].r;
 compare(v,ref,'replay '+i);
 for(const [j,r] of v.orders.entries())assert.equal(JSON.stringify(r),JSON.stringify(ref.orders[j]));
 v.path.forEach((r,j)=>{assert.equal(r.equity,ref.path[j].equity);assert.equal(r.cash,ref.path[j].cash);});
 rows+=v.path.length;orders+=v.orders.length;
}
if(bundle.radar)compare(run('radar(d,s)'),bundle.radar,'ETF radar');
compare(run("leaders(d,s,d.policy.windows.W5)"),bundle.leaders,'stock profiles');
if(bundle.corr)compare(run(`(() => {
 const profiles=leaders(d,s,d.policy.windows.W1);
 return Object.fromEntries(profiles.map(a=>[a.code,Object.fromEntries(profiles.map(b=>
  [b.code,correlated(a.returns,b.returns,d.days)]))]));
})()`),bundle.corr,'correlations');
const short=run(`const cut=bundle.prefix.date; const raw=JSON.parse(JSON.stringify(bundle.raw));
 const short=prepare(raw,cut,bundle.fixture!==false);
 const sub=scan(short);sub`);
compare(short.states,s.states.slice(0,short.states.length),'prefix stocks');
compare(short.ranks,s.ranks.slice(0,short.ranks.length),'prefix rankings');
if(bundle.radar)compare(run('radar(short,sub).daily'),bundle.radar.daily.slice(0,short.states.length),'prefix ETF');
for(let i=0;i<bundle.paths.length;i++){
 const span=bundle.paths[i].key[1];
 if(span[0]>bundle.prefix.date||span[1]<bundle.prefix.date)continue;
 const actual=run(`(()=>{const [mode,span,rate,delay,slip]=bundle.paths[${i}].key;return replay(short,sub,mode,[span[0],cut],rate,delay,slip)})()`);
 const ref=bundle.paths[i].r;
 compare(actual.path,ref.path.filter(r=>r.date<=bundle.prefix.date),'prefix equity '+i);
 compare(actual.orders,ref.orders.filter(r=>r.date<=bundle.prefix.date),'prefix fills '+i);
}
if(bundle.auction)compare(run("replay(bundle.auction[0],bundle.auction[1],'R1',['2023-08-28','2023-09-04'])"),
 bundle.auction[2],'blocked auction');
console.log(JSON.stringify({states:s.states.length,paths:bundle.paths.length,rows,orders,
 ...(bundle.fixture===false?{}:{expansion_test_codes:50})}));
