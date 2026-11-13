import {jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {esc,date,fmt} from '../utils/dom.js';

const KIND_LABEL={split:'分裂',merge:'合并',band_to_noise:'全部转孤立点',band_partial_noise:'部分转孤立点',noise_to_band:'并入缺陷带',split_target:'分裂产物',band_absorbs_noise:'吸收孤立点',unchanged:'未变'};

function sourceNote(basis,detail){if(basis!=='per_point')return '历史结果无逐点明细：按里程区间与点数推断';const srcs=[detail?.first,detail?.second].map(v=>v==='recorded'?'逐点明细':'按里程区间还原');return '逐点对比 · '+srcs.join(' / ');}

function groupTable(title,groups,side){
 const linksFor=g=>(side==='first'?g.targets:g.sources)||[];
 const moves=groups.map(g=>{const links=linksFor(g);return '<tr><td>'+esc(g.name)+'</td><td>'+esc(g.count)+'</td><td>'+esc(g.from_km===null?'—':g.from_km)+' ~ '+esc(g.to_km===null?'—':g.to_km)+'</td><td><span class="fate '+esc(g.kind)+'">'+esc(KIND_LABEL[g.kind]||g.change)+'</span></td><td>'+esc(g.change)+'</td><td>'+(links.length?links.map(l=>esc(l.group)+' '+esc(l.count)+'点').join('；'):'—')+'</td></tr>';}).join('');
 return '<div class="section-title"><h2>'+esc(title)+'</h2><span>'+groups.length+' 个分组</span></div><div class="table-wrap"><table><thead><tr><th>分组</th><th>点数</th><th>里程区间km</th><th>去向判定</th><th>说明</th><th>'+(side==='first'?'对应到的分组':'来源分组')+'</th></tr></thead><tbody>'+moves+'</tbody></table></div>';
}

function totalsBlock(c){
 const t=c.totals;
 if(c.basis!=='per_point')return '<div class="metrics">'+[['第一次合计',t.first_total+' 点'],['第二次合计',t.second_total+' 点'],['逐点明细','不可用']].map(([k,v])=>'<div><span>'+esc(k)+'</span><strong>'+esc(v)+'</strong></div>').join('')+'</div>';
 return '<div class="metrics">'+[['逐点合计',t.dataset_total+' 点'],['未变',c.counts.unchanged_points],['分裂',c.counts.split_points],['合并',c.counts.merged_points],['带→孤立点',c.counts.band_to_noise_points],['孤立点→带',c.counts.noise_to_band_points]].map(([k,v])=>'<div><span>'+esc(k)+'</span><strong>'+esc(v)+'</strong></div>').join('')+'</div>'+
  '<div class="panel checks"><div class="section-title"><h2>逐点对账</h2><span>两次试验、匹配与五类去向必须相等</span></div>'+table([{key:'label',label:'核对项'},{key:'value',label:'实测'},{key:'expected',label:'应等于'}],c.checks.map(x=>({label:x.label,value:fmt(x.value)+(x.ok?' ✓':' ✗'),expected:fmt(x.expected)})))+'</div>';
}

function defectTable(c){
 if(c.basis!=='per_point')return '<p class="hint">历史结果缺少逐点明细，无法列出每个缺陷编号的去向。上方分组表为推断结果。</p>';
 const moved=c.defects.filter(d=>d.first_group!==d.second_group);
 return '<div class="section-title"><h2>逐点去向明细</h2><span>按里程排序 · '+c.defects.length+' 点 · 去向变化 '+moved.length+' 点</span></div>'+
  '<p class="hint">共 '+c.defects.length+' 点，逐点表格较长；以下先列出 '+Math.min(moved.length,60)+' 个分组发生变化的缺陷，其余未变点可按编号在完整数据中核对。</p>'+
  '<div class="table-wrap"><table><thead><tr><th>缺陷编号</th><th>里程km</th><th>等级</th><th>第一次分组</th><th>第二次分组</th><th>变化</th></tr></thead><tbody>'+(moved.length?moved.slice(0,60):c.defects.slice(0,60)).map(d=>'<tr><td>'+esc(d.defect_id)+'</td><td>'+esc(d.mileage_km)+'</td><td>'+esc(d.severity)+'</td><td>'+esc(d.first_group)+'</td><td>'+esc(d.second_group)+'</td><td><span class="fate '+(d.first_group!==d.second_group?'moved':'unchanged')+'">'+esc(d.change)+'</span></td></tr>').join('')+'</tbody></table></div>';
}

export async function compare(ids){
 workspace.jobs=await jobs.list();
 const options=workspace.jobs.filter(j=>j.status==='completed');
 if(!options.length)return heading('05 / COMPARE','两次试验对比','按缺陷编号追踪同一批缺陷点在两组参数下的去向。')+'<div class="empty">还没有已完成的分析任务。先到分析工作台提交两次不同参数的计算。<a href="#/workbench">前往工作台 ↗</a></div>';
 const first=ids.first??options[options.length-1]?.id,second=ids.second??options[0]?.id;
 const select=(name,value)=>'<select name="'+name+'">'+options.map(j=>'<option value="'+j.id+'" '+(String(j.id)===String(value)?'selected':'')+'>'+esc(j.name)+' · '+esc(date(j.created_at))+'</option>').join('')+'</select>';
 let c=null,error='';
 if(first&&second&&String(first)!==String(second)){
   try{c=await jobs.compare(first,second);}catch(e){error=e.message;}
 }else if(first&&second){error='请选择两条不同的试验进行对比';}
 const bar='<form id="compare-form"><label>基准试验（第一次）'+select('first',first)+'</label><label>对比试验（第二次）'+select('second',second)+'</label><button class="primary" type="submit">按编号追踪去向</button></form>';
 if(!c)return heading('05 / COMPARE','两次试验对比','同一批缺陷点在两组密度参数下：哪些带分裂、哪些合并、哪些点转成噪声。','')+'<section class="panel compare-form">'+bar+'</section>'+(error?'<section class="panel empty">'+esc(error)+'</section>':'<p class="hint">选择两次已完成试验。仅同一数据批次且缺陷编号唯一的试验可逐点对比。</p>');
 const t=c.totals;
 return heading('05 / COMPARE','两次试验对比',c.first.name+' → '+c.second.name,'')+'<section class="panel compare-form">'+bar+'<p class="hint">'+esc(sourceNote(c.basis,c.detail_source))+' · 数据批次 '+esc(c.dataset.name)+' · 缺陷编号 '+esc(c.dataset.rows)+' 个</p></section>'+
  (c.params_changed.length?'<section class="panel"><div class="section-title"><h2>参数变化</h2></div><ul class="questions">'+c.params_changed.map(p=>'<li>'+esc(p.key)+'：'+esc(p.first)+' → '+esc(p.second)+'</li>').join('')+'</ul></section>':'')+
  '<section class="result-lead"><p class="eyebrow">TRACE RESULT</p><h2>'+(c.basis==='per_point'?'已按缺陷编号逐点对账：'+t.matched+'/'+t.dataset_total+'点匹配。':'历史结果无法逐点还原，以下为分组级推断。')+'</h2>'+totalsBlock(c)+'</section>'+
  '<div class="two-columns"><section class="panel"><div class="section-title"><h2>复核结论</h2></div><ol class="questions">'+c.findings.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ol>'+c.warnings.map(w=>'<p class="warning">'+esc(w)+'</p>').join('')+'</section><section class="panel"><div class="section-title"><h2>去向汇总</h2><span>第一次分组 → 第二次分组 · 全部 '+c.dataset.rows+' 点</span></div>'+table([{key:'from_group',label:'第一次'},{key:'to_group',label:'第二次'},{key:'count',label:'点数'}],c.edges.map(e=>({from_group:e.from_group,to_group:e.to_group,count:e.count})))+'</section></div>'+
  '<div class="two-columns"><section class="panel">'+groupTable('第一次试验分组去向',c.first_groups,'first')+'</section><section class="panel">'+groupTable('第二次试验分组来源',c.second_groups,'second')+'</section></div>'+
  '<section class="panel">'+defectTable(c)+'</section>';
}

export function bindCompare(refresh){
 const first=document.querySelector('[name=first]'),second=document.querySelector('[name=second]');
 const submit=document.querySelector('#compare-form');
 if(submit)submit.addEventListener('submit',e=>{e.preventDefault();location.hash='#/compare/'+first.value+'/'+second.value;});
}
