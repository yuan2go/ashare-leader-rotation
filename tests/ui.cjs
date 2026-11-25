const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.dirname(__dirname);
const html=fs.readFileSync(path.join(root,'rotation_quant_lab_v4_0_offline.html'),'utf8');
assert.ok(!/<script src=|<link rel="stylesheet"|\bfetch\(|@import\b/.test(html));
const scripts=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length,7);
const nodes=new Map(),storage=new Map();
let saved,downloaded;
const url=Object.assign(URL,{createObjectURL(blob){downloaded=blob;return 'blob:local';},revokeObjectURL(){}});
function node(id){
 if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',checked:false,hidden:true});
 return nodes.get(id);
}
const indexedDB={open(){
 const r={result:{close(){},transaction(){
  const t={objectStore(){return {get(){return {result:saved};},put(value){saved=structuredClone(value);return {};}};}};
  queueMicrotask(()=>t.oncomplete());return t;
 }}};
 queueMicrotask(()=>r.onsuccess());return r;
}};
const context=vm.createContext({document:{getElementById:node,documentElement:{dataset:{theme:'dark'}},
 createElement(){return {click(){}};}},URL:url,Blob,structuredClone,indexedDB,
 localStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k)},
 window:{scrollTo(){}},location:{hash:''},setTimeout:()=>1,clearTimeout(){}});
