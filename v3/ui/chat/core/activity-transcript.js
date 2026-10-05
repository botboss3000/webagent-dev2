'use strict';

// Presentation only: journal IDs are never treated as legacy interaction IDs.
import {copyText} from '../../shared/models/clipboard.js';
import {_refreshLucideIcons} from './presentation.js';
import {openChatElementFullView, closeChatElementFullView} from './full-view.js';

function el(tag, text='', cls='') {
  const node=document.createElement(tag);node.className=cls;node.textContent=text;return node;
}
function button(title, icon, action) {
  const node=el('button','','turn-gutter-btn');node.type='button';
  node.title=title;node.setAttribute('aria-label',title);
  node.innerHTML='<i data-lucide="'+icon+'" style="width:14px;height:14px"></i>';
  node.addEventListener('click',event=>{event.stopPropagation();return action(node);});return node;
}
function copy(title, value) {
  return button(title,'copy',async node=>{
    try {await copyText(value());node.title='Copied!';node.setAttribute('aria-label','Copied!');}
    catch (_) {node.title='Copy failed';node.setAttribute('aria-label','Copy failed');}
  });
}
function payloadValue(payload,keys) {
  for(const key of keys) if(payload && Object.hasOwn(payload,key))return payload[key];
}
const printable=value=>typeof value==='string'?value:JSON.stringify(value,null,2)??'';
export function normalizeRow(row) {
  const call=row.events.find(e=>e.kind==='tool_call');
  const result=[...row.events].reverse().find(e=>e.kind==='tool_result');
  return {text:typeof row.text==='string'?row.text:'',
    time:row.events.at(-1)?.created_at,
    args:payloadValue(call?.payload,['arguments','args']),
    result:payloadValue(result?.payload,['result','output',...(result?.payload?.error?['error']:[])]),
    status:result?(result.payload?.error?'error':'done'):'running'};
}
function markdown(body,text) {
  body.replaceChildren();
  if(window.marked?.parse && window.DOMPurify?.sanitize) {
    try {
      body.innerHTML=window.DOMPurify.sanitize(window.marked.parse(text,{gfm:true,breaks:true}),{FORBID_ATTR:['style']});
      body.querySelectorAll('a[href]').forEach(a=>{a.target='_blank';a.rel='noopener noreferrer';});return;
    } catch (_) { /* Plain text is the safe fallback. */ }
  }
  body.textContent=text;
}
function diagnostic() {
  const node=el('details','','v2-mini-diagnostics');node.append(el('summary','Raw diagnostics'),el('pre'));return node;
}
export function createTranscriptRow(row) {
  const tool=row.category==='tools';
  const node=el('div','','v2-mini-row '+(tool?'v2-mini-tool':'v2-mini-message'));
  const content=el('div','','v2-mini-content');
  const footer=el('div','','turn-gutter ca-activity-entry-footer notice-gutter');
  const raw=diagnostic();
  node._parts={content,footer,raw,tool};
  if(tool) {
    const disclosure=el('details','','v2-mini-tool-disclosure');
    const head=el('summary');disclosure.append(head,content);node.append(disclosure,footer,raw);
    node._parts.head=head;
    footer.append(copy('Copy arguments and result',()=>content.textContent));
    for(const [key,title] of [['args','Arguments'],['result','Result']]) {
      const section=el('section');const label=el('div',title,'v2-mini-section-head');
      const pre=el('pre');label.append(copy('Copy '+title.toLowerCase(),()=>pre.textContent));
      section.append(label,pre);content.append(section);node._parts[key]={section,pre};
    }
  } else {
    const time=el('time','','v2-mini-time');footer.append(time);node._parts.time=time;
    footer.append(button('Collapse message','chevron-down',b=>{
      content.hidden=!content.hidden;b.title=content.hidden?'Expand message':'Collapse message';
      b.setAttribute('aria-label',b.title);b.setAttribute('aria-expanded',String(!content.hidden));
    }),button('Open full view','maximize-2',b=>{
      const source=content.cloneNode(true);source.hidden=false;
      openChatElementFullView(source,b,'Full message');node._viewer=true;
    }),copy('Copy text',()=>node._text));
    if(window.speechSynthesis && window.SpeechSynthesisUtterance) footer.append(button('Read aloud','volume-2',()=>{
      window.speechSynthesis.cancel();window.speechSynthesis.speak(new window.SpeechSynthesisUtterance(node._text));
    }));
    node.append(content,footer,raw);
  }
  patchTranscriptRow(node,row);_refreshLucideIcons(node);return node;
}
export function patchTranscriptRow(node,row) {
  const signature=JSON.stringify(row.events);if(node.dataset.signature===signature)return;
  node.dataset.signature=signature;node.dataset.category=row.category;
  const value=normalizeRow(row),p=node._parts;node._text=value.text;
  if(p.tool) {
    p.head.textContent=(value.status==='done'?'✓ ':value.status==='error'?'! ':'◷ ')+row.label;
    node.dataset.status=value.status;
    for(const key of ['args','result']) {
      p[key].section.hidden=value[key]===undefined;
      p[key].pre.textContent=printable(value[key]);
    }
  } else {
    markdown(p.content,value.text || row.label);
    const date=value.time?new Date(value.time):null;
    p.time.textContent=date && Number.isFinite(date.getTime())?date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'';
    if(date && Number.isFinite(date.getTime()))p.time.dateTime=date.toISOString();
  }
  p.raw.lastElementChild.textContent=JSON.stringify(row.events,null,2);
}
export function mountTranscript(body) {
  const nav=el('div','','v2-mini-nav');const rows=el('div','','v2-mini-rows');
  let following=true,disposed=false;
  const latest=button('Follow latest','arrow-down',()=>{following=!following;if(following)body.scrollTop=body.scrollHeight;refresh();});
  nav.append(button('Jump to oldest','arrow-up',()=>{following=false;body.scrollTop=0;refresh();}),latest);
  body.append(nav,rows);_refreshLucideIcons(nav);
  function refresh() {
    if(disposed)return;nav.hidden=body.scrollHeight<=body.clientHeight+1;
    latest.classList.toggle('is-following',following);latest.setAttribute('aria-pressed',String(following));
  }
  const scroll=()=>{if(body.scrollTop+body.clientHeight<body.scrollHeight-12)following=false;refresh();};
  body.addEventListener('scroll',scroll,{passive:true});
  const observer=typeof ResizeObserver==='function'?new ResizeObserver(()=>{if(following)body.scrollTop=body.scrollHeight;refresh();}):null;
  observer?.observe(rows);observer?.observe(body);
  return {rows,update(){if(following)body.scrollTop=body.scrollHeight;refresh();},dispose(){
    disposed=true;observer?.disconnect();body.removeEventListener('scroll',scroll);
    if([...rows.children].some(n=>n._viewer))closeChatElementFullView();
  }};
}
