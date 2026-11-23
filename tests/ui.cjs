const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.dirname(__dirname);
const html=fs.readFileSync(path.join(root,'rotation_quant_lab_v4_0_offline.html'),'utf8');
assert.ok(!/<script src=|<link rel="stylesheet"|\bfetch\(/.test(html));
const scripts=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
const nodes=new Map();
function node(id){
 if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',checked:false,hidden:true});
 return nodes.get(id);
}
const context=vm.createContext({document:{getElementById:node},URL,structuredClone,
 window:{scrollTo(){}},location:{hash:''},setTimeout:()=>1,clearTimeout(){}});
const run=s=>vm.runInContext(s,context);
run(scripts.map(x=>x[1]).join('\n'));
assert.match(node('main').innerHTML,/16 只种子股已确认/);
assert.match(node('main').innerHTML,/待验收/);
run("setTab('moves');setWindow('W_Bear');setPressure('T2')");
assert.match(node('main').innerHTML,/逐日持仓甘特图/);
assert.match(node('main').innerHTML,/不适用/);
if(run("!ready(dataset())&&ready(referenceData())")){
 assert.ok(run("run('T1','W_Bear','T2').orders.length")>0);
 assert.equal(run("run('R2','W_Bear','T2')"),null);
}
run("setTab('radar')");
assert.match(node('main').innerHTML,/7 只 ETF/);
run("setTab('pool')");
assert.match(node('main').innerHTML,/牧原股份/);
assert.match(node('main').innerHTML,/相关性矩阵/);
run("setTab('oos');setImport('etf')");
assert.match(node('main').innerHTML,/批量粘贴 CSV/);
assert.match(node('main').innerHTML,/shares_source/);
assert.match(node('bar').innerHTML,/待历史数据/);
assert.throws(()=>run("mergeCsv('etf','bad')"),/表头/);
run("raw.tables.calendar=structuredClone(raw.tables.calendar.slice(-1));invalidate();render()");
assert.match(node('main').innerHTML,/与 v3.1 同日索引不匹配/);
const matched=run(`trial({
 errors:[],days:['2026-11-20'],week_ends:['2026-11-20'],
 scan:{ranks:[['002714','601398']],states:[{'002714':{gate:true,mark:44.85},
     '601398':{gate:true,mark:7.11}}]},
 universe:{'002714':{name:'牧原',bucket:'农业'},'601398':{name:'工行',bucket:'红利'}}
});`);
assert.equal(matched.orders.length,2);
assert.ok(matched.orders.every(x=>x.shares>0&&x.shares%100===0));
assert.ok(matched.balance>=0);
if(process.argv[2]){
 context.fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
 run(`raw=structuredClone(fixture.raw);raw.policy.status='approved';
   memo=structuredClone(fixture.d);latest=memo;memo.scan=fixture.states;
   runs.clear();profiles.clear();radarCache=null;setTab('moves');setWindow('2026');`);
 assert.match(node('main').innerHTML,/aria-label="逐日持仓"/);
 assert.match(node('main').innerHTML,/aria-label="净值"/);
 run("setTab('radar');flowWindow='all';render()");
 assert.match(node('main').innerHTML,/aria-label="资金流/);
 run("setTab('pool')");
 assert.match(node('main').innerHTML,/共同样本/);
 run("setTab('home')");
 assert.match(node('main').innerHTML,/可回放|最新趋势质量榜/);
}
console.log('Five offline views, official completeness gate, CSV import, confirmation and lot preview passed.');
