'use strict';
const STUDY_GROUPS={all:'全部',up:'T1=1 上升',down:'T1=0 下行'};
let selectedStock='',stockHorizon=240,earningsHorizon=5,earningsGroup='all',flowTrack='actual';
const stockButton=(code,name)=>'<button class="text-btn" data-code="'+esc(code)+'" onclick="showStock(this.dataset.code)">'+esc(name)+'</button>';
function showStock(code){
 if(!Object.hasOwn(dataset().universe,code))return;
 selectedStock=code;setTab('research');
}
function seriesFor(d,code){
 if(!d.stockSeries)d.stockSeries=new Map();
 if(!d.stockSeries.has(code))d.stockSeries.set(code,stockSeries(d,scanned(d),code));
 return d.stockSeries.get(code);
}
function getProfiles(d,w){
 if(!profiles.has(w))profiles.set(w,leaders(d,scanned(d),raw.policy.windows[w]));
 return profiles.get(w);
}
function earningsStudy(x){
 const horizon=x.horizons.includes(earningsHorizon)?earningsHorizon:x.horizons[0];
 const kinds={forecast:'业绩预告',preliminary:'业绩快报',annual:'正式年报'};
 const summary=table(['周期状态','样本','独立 N','均收益','超额均收益','胜率','盈亏比'],entries(STUDY_GROUPS).map(([key,label])=>{
  const v=x.summary[key][horizon];
  return [label,v.n,v.independent,signed(v.gross),signed(v.excess),pct(v.win),bl(v.payoff)];
 }));
 const detail=x.events.filter(v=>earningsGroup==='all'||v.t1===(earningsGroup==='up'?1:0)).slice().reverse();
 return panel(x.title,toolsBox(x.horizons.map(h=>btn(h+'D','earningsHorizon='+Number(h)+';render()',h===horizon)).join('')+
  picker(STUDY_GROUPS,earningsGroup,'earningsGroup=this.value;render()'))+
  note(x.notice_count+' 条公告 · '+x.timed_count+' 条有公开时点 · '+(x.document_count??0)+' 条附原文 PDF')+summary+
  '<details><summary>逐笔事件明细（'+detail.length+'）</summary>'+
  table(['公告','公开时点 · 中国时间','开盘入场','T1','到期退出','绝对收益','超额'],detail.map(v=>{
   const r=v.returns[horizon];
   return [esc(kinds[v.kind])+'<br>'+esc(v.title)+'<br>'+
    evidence(v.disclosure_url,v.document_basis==='issuer_pdf'?'公告原文':'公告索引')+
    (v.timestamp_url&&v.timestamp_url!==v.disclosure_url?' · '+evidence(v.timestamp_url,'时点依据'):''),
    esc(v.published_at?.replace('T',' ').slice(0,19)||v.date)+' · '+esc(v.session),
    cell(v.entry),cell(v.t1),cell(r.exit??r.reason),signed(r.gross),signed(r.excess)];
  }))+'</details>'+note(x.method));
}
function flowStudy(x){
 const track=x.tracks[flowTrack];
 const rows=entries(STUDY_GROUPS).flatMap(([key,label])=>x.horizons.map(h=>{
  const v=track.summary[key][h];
  return [label,h+'D',v.n,v.independent,signed(v.gross),pct(v.win_gross),signed(v.net_2bp),pct(v.win_net_2bp),
   signed(v.net_7bp),pct(v.win_net_7bp),pct(v.drag_net_2bp),pct(v.drag_net_7bp)];
 }));
 return panel(x.title,toolsBox(picker(Object.fromEntries(entries(x.tracks).map(([k,v])=>[k,v.title])),
  flowTrack,'flowTrack=this.value;render()'))+note((track.available.join(' 至 ')||'当前无覆盖')+
  ' · '+track.observations+' 条观察 · '+track.events.length+' 次触发')+
  table(['周期状态','持有','样本','独立 N','毛收益','毛胜率','净收益 2bp','净胜率','净收益 7bp','净胜率','年化拖累 2bp','年化拖累 7bp'],rows)+
  '<details><summary>逐笔信号（'+track.events.length+'）</summary>'+table(
   ['信号日','T1','收盘涨跌','大单净额 / 放量倍数','开盘入场',...x.horizons.map(h=>h+'D · 净收益 2bp')],
   track.events.slice().reverse().map(v=>[esc(v.date),v.t1,signed(v.change),
    Number.isFinite(v.large_net_yuan)?money(v.large_net_yuan):bl(v.volume_ratio)+'×',
    cell(v.entry),...x.horizons.map(h=>v.returns[h].reason?cell(v.returns[h].reason):signed(v.returns[h].net_2bp))]))+
   '</details>'+note(x.method));
}
function researchPage(){
 const d=dataset(),pool=Object.values(d.universe),plugins=RAW.research??{};
 if(!Object.hasOwn(d.universe,selectedStock))
  selectedStock=Object.keys(plugins).find(k=>Object.hasOwn(d.universe,k))??pool[0]?.code??'';
 const code=selectedStock,meta=d.universe[code],plugin=plugins[code];
 const verified=Boolean(meta&&ready(d));
 const state=verified?scanned(d).states.at(-1)[code]:null;
 const p=verified?getProfiles(d,'W5').find(r=>r.code===code):null;
 const candles=verified?seriesFor(d,code).slice(-stockHorizon):[];
 const events=new Set((plugin?.experiments?.earnings_event_study?.events??[]).map(v=>v.entry));
 if(candles.length)chartJobs.set('candles',(c,w,h,t)=>candleChart(c,w,h,t,candles,events));
 const controls=toolsBox(picker(Object.fromEntries(pool.map(v=>[v.code,v.code+' '+v.name])),code,'showStock(this.value)','选择股票')+
  [60,120,240,3000].map(h=>btn(h===3000?'全区间':h+'D','stockHorizon='+h+';render()',h===stockHorizon)).join(''));
 const renderers={earnings_event_study:earningsStudy,price_down_flow_in:flowStudy};
 const cards=entries(renderers).map(([key,render])=>plugin?.experiments[key]?render(plugin.experiments[key]):'').join('');
 return panel(meta?meta.code+' '+meta.name:'个股',controls+
  '<div class="counters">'+card('冻结收盘价',money(state?.mark),meta?.subsector)+
   card('T1 门禁',state?String(Number(state.gate)):'—',state?.warm?'预热完成':'待预热')+
   card('趋势质量分',bl(state?.score,4),'冻结评分')+
   card('W5 单票 T1',pct(p?.m.net),'最大回撤 '+pct(p?.m.dd))+
   card('20D 独立多头命中',pct(p?.hold_on.independent_win),'N = '+(p?.hold_on.independent??'—'))+'</div>'+
  (candles.length?chart('candles','个股因果复权 K 线、20 周均线、T1 背景及财报节点',350)+
   note('紫线为 20 周均线，蓝色背景为 T1=1，黄点为财报事件的可交易入场日。'):empty('完成冻结行情验收后可绘制因果复权 K 线。')))+
  (cards||panel('专项研究',empty('此标的暂无专项研究插件。')));
}
