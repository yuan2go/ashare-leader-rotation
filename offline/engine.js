'use strict';
const MODELS=['Equal','CSI300','R1','R2','R2_Bucket','T1'];
const RULES={R1:[1,3,false],R2:[2,5,false],R2_Bucket:[2,5,true]};
const SCENARIOS={base:[2,0],slip7:[7,0],T2:[2,1]};
const LOT=100;
const mean=a=>sum(a)/a.length;
const sum=a=>a.reduce((s,x)=>s+x,0);
const rounded=v=>Math.floor(v+.5);
const sortCodes=a=>a.sort((x,y)=>x<y?-1:x>y?1:0);
const entries=x=>Object.entries(x);
const ms=s=>Date.parse(s+'T00:00:00Z');
function regression(prices,n){
 const y=prices.slice(-n).map(Math.log),center=(n-1)/2,my=mean(y);
 let xx=0,yy=0,xy=0;
 y.forEach((value,i)=>{
  const dx=i-center,dy=value-my;
  xx+=dx*dx;yy+=dy*dy;xy+=dx*dy;
 });
 return 252*xy/xx*(yy?Math.min(1,xy*xy/(xx*yy)):0);
}
function scan(d){
 const {days,stocks,actions}=d,weekEnds=new Set(d.week_ends),seen={},states=days.map(()=>({}));
 for(const code of sortCodes(Object.keys(stocks))){
  const quotes=stocks[code],prices=[],weeks=[];
  let factor=1,prior=null,lastInWeek=null,weekMA=null,previous=null,weekClose=null,t0=false,gate=false;
  days.forEach((day,i)=>{
   const q=quotes[day],act=actions[code+'|'+day];
   if(act&&prior!==null){factor*=prior/q.ref;if(!q.opened)prior=q.ref;}
   if(q?.opened){prior=q.close;prices.push(prior*factor);lastInWeek=prices.at(-1);}
   if(weekEnds.has(day)){
    if(lastInWeek!==null){weeks.push(lastInWeek);weekClose=lastInWeek;previous=weekMA;weekMA=weeks.length>=20?mean(weeks.slice(-20)):null;}
    lastInWeek=null;
   }
   if(!q)return;
   const warm=prices.length>=252;
   if(warm){
    t0=mean(prices.slice(-20))>mean(prices.slice(-60));
    if(!t0||(weekMA!==null&&weekClose<weekMA))gate=false;
    else if(previous!==null&&weekMA>=previous&&weekClose>weekMA)gate=true;
   }
   const score=gate&&q.opened?rounded(1e10*(regression(prices,20)+regression(prices,60))/2)/1e10:null;
   states[i][code]={gate,warm,t0,score,factor,week_close:weekClose,week_ma:weekMA,previous,mark:prior,opened:q.opened};
  });
  seen[code]=prices.length;
 }
 const ranks=states.map(row=>Object.keys(row).filter(k=>row[k].score!==null).sort((a,b)=>row[b].score-row[a].score||(a<b?-1:1)));
 return {states,ranks,seen};
}
function pick(mode,ranks,held,universe){
 const [slots,buffer,across]=RULES[mode],choices=[],buckets=new Set(),old=new Set(held);
 for(const candidates of [ranks.slice(0,buffer).filter(k=>old.has(k)),ranks])
  for(const k of candidates){
   if(choices.length===slots)return choices;
   const b=universe[k].bucket;
   if(!choices.includes(k)&&(!across||!buckets.has(b))){choices.push(k);buckets.add(b);}
  }
 return choices;
}
function fee(price,qty,sell,day){
 const gross=price*qty;
 return [Math.max(500,rounded(gross*25/100000)),sell?rounded(gross/(day<'2023-08-28'?1000:2000)):0];
}
function openPrice(q,sell,slip){
 const v=rounded(q.open*100*(1+(sell?-slip:slip)/10000));
 if(!sell&&q.upper!==null)return Math.min(v,rounded(q.upper*100));
 if(sell&&q.lower!==null)return Math.max(v,rounded(q.lower*100));
 return v;
}
function fillable(q,sell){
 if(!q?.opened)return false;
 const limit=sell?q.lower:q.upper;
 return limit===null||(sell?q.open>limit:q.open<limit);
}
function canBuy(price,budget,day){
 let lots=Math.floor(budget/(price*LOT));
 while(lots&&lots*LOT*price+fee(price,lots*LOT,false,day)[0]>budget)lots--;
 return lots*LOT;
}
function metrics(path,orders,capital){
 if(!path.length)return null;
 let top=capital,dd=0;
 for(const r of path){top=Math.max(top,r.equity);dd=Math.min(dd,r.equity/top-1);}
 const net=path.at(-1).equity/capital-1;
 return {net,dd,rr:dd?net/-dd:null,weight:mean(path.map(r=>r.weight)),flat:mean(path.map(r=>!Object.keys(r.hold).length)),
  trades:orders.length,commission:sum(orders.map(r=>r.commission))/100,tax:sum(orders.map(r=>r.stamp))/100,
  slippage:sum(orders.map(r=>r.slippage))/100};
}
function replay(d,signals,mode,span,rate=0,delay=0,slip=2,code=null){
 const {policy,days,stocks}=d,base=rounded(policy.capital*100),weekEnds=new Set(d.week_ends);
 if(mode==='CSI300'){
  let before=null;
  const path=days.filter(day=>day>=span[0]&&day<=span[1]).map(day=>{
   const [o,c]=d.benchmark[day];before??=o;
   return {date:day,equity:rounded(base*c/before),cash:0,hold:{CSI300:1},weight:1};
  });
  return {path,orders:[],metrics:metrics(path,[],base)};
 }
 let selected=[],cash=base,ready={},exits={},trims=new Set(),forced=new Set(),remaining=new Set(),avg=false;
 let prevDay=null,lastSignals={};
 const pos={},cashDue={},shareDue={},queued={},orders=[],path=[];
 const queue=(i,key,value)=>{queued[i]??={};queued[i][key]=value;};
 const totalPos=k=>(pos[k]??0)+sum(entries(shareDue).filter(([key])=>key.split('|')[1]===k).map(([,v])=>v));
 const active=()=>Object.fromEntries(sortCodes([...new Set([...Object.keys(pos),...Object.keys(shareDue).map(a=>a.split('|')[1])])])
   .map(k=>[k,totalPos(k)]).filter(([,n])=>n));
 const valuation=(day,opening)=>sum(entries(active()).map(([k,n])=>{
  const q=stocks[k][day];
  const p=opening?(q?.opened?q.open:q?.ref||lastSignals[k]?.mark):lastSignals[k]?.mark;
  if(p===null||p===undefined)throw Error(k+' '+day+' 缺持仓估值');
  return n*rounded(p*100);
 }));
 function execute(day,k,qty,sell,reason){
  const q=stocks[k][day],price=openPrice(q,sell,slip),[commission,stamp]=fee(price,qty,sell,day),gross=qty*price;
  cash+=sell?gross-commission-stamp:-gross-commission;
  pos[k]=(pos[k]??0)+(sell?-qty:qty);
  if(cash<0||pos[k]<0)throw Error('负现金或负持仓');
  orders.push({date:day,code:k,side:sell?'卖':'买',shares:qty,price,commission,stamp,
      slippage:qty*Math.abs(price-rounded(q.open*100)),signal:reason,cash});
 }
 for(let i=0;i<days.length;i++){
  const day=days[i];
  if(day>span[1])break;
  if(day<span[0]){lastSignals=signals.states[i];continue;}
  if(!i)throw Error('缺首日交易前的预热数据');
  if(prevDay===null){
   const p=i-1;
   if(mode==='Equal')queue(i+delay,'targets',[days[p],sortCodes(Object.keys(signals.states[p]).filter(k=>signals.states[p][k].warm))]);
   else if(mode==='T1'){
    const key=code??policy.reference;
    if(signals.states[p][key]?.gate)queue(i+delay,'targets',[days[p],[key]]);
   }else if(weekEnds.has(days[p]))queue(i+delay,'targets',[days[p],pick(mode,signals.ranks[p],[],d.universe)]);
  }else{
   cash=rounded(cash*(1+rate)**((ms(day)-ms(prevDay))/86400000/365));
   if(mode==='Equal'&&days[i-1].slice(0,7)!==day.slice(0,7))
    queue(i+delay,'targets',[days[i-1],sortCodes(Object.keys(lastSignals).filter(k=>lastSignals[k].warm))]);
  }
  prevDay=day;
  for(const k of sortCodes(Object.keys(active()))){
   const n=totalPos(k),a=d.actions[k+'|'+day];
   if(a?.div)cashDue[a.pay]=(cashDue[a.pay]??0)+rounded(n*a.div*100);
   if(a?.bonus){
    const key=a.share+'|'+k;
    shareDue[key]=(shareDue[key]??0)+Math.floor(n*a.bonus+1e-9);
   }
  }
  for(const dt of Object.keys(cashDue).sort())if(dt<=day){cash+=cashDue[dt];delete cashDue[dt];}
  for(const key of Object.keys(shareDue).sort()){
   const [dt,k]=key.split('|');
   if(dt<=day){pos[k]=(pos[k]??0)+shareDue[key];delete shareDue[key];}
  }
  const events=queued[i]??{};
  delete queued[i];
  if(events.targets){
   const [sd,recipe]=events.targets;
   selected=[...recipe];
   ready=Object.fromEntries(selected.map(k=>[k,sd]));
   exits=Object.fromEntries(Object.keys(active()).filter(k=>!selected.includes(k)).map(k=>[k,exits[k]??sd]));
   trims=new Set(['R2','R2_Bucket','Equal'].includes(mode)?selected:[]);
   avg=mode==='Equal';remaining=new Set(avg?selected:[]);
  }
  for(const [k,sd] of events.stops??[]){
   forced.add(k);selected=selected.filter(x=>x!==k);exits[k]??=sd;
  }
  if(forced.size)selected=selected.filter(k=>!forced.has(k));
  const current=active(),buylimit=RULES[mode]?.[0]??(avg?selected.length:1);
  const nav=cash+sum(Object.values(cashDue))+valuation(day,true),unit=Math.floor(nav/Math.max(1,buylimit));
  let delayedSale=false;
  for(const k of sortCodes(Object.keys(current))){
   const n=current[k],q=stocks[k][day];
   if(!selected.includes(k)){
    if(pos[k]&&fillable(q,true))execute(day,k,pos[k],true,exits[k]??day);
    if(totalPos(k))delayedSale=true;else delete exits[k];
   }else if(trims.has(k)){
    if(fillable(q,true)){
     const goal=Math.floor(unit/(rounded(q.open*100)*LOT))*LOT;
     const qty=Math.min(Math.floor((pos[k]??0)/LOT)*LOT,Math.max(0,Math.floor((n-goal+LOT-1)/LOT)*LOT));
     if(qty)execute(day,k,qty,true,ready[k]);
     trims.delete(k);
    }else delayedSale=true;
   }
  }
  forced=new Set([...forced].filter(k=>totalPos(k)>0));
  for(const k of selected){
   const own=active(),q=stocks[k][day],topUp=remaining.has(k)&&avg;
   if((own[k]&&!topUp)||forced.has(k)||!fillable(q,false))continue;
   if(Object.keys(own).length>=buylimit&&!own[k])continue;
   if(mode==='R2_Bucket'&&!own[k]&&Object.keys(own).some(old=>d.universe[old].bucket===d.universe[k].bucket))continue;
   const room=unit-(own[k]??0)*rounded(q.open*100),price=openPrice(q,false,slip);
   const qty=canBuy(price,Math.max(0,Math.min(cash,room)),day);
   if(qty)execute(day,k,qty,false,ready[k]??day);
   if(!delayedSale||qty)remaining.delete(k);
  }
  lastSignals=signals.states[i];
  const owned=active(),amount=valuation(day,false),equity=cash+sum(Object.values(cashDue))+amount;
  path.push({date:day,equity,cash,hold:owned,weight:amount/equity});
  if(RULES[mode]||mode==='T1'){
   const watch=sortCodes([...new Set([...Object.keys(owned),...selected])]);
   const stops=watch.filter(k=>!lastSignals[k]?.gate).map(k=>[k,day]);
   if(stops.length)queue(i+1+delay,'stops',stops);
   if(mode==='T1'){
    const key=code??policy.reference;
    queue(i+1+delay,'targets',[day,lastSignals[key]?.gate?[key]:[]]);
   }else if(weekEnds.has(day))queue(i+1+delay,'targets',[day,pick(mode,signals.ranks[i],Object.keys(owned),d.universe)]);
  }
 }
 return {path,orders,metrics:metrics(path,orders,base),next:entries(queued).map(([k,v])=>[Number(k),v]),
  intent:selected,pending:sortCodes([...forced])};
}
