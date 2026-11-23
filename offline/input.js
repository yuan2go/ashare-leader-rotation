'use strict';
const EXCHANGES=['sse.com.cn','szse.cn'];
const PUBLIC=['baostock.com','eastmoney.com','sina.com.cn','gtimg.cn','akshare.akfamily.xyz','cninfo.com.cn','cngold.org','dfcfw.com','stock.sohu.com'];
const MARKET_HOSTS=[...EXCHANGES,...PUBLIC];
const INDEX_HOSTS=[...MARKET_HOSTS,'csindex.com.cn'];
const FUND_HOSTS=[...MARKET_HOSTS,'huatai-pb.com','chinaamc.com','jsfund.cn','nffund.com','efunds.com.cn'];
const OPTIONAL_COLUMNS={actions:['ref_cash_per_share','ref_bonus_per_share','diff_note'],
 etf:['published_basis','published_at']};
const REF_COLUMNS=OPTIONAL_COLUMNS.actions.slice(0,2);
function csv(text){
 const lines=[];
 let line=[],cell='',quoted=false;
 text=text.replace(/^\uFEFF/,'');
 for(let i=0;i<text.length;i++){
  const c=text[i];
  if(c==='"'){
   if(quoted&&text[i+1]==='"'){cell+='"';i++;}
   else quoted=!quoted;
  }else if(c===','&&!quoted){line.push(cell.trim());cell='';}
  else if((c==='\n'||c==='\r')&&!quoted){
   if(c==='\r'&&text[i+1]==='\n')i++;
   line.push(cell.trim());
   if(line.some(Boolean))lines.push(line);
   line=[];cell='';
  }else cell+=c;
 }
 if(quoted)throw Error('CSV 引号未闭合');
 line.push(cell.trim());
 if(line.some(Boolean))lines.push(line);
 return lines;
}
function parseCsv(text,key){
 const rows=csv(text),heads=RAW.schemas[key].split(',');
 const actual=rows.shift()??[],optional=OPTIONAL_COLUMNS[key]??[];
 const required=heads.slice(0,heads.length-optional.filter(x=>heads.includes(x)).length);
 const allowed=[...required,...optional.filter(k=>actual.includes(k)&&heads.includes(k))];
 if(actual.join(',')!==allowed.join(','))throw Error('表头须为 '+heads.join(','));
 return rows.map((line,i)=>{
  if(line.length!==actual.length)throw Error('第 '+(i+2)+' 行列数不匹配');
  return Object.fromEntries(heads.map(k=>[k,line[actual.indexOf(k)]??'']));
 });
}
function isDay(s){
 return /^\d{4}-\d{2}-\d{2}$/.test(s)&&Number.isFinite(ms(s))&&new Date(ms(s)).toISOString().slice(0,10)===s;
}
const dow=d=>new Date(ms(d)).getUTCDay();
const week=d=>ms(d)-((dow(d)+6)%7)*86400000;
function prepare(raw,end=raw.policy.freeze,test=false){
 const {policy,tables:t}=raw,errors=[],etfErrors=[];
 function require(ok,msg,box=errors){if(!ok)box.push(msg);}
 function number(str,min){
  const v=Number(str);
  if(!/^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/.test(str)||!Number.isFinite(v)||v<min)throw Error('非法数值 '+str);
  return v;
 }
 function source(url,hosts,archive=null){
  if(test)return url==='fixture';
  if(archive!==null&&url===archive)return true;
  try{const u=new URL(url);return u.protocol==='https:'&&!u.username&&!u.password&&hosts.some(x=>u.hostname===x||u.hostname.endsWith('.'+x));}
  catch{return false;}
 }
 function officialEtfHistory(url,observed){
  if(test||!source(url,EXCHANGES))return false;
  const u=new URL(url),q=u.searchParams,one=(k,v)=>q.getAll(k).length===1&&q.get(k)===v;
  if(u.hostname==='query.sse.com.cn'&&u.pathname==='/commonQuery.do')
   return one('sqlId','COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L')&&one('STAT_DATE',observed);
  if(u.hostname==='www.szse.cn'&&u.pathname==='/api/report/ShowReport')
   return one('CATALOGID','scsj_fund_jjgm')&&one('jjlb','ETF')&&q.getAll('txtStart').length===1&&
    q.getAll('txtEnd').length===1&&q.get('txtStart')<=observed&&observed<=q.get('txtEnd');
  return false;
 }
 function verifiedLiveDay(stamp){
  const m=/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?\+08:00$/.exec(stamp);
  if(!m||!isDay(m[1])||+m[2]>23||+m[3]>59||+m[4]>59)throw Error('ETF 实时发布时间须为北京时间完整时间戳');
  const late=+m[2]>15||+m[2]===15&&(+m[3]||+m[4]||Number(m[5]??0));
  return late?new Date(ms(m[1])+86400000).toISOString().slice(0,10):m[1];
 }
 function uniq(rows,keys,box=errors){
  const result={};
  for(const r of rows){
   const k=keys.map(x=>r[x]).join('|');
   require(!Object.hasOwn(result,k),'重复记录 '+k,box);
   result[k]=r;
  }
  return result;
 }
 const universe=uniq(t.universe,['code']),funds=uniq(t.funds,['code']),ins={};
 for(const r of t.instruments){
  try{
   if(Object.hasOwn(ins,r.code)||!source(r.source,MARKET_HOSTS))throw Error('重复或来源不合规');
   if(!isDay(r.listing)||(r.delisting&&(!isDay(r.delisting)||r.delisting<=r.listing)))throw Error('最后上市日有误');
   ins[r.code]=r;
  }catch(e){(funds[r.code]&&!universe[r.code]?etfErrors:errors).push(r.code+' 上市/退市 '+e.message);}
 }
 const groups=Object.fromEntries(entries(funds).map(([k,v])=>[k,v.group]));
 const listings=Object.fromEntries(entries(ins).map(([k,v])=>[k,v.listing]));
 require(Object.keys(universe).length&&entries(universe).every(([k,r])=>/^\d{6}$/.test(k)&&r.bucket&&r.name),'股票池为空或字段无效');
 require(Object.values(groups).every(g=>g==='蓝筹'||g==='成长'),'ETF 分组无效');
 const calendar=t.calendar.filter(r=>r.date<=end).slice().sort((a,b)=>a.date.localeCompare(b.date));
 uniq(calendar,['date']);
 const days=calendar.map(r=>r.date),dayset=new Set(days),week_ends=[];
 const allDays=t.calendar.map(r=>r.date).sort(),lastMarket=allDays.at(-1)??'';
 const lookahead=test?[]:(raw.calendar_lookahead??[]).filter(r=>isDay(r.date)&&r.date>lastMarket&&
  dow(r.date)>0&&dow(r.date)<6&&source(r.source,MARKET_HOSTS)).map(r=>r.date);
 const marketDays=[...new Set([...allDays,...lookahead])].sort();
 const nextSession=Object.fromEntries(marketDays.slice(1).map((d,i)=>[marketDays[i],d]));
 if(!test){
  const expected=(raw.crosscheck||[]).filter(d=>d<=end&&d<=policy.freeze);
  require(expected.length&&days.filter(d=>d<=policy.freeze).join('|')===expected.join('|'),
    '历史市场日历与 v3.1 同日索引不匹配，需复核交易所原表');
 }
 require(days[0]?.slice(0,4)===policy.start.slice(0,4)&&days.at(-1)===end,'交易日历首尾未覆盖要求');
 calendar.forEach((r,i)=>{
  const d=r.date,wd=dow(d);
  require(isDay(d)&&wd>0&&wd<6&&['0','1'].includes(r.week_end)&&source(r.source,MARKET_HOSTS,'archive:v3.1:calendar'),d+' 日历日期或来源有误');
  const exp=i+1<days.length?week(days[i+1])!==week(d):wd===5||r.week_end==='1';
  require((r.week_end==='1')===exp,d+' 周界不相符');
  if(r.week_end==='1')week_ends.push(d);
 });
 const stocks=Object.fromEntries(Object.keys(universe).map(k=>[k,{}])),actions={},referenceTerms={},referenceChecks=[];
 const orders=t.stocks.filter(r=>r.date<=end);
 uniq(orders,['code','date']);
 for(const r of orders){
  const k=r.code,d=r.date,meta=ins[k],opened=r.status==='open';
  if(!stocks[k]){require(false,'未配置股票 '+k);continue;}
  require(isDay(d)&&dayset.has(d)&&['open','suspended'].includes(r.status)&&meta&&meta.listing<=d&&(!meta.delisting||d<=meta.delisting),k+' '+d+' 上市日期或交易状态有误');
  require(source(r.source,MARKET_HOSTS,k==='002714'?'archive:v3.1:002714:quotes':null)&&source(r.actions_source,MARKET_HOSTS,k==='002714'?'archive:v3.1:002714:actions':null),k+' '+d+' 行情或除权来源未通过');
  const ref=r.reference?number(r.reference,.01):null;
  if(opened){
   const o=number(r.open,.01),h=number(r.high,.01),l=number(r.low,.01),c=number(r.close,.01);
   require(l<=Math.min(o,c)&&Math.max(o,c)<=h&&number(r.volume,0)>0,k+' '+d+' OHLC/成交量有误');
   let upper=null,lower=null;
   if(r.limit==='bounded'){
    upper=number(r.upper,.01);lower=number(r.lower,.01);
    require(lower<=l&&h<=upper,k+' '+d+' 超出涨跌停价');
   }else require(r.limit==='unlimited'&&!r.upper&&!r.lower,k+' '+d+' 涨跌停字段缺失');
   stocks[k][d]={open:o,high:h,low:l,close:c,upper,lower,opened,ref};
  }else{
   require(!['open','high','low','close','upper','lower'].slice(0,4).some(x=>r[x])&&(!r.volume||number(r.volume,0)===0),k+' '+d+' 停牌行含成交');
   stocks[k][d]={open:null,close:null,upper:null,lower:null,opened,ref};
  }
 }
 const relevant=t.actions.filter(r=>r.ex_date<=end);
 uniq(relevant,['code','ex_date']);
 const reviewed=uniq(raw.verified_ref_actions??[],['code','ex_date']);
 const evidenceFields=['code','ex_date','announce_date','pay_date','share_date','cash_per_share','bonus_per_share','source',...REF_COLUMNS];
 for(const r of relevant){
  const k=r.code,d=r.ex_date,div=number(r.cash_per_share,0),bonus=number(r.bonus_per_share,0);
  require(isDay(d)&&isDay(r.announce_date)&&r.announce_date<d&&stocks[k]?.[d]?.ref&&source(r.source,MARKET_HOSTS,k==='002714'?'archive:v3.1:002714:actions':null),k+' '+d+' 除权参考/公告有误');
  require((div||bonus)&&(!div||isDay(r.pay_date)&&r.pay_date>=d)&&
    (!bonus||isDay(r.share_date)&&r.share_date>=d),k+' '+d+' 权益支付日有误');
  actions[k+'|'+d]={div,bonus,pay:r.pay_date,share:r.share_date};
  if(REF_COLUMNS.some(key=>r[key])){
   const evidence=reviewed[k+'|'+d];
   if(!evidence||!source(r.source,MARKET_HOSTS)||!evidenceFields.every(key=>(r[key]??'')===(evidence[key]??''))){
    errors.push(k+' '+d+' 差异化折算缺少一致的公告核验底稿');continue;
   }
   try{
    referenceTerms[k+'|'+d]={div:r.ref_cash_per_share?number(r.ref_cash_per_share,0):div,
     bonus:r.ref_bonus_per_share?number(r.ref_bonus_per_share,0):bonus,evidence:r};
   }catch(e){errors.push(k+' '+d+' 差异化折算 '+e.message);}
  }
 }
 const coverage=Object.keys(stocks).sort().map(k=>{
  const q=stocks[k];
  let previous=null;
  for(const d of Object.keys(q).sort()){
   const v=q[d];
   if(v.opened)require(v.ref!==null,k+' '+d+' 缺官方参考价');
   if(previous!==null&&v.ref!==null&&Math.abs(previous-v.ref)>.005)
    require(actions[k+'|'+d],k+' '+d+' 参考价跳变缺少已验证的权益事件');
   const action=actions[k+'|'+d];
   const terms=referenceTerms[k+'|'+d];
   if(previous!==null&&v.ref!==null&&action){
    const basis=terms??action,implied=(previous-basis.div)/(1+basis.bonus),error=Math.abs(implied-v.ref);
    require(error<=.015,k+' '+d+' 权益记录与交易所参考价不符');
    if(terms)referenceChecks.push({code:k,date:d,announce_date:terms.evidence.announce_date,cash:action.div,
     bonus:action.bonus,ref_cash:basis.div,ref_bonus:basis.bonus,previous,reference:v.ref,implied,error,
     passed:error<=.015,source:terms.evidence.source,note:terms.evidence.diff_note??''});
   }else if(terms)require(false,k+' '+d+' 差异化折算缺少可核验前收');
   if(v.opened)previous=v.close;
   else if(v.ref!==null)previous=v.ref;
  }
  const a=ins[k],expected=a?days.filter(d=>d>=a.listing&&(!a.delisting||d<=a.delisting)):null;
  const missing=expected?expected.filter(d=>!q[d]).length:null;
  require(a&&missing===0,k+' 缺上市资料或 '+missing+' 日行情');
  require(!a?.delisting||a.delisting>=end,k+' 的退市清算需提供专门项目');
  const open=Object.values(q).filter(r=>r.opened).length,rows=Object.keys(q).length;
  const quotes=orders.filter(r=>r.code===k);
  const act=relevant.filter(r=>r.code===k);
  const sources=[...new Set([a?.source,...quotes.flatMap(r=>[r.source,r.actions_source]),...act.map(r=>r.source)].filter(Boolean))].sort();
  return {code:k,listing:a?.listing??null,expected:a&&days.length?expected.length:null,rows,open,suspended:rows-open,
   actions:act.length,missing:a&&days.length?missing:null,missing_days:expected?expected.filter(d=>!q[d]):[],sources};
 });
 const benchmark={},indexRows=t.index.filter(r=>r.date<=end);
 uniq(indexRows,['date']);
 for(const r of indexRows){
  require(dayset.has(r.date)&&source(r.source,INDEX_HOSTS),'指数日期或来源错误 '+r.date);
  benchmark[r.date]=[number(r.open,.01),number(r.close,.01)];
 }
 const index_missing=days.filter(d=>!benchmark[d]);
 require(indexRows.length>0&&!index_missing.length,'缺沪深300官方序列');
 const etf=Object.fromEntries(Object.keys(funds).map(k=>[k,{}])),etfRows=t.etf.filter(r=>r.date<=end);
 const frequencies=new Map(),fundSources=Object.fromEntries(Object.keys(etf).map(k=>[k,new Set()]));
 const fundBases=Object.fromEntries(Object.keys(etf).map(k=>[k,{}]));
 for(const r of etfRows){const key=r.code+'|'+r.date;frequencies.set(key,(frequencies.get(key)??0)+1);}
 for(const r of etfRows){
  try{
   const code=r.code,d=r.date,m=ins[code];
   if(!isDay(d)||!Object.hasOwn(etf,code)||!dayset.has(d)||frequencies.get(code+'|'+d)!==1)throw Error('ETF 不在配置/日历内或重复');
   if(m&&(d<m.listing||(m.delisting&&d>m.delisting)))throw Error('ETF 观察日不在上市区间');
   if(!isDay(r.published)||r.published<d||!source(r.source,FUND_HOSTS)||!source(r.shares_source,FUND_HOSTS))
    throw Error('ETF 来源或发布时点缺失');
   const basis=r.published_basis??'',stamp=r.published_at??'',exchange=officialEtfHistory(r.shares_source,d);
   if(basis==='exchange_t1_convention'){
    if(!(exchange||test&&r.shares_source==='fixture')||r.published!==nextSession[d]||stamp)
     throw Error('ETF T+1 需官方历史报表及下一交易日，实时时间戳须留空');
   }else if(basis==='verified_live'){
    if(exchange||verifiedLiveDay(stamp)!==r.published||stamp.slice(0,10)<d)
     throw Error('ETF 实时发布时间与统计日或可见日有误');
   }else if(basis||stamp||exchange&&(!nextSession[d]||r.published<nextSession[d]))
    throw Error('ETF 披露口径或历史可见日有误');
   etf[code][d]=[r.published,number(r.shares,.01),number(r.price,.001),number(r.amount,0),number(r.share_scale,.0001)];
   fundBases[code][d]=basis||'legacy_date_only';
   fundSources[code].add(r.source);fundSources[code].add(r.shares_source);
  }catch(e){etfErrors.push(r.code+' '+r.date+' '+e.message);}
 }
 const etf_coverage=Object.keys(etf).sort().map(k=>{
  const v=etf[k];
  const a=ins[k],observed=Object.keys(v).sort(),first=observed[0]??null,last=observed.at(-1)??null,endLive=a?.delisting||end;
  const missing=first?days.filter(d=>d>=first&&d<=endLive&&!v[d]):[];
  const pre=days.filter(d=>a&&d>=a.listing&&d>='2016-01-01'&&d<=endLive&&(!first||d<first));
  const sources=[...new Set([...fundSources[k],...(a?[a.source]:[])])].sort();
  const usable=observed.filter(d=>v[d][0]<=end),published_basis={};
  for(const basis of Object.values(fundBases[k]))published_basis[basis]=(published_basis[basis]??0)+1;
  return {code:k,listing:a?.listing??null,first,last,rows:observed.length,usable:usable.length,published_basis,
   last_usable_observation:usable.at(-1)??null,
   missing:first?missing.length:null,missing_days:missing,precoverage:a&&days.length?pre.length:null,
   precoverage_days:pre,sources};
 });
 return {policy,days,week_ends,universe,groups,stocks,actions,benchmark,etf,listings,coverage,etf_coverage,errors,etf_errors:etfErrors,index_missing,
  reference_checks:referenceChecks,next_session:nextSession[end]??null,
  index_sources:[...new Set(indexRows.map(r=>r.source))].sort(),calendar_sources:[...new Set(calendar.map(r=>r.source))].sort()};
}
