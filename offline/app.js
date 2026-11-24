'use strict';
const PAGES={desk:'实盘驾驶舱',universe:'股票矩阵与体检',radar:'汪汪队 ETF 雷达',research:'个股专项研究',audit:'样本外与审计'};
const TITLES={Equal:'动态月度等权',CSI300:'沪深300',R1:'R1 · Top 1',R2:'R2 · Top 2',
 R2_Bucket:'R2 · 跨桶 Top 2',T1:'单票 T1'};
const PALETTE={Equal:'#d3a36d',CSI300:'#94a3b8',R1:'#a78bfa',R2:'#3b82f6',R2_Bucket:'#06b6d4',T1:'#ec4899'};
const FIELDS={universe:'股票池',instruments:'上市资料',calendar:'交易所日历',stocks:'股票日线',
 actions:'分红除权',index:'沪深300',funds:'ETF 清单',etf:'ETF 日份额'};
const KEYS={universe:['code'],funds:['code'],instruments:['code'],calendar:['date'],
  stocks:['code','date'],actions:['code','ex_date'],index:['date'],etf:['code','date']};
const bl=(x,d=2)=>Number.isFinite(x)?x.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const pct=x=>Number.isFinite(x)?bl(x*100)+'%':'—';
const cash=x=>Number.isFinite(x)?'¥'+bl(x,0):'—';
const money=x=>Number.isFinite(x)?'¥'+bl(x):'—';
const signed=x=>'<span class="'+(x>0?'up':x<0?'down':'')+'">'+pct(x)+'</span>';
const evidence=(url,label)=>{
 try{
  const x=new URL(url);
  if(x.protocol==='https:'||x.protocol==='http:')
   return '<details class="evidence"><summary>'+esc(label)+'</summary><code>'+esc(x.href)+'</code></details>';
 }catch{}
 return esc(label);
};
const tag=(text,good=false)=>'<span class="status '+(good?'pass':'')+'">'+esc(text)+'</span>';
const esc=x=>String(x??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[c]);
const cell=x=>x==null?'—':esc(x);
const btn=(x,code,on=false)=>'<button type="button" class="btn" aria-pressed="'+on+'" onclick="'+code+'">'+esc(x)+'</button>';
const picker=(x,value,code,label='筛选')=>'<select aria-label="'+esc(label)+'" onchange="'+code+'">'+entries(x).map(([k,v])=>'<option value="'+esc(k)+'"'+(k===value?' selected':'')+'>'+esc(v)+'</option>').join('')+'</select>';
const panel=(title,body,side='')=>'<section class="panel"><div class="panel-head"><h2>'+esc(title)+'</h2>'+side+'</div>'+body+'</section>';
const toolsBox=s=>'<div class="tools">'+s+'</div>';
const note=s=>'<p class="notes">'+esc(s)+'</p>';
const empty=s=>'<p class="empty">'+esc(s)+'</p>';
function table(head,rows,cl='',attributes=null){
 return '<div class="scroll '+cl+'"><table><thead><tr>'+head.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+
 rows.map((r,i)=>'<tr'+(attributes?attributes(i):'')+'>'+r.map(x=>'<td>'+(x??'—')+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
const get=id=>document.getElementById(id);
let raw={...RAW,policy:{...RAW.policy},tables:{...RAW.tables}},tab='desk',importKind='stocks',mode='R2',
 windowId='W5',pressure='base',trendWindow='W5',wallet={cash:RAW.policy.capital,hold:'',set:false};
let memo=null,latest=null,reference=null,lastImport='';
const runs=new Map(),profiles=new Map();
let radarCache=null;
function invalidate(){memo=latest=reference=radarCache=null;runs.clear();profiles.clear();}
function dataset(live=false){
 if(live&&latest)return latest;
 if(!live&&memo)return memo;
 let end=raw.policy.freeze;
 if(live)for(const r of raw.tables.calendar)if(r.date>end)end=r.date;
 if(live&&end===raw.policy.freeze)return latest=dataset();
 let data;
 try{
  data=prepare(raw,end);
  data.errors.push(...(Array.isArray(RAW.ingest_blockers) ? RAW.ingest_blockers : ['缺少采集回执 blockers']));
 }
 catch(e){return {universe:Object.fromEntries(raw.tables.universe.map(x=>[x.code,x])),days:[],errors:[e.message],etf_errors:[e.message],
  coverage:[],etf_coverage:[],stocks:{},policy:raw.policy};}
 if(live)latest=data;else memo=data;
 return data;
}
function ready(d){return raw.policy.status==='approved'&&!d.errors.length;}
function scanned(d){return d.scan??(d.scan=scan(d));}
function referenceData(){
 if(reference!==null)return reference;
 const subset=structuredClone(raw),code=raw.policy.reference;
 for(const key of ['universe','stocks','actions'])subset.tables[key]=subset.tables[key].filter(row=>row.code===code);
 try{reference=prepare(subset);}
 catch(e){reference={errors:[e.message]};}
 return reference;
}
function run(name,w=windowId,key='base',interest=0){
 let d=dataset();
 if(!ready(d)&&raw.policy.status==='approved'&&['CSI300','T1'].includes(name))d=referenceData();
 const id=[name,w,key,interest].join('|');
 if(!ready(d)||(name==='T1'&&!d.universe[raw.policy.reference]))return null;
 if(!runs.has(id)){
  const [slip,delay]=SCENARIOS[key];
  runs.set(id,replay(d,scanned(d),name,raw.policy.windows[w],interest,delay,slip));
 }
 return runs.get(id);
}
function radarData(d){
 if(!d.days.length||!d.etf_coverage.some(x=>x.rows))return null;
 if(radarCache?.d===d)return radarCache.r;
 // A verified ETF-only upload can display its raw telemetry even if stock data is pending.
 const signals=ready(d)?scanned(d):{states:d.days.map(()=>({}))};
 const r=radar(d,signals);radarCache={d,r};return r;
}
function setTab(k){
 if(!Object.hasOwn(PAGES,k))return;
 tab=k;render();globalThis.window?.scrollTo(0,0);
}
function setMode(k){mode=k;render();}
function setWindow(k){windowId=k;render();}
function setPressure(k){pressure=k;render();}
function tell(s){
 const n=get('toast');n.textContent=s;n.hidden=false;clearTimeout(n.wait);
 n.wait=setTimeout(()=>{n.hidden=true;},4200);
}
function storeBundle(value){
 if(!globalThis.indexedDB)return Promise.reject(Error('浏览器禁止离线存储'));
 return new Promise((yes,no)=>{
  const r=indexedDB.open('rotation-quant-lab-v4',1);
  r.onupgradeneeded=()=>r.result.createObjectStore('session');
  r.onerror=()=>no(r.error);
  r.onsuccess=()=>{
   const db=r.result,t=db.transaction('session',value?'readwrite':'readonly');
   const os=t.objectStore('session'),req=value?os.put(value,'data'):os.get('data');
   t.oncomplete=()=>{db.close();yes(req.result);};
   t.onerror=()=>{db.close();no(t.error);};
  };
 });
}
function persist(){
 storeBundle({raw,wallet}).catch(()=>tell('浏览器未保存会话，可导出 JSON 留存。'));
}
function download(name,body,type='text/plain'){
 const a=document.createElement('a');a.download=name;a.href=URL.createObjectURL(new Blob([body],{type}));
 a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}
function exportBundle(){download('rotation_v4_workspace.json',JSON.stringify({raw,wallet}),'application/json');}
function validWallet(v){
 if(!v||!Number.isFinite(v.cash)||v.cash<0||typeof v.hold!=='string')throw Error('现金须为非负数字');
 const seen=new Set();
 for(const [k,n,...rest] of csv(v.hold)){
  if(rest.length||!/^[0-9]{6}$/.test(k)||!Number.isSafeInteger(Number(n))||Number(n)<=0||seen.has(k))
   throw Error('持仓行格式须为 证券代码,正整数股数');
  seen.add(k);
 }
 return {cash:v.cash,hold:v.hold,set:Boolean(v.set)};
}
function importBundle(text){
 const incoming=JSON.parse(text);
 if(incoming.raw?.policy?.freeze!==RAW.policy.freeze||!incoming.raw.tables)throw Error('JSON 界碑或资料协议不符');
 const restored=validWallet(incoming.wallet),tables={};
 const locked=ready(dataset());
 for(const [key,fields] of entries(KEYS)){
  const id=r=>fields.map(k=>r[k]).join('|');
  const old=new Map(raw.tables[key].map(r=>[id(r),r])),seen=new Set();
  const cols=RAW.schemas[key].split(','),allowed=new Set(cols);
  if(!Array.isArray(incoming.raw.tables[key]))throw Error('缺少工作表 '+key);
  for(const row of incoming.raw.tables[key]){
   if(!row||typeof row!=='object'||fields.some(k=>typeof row[k]!=='string'||!row[k])||
     entries(row).some(([k,v])=>!allowed.has(k)||typeof v!=='string'))throw Error('无效工作表 '+key);
   const k=id(row);
   if(seen.has(k))throw Error('重复记录 '+k);
   seen.add(k);
   const existing=old.get(k);
   if(locked&&(key==='universe'||key==='funds'||existing||(row.ex_date??row.date)<=RAW.policy.freeze)){
    if(!existing||cols.some(c=>(existing[c]??'')!==(row[c]??'')))throw Error('JSON 试图覆盖冻结记录 '+k);
   }else old.set(k,row);
  }
  tables[key]=[...old.values()];
 }
 const candidate={...raw,tables};
 const checked=prepare(candidate,tables.calendar.reduce((max,r)=>r.date>max?r.date:max,RAW.policy.freeze));
 if(checked.errors.length||checked.etf_errors.length)throw Error((checked.errors[0]??checked.etf_errors[0]));
 raw=candidate;wallet=restored;lastImport='JSON 工作区已导入。';invalidate();persist();render();
}
async function uploadBundle(e){
 const file=e.target.files?.[0];
 if(file)try{importBundle(await file.text());}catch(err){tell(err.message);}
}
function template(){download(importKind+'.csv','\ufeff'+RAW.schemas[importKind]+'\r\n','text/csv');}
function approve(){
 if(!get('approve').checked){tell('请先核对名单并勾选确认');return;}
 raw={...raw,policy:{...raw.policy,status:'approved'}};invalidate();persist();render();
}
function mergeCsv(key,text){
 const incoming=parseCsv(text,key),prior=raw.tables[key],index=KEYS[key];
 const by=Object.fromEntries(prior.map(r=>[index.map(x=>r[x]).join('|'),r]));
 const frozen=ready(dataset());
 const seen=new Set();
 for(const r of incoming){
  const id=index.map(x=>r[x]).join('|');
  if(seen.has(id))throw Error('同一批重复 '+id);
  seen.add(id);
  if(frozen){
   if((key==='universe'||key==='funds')&&(!by[id]||JSON.stringify(by[id])!==JSON.stringify(r)))
    throw Error('已验收的开发期池请通过单独工作区调整');
   if(by[id]&&JSON.stringify(by[id])!==JSON.stringify(r))throw Error('冻结记录已存在 '+id);
   const d=key==='actions'?r.ex_date:r.date;
   if(d&&d<=raw.policy.freeze&&!by[id])throw Error('冻结区间不能补写 '+id);
  }
  by[id]=r;
 }
 raw={...raw,tables:{...raw.tables,[key]:Object.values(by)}};
 invalidate();lastImport=FIELDS[key]+' 本批 '+incoming.length+' 行。';persist();render();
}
function paste(e){
 e.preventDefault();
 try{mergeCsv(importKind,new FormData(e.target).get('csv'));}
 catch(err){tell(err.message);}
}
async function upload(e){
 const f=e.target.files?.[0];if(!f)return;
 const key=importKind;
 try{mergeCsv(key,await f.text());}catch(err){tell(err.message);}
}
function setImport(k){importKind=k;render();}
function submitWallet(e){
 e.preventDefault();
 try{
  const r=new FormData(e.target);
  wallet=validWallet({cash:Number(r.get('cash')),hold:r.get('positions').trim(),set:true});persist();render();
 }catch(err){tell(err.message);}
}
function trial(d){
 if(!ready(d))return null;
 const s=scanned(d),day=d.days.at(-1),point=s.states.at(-1);
 const holds=Object.fromEntries(csv(wallet.hold).map(([k,n])=>[k,Number(n)]));
 if(Object.keys(holds).some(k=>!point[k]?.mark))return {note:'账户持仓有股票未包含在已验证的池内，无法试算。',orders:[]};
 const current=Object.keys(holds);
 const desired=d.week_ends.includes(day)?pick(mode,s.ranks.at(-1),current,d.universe):current.filter(k=>point[k].gate);
 const slots=RULES[mode][0];
 const equity=rounded(wallet.cash*100)+sum(current.map(k=>holds[k]*rounded(point[k].mark*100)));
 const unit=Math.floor(equity/slots);
 let budget=rounded(wallet.cash*100);
 const orders=[];
 for(const k of current){
  const last=point[k].mark,price=rounded(last*100);
  const maximum=Math.floor(unit/(price*LOT))*LOT;
  let n=!desired.includes(k)?holds[k]:0;
  if(!n&&d.week_ends.includes(day)&&slots>1)
    n=Math.min(Math.floor(holds[k]/LOT)*LOT,Math.max(0,Math.ceil((holds[k]-maximum)/LOT)*LOT));
  if(n){
   const p=rounded(price*(1-2/10000)),[f,t]=fee(p,n,true,day);
   budget+=n*p-f-t;holds[k]-=n;
   orders.push({k,side:'卖出',shares:n,estimated:p/100,fee:(f+t)/100});
  }
 }
 if(d.week_ends.includes(day))for(const k of desired){
  if(holds[k])continue;
  const p=rounded(point[k].mark*100*(1+2/10000)),allocation=Math.min(budget,unit),n=canBuy(p,allocation,day);
  if(n){
   const f=fee(p,n,false,day)[0];budget-=n*p+f;
   orders.push({k,side:'买入',shares:n,estimated:p/100,fee:f/100,idle:(allocation-n*p-f)/unit});
  }else orders.push({k,side:'整手不足',shares:0,estimated:p/100,fee:0,idle:1});
 }
 return {orders,desired,balance:budget/100,note:d.week_ends.includes(day)?'完整周选股。':'周中仅检查持仓退出。'};
}
function card(label,v,foot){
 return '<div class="card"><small>'+esc(label)+'</small><b>'+esc(v)+'</b><small>'+esc(foot)+'</small></div>';
}
function ranking(d){
 if(!ready(d))return empty('待确认名单并补齐全池官方日线。');
 const s=scanned(d),current=s.states.at(-1),top=s.ranks.at(-1).slice(0,10);
 const buffer=RULES[mode][1];
 return table(['排名','证券','宏观桶','趋势质量分'],top.map((k,i)=>[
  (i+1)+(i+1===buffer?' <span class="down">缓冲线</span>':''),
  stockButton(k,k+' '+d.universe[k].name),bucketLabel(d.universe[k]),bl(current[k].score,4)]),'ranking',
  i=>' class="'+(i+1===buffer?'buffer':'')+'" data-code="'+esc(top[i])+
  '" onclick="if(!event.target.closest(\'button\'))showStock(this.dataset.code)"');
}
function trialPanel(d){
 const q=trial(d);
 if(!q)return empty('通过正式校验后显示账户整手试算。');
 const account=wallet.set?'已录入账户':'默认模拟账户，¥450,000 现金';
 const target=(q.desired??[]).map(k=>d.universe[k].name).join(' / ')||'现金';
 const balance=Number.isFinite(q.balance)?' · 预计余现金 ¥'+bl(q.balance):'';
 return note(account+' · '+q.note+' 目标 '+target+balance)+
  (q.orders.length?table(['代码','方向','股数','试算成交价','估算费用','槽位闲置'],q.orders.map(r=>[
   stockButton(r.k,r.k),r.side,r.shares,bl(r.estimated),bl(r.fee),Number.isFinite(r.idle)?
    '<span class="'+(r.idle<=.05?'idle-good':r.idle<=.10?'idle-mid':'idle-high')+'">'+pct(r.idle)+'</span>':'—'
  ])):empty('本次无可试算订单。'))+
  note('估算以收盘价加减 2bp 为参考。实际开盘按限价、可用现金和成交状态复核。');
}
function monthly(d,n){
 const rows=run(n,'W1')?.path;
 if(!rows)return empty('待复跑 2026 逐月调仓。');
 const trades=run(n,'W1').orders,months={};
 let last=raw.policy.capital*100;
 for(const r of rows){
  const mo=r.date.slice(0,7);
  if(mo.startsWith('2026')){
   const v=(months[mo]??={base:last,final:0,exposure:{},cash:0,hold:{}});
   v.final=r.equity;v.hold=r.hold;
   const held=Object.keys(r.hold);
   if(!held.length)v.cash++;
   for(const k of held){const b=d.universe[k].bucket;v.exposure[b]=(v.exposure[b]??0)+1;}
  }
  last=r.equity;
 }
 const amount=(p)=>Object.entries(p).map(([k,v])=>esc(k)+' '+v).join(' / ');
 return table(['月份','收益','空仓日','各桶持仓票日','月末持仓','逐笔换仓'],entries(months).map(([m,v])=>[
  m,pct(v.final/v.base-1),v.cash,amount(v.exposure),amount(v.hold),
  trades.filter(o=>o.date.startsWith(m)).map(o=>esc(o.date.slice(5)+' '+o.side+' '+d.universe[o.code].name+' '+o.shares)).join('<br>')||'—'
 ]));
}
const MODES={R2:'R2 · Top 2',R2_Bucket:'R2_Bucket · 跨桶',R1:'R1 · Top 1'};
const PRESSURES={base:'2bp',income:'含 1.8% 现金息',slip7:'7bp',T2:'T+2'};
const PRICE_BANDS={low:'≤ ¥112.43',mid:'¥112.44–224.90',high:'> ¥224.90',pending:'待核'};
const dot=(label,color)=>'<span class="legend-item"><i style="--dot:'+color+'"></i>'+esc(label)+'</span>';
const bucketLabel=meta=>dot(macro(meta),macroColor(macro(meta)));
const legend=items=>'<div class="legend">'+items.join('')+'</div>';
const units=v=>Number.isFinite(v)?'¥'+bl(v/1e8)+' 亿':'—';
function deskPage(){
 const live=dataset(true),d=dataset();
 const key=pressure==='income'?'base':pressure,rate=pressure==='income'?.018:0;
 const series=MODELS.flatMap(code=>{
  const r=run(code,windowId,key,code==='CSI300'?0:rate);
  return r?[{code,path:r.path}]:[];
 });
 const chosen=series.find(s=>s.code===mode);
 if(series.length)chartJobs.set('equity',(c,w,h,t)=>equityChart(c,w,h,t,series,mode,d.universe));
 if(chosen)chartJobs.set('gantt',(c,w,h,t)=>ganttChart(c,w,h,t,chosen.path,d.universe,RULES[mode][0]));
 const results=table(['策略','收益','含息收益','最大回撤','收益回撤比','平均仓位','空仓','成交','佣金','印花税','滑点'],MODELS.map(code=>{
  if(code==='CSI300'&&key!=='base')return [TITLES[code],...Array(10).fill('不适用')];
  const v=run(code,windowId,key)?.metrics;
  const inc=code==='CSI300'?null:run(code,windowId,key,.018)?.metrics;
  const active=pressure==='income'?inc??v:v;
  return [TITLES[code],signed(v?.net),signed(inc?.net),signed(active?.dd),bl(active?.rr),pct(active?.weight),
   pct(active?.flat),cell(active?.trades),money(active?.commission),money(active?.tax),money(active?.slippage)];
 }));
 return '<div class="cols desk-grid">'+panel('下次开盘调仓'+(live.next_session?' · '+live.next_session:''),trialPanel(live))+
  panel('Top 10 入围榜',ranking(live),tag('缓冲 '+RULES[mode][1]+' 名',true))+'</div>'+
  panel('四窗绩效',toolsBox(Object.keys(raw.policy.windows).map(w=>btn(w,"setWindow('"+w+"')",w===windowId)).join('')+
   Object.keys(PRESSURES).map(p=>btn(PRESSURES[p],"setPressure('"+p+"')",p===pressure)).join(''))+
   results+(!ready(d)?note('全池待验收。条件满足时显示牧原 T1 与沪深 300 的独立回放。'):''))+
  panel('累计净值',series.length?chart('equity','净值曲线')+legend(series.map(v=>dot(TITLES[v.code],PALETTE[v.code]))):
   empty('缺少可回放行情。'),tag(PRESSURES[pressure]))+
  panel('逐日持仓甘特图',chosen?chart('gantt','逐日持仓时间线',97)+legend(entries(MACRO).map(([k,v])=>dot(k,v))):
   empty('缺少全池持仓轨迹。'))+
  panel('2026 逐月换仓与行业敞口',monthly(d,mode));
}
let filters={query:'',bucket:'all',gate:'all',band:'all',sort:'score',descending:true};
function matrixRows(d){
 const s=ready(d)?scanned(d).states.at(-1):{};
 return Object.values(d.universe).map(meta=>({code:meta.code,meta,macro:macro(meta),
  band:lotBand(s[meta.code]?.mark),mark:s[meta.code]?.mark,gate:s[meta.code]?.gate,score:s[meta.code]?.score}));
}
function filteredMatrix(rows,f=filters){
 const query=f.query.trim().toLowerCase();
 return rows.filter(r=>(!query||[r.code,r.meta.name,r.meta.subsector].some(v=>v?.toLowerCase().includes(query)))&&
  (f.bucket==='all'||r.macro===f.bucket)&&(f.band==='all'||r.band===f.band)&&
  (f.gate==='all'||r.gate===(f.gate==='1'))).sort((a,b)=>{
   const x=a[f.sort],y=b[f.sort];
   if(x==null||y==null)return (Number(x==null)-Number(y==null))||a.code.localeCompare(b.code);
   const value=typeof x==='number'?x-y:String(x).localeCompare(String(y),'zh-CN');
   return (f.descending?-value:value)||a.code.localeCompare(b.code);
  });
}
function updateMatrix(key,value){
 filters={...filters,[key]:value};
 if(key==='query')renderMatrixRows(dataset());else render();
}
function sortMatrix(key){
 filters={...filters,descending:key===filters.sort?!filters.descending:['mark','score','gate'].includes(key),sort:key};render();
}
function matrixTable(d){
 const rows=filteredMatrix(matrixRows(d)),verified=ready(d);
 const attach=verified?getProfiles(d,trendWindow):[],profileBy=new Map(attach.map(v=>[v.code,v]));
 const labels={code:'证券',macro:'宏观桶',gate:'T1',mark:'收盘价',score:'趋势质量分'};
 const headers=entries(labels).map(([key,label])=>'<th'+(filters.sort===key?' aria-sort="'+(filters.descending?'descending':'ascending')+'"':'')+
  '><button class="text-btn" onclick="sortMatrix(\''+key+'\')">'+label+
  (filters.sort===key?(filters.descending?' ↓':' ↑'):'')+'</button></th>').join('');
 return note('显示 '+rows.length+' / '+Object.keys(d.universe).length+' 只')+(rows.length?
  '<div class="scroll matrix-table"><table><thead><tr>'+headers+
   '<th>整手分档</th><th>近 60D</th><th>独立多头</th><th>独立空头</th><th>T1 收益 / 回撤</th></tr></thead><tbody>'+
   rows.map(r=>{
    const v=profileBy.get(r.code),trend=verified?spark(stockSeries(d,scanned(d),r.code,60)):'—';
    return '<tr>'+[stockButton(r.code,r.code+' '+r.meta.name),bucketLabel(r.meta),r.gate===undefined?'—':tag(Number(r.gate),r.gate),
     money(r.mark),bl(r.score,4),esc(PRICE_BANDS[r.band]),trend,
     v?pct(v.hold_on.independent_win)+' · N '+v.hold_on.independent:'—',
     v?pct(v.hold_off.independent_win)+' · N '+v.hold_off.independent:'—',v?signed(v.m.net)+' / '+signed(v.m.dd):'—']
     .map(value=>'<td>'+value+'</td>').join('')+'</tr>';
   }).join('')+'</tbody></table></div>':empty('当前过滤条件下没有股票。'));
}
function renderMatrixRows(d){get('matrix-rows').innerHTML=matrixTable(d);}
function universePage(){
 const d=dataset(),rows=matrixRows(d),verified=ready(d);
 const macros=[...new Set([...Object.keys(MACRO),...rows.map(r=>r.macro)])];
 const cluster=verified?(d.cluster??=clusterStocks(d,getProfiles(d,'W1'))):null;
 if(cluster)chartJobs.set('matrix',(c,w,h,t,canvas)=>matrixChart(c,w,h,t,canvas,cluster,d.universe));
 const chip=(label,value)=>'<button class="btn" data-bucket="'+esc(value)+'" aria-pressed="'+(filters.bucket===value)+
  '" onclick="updateMatrix(\'bucket\',this.dataset.bucket)">'+esc(label)+'</button>';
 const bands=entries(PRICE_BANDS).map(([k,v])=>card(v,String(rows.filter(r=>r.band===k).length)+' 只','按冻结收盘价'));
 return panel('股票筛选',toolsBox('<input type="search" aria-label="搜索证券" placeholder="代码 / 简称 / 细分行业" value="'+esc(filters.query)+
  '" oninput="updateMatrix(\'query\',this.value)">'+picker({all:'全部 T1',1:'T1=1',0:'T1=0'},filters.gate,
  "updateMatrix('gate',this.value)",'T1 门禁')+picker({all:'全部股价',...PRICE_BANDS},filters.band,
  "updateMatrix('band',this.value)",'整手股价')+picker({W1:'W1 体检',W5:'W5 体检'},trendWindow,'trendWindow=this.value;render()'))+
  toolsBox(chip('全部桶','all')+macros.map(m=>chip(m,m)).join(''))+'<div id="matrix-rows">'+matrixTable(d)+'</div>')+
  panel('整手红线体检','<div class="counters">'+bands.join('')+'</div>'+
   note('按 ¥225,000 双槽预算、100 股整手衡量。色桶用于展示，策略跨桶沿用配置文件。'))+
  '<div class="cols cluster-grid">'+panel('相关性矩阵',cluster?chart('matrix','按宏观桶聚类的相关性矩阵',
   Math.max(330,Math.min(610,cluster.codes.length*13+50)))+'<p id="matrix-detail" class="notes" aria-live="polite">共同样本</p>':
   empty('完整行情验收后计算共同交易日收益。'))+
  panel('高相关冗余 · r ≥ 0.65',cluster?.pairs.length?table(['股票 A','股票 B','r','共同样本'],cluster.pairs.map(v=>[
   stockButton(v.a,d.universe[v.a].name),stockButton(v.b,d.universe[v.b].name),bl(v.r,3),v.n])):
   empty(cluster?'当前没有达到阈值的股票对。':'待全池验收。'))+'</div>';
}
function radarPage(){
 const d=dataset(true),r=radarData(d),latest=r?.daily.at(-1),seq=r?.daily.slice(-240);
 if(seq)chartJobs.set('flow-chart',(c,w,h,t)=>flowChart(c,w,h,t,seq));
 const kpis=latest?[5,20].flatMap(h=>['blue','growth','total'].map(k=>card(
  ({blue:'蓝筹',growth:'成长',total:'合计'})[k]+' '+h+'D',units(latest[k+h]),latest.coverage[k+h].length+' 只固定成员'))):[];
 const funds=raw.tables.funds;
 return '<div class="counters">'+kpis.join('')+'</div>'+panel('蓝筹与成长 · 20D 净申赎、Bias20 与脉冲',
  seq?chart('flow-chart','资金流堆叠与 Bias20',294)+legend([
   dot('蓝筹','#3b82f6'),dot('成长','#ec4899'),dot('Bias20','#a78bfa'),dot('护盘脉冲','#eab308')])+
   note('截至 '+latest.date+' · Bias20 '+units(latest.bias20)+' · '+(latest.pulse?'脉冲触发':'当日无脉冲')):
   empty('暂无可核验的连续日份额。'))+
  panel(funds.length+' 只 ETF 明细',table(['证券','分组','观测覆盖','最近可见观察日','当期已知','5D','20D','历史 T+1','实时有时戳','覆盖内缺口'],funds.map(v=>{
   const x=d.etf_coverage.find(c=>c.code===v.code);
   const flow=h=>latest?.coverage['total'+h].includes(v.code)?
    units(sum(r.daily.slice(-h).map(day=>day.funds[v.code]))):'—';
   return [esc(v.code+' '+v.name),esc(v.group),esc(x?.first?x.first+' 至 '+x.last:'—'),cell(x?.last_usable_observation),
    (x?.usable??0)+' / '+(x?.rows??0),flow(5),flow(20),x?.published_basis.exchange_t1_convention??0,
    x?.published_basis.verified_live??0,x?.missing??'—'];
  }))+note('官方历史份额按下一交易日可见。各窗口采用全窗数据齐备的固定成员，日份额净流无法识别持有人。'))+
  panel('条件前瞻 · 2020 年以来',r?table(['雷达状态','策略桶','期限','平均收益','胜率','样本','独立 N'],
   r.conditional.map(v=>[esc(v.state),esc(v.bucket),v.h+'D',signed(v.average),pct(v.win),v.n,v.independent])):
   empty('等待可验收的 ETF 日规模及股票观察。'));
}
function auditPage(){
 const d=dataset(true),issues=[...d.errors,...d.etf_errors];
 const approval=raw.policy.status==='approved'?tag('名单已确认',true):
  '<label class="check"><input id="approve" type="checkbox"> 已核定当前股票池名单</label>'+btn('确认种子池','approve()');
 const input=panel('周度数据录入 · 界碑 '+raw.policy.freeze,
  '<form class="form" onsubmit="paste(event)"><label>资料类型 '+picker(FIELDS,importKind,'setImport(this.value)')+'</label>'+
  '<label>上传 CSV<input type="file" accept=".csv,text/csv" onchange="upload(event)"></label>'+
  '<label>批量粘贴 CSV<textarea name="csv" placeholder="'+esc(RAW.schemas[importKind])+'" required></textarea></label>'+
  '<div class="tools"><button class="btn primary" type="submit">导入选中表</button>'+
  btn('下载模板','template()')+btn('导出工作数据','exportBundle()')+'</div></form>'+
  '<div class="tools"><label>导入 JSON 工作区<input type="file" accept=".json,application/json" onchange="uploadBundle(event)"></label></div>'+
  (lastImport?note(lastImport):'')+toolsBox(approval)+note('请先录入官方交易日历。周末标记按实际开休市规则。'));
 const account=panel('样本外账户整手试算','<form class="form" onsubmit="submitWallet(event)"><label>实际可用现金'+
  '<input type="number" name="cash" min="0" step="0.01" value="'+esc(wallet.cash)+'" required></label>'+
  '<label>持仓，每行填写 证券代码,股数<textarea name="positions" placeholder="601398,100">'+esc(wallet.hold)+'</textarea></label>'+
  '<button class="btn" type="submit">更新账户</button></form>'+trialPanel(d));
 const recent=panel('实时数据审计','<div class="counters">'+card('股票问题',String(d.errors.length),'当前工作区')+
  card('ETF 问题',String(d.etf_errors.length),'结构及可见日审计')+card('交易日',String(d.days.length),'至 '+(d.days.at(-1)??raw.policy.freeze))+'</div>'+
  (issues.length?table(['问题'],issues.slice(0,30).map(v=>[esc(v)])):note('行情和 ETF 结构校验通过。'))+
  '<details><summary>股票覆盖明细</summary>'+table(['股票','日线','停牌','权益','缺口'],
   d.coverage.map(v=>[esc(v.code),v.rows,v.suspended,v.actions,cell(v.missing)]))+'</details>');
 const terms=panel('差异化权益核验台账',d.reference_checks?.length?table(
  ['证券 / 除权日','实派现金 / 送转','折算现金 / 送转','行情参考价','误差','公告'],
  d.reference_checks.map(v=>[esc(v.code+' '+v.date),bl(v.cash,6)+' / '+bl(v.bonus,6),
   bl(v.ref_cash,8)+' / '+bl(v.ref_bonus,8),money(v.reference),bl(v.error,6),evidence(v.source,'原始公告')])):
  empty('当前数据未产生差异化权益核验记录。'));
 return '<div class="cols audit-grid"><div>'+input+account+'</div><div>'+recent+terms+'</div></div>';
}
function toggleTheme(){
 const html=document.documentElement;
 if(!html)return;
 const theme=html.dataset.theme==='light'?'dark':'light';
 html.dataset.theme=theme;
 try{globalThis.localStorage?.setItem('rotation-theme',theme);}catch{}
 render();
}
function render(){
 chartJobs.clear();
 const panes={desk:deskPage,universe:universePage,radar:radarPage,research:researchPage,audit:auditPage};
 const d=dataset(true),ok=ready(d),states=ok?scanned(d).states.at(-1):null;
 const count=states?Object.values(states).filter(x=>x.gate).length:null;
 const radarState=radarData(d)?.daily.at(-1);
 const radarLabel=radarState?.pulse?'护盘脉冲':radarState?.total20>0?'托底':radarState?.total20<0?'降温':'待观察';
 get('nav').innerHTML='<nav aria-label="研究页面">'+entries(PAGES).map(([key,label])=>
  '<button onclick="setTab(\''+key+'\')" '+(key===tab?'aria-current="page"':'')+'>'+esc(label)+'</button>').join('')+'</nav>';
 get('bar').innerHTML='<div class="brand"><span class="mark">R</span><strong>A 股轮动研究台</strong><small>v4.1</small></div>'+
  '<div class="bar-controls">'+picker(MODES,mode,'setMode(this.value)','全局策略模式')+
  '<span>资金 <b>'+cash(raw.policy.capital)+'</b> · 单槽 <b>'+cash(raw.policy.capital/RULES[mode][0])+'</b></span>'+
  tag('T1=1 '+(count??'—')+' / '+Object.keys(d.universe).length,Boolean(count))+tag('ETF '+radarLabel,Boolean(radarState?.pulse||radarState?.total20>0))+
  tag('🔒 '+raw.policy.freeze)+tag(ok?'可回放':'待历史数据',ok)+btn(document.documentElement?.dataset.theme==='light'?'深色主题':'浅色主题','toggleTheme()')+'</div>';
 get('main').innerHTML='<div class="hero"><h1>'+esc(PAGES[tab])+'</h1><span class="hint">行情截止 '+esc(d.days.at(-1)??raw.policy.freeze)+'</span></div>'+
  (!ok&&tab==='desk'?'<div class="notice">'+raw.tables.universe.length+
   ' 只种子股'+(raw.policy.status==='approved'?'已确认':'待确认')+'。行情待验收。</div>':'')+panes[tab]();
 paintCharts();
}
try{
 const theme=globalThis.localStorage?.getItem('rotation-theme');
 if(theme==='light')document.documentElement.dataset.theme=theme;
}catch{}
if(Object.hasOwn(PAGES,globalThis.location?.hash?.slice(1)??''))tab=location.hash.slice(1);
globalThis.window?.addEventListener?.('resize',paintCharts);
render();
storeBundle().then(saved=>{
 if(saved?.raw?.policy?.freeze!==RAW.policy.freeze)return;
 const both=Object.fromEntries(entries(raw.tables).map(([key,rows])=>{
  const id=r=>KEYS[key].map(k=>r[k]).join('|');
  if(key==='universe'||key==='funds')return [key,rows];
  const old=saved.raw.tables[key]??[],frozen=ready(dataset());
  const merged=new Map(old.filter(r=>!frozen||!r.date&&!r.ex_date||(r.ex_date??r.date)>RAW.policy.freeze)
   .map(r=>[id(r),r]));
  for(const row of rows)merged.set(id(row),row);
  return [key,[...merged.values()]];
 }));
 const account=saved.wallet?validWallet(saved.wallet):wallet;
 raw={...raw,tables:both};wallet=account;invalidate();render();
}).catch(()=>{});
