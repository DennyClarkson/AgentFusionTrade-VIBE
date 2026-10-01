/* Small safe Markdown renderer: fixed tags only; never interprets HTML or URLs. */
(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.FusionMarkdown=value;})(typeof window==='undefined'?globalThis:window,function(){
  'use strict';
  const escape=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function inline(value){
    const s=escape(value.replace(/\\\|/g,'|'));
    return s.replace(/(`+)([^`\n]+)\1|\*\*([^*\n]+)\*\*/g,(_,ticks,code,bold)=>code!==undefined?`<code>${code}</code>`:`<strong>${bold}</strong>`);
  }
  function cells(line){
    const s=line.trim();let result=[],cell='',code=0,delimiters=0;
    for(let i=0;i<s.length;i++){
      const c=s[i];
      if(c==='\\'&&i+1<s.length){cell+=c+s[++i];continue;}
      if(c==='`'){
        let end=i+1;while(s[end]==='`')end++;
        const size=end-i;if(!code)code=size;else if(code===size)code=0;
        cell+=s.slice(i,end);i=end-1;continue;
      }
      if(c==='|'&&!code){result.push(cell.trim());cell='';delimiters++;}
      else cell+=c;
    }
    result.push(cell.trim());
    if(s.startsWith('|'))result.shift();
    if(s.endsWith('|')&&result.at(-1)==='')result.pop();
    return delimiters?result:null;
  }
  function tableStart(lines,i){
    const head=cells(lines[i]||''),rule=cells(lines[i+1]||'');
    if(!head||!rule||head.length!==rule.length||head.length<1||!rule.every(c=>/^:?-{3,}:?$/.test(c)))return null;
    return {head,align:rule.map(c=>c.startsWith(':')&&c.endsWith(':')?'center':c.endsWith(':')?'right':'left')};
  }
  function render(value){
    const lines=String(value??'').replace(/\r\n?/g,'\n').split('\n');let html='',paragraph=[],items=[],listTag=null;
    function flushParagraph(){if(paragraph.length){html+=`<p>${paragraph.map(inline).join('<br>')}</p>`;paragraph=[];}}
    function flushList(){if(items.length){html+=`<${listTag}>${items.map(item=>`<li>${inline(item)}</li>`).join('')}</${listTag}>`;items=[];listTag=null;}}
    for(let i=0;i<lines.length;i++){
      const line=lines[i];
      if(!line.trim()){flushParagraph();flushList();continue;}
      const fence=line.match(/^\s*(`{3,}|~{3,})[^`]*$/);
      if(fence){
        flushParagraph();flushList();const body=[],marker=fence[1];
        while(++i<lines.length){if(new RegExp('^\\s*'+marker[0]+'{'+marker.length+',}\\s*$').test(lines[i]))break;body.push(lines[i]);}
        html+=`<pre><code>${escape(body.join('\n'))}</code></pre>`;continue;
      }
      const table=tableStart(lines,i);
      if(table){
        flushParagraph();flushList();
        const row=(values,tag)=>`<tr>${table.head.map((_,col)=>`<${tag} class="align-${table.align[col]}"${tag==='th'?' scope="col"':''}>${inline(values[col]||'')}</${tag}>`).join('')}</tr>`;
        html+=`<div class="markdown-table-wrap" role="region" aria-label="报告表格" tabindex="0"><table class="markdown-table"><thead>${row(table.head,'th')}</thead><tbody>`;
        i+=2;
        for(;i<lines.length;i++){
          const values=cells(lines[i]);
          if(!lines[i].trim()||!values||tableStart(lines,i))break;
          html+=row(values,'td');
        }
        i--;html+='</tbody></table></div>';continue;
      }
      const heading=line.match(/^\s*(#{2,3})\s+(.+)$/);
      if(heading){flushParagraph();flushList();const level=heading[1].length;html+=`<h${level}>${inline(heading[2])}</h${level}>`;continue;}
      const bullet=line.match(/^\s*[-*]\s+(.+)$/),ordered=line.match(/^\s*\d+[.)]\s+(.+)$/);
      if(bullet||ordered){flushParagraph();const next=bullet?'ul':'ol';if(listTag&&next!==listTag)flushList();listTag=next;items.push((bullet||ordered)[1]);}
      else{flushList();paragraph.push(line);}
    }
    flushParagraph();flushList();return html;
  }
  return {render};
});
