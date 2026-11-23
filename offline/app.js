'use strict';
const PAGES={home:'研究总览',moves:'净值与持仓',radar:'ETF 资金雷达',pool:'股票池体检',oos:'样本外与导入'};
const TITLES={Equal:'动态月度等权',CSI300:'沪深300',R1:'R1 · Top 1',R2:'R2 · Top 2',
 R2_Bucket:'R2 · 跨桶 Top 2',T1:'单票 T1'};
const PALETTE={Equal:'#98714b',CSI300:'#68767b',R1:'#5d50a0',R2:'#067658',R2_Bucket:'#298397',T1:'#c06a42'};
const FIELDS={universe:'股票池',instruments:'上市资料',calendar:'交易所日历',stocks:'股票日线',
 actions:'分红除权',index:'沪深300',funds:'ETF 清单',etf:'ETF 日份额'};
const KEYS={universe:['code'],funds:['code'],instruments:['code'],calendar:['date'],
  stocks:['code','date'],actions:['code','ex_date'],index:['date'],etf:['code','date']};
const bl=(x,d=2)=>Number.isFinite(x)?x.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const pct=x=>Number.isFinite(x)?bl(x*100)+'%':'—';
const cash=x=>Number.isFinite(x)?'¥'+bl(x,0):'—';
const tag=(text,good=false)=>'<span class="status '+(good?'pass':'')+'">'+esc(text)+'</span>';
const esc=x=>String(x??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[c]);
const cell=x=>x==null?'—':esc(x);
const btn=(x,code,on=false)=>'<button type="button" class="btn" aria-pressed="'+on+'" onclick="'+code+'">'+esc(x)+'</button>';
const picker=(x,value,code)=>'<select aria-label="筛选" onchange="'+code+'">'+entries(x).map(([k,v])=>'<option value="'+esc(k)+'"'+(k===value?' selected':'')+'>'+esc(v)+'</option>').join('')+'</select>';
const panel=(title,body,side='')=>'<section class="panel"><div class="panel-head"><h2>'+esc(title)+'</h2>'+side+'</div>'+body+'</section>';
const toolsBox=s=>'<div class="tools">'+s+'</div>';
const note=s=>'<p class="notes">'+esc(s)+'</p>';
const empty=s=>'<p class="empty">'+esc(s)+'</p>';
function table(head,rows,cl=''){
 return '<div class="scroll '+cl+'"><table><thead><tr>'+head.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+
 rows.map(r=>'<tr>'+r.map(x=>'<td>'+(x??'—')+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
const get=id=>document.getElementById(id);
let raw=structuredClone(RAW),tab='home',importKind='stocks',mode='R2',windowId='W5',pressure='base',trendWindow='W5',flowWindow='all',wallet={cash:450000,hold:'',set:false};
let memo=null,latest=null,reference=null,lastImport='';
const runs=new Map(),profiles=new Map();
let radarCache=null;
function invalidate(){memo=latest=reference=radarCache=null;runs.clear();profiles.clear();}
function dataset(live=false){
 if(live&&latest)return latest;
 if(!live&&memo)return memo;
 let end=raw.policy.freeze;
 if(live)for(const r of raw.tables.calendar)if(r.date>end)end=r.date;
 let data;
 try{
  data=prepare(raw,end);
  data.errors.push(...(RAW.ingest_blockers??[]));
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
function setTab(k){tab=k;render();window.scrollTo(0,0);}
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
  const r=new FormData(e.target),v=Number(r.get('cash')),h=r.get('positions').trim();
  if(!Number.isFinite(v)||v<0)throw Error('现金须为非负数字');
  const seen=new Set();
  for(const [k,n,...rest] of csv(h)){
   if(rest.length||!/^\d{6}$/.test(k)||!Number.isSafeInteger(Number(n))||Number(n)<=0||seen.has(k))throw Error('持仓行格式须为 证券代码,正整数股数');
   seen.add(k);
  }
  wallet={cash:v,hold:h,set:true};persist();render();
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
  const p=rounded(point[k].mark*100*(1+2/10000)),n=canBuy(p,Math.min(budget,unit),day);
  if(n){
   const f=fee(p,n,false,day)[0];budget-=n*p+f;
   orders.push({k,side:'买入',shares:n,estimated:p/100,fee:f/100});
  }
 }
 return {orders,desired,balance:budget/100,note:d.week_ends.includes(day)?'完整周选股。':'周中仅检查持仓退出。'};
}
function card(label,v,foot){
 return '<div class="card"><small>'+esc(label)+'</small><b>'+esc(v)+'</b><small>'+esc(foot)+'</small></div>';
}
function ranking(d){
 if(!ready(d))return empty('待确认名单并补齐全池官方日线。');
 const s=scanned(d),current=s.states.at(-1);
 return table(['排名','代码','名称','桶','趋势质量分'],s.ranks.at(-1).slice(0,5).map((k,i)=>[
  i+1,esc(k),esc(d.universe[k].name),esc(d.universe[k].bucket),bl(current[k].score,4)]));
}
function trialPanel(d){
 const q=trial(d);
 if(!q)return empty('通过正式校验后显示账户整手试算。');
 const account=wallet.set?'已录入账户':'默认模拟账户，¥450,000 现金';
 const target=(q.desired??[]).map(k=>d.universe[k].name).join(' / ')||'现金';
 return note(account+' · '+q.note+' 目标 '+target)+
  (q.orders.length?table(['代码','方向','股数','试算成交价','估算费用'],q.orders.map(r=>[esc(r.k),r.side,r.shares,bl(r.estimated),bl(r.fee)])):empty('本次无可试算订单。'))+
  note('估算以收盘价加减 2bp 为参考。实际开盘按限价、可用现金和成交状态复核。');
}
function home(){
 const d=dataset(true),ok=ready(d),s=ok?scanned(d):null,flow=radarData(d)?.daily.at(-1);
 const count=s?Object.values(s.states.at(-1)).filter(x=>x.gate&&x.opened).length:null;
 const float=note('净流为总份额变动估算。来源无法区分具体持有人。');
 return '<div class="counters">'+card('入围龙头',count===null?'待验收':count+' / '+Object.keys(d.universe).length,'T1=1 与已满 252 日')+
  card('模拟初始资金',cash(raw.policy.capital),'整手开盘撮合')+
  card('轮动试算',({R1:'Top 1',R2:'Top 2',R2_Bucket:'跨桶 Top 2'})[mode],'周末选股 · 周中先清仓')+
  card('ETF 20 日净流',flow?.total20==null?'资料待齐':cash(flow.total20/1e8)+' 亿','蓝筹与成长组')+'</div>'+
  '<div class="cols">'+panel('最新趋势质量榜',ranking(d))+panel('下次开盘试算',
   toolsBox(btn('R2 · Top 2',"setMode('R2')",mode==='R2')+btn('R1 · Top 1',"setMode('R1')",mode==='R1'))+trialPanel(d))+'</div>'+
  panel('宽基资金监测',float+(flow?table(['日净流','5 日净流','20 日净流','蓝筹 20 日','成长 20 日','放量脉冲'],
   [[cash(flow.total),cash(flow.total5),cash(flow.total20),cash(flow.blue20),cash(flow.growth20),flow.pulse?'触发':'未触发']]):
   empty('取得任意可核验的 ETF 连续日份额后显示部分覆盖。')),btn('打开雷达',"setTab('radar')"));
}
function seriesPlot(series,kind){
 if(!series.length)return empty('暂无可核验序列。');
 const n=Math.max(...series.map(x=>x.v.length)),axes={left:59,right:920,top:17,bottom:169};
 const xs=i=>axes.left+(axes.right-axes.left)*i/Math.max(n-1,1);
 let hi=0,lo=0;
 for(const line of series)for(const y of line.v)if(y!==null){hi=Math.max(hi,y);lo=Math.min(lo,y);}
 if(hi===lo){hi+=1;lo-=1;}
 const ys=y=>axes.bottom-(axes.bottom-axes.top)*(y-lo)/(hi-lo);
 let svg='<svg viewBox="0 0 942 198" role="img" aria-label="'+esc(kind)+'"><path d="M59 17V169H920" stroke="#d7e9e2" fill="none"/>';
 for(let i=0;i<=4;i++){
  const v=lo+(hi-lo)*i/4,y=ys(v);
  svg+='<path d="M59 '+y+'H920" stroke="#e4eeea"/><text x="6" y="'+(y+3)+'">'+(kind==='净值'?pct(v):bl(v,1))+'</text>';
 }
 for(const l of series){
  let go=false,poly='';
  l.v.forEach((v,i)=>{
   if(v===null){go=false;return;}
   poly+=(go?'L':'M')+xs(i).toFixed(1)+' '+ys(v).toFixed(1);go=true;
  });
  svg+='<path d="'+poly+'" stroke="'+l.color+'" stroke-width="2" fill="none"><title>'+esc(l.label)+'</title></path>';
  for(const i of l.pulse??[])svg+='<line x1="'+xs(i)+'" x2="'+xs(i)+'" y1="18" y2="169" stroke="#a95b28" stroke-dasharray="3 4"><title>'+esc(l.days[i])+' 放量脉冲</title></line>';
 }
 for(let j=0;j<5;j++){
  const i=Math.round((n-1)*j/4);
  svg+='<text x="'+Math.min(xs(i),873)+'" y="189">'+esc(series[0].days[i]?.slice(0,7))+'</text>';
 }
 return '<div class="chart">'+svg+'</svg></div>'+toolsBox(series.map(x=>'<span class="legend"><i style="background:'+x.color+'"></i>'+esc(x.label)+'</span>').join(''));
}
function gantt(d,n){
 const p=run(n)?.path;
 if(!p?.length)return empty('持仓时间线将在正式复跑后生成。');
 const names=['现金',...sortCodes([...new Set(p.flatMap(r=>Object.keys(r.hold)))])],x=i=>124+800*i/p.length;
 let body='<svg viewBox="0 0 942 '+(34+names.length*24)+'" role="img" aria-label="逐日持仓">';
 names.forEach((k,j)=>{
  const y=8+j*24;
  body+='<text x="1" y="'+(y+13)+'">'+esc(d.universe[k]?.name??k)+'</text><rect x="124" y="'+y+'" width="800" height="15" fill="#f0f4f2"/>';
  let start=-1;
  for(let i=0;i<=p.length;i++){
   const q=p[i],held=q&&(k==='现金'?!Object.keys(q.hold).length:q.hold[k]>0);
   if(held&&start<0)start=i;
   if(!held&&start>=0){
    body+='<rect x="'+x(start)+'" y="'+y+'" width="'+Math.max(1,x(i)-x(start))+'" height="15" rx="2" fill="'+(k==='现金'?'#a5b7af':'#197b64')+
     '"><title>'+esc(p[start].date)+' 至 '+esc(p[i-1].date)+'</title></rect>';start=-1;
   }
  }
 });
 for(let i=0;i<5;i++){
  const j=Math.round((p.length-1)*i/4);
  body+='<text x="'+Math.min(880,x(j))+'" y="'+(names.length*24+22)+'">'+esc(p[j].date.slice(0,7))+'</text>';
 }
 return '<div class="chart scroll gantt">'+body+'</svg></div>';
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
function moves(){
 const d=dataset();
 const head=['模式','收益','含息收益','最大回撤','收益回撤比','平均仓位','空仓','成交','实缴佣金','印花税','滑点'];
 const modes=MODELS.map(m=>{
  if(m==='CSI300'&&pressure!=='base')return [TITLES[m],...Array(10).fill('不适用')];
  const v=run(m,windowId,pressure)?.metrics,income=run(m,windowId,pressure,.018)?.metrics;
  return [TITLES[m],pct(v?.net),pct(income?.net),pct(v?.dd),bl(v?.rr),pct(v?.weight),pct(v?.flat),cell(v?.trades),cash(v?.commission),cash(v?.tax),cash(v?.slippage)];
 });
 const curves=MODELS.map(m=>[m,run(m)?.path]).filter(([,p])=>p?.length).map(([m,p])=>({
  label:TITLES[m],color:PALETTE[m],days:p.map(r=>r.date),v:p.map(r=>r.equity/(raw.policy.capital*100)-1)}));
 return panel('四窗绩效',toolsBox(Object.keys(raw.policy.windows).map(w=>btn(w,"setWindow('"+w+"')",w===windowId)).join('')+
  picker({base:'默认 2bp',T2:'T+2',slip7:'滑点 7bp'},pressure,'setPressure(this.value)'))+
  table(head,modes)+note(ready(d)?'图和持仓时间线采用默认 2bp。压力测试在上表切换。':
   '全池等待行情校验。牧原 T1 与沪深 300 的独立回放在数据合格时显示。'))+
  panel('累计净值',seriesPlot(curves,'净值'))+
  panel('逐日持仓甘特图',toolsBox(Object.keys(RULES).map(k=>btn(TITLES[k],"setMode('"+k+"')",mode===k)).join(''))+
   gantt(d,mode))+panel('2026 逐月换仓与行业敞口',monthly(d,mode));
}
function radarPage(){
 const d=dataset(true),r=radarData(d),latest=r?.daily.at(-1);
 const scope=latest?table(['周期','蓝筹净流','成长净流','合计净流','蓝筹覆盖','成长覆盖'],[1,5,20].map(h=>{
  const key=h>1?h:'',b=latest.coverage['blue'+h],g=latest.coverage['growth'+h];
  return [h+'D',cash(latest['blue'+key]),cash(latest['growth'+key]),cash(latest['total'+key]),
    b.length+' 只 '+esc(b.join(' / ')),g.length+' 只 '+esc(g.join(' / '))];
 })):empty('截至当前决策日没有可核验的连续流量样本。');
 const picks=Object.fromEntries([['all','分组 20 日净流'],...raw.tables.funds.map(x=>[x.code,x.name])]);
 let chart=empty('缺少可验收的 ETF 日份额与价格序列。');
 if(r){
  const seq=r.daily.slice(-200),days=seq.map(x=>x.date);
  const selected=flowWindow==='all'?[['total20','全组','#077758'],['blue20','蓝筹','#4d66a1'],['growth20','成长','#b07642']]:
    [[flowWindow,picks[flowWindow],'#077758']];
  chart=seriesPlot(selected.map(([k,label,color],i)=>({label,color,days,
    v:seq.map(v=>(v[k]??v.funds[k])==null?null:(v[k]??v.funds[k])/1e8),
    pulse:i===0?seq.flatMap((x,j)=>x.pulse?[j]:[]):[]})),'资金流（亿元）');
 }
 return panel('ETF 净申赎与放量脉冲',toolsBox(picker(picks,flowWindow,"flowWindow=this.value;render()"))+chart+
  note('选择单只基金查看日净申赎。虚线标记蓝筹成交额达到过去 20 日均额 3 倍且沪深 300 收跌。'))+
  panel('时点可用的最新资金流',scope+note('每个周期仅汇总全窗口均有可用数据的 ETF，同一窗口的成分保持一致。'))+
  panel('条件前瞻检验',r?table(['条件','行业桶','期限','平均收益','上涨胜率','样本','非重叠样本'],
    r.conditional.map(v=>[v.state,esc(v.bucket),v.h+'D',pct(v.average),pct(v.win),v.n,v.independent])):
    empty('完成官方序列审计后统计 2020 年以来的 20D 与 60D。'))+
  panel('7 只 ETF 的资料覆盖',table(['代码','名称','分组','最早份额日','最近份额日','已观测','当期已知','覆盖内缺口'],raw.tables.funds.map(x=>{
   const c=d.etf_coverage.find(v=>v.code===x.code);
   return [esc(x.code),esc(x.name),esc(x.group),cell(c?.first),cell(c?.last),c?.rows??0,c?.usable??0,c?.missing??'待核'];
  }))+note('净申赎为基金层面估算，无法从日份额确认国家队身份。'));
}
function pool(){
 const d=dataset(),r=ready(d);
 let x=[];
 if(r){
  if(!profiles.has(trendWindow))profiles.set(trendWindow,leaders(d,scanned(d),raw.policy.windows[trendWindow]));
  x=profiles.get(trendWindow);
 }
 const diag=r?table(['代码','名称','预热完成','20D 多头命中','独立样本','20D 空头命中','独立样本','T1 回放收益','最大回撤'],
  x.map(v=>[esc(v.code),esc(d.universe[v.code].name),cell(v.ready),pct(v.hold_on.win),v.hold_on.independent,
   pct(v.hold_off.win),v.hold_off.independent,pct(v.m.net),pct(v.m.dd)])):empty('缺少正式底稿，单票体检留空。');
 const matrix=r?table(['代码',...x.map(o=>o.code)],x.map(a=>[esc(a.code),...x.map(b=>{
  const p=correlated(a.returns,b.returns,d.days);
  const c=p.r===null?'#f8faf8':p.r>=0?'rgba(5,130,95,'+(.12+Math.abs(p.r)*.28)+')':'rgba(181,82,58,'+(.12+Math.abs(p.r)*.28)+')';
  return '<span style="background:'+c+';display:inline-block;padding:5px;border-radius:4px" title="共同样本 '+p.n+'">'+bl(p.r,2)+'</span>';
 })]),'heat'):empty('按同日配对的因果复权价格变动计算。');
 return panel('全票 T1 盲测',toolsBox(picker({W1:'W1 全区间',W5:'W5 现代段'},trendWindow,"trendWindow=this.value;render()"))+
  diag)+panel('相关性矩阵',matrix)+panel('种子池配置',table(['代码','证券','行业桶','细分行业','已验日线行'],
  raw.tables.universe.map((v)=>[esc(v.code),esc(v.name),esc(v.bucket),esc(v.subsector),d.coverage.find(c=>c.code===v.code)?.rows??0])));
}
function oos(){
 const d=dataset(true),approved=raw.policy.status==='approved',issues=[...d.errors,...d.etf_errors];
 const schema=RAW.schemas[importKind];
 const approval=approved?tag('名单已确认',true):
  '<label class="check"><input id="approve" type="checkbox"> 已核定当前股票池名单</label>'+btn('确认种子池','approve()');
 return panel('样本外数据 · 界碑 '+raw.policy.freeze,
  '<form class="form" onsubmit="paste(event)"><label>资料类型 '+picker(FIELDS,importKind,'setImport(this.value)')+'</label>'+
  '<label>上传 CSV<input type="file" accept=".csv,text/csv" onchange="upload(event)"></label>'+
  '<label>批量粘贴 CSV<textarea name="csv" placeholder="'+esc(schema)+'" required></textarea></label>'+
  '<div class="tools" style="padding:0"><button class="btn primary" type="submit">导入选中表</button>'+
  btn('下载本表模板','template()')+btn('导出工作数据','exportBundle()')+'</div></form>'+
  (lastImport?note(lastImport):'')+toolsBox(approval)+
  note('各表可分批导入。请先录入官方交易日历。每个周末标记需与交易所实际开休市规则一致。'))+
  panel('账户整手试算','<form class="form" onsubmit="submitWallet(event)"><label>实际可用现金'+
    '<input type="number" name="cash" min="0" step="0.01" value="'+esc(wallet.cash)+'" required></label>'+
    '<label>持仓，每行填写 证券代码,股数<textarea name="positions" placeholder="601398,100">'+esc(wallet.hold)+'</textarea></label>'+
    '<button class="btn" type="submit">更新账户</button></form>'+trialPanel(d))+
  panel('数据验收结果',issues.length?'<div class="error">'+esc(issues.length+' 项待核。')+'</div>'+
    table(['问题'],issues.slice(0,20).map(x=>[esc(x)])):note('通过行情与 ETF 结构校验。来源链接需保留原始公告作为依据。'));
}
function render(){
 const pane={home,moves,radar:radarPage,pool,oos},d=dataset(true),isReady=ready(d);
 get('nav').innerHTML='<div class="brand"><span class="mark">R</span><div><strong>A 股轮动研究台</strong><small>V4.0 · DAILY</small></div></div>'+
  '<nav aria-label="研究页面">'+entries(PAGES).map(([k,t])=>'<button onclick="setTab(\''+k+'\')" '+(k===tab?'aria-current="page"':'')+'>'+esc(t)+'</button>').join('')+
  '</nav><p class="asidefoot">自然周确认<br>独立的 ETF 资金观察</p>';
 get('bar').innerHTML='<span class="hint">研究工作区 / '+esc(PAGES[tab])+'</span>'+tag(isReady?'可回放':raw.policy.status==='approved'?'待历史数据':'种子池待确认',isReady);
 get('main').innerHTML='<div class="hero"><h1>'+esc(PAGES[tab])+'</h1><span class="hint">'+
  esc(d.days.at(-1)??raw.policy.freeze)+(d.days.length?' 行情截止':' 开发期界碑')+'</span></div>'+
  (!isReady&&tab==='home'?'<div class="notice">'+raw.tables.universe.length+
   (raw.policy.status==='approved'?' 只种子股已确认。':' 只种子名单待确认。')+
   ' 行情通过来源与覆盖校验后显示排名与试算。ETF 独立验收。</div>':'')+
  pane[tab]();
}
if(Object.hasOwn(PAGES,globalThis.location?.hash?.slice(1)??''))tab=location.hash.slice(1);
render();
storeBundle().then(saved=>{
 if(saved?.raw?.policy?.freeze===RAW.policy.freeze){
  const current=raw;
  const both=Object.fromEntries(entries(current.tables).map(([key,rows])=>{
   const id=r=>KEYS[key].map(k=>r[k]).join('|');
   if(key==='universe'||key==='funds')return [key,rows];
   const old=saved.raw.tables[key]??[];
   const merged=new Map(old.map(r=>[id(r),r]));
   for(const row of rows)merged.set(id(row),row);
   return [key,[...merged.values()]];
  }));
  raw={...current,tables:both};
  wallet=saved.wallet??wallet;invalidate();render();
 }
}).catch(()=>{});
