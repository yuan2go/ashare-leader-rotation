'use strict';
const chartJobs=new Map();
const chart=(id,label,height=255)=>{
 return '<canvas id="'+id+'" class="chart" style="--height:'+height+'px" role="img" aria-label="'+esc(label)+'"></canvas>';
};
function paintCharts(){
 for(const [id,draw] of chartJobs){
  const canvas=document.getElementById(id);
  if(!canvas?.getContext||!canvas.clientWidth)continue;
  const w=canvas.clientWidth,h=canvas.clientHeight,dpr=globalThis.devicePixelRatio||1;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const c=canvas.getContext('2d');
  if(!c)continue;
  c.setTransform(dpr,0,0,dpr,0,0);c.font='10px monospace';
  const tokens=getComputedStyle(canvas);
  draw(c,w,h,Object.fromEntries(['line','muted','text','surface','accent','up','down'].map(
   key=>[key,tokens.getPropertyValue('--'+key).trim()])),canvas);
 }
}
function chartFrame(c,w,h,t,values,days,format,showTicks=true){
 const left=w<450?48:59,right=w-12,top=15,bottom=h-25;
 let lo=Math.min(...values),hi=Math.max(...values);
 if(!Number.isFinite(lo)){lo=0;hi=1;}
 if(hi===lo){lo-=1;hi+=1;}
 const span=(right-left)/Math.max(1,days.length-1);
 const x=i=>left+i*span,y=v=>bottom-(v-lo)*(bottom-top)/(hi-lo);
 for(let i=0;i<=4;i++){
  const level=lo+i*(hi-lo)/4,py=y(level);
  c.strokeStyle=t.line;c.beginPath();c.moveTo(left,py);c.lineTo(right,py);c.stroke();
  c.fillStyle=t.muted;c.textAlign='right';c.fillText(format(level),left-6,py+3);
 }
 c.textAlign='center';
 if(showTicks&&days.length)for(let j=0;j<=Math.min(4,days.length-1);j++){
  const i=Math.round(j*(days.length-1)/Math.max(1,Math.min(4,days.length-1)));
  c.fillText(days[i].slice(0,10),Math.min(w-33,Math.max(33,x(i))),h-7);
 }
 return {x,y,left,right,top,bottom,span};
}
function strokePlot(c,frame,values,color,width=1.5){
 c.beginPath();c.strokeStyle=color;c.lineWidth=width;
 let active=false;
 values.forEach((v,i)=>{
  if(!Number.isFinite(v)){active=false;return;}
  const x=frame.x(i),y=frame.y(v);
  if(active)c.lineTo(x,y);else c.moveTo(x,y);
  active=true;
 });
 c.stroke();c.lineWidth=1;
}
function equityChart(c,w,h,t,series,selected,universe){
 if(!series.length)return;
 const days=series[0].path.map(x=>x.date);
 const values=series.flatMap(x=>x.path.map(v=>v.equity/(raw.policy.capital*100)-1));
 const frame=chartFrame(c,w,h,t,[0,...values],days,v=>bl(v*100,0)+'%');
 const history=series.find(s=>s.code===selected)?.path??[];
 history.forEach((r,i)=>{
  const held=Object.keys(r.hold??{});
  held.forEach((code,slot)=>{
   c.fillStyle=macroColor(macro(universe[code]));c.globalAlpha=.18;
   c.fillRect(frame.x(i)-frame.span/2,frame.bottom-4*(slot+1),Math.max(1,frame.span),3);
  });
 });
 c.globalAlpha=1;
 for(const {code,path} of series)strokePlot(c,frame,path.map(v=>v.equity/(raw.policy.capital*100)-1),
  PALETTE[code],code===selected?2.2:1);
}
function ganttChart(c,w,h,t,path,universe,slots){
 if(!path.length)return;
 const x=44,step=(w-x-12)/path.length;
 let active=[];
 path.forEach((r,i)=>{
  const codes=Object.keys(r.hold);
  active=active.map(k=>codes.includes(k)?k:null);
  for(const code of codes)if(!active.includes(code)){
   const open=active.indexOf(null);
   if(open>=0)active[open]=code;else active.push(code);
  }
  for(let slot=0;slot<slots;slot++){
   const k=active[slot];
   c.fillStyle=k?macroColor(macro(universe[k])):t.line;
   c.fillRect(x+step*i,13+slot*26,Math.max(step,1),20);
  }
 });
 c.fillStyle=t.muted;c.textAlign='left';
 for(let i=0;i<slots;i++)c.fillText('槽 '+(i+1),5,27+i*26);
 for(let n=0;n<=3;n++){
  const i=Math.floor((path.length-1)*n/3);
  c.fillText(path[i].date,Math.min(w-66,x+step*i),h-8);
 }
}
function matrixChart(c,w,h,t,canvas,cluster,universe){
 const {codes,matrix}=cluster,n=codes.length;
 if(!n)return;
 const start=51,end=24,size=Math.min(w-start-10,h-end-13),step=size/n;
 const draw=(a=-1,b=-1)=>{
  c.clearRect(0,0,w,h);
  c.font=Math.max(6,Math.min(10,step*.75))+'px monospace';
  matrix.forEach((row,i)=>{
   row.forEach(({r},j)=>{
    c.fillStyle=r===null?t.line:r<0?'rgba(34,197,94,'+(.12+Math.abs(r)*.85)+')':'rgba(240,82,82,'+(.12+r*.85)+')';
    c.fillRect(start+j*step,end+i*step,Math.ceil(step),Math.ceil(step));
   });
   c.fillStyle=macroColor(macro(universe[codes[i]]));c.textAlign='right';
   if(n<=24||i%2===0||i===a)c.fillText(codes[i],start-5,end+(i+.74)*step);
   c.save();c.translate(start+(i+.65)*step,end-3);c.rotate(-Math.PI/3);c.textAlign='left';
   if(n<=24||i%2===0||i===b)c.fillText(codes[i],0,0);
   c.restore();
  });
  if(a<0||b<0)return;
  c.strokeStyle=t.text;
  c.strokeRect(start,end+a*step,size,step);c.strokeRect(start+b*step,end,step,size);
 };
 const detail=document.getElementById('matrix-detail');
 const initial=n+' × '+n+' · 同日因果复权收益，始于 2016-01-04';
 const move=(a,b)=>{
  draw(a,b);
  detail.textContent=a<0||b<0?initial:codes[a]+' × '+codes[b]+'  r = '+bl(matrix[a][b].r,3)+
   '  共同样本 '+matrix[a][b].n;
 };
 canvas.onpointermove=e=>{
  const rect=canvas.getBoundingClientRect(),a=Math.floor((e.clientY-rect.top-end)/step),b=Math.floor((e.clientX-rect.left-start)/step);
  move(a>=0&&a<n&&b>=0&&b<n?a:-1,b>=0&&b<n?b:-1);
 };
 canvas.onpointerleave=()=>move(-1,-1);
 canvas.onkeydown=e=>{
  const delta={ArrowUp:[-1,0],ArrowDown:[1,0],ArrowLeft:[0,-1],ArrowRight:[0,1]}[e.key];
  if(!delta)return;
  e.preventDefault();
  const old=canvas.cursor??[0,0];
  canvas.cursor=old.map((v,i)=>Math.max(0,Math.min(n-1,v+delta[i])));
  move(...canvas.cursor);
 };
 canvas.tabIndex=0;move(-1,-1);
}
function flowChart(c,w,h,t,daily){
 const values=daily.map(v=>[v.blue20===null?null:v.blue20/1e8,v.growth20===null?null:v.growth20/1e8]);
 const bias=daily.map(v=>v.bias20===null?null:v.bias20/1e8);
 const bounds=values.flatMap(p=>[0,...p.filter(Number.isFinite),p.reduce((s,v)=>s+Math.max(0,v??0),0),
  p.reduce((s,v)=>s+Math.min(0,v??0),0)]);
 const f=chartFrame(c,w,h,t,[...bounds,...bias.filter(Number.isFinite)],daily.map(v=>v.date),v=>bl(v,0)+' 亿');
 values.forEach((row,i)=>{
  let positive=0,negative=0;
  row.forEach((v,j)=>{
   if(v===null)return;
   const start=v<0?negative:positive;
   if(v<0)negative+=v;else positive+=v;
   c.fillStyle=j?'#ec4899':'#3b82f6';c.globalAlpha=.57;
   c.fillRect(f.x(i)-f.span/2,Math.min(f.y(start),f.y(start+v)),Math.max(1,f.span*.92),Math.abs(f.y(start+v)-f.y(start)));
  });
 });
 c.globalAlpha=1;
 daily.forEach((v,i)=>{
  if(!v.pulse)return;
  c.fillStyle='#eab308';c.fillRect(f.x(i)-1,f.top,3,10);
 });
 strokePlot(c,f,bias,'#a78bfa');
}
function candleChart(c,w,h,t,series,events){
 const values=series.flatMap(v=>[v.low,v.high,v.weekMA]).filter(Number.isFinite);
 const f=chartFrame(c,w,h,t,values,series.map(v=>v.date),v=>bl(v,1));
 series.forEach((v,i)=>{
  const x=f.x(i),width=Math.max(1,Math.min(10,f.span*.65));
  if(v.gate){
   c.globalAlpha=.11;c.fillStyle=t.accent;
   c.fillRect(x-f.span/2,f.top,Math.max(1,f.span),f.bottom-f.top);c.globalAlpha=1;
  }
  if(v.close===null)return;
  c.fillStyle=c.strokeStyle=v.close>=v.open?t.up:t.down;
  c.beginPath();c.moveTo(x,f.y(v.high));c.lineTo(x,f.y(v.low));c.stroke();
  c.fillRect(x-width/2,Math.min(f.y(v.open),f.y(v.close)),width,Math.max(1,Math.abs(f.y(v.open)-f.y(v.close))));
  if(events.has(v.date)){
   c.fillStyle='#eab308';c.beginPath();c.arc(x,f.top+6,3,0,Math.PI*2);c.fill();
  }
 });
 strokePlot(c,f,series.map(v=>v.weekMA),'#a78bfa');
}
function spark(series){
 const points=series.map(v=>v.close),known=points.filter(Number.isFinite);
 if(known.length<2)return '—';
 const lo=Math.min(...known),span=Math.max(...known)-lo||1;
 let active=false;
 const path=points.map((v,i)=>{
  if(v===null){active=false;return '';}
  const mark=active?'L':'M';active=true;
  return mark+(i*99/(points.length-1)).toFixed(1)+' '+(21-(v-lo)*18/span).toFixed(1);
 }).join('');
 return '<svg class="spark" viewBox="0 0 100 24" role="img" aria-label="60日因果复权迷你图"><path fill="none" class="'+
  (known.at(-1)>=known[0]?'line-up':'line-down')+'" stroke-width="1.4" d="'+path+'"/></svg>';
}
