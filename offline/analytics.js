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
   const pos=lower(dates,now[0]>before[0]?now[0]:before[0]);
   if(pos<dates.length)schedule[pos][code]=(now[1]-before[1]*now[4])*now[2];
  }
 }
 const ordered=Object.keys(groups).sort();
 const members={blue:ordered.filter(k=>groups[k]==='蓝筹'),growth:ordered.filter(k=>groups[k]==='成长')};
 const states={};
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
  const span=i>=20&&day>='2016-01-01'?dates.slice(i-20,i+1):[];
  const blue=members.blue.filter(k=>span.length&&span.every(x=>funds[k][x]));
  coverage.pulse21=blue;
  const turnover=blue.length?span.map(x=>sum(blue.map(k=>funds[k][x][3]))):[];
  r.turnover=turnover.at(-1)??null;
  const prev=i?d.benchmark[dates[i-1]]:null,current=d.benchmark[day];
  const prior=turnover.slice(0,-1);
  r.pulse=Boolean(turnover.length&&current&&prev&&mean(prior)>0&&r.turnover>=3*mean(prior)&&current[1]<prev[1]);
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