const run=s=>vm.runInContext(s,context);
const main=()=>node('main').innerHTML;
async function verify(){
 run(scripts.map(x=>x[1]).join('\n'));
 const rejected=run(`(() => {
  const code='600001',date='2015-01-05',source='https://www.sse.com.cn/';
  const q={code,date,status:'open',open:'10',high:'10',low:'10',close:'10',reference:'10',
   volume:'100',limit:'bounded',upper:'11',lower:'9',actions_source:'tushare_pro:dividend?ts_code=600001.SH',
   source:'tushare_pro:daily?ts_code=600001.SH'};
  const input={policy:{...RAW.policy,freeze:date},crosscheck:[date],tables:{stocks:[q],actions:[],funds:[],etf:[],
   universe:[{code,name:'测试',bucket:'红利'}],instruments:[{code,listing:'2014-01-01',source,delisting:''}],
   calendar:[{date,source,week_end:'0'}],index:[{date,source,open:'100',close:'100'}]}};
  const clean=prepare(input).errors;
  q.source+='&token=offline-test';
  return [clean,prepare(input).errors];
 })()`);
 assert.equal(rejected[0].length,0);
 assert.ok(rejected[1].includes('600001 2015-01-05 行情或除权来源未通过'));
 await run('storeBundle()');
 const initialReady=run('ready(dataset())');
 assert.equal(run('Object.keys(dataset().universe).length'),run('RAW.tables.universe.length'));
 assert.equal(run('raw.policy.status'),'approved');
 assert.equal((node('nav').innerHTML.match(/<button/g)||[]).length,5);
 assert.match(main(),initialReady?/趋势质量分/:/只种子股已确认/);
 assert.match(node('bar').innerHTML,/单槽/);
 if(initialReady){
  run("setMode('R2_Bucket')");assert.match(main(),/预计余现金/);
  run("setMode('R2');setWindow('W_Bear');setPressure('T2')");
  assert.match(main(),/aria-label="净值曲线"/);assert.match(main(),/不适用/);
 }
 if(run("!ready(dataset())&&ready(referenceData())")){
  assert.ok(run("run('T1','W_Bear','T2').orders.length")>0);
  assert.equal(run("run('R2','W_Bear','T2')"),null);
 }
 run('toggleTheme()');assert.equal(storage.get('rotation-theme'),'light');
 run("setTab('radar')");assert.match(main(),/7 只 ETF/);
 run("setTab('universe')");assert.match(main(),/牧原股份/);assert.match(main(),/相关性矩阵/);
 run("updateMatrix('query','002714')");assert.match(node('matrix-rows').innerHTML,/显示 1 \/ /);
 run("updateMatrix('query','不存在')");assert.match(node('matrix-rows').innerHTML,/没有股票/);
 run("updateMatrix('query','')");
 run("setTab('research')");
 if(run("Boolean(RAW.research?.['600428']?.experiments.earnings_event_study)")){
  assert.match(main(),/600428 中远海特/);assert.match(main(),/财报发布事件驱动/);
  assert.match(main(),/收跌与大单承接/);assert.match(main(),/逐笔事件明细/);
  run("flowTrack='proxy';earningsHorizon=20;earningsGroup='down';render()");
  assert.match(main(),/OHLCV 放量承接代理/);
 }
 run("showStock('002714')");assert.match(main(),/暂无专项研究插件/);assert.match(main(),/牧原股份/);
 run("setTab('audit');setImport('etf')");assert.match(main(),/批量粘贴 CSV/);
 assert.match(main(),/shares_source/);assert.match(main(),/导入 JSON/);
 if(initialReady)assert.equal((main().match(/class="evidence"/g)||[]).length,run('dataset(true).reference_checks.length'));
 assert.match(node('bar').innerHTML,initialReady?/可回放/:/待历史数据/);
 assert.throws(()=>run("mergeCsv('etf','bad')"),/表头/);
 if(initialReady){
  run("mergeCsv('etf',RAW.schemas.etf+'\\n')");
  await run('storeBundle()');assert.ok(saved.raw.tables.etf.length);
  run('exportBundle()');context.uploaded=await downloaded.text();
  run('importBundle(uploaded)');assert.match(main(),/JSON 工作区已导入/);
  assert.throws(()=>run("{const b=JSON.parse(uploaded);b.raw.tables.stocks[0].close='99.99';importBundle(JSON.stringify(b));}"),/覆盖冻结记录/);
 }
 run('raw.tables.calendar=raw.tables.calendar.slice(-1);invalidate();render()');
 assert.match(main(),/与 v3.1 同日索引不匹配/);
 const matched=run(`trial({errors:[],days:['2026-11-20'],week_ends:['2026-11-20'],
  scan:{ranks:[['002714','601398']],states:[{'002714':{gate:true,mark:44.85},'601398':{gate:true,mark:7.11}}]},
  universe:{'002714':{name:'牧原',bucket:'农业'},'601398':{name:'工行',bucket:'红利'}}});`);
 assert.equal(matched.orders.length,2);
 assert.ok(matched.orders.every(x=>x.shares>0&&x.shares%100===0&&x.idle>=0));
 assert.ok(matched.balance>=0);
 const stress=run(`(() => {
  const rows=Array.from({length:50},(_,i)=>({code:String(700000+i),macro:i<25?'蓝组':'紫组',
   meta:{name:'测试 '+i},score:i,mark:i*10,gate:i%2===0,band:lotBand(i*10),returns:[0,.1,-.2,.3]}));
  const d={days:['2025-01-01','2025-01-02','2025-01-03','2025-01-04'],
   universe:Object.fromEntries(rows.map(x=>[x.code,{bucket:x.macro}]))};
  const result=filteredMatrix(rows,{query:'测试',bucket:'紫组',gate:'1',band:'high',sort:'mark',descending:true});
  const cluster=clusterStocks(d,rows);
  return {filtered:result.length,first:result[0].code,n:cluster.matrix.length,m:cluster.matrix[49].length,
   pairs:cluster.pairs.length,red:lotBand(224.91),amber:lotBand(112.44),green:lotBand(112.43)};
 })()`);
 assert.deepEqual({...stress},{filtered:12,first:'700048',n:50,m:50,pairs:1225,red:'high',amber:'mid',green:'low'});
 if(process.argv[2]){
  context.fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
  run(`raw=structuredClone(fixture.raw);raw.policy.status='approved';memo=structuredClone(fixture.d);latest=memo;
   memo.scan=fixture.states;runs.clear();profiles.clear();radarCache=null;setTab('desk');setWindow('2026');setPressure('base');`);
  assert.match(main(),/aria-label="逐日持仓时间线"/);assert.match(main(),/aria-label="净值曲线"/);
  run("setTab('radar')");assert.match(main(),/aria-label="资金流/);
  run("setTab('universe')");assert.match(main(),/共同样本/);
 }
 console.log('Five offline tabs, theme, 50-stock filters and correlation, research, completeness, IndexedDB and CSV/JSON workspace passed.');
}
verify().catch(e=>{console.error(e);process.exitCode=1;});
