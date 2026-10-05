'use strict';
// Reference chat-bubble DOM and configured gutters, with V3 committed-row transport.
import {currentChatProfile} from '../controls/chat-config.js';
import {_refreshLucideIcons} from './presentation.js';
import {copyText} from '../../shared/models/clipboard.js';
import {openChatElementFullView} from './full-view.js';
import {createBubbleGutter} from './bubble-gutters.js';
function button(title,icon,action){const node=document.createElement('button');node.type='button';node.className='turn-gutter-btn';node.title=title;node.setAttribute('aria-label',title);node.innerHTML='<i data-lucide="'+icon+'" style="width:14px;height:14px"></i>';node.addEventListener('click',action);return node;}
function linkify(body,text){let last=0;for(const match of text.matchAll(/https?:\/\/[^\s<>"]+/g)){body.append(document.createTextNode(text.slice(last,match.index)));const a=document.createElement('a');a.textContent=match[0];a.href=match[0];a.target='_blank';a.rel='noopener noreferrer';body.append(a);last=match.index+match[0].length;}body.append(document.createTextNode(text.slice(last)));}
function markdown(body,text){if(window.marked?.parse&&window.DOMPurify?.sanitize){body.className='md-body';body.innerHTML=window.DOMPurify.sanitize(window.marked.parse(text,{gfm:true,breaks:true}),{FORBID_ATTR:['style']});body.querySelectorAll('a[href]').forEach(a=>{a.target='_blank';a.rel='noopener noreferrer';});body.querySelectorAll('pre').forEach(pre=>{const wrap=document.createElement('div');wrap.className='md-code-block';pre.replaceWith(wrap);wrap.append(pre);const copy=button('Copy code','copy',async()=>{try{await copyText(pre.textContent);copy.title='Copied';}catch{copy.title='Copy failed';}});copy.classList.add('md-code-copy');wrap.append(copy);});}else{body.className='bubble-body';linkify(body,text);}}
export function createMessage(item,{getRun=()=>null,client,getSession=()=>null,note=()=>{},showContext=()=>{}}={}){
  if(!['user','assistant'].includes(item.role)||!Number.isSafeInteger(item.seq)||item.seq<=0||typeof item.id!=='string')return null;
  const profile=currentChatProfile()||{},role=item.role==='user'?'user':'agent';
  let text=typeof item.content==='string'?item.content:JSON.stringify(item.content);
  const bubble=document.createElement('article');bubble.className='chat-bubble '+role+' published';bubble.dataset.publicId=item.id;bubble.dataset.msgId=item.id;bubble.dataset.seq=String(item.seq);bubble.dataset.turnId=item.run_id;
  const label=document.createElement('span');label.className='label';label.textContent=role==='user'?'You':'Agent';bubble.append(label);
  const body=document.createElement('div');
  if(role==='agent'){const section=document.createElement('div');section.className='turn-section llm-section md';section.dataset.sectionIdx='0';section.dataset.msgId=item.id;markdown(body,text);section.append(body);bubble.append(section);}else{body.className='bubble-body';linkify(body,text);bubble.append(body);}
  if(role==='user'&&Array.isArray(item.attachments)){
    const files=document.createElement('div');files.className='chat-attachments';
    const sid=new URL(location.href).searchParams.get('session_id');
    for(const file of item.attachments){
      if(typeof file.id!=='string'||!/^[0-9a-f-]{36}$/i.test(file.id)||!sid)continue;
      const link=document.createElement('a');link.href='/api/v3/sessions/'+encodeURIComponent(sid)+'/attachments/'+encodeURIComponent(file.id)+'/content';link.textContent=file.name||'Attachment';link.download=file.name||'attachment';link.target='_blank';link.rel='noopener';
      if(file.preview===true){const image=document.createElement('img');image.src=link.href;image.alt=file.name||'Image attachment';image.loading='lazy';link.prepend(image);}
      files.append(link);
    }
    bubble.append(files);
  }
  const cfg=profile.message_bubbles||{},lane=cfg[role]||{};bubble.style.fontSize=(cfg.font_size_px||17)+'px';bubble.style.padding=(cfg.padding_y_px??10)+'px '+(cfg.padding_x_px??14)+'px';if(role==='agent')bubble.style.paddingLeft=(cfg.agent_padding_left_px??4)+'px';bubble.style.border=lane.border||'none';bubble.style.borderRadius=(lane.radius_px??14)+'px';bubble.style.lineHeight=String(lane.line_height||1.5);bubble.style.setProperty('--message-bg-dark',lane.bg_dark||'transparent');bubble.style.setProperty('--message-bg-light',lane.bg_light||'transparent');bubble.classList.add('v3-config-bubble');
  const gutter=createBubbleGutter({item,body,bubble,getText:()=>text,getRun,showContext,refreshMessage:async()=>{
    const sid=getSession();
    if(!sid||!client)throw Error('Conversation unavailable');
    const snapshot=await client.snapshot(sid);
    if(sid!==getSession()||!bubble.isConnected)return;
    const fresh=snapshot.messages.find(row=>row.id===item.id);
    if(!fresh)throw Error('Message unavailable');
    Object.assign(item,fresh);text=typeof fresh.content==='string'?fresh.content:JSON.stringify(fresh.content);
    body.replaceChildren();if(role==='agent')markdown(body,text);else linkify(body,text);
    note('Message refreshed');
  }});
  bubble.append(gutter);_refreshLucideIcons(bubble);
  if(role==='user')requestAnimationFrame(()=>{
    const cfg=profile.user_message_truncation||{},maxLines=Math.max(1,Math.min(100,Number(cfg.max_lines)||3));if(cfg.enabled===false||!bubble.isConnected)return;const lineHeight=parseFloat(getComputedStyle(body).lineHeight)||20;if(body.scrollHeight<=lineHeight*maxLines+1)return;bubble.style.setProperty('--user-message-max-lines',String(maxLines));bubble.classList.add('user-message-clamped');const toggle=document.createElement('button');toggle.type='button';toggle.className='user-message-expand';toggle.textContent=cfg.expand_label||'See more';toggle.setAttribute('aria-expanded','false');toggle.onclick=()=>{const expanded=bubble.classList.toggle('user-message-expanded');toggle.textContent=expanded?(cfg.collapse_label||'Show less'):(cfg.expand_label||'See more');toggle.setAttribute('aria-expanded',String(expanded));};body.after(toggle);
  });
  return bubble;
}
