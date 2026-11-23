'use strict';
const average=a=>a.length?mean(a):null;
function stats(samples,h){
 const independent=[];
 let limit=-1;
 for(const [i,v] of samples)if(i+1>limit){independent.push(v);limit=i+h;}
 return {n:samples.length,independent:independent.length,average:average(samples.map(r=>r[1])),
  win:average(samples.map(r=>r[1]>0)),independent_win:average(independent.map(r=>r>0))};
}
function leaders(d,s,w){
 const {days,stocks}=d;
 return sortCodes(Object.keys(stocks)).map(code=>{
  const quotes=stocks[code];
  const returns=[],on=[],off=[];
  let previous=null,ready=null;
  days.forEach((day,i)=>{
   const x=quotes[day],state=s.states[i][code];
   const price=x?.opened?x.close*state.factor:null;
   if(!ready&&state?.warm)ready=day;
   returns.push(price&&previous?price/previous-1:null);
   previous=price;
   if(i<1||!price||day<w[0]||day>w[1]||!state.warm||i+20>=days.length||days[i+20]>w[1])return;
   const a=quotes[days[i+1]],b=quotes[days[i+20]];
   if(!a?.opened||!b?.opened)return;
   const r=b.close*s.states[i+20][code].factor/(a.open*s.states[i+1][code].factor)-1;
   (state.gate?on:off).push([i,state.gate?r:-r]);
  });
  return {code,ready,hold_on:stats(on,20),hold_off:stats(off,20),
   m:replay(d,s,'T1',w,0,0,2,code).metrics,returns};
 });
}
function correlated(x,y,d){
 const a=[],b=[];
 for(let i=0;i<d.length;i++)if(x[i]!==null&&y[i]!==null&&d[i]>='2016-01-04'){a.push(x[i]);b.push(y[i]);}
 if(a.length<2)return {r:null,n:a.length};
 const xa=mean(a),ya=mean(b);
 const scale=Math.sqrt(sum(a.map(v=>(v-xa)**2))*sum(b.map(v=>(v-ya)**2)));
 return {r:scale?sum(a.map((v,i)=>(v-xa)*(b[i]-ya)))/scale:null,n:a.length};
}
function stockSeries(d,s,code,length=d.days.length){
 const start=Math.max(0,d.days.length-length);
 return d.days.slice(start).map((date,offset)=>{
  const i=start+offset;
  const q=d.stocks[code]?.[date],state=s.states[i][code];
  const price=key=>q.opened?q[key]*state.factor:null;
  return q&&state?{date,open:price('open'),high:price('high'),low:price('low'),close:price('close'),
   weekClose:state.week_close,weekMA:state.week_ma,gate:state.gate}:null;
 }).filter(Boolean);
}
function lotBand(mark){
 return !Number.isFinite(mark)?'pending':mark<=112.43?'low':mark<=224.90?'mid':'high';
}
const MACRO={
 '红利避险盾':'#3b82f6','上游能源化工':'#f97316','贵金属有色':'#eab308','农业与必选消费':'#22c55e',
 '硬科技半导体':'#a855f7','AI算力数字基建':'#06b6d4','高端制造出海':'#ec4899','医药军工顺周期金融':'#6366f1'
};
function macro(meta){
 const {bucket,subsector=''}=meta;
 if(Object.hasOwn(MACRO,bucket))return bucket;
 if(bucket==='红利')return '红利避险盾';
 if(bucket==='资源')return /黄金|有色|铜|稀土|金属/.test(subsector)?'贵金属有色':'上游能源化工';
 if(bucket==='农业'||/必选消费/.test(bucket))return '农业与必选消费';
 if(bucket==='科技成长')return /半导体/.test(subsector)?'硬科技半导体':'AI算力数字基建';
 if(bucket==='航运'||bucket==='制造'&&!/军工/.test(subsector))return '高端制造出海';
 if(/医药|军工|金融/.test(bucket)||/军工/.test(subsector))return '医药军工顺周期金融';
 return bucket;
}
function macroColor(name){return MACRO[name]??'#64748b';}
function clusterStocks(d,rows){
 const sorted=[...rows].sort((a,b)=>macro(d.universe[a.code]).localeCompare(macro(d.universe[b.code]),'zh-CN')||
  a.code.localeCompare(b.code));
 const matrix=sorted.map(()=>[]),pairs=[];
 for(let i=0;i<sorted.length;i++)for(let j=0;j<=i;j++){
  const pair=correlated(sorted[i].returns,sorted[j].returns,d.days);
  matrix[i][j]=matrix[j][i]=pair;
  if(i!==j&&pair.r>=.65)pairs.push({a:sorted[j].code,b:sorted[i].code,...pair});
 }
 return {codes:sorted.map(x=>x.code),matrix,pairs:pairs.sort((a,b)=>b.r-a.r)};
}
function radar(d,s){
 const {days:dates,groups,etf:funds}=d;
 const schedule=dates.map(()=>({})),out=[];
 function lower(sequence,target){
  let a=0,b=sequence.length;
  while(a<b){const m=(a+b)>>1;if(sequence[m]<target)a=m+1;else b=m;}
  return a;
 }
 for(const [code,series] of entries(funds)){
  for(let i=1;i<dates.length;i++){
   const now=series[dates[i]],before=series[dates[i-1]];
   if(!(now&&before))continue;
   const pos=lower(dates,[dates[i],now[0],before[0]].sort().at(-1));
   if(pos<dates.length)schedule[pos][code]=(now[1]-before[1]*now[4])*now[2];
  }
 }
 const ordered=Object.keys(groups).sort();
 const members={blue:ordered.filter(k=>groups[k]==='蓝筹'),growth:ordered.filter(k=>groups[k]==='成长')};
 const states={};
 let previousPulse=null;
 dates.forEach((day,i)=>{
  const r={date:day,funds:day>='2016-01-01'?schedule[i]:{}},coverage={};
  out.push(r);
  for(const h of [1,5,20]){
   const seq=i+1>=h?out.slice(i-h+1,i+1):[];
   const cohort=ordered.filter(k=>seq.length===h&&seq.every(p=>Object.hasOwn(p.funds,k)));
   const combined=[];
   const aggregate=list=>list.length?sum(seq.flatMap(p=>list.map(k=>p.funds[k]))):null;
   for(const [key,group] of entries(members)){
    const covered=cohort.filter(k=>group.includes(k));
    combined.push(...covered);
    coverage[key+h]=covered;
    r[key+(h>1?h:'')]=aggregate(covered);
   }
   coverage['total'+h]=combined.slice().sort();
   r['total'+(h>1?h:'')]=aggregate(combined);
  }
  r.coverage=coverage;
  r.bias20=r.blue20!==null&&r.growth20!==null?r.blue20-r.growth20:null;
  let span=[],blue=[],observation=null;
  for(const j of [i,i-1]){
   if(j<20||dates[j]<'2016-01-01')continue;
   span=dates.slice(j-20,j+1);
   blue=members.blue.filter(k=>span.every(x=>funds[k][x]?.[0]<=day));
   if(blue.length){observation=dates[j];break;}
  }
  coverage.pulse21=blue;
  const turnover=blue.length?span.map(x=>sum(blue.map(k=>funds[k][x][3]))):[];
  r.turnover=turnover.at(-1)??null;
  r.turnover_date=observation;
  const prev=observation?d.benchmark[span.at(-2)]:null,current=d.benchmark[observation];
  const prior=turnover.slice(0,-1);
  r.pulse=Boolean(turnover.length&&current&&prev&&mean(prior)>0&&r.turnover>=3*mean(prior)&&
   current[1]<prev[1]&&observation!==previousPulse);
  if(r.pulse)previousPulse=observation;
  if(r.date>='2020-01-01'){
   const active=[];
   if(r.total20!==null&&r.total20!==0)active.push(r.total20>0?'托底':'降温');
   if(r.pulse)active.push('护盘脉冲');
   if(active.length)states[i]=active;
  }
 });
 const conditional=[];
 for(const state of ['托底','护盘脉冲','降温'])for(const bucket of d.policy.buckets)for(const h of [20,60]){
  const samples=[];
  for(const [key,flags] of entries(states)){
   const i=Number(key);
   if(!flags.includes(state)||i+h>=dates.length)continue;
   const list=entries(s.states[i]).filter(([c,x])=>x.warm&&d.universe[c].bucket===bucket).map(([c])=>c);
   if(!list.length)continue;
   const returns=[];
   for(const c of list){
    const a=d.stocks[c][dates[i+1]],b=d.stocks[c][dates[i+h]];
    if(a?.opened&&b?.opened)returns.push(b.close*s.states[i+h][c].factor/(a.open*s.states[i+1][c].factor)-1);
   }
   if(returns.length===list.length)samples.push([i,mean(returns)]);
  }
  conditional.push({state,bucket,h,...stats(samples,h)});
 }
 return {daily:out,conditional};
}
