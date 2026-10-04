// Canonical v2 gutter order and More menu, with explicit v3 backend placeholders.
import {currentChatProfile} from '../controls/chat-config.js';
import {copyText} from '../../shared/models/clipboard.js';
import {openChatElementFullView} from './full-view.js';
let overlay=null, playback=null;
export function formatRelativeTime(createdAtMs,now=Date.now()) {
  if(!Number.isFinite(createdAtMs))return 'time unavailable';
  const seconds=Math.floor(Math.max(0,now-createdAtMs)/1000);
  if(seconds<60)return seconds+'s';
  const minutes=Math.floor(seconds/60);
  return minutes<60?minutes+'m':Math.floor(minutes/60)+'h '+minutes%60+'m';
}
export function updateGutterTimes(root) {
  for(const node of root.querySelectorAll('[data-gutter-created-at]')) {
    const value=formatRelativeTime(Number(node.dataset.gutterCreatedAt));
    if(node.textContent!==value)node.textContent=value;
  }
}
function iconButton(label,icon,action) {
  const node=document.createElement('button');node.type='button';node.className='turn-gutter-btn';
  node.title=label;node.setAttribute('aria-label',label);
  node.innerHTML='<i data-lucide="'+icon+'" style="width:14px;height:14px"></i>';
  node.addEventListener('click',event=>{event.stopPropagation();action?.(node);});return node;
}
function unavailable(node,label) {
  node.disabled=true;node.classList.add('v3-backend-pending');node.title=label+' — backend port pending';
  node.setAttribute('aria-label',node.title);return node;
}
export function closeGutterOverlays() {
  overlay?.close();overlay=null;
  playback?.stop();playback=null;
}
function speak(body,trigger,gutter) {
  if(!window.speechSynthesis||!window.SpeechSynthesisUtterance)return;
  if(playback?.trigger===trigger){playback.stop();return;}
  playback?.stop();
  const text=body.innerText||body.textContent||'';
  const utterance=new SpeechSynthesisUtterance(text);
  const pause=document.createElement('button');pause.type='button';pause.className='tts-pause-btn';pause.textContent='Pause';
  let finished=false,highlighted=null;
  const blocks=[...body.querySelectorAll('p,li,h1,h2,h3,h4,blockquote')];
  const positions=[];let cursor=0;
  for(const block of blocks){const part=block.textContent;const offset=text.indexOf(part,cursor);if(offset>=0){positions.push({block,start:offset,end:offset+part.length});cursor=offset+part.length;}}
  const stop=()=>{if(finished)return;finished=true;window.speechSynthesis.cancel();highlighted?.classList.remove('tts-speaking');pause.remove();trigger.title='Read aloud';trigger.setAttribute('aria-label','Read aloud');if(playback?.trigger===trigger)playback=null;};
  utterance.onboundary=event=>{const found=positions.find(p=>event.charIndex>=p.start&&event.charIndex<p.end);if(found?.block!==highlighted){highlighted?.classList.remove('tts-speaking');highlighted=found?.block;highlighted?.classList.add('tts-speaking');}};
  utterance.onend=stop;utterance.onerror=stop;
  pause.onclick=event=>{event.stopPropagation();if(window.speechSynthesis.paused){window.speechSynthesis.resume();pause.textContent='Pause';}else{window.speechSynthesis.pause();pause.textContent='Resume';}};
  trigger.title='Stop reading';trigger.setAttribute('aria-label','Stop reading');trigger.after(pause);
  playback={trigger,stop};window.speechSynthesis.speak(utterance);
}
export function createBubbleGutter({item,body,bubble,getText,getRun=()=>null,showContext=()=>{},refreshMessage=async()=>{}}) {
  const role=item.role==='user'?'user':'agent',profile=currentChatProfile()||{};
  const config=profile.message_gutters||{},spec=config[role]||{};
  const defaults=role==='user'?['time','full_view','copy','undo','delete','more']:['time','full_view','collapse','read_aloud','copy','fork','delete','more'];
  const gutter=document.createElement('div');gutter.className='turn-gutter';gutter.dataset.sectionIdx='0';
  function copy(node,text) {void copyText(text).then(()=>{node.title='Copied';}).catch(()=>{node.title='Copy failed';});}
  function more(trigger) {
    if(overlay?.trigger===trigger){overlay.close();return;}
    overlay?.close();
    const menu=document.createElement('div');menu.className='bubble-more-menu';menu.setAttribute('role','menu');
    const abort=new AbortController(),title=document.createElement('div');title.className='bubble-more-title';title.textContent='More';menu.append(title);
    const settings=config.more_menu||{},run=getRun(),ctx=run?.context_summary,usage=run?.usage;
    const names={context:['Context','gauge'],cost:['Cost','coins'],model:['Model','bot'],message_id:['Message','hash'],row_details:['Row details','database'],schema:['View schema sent to LLM','menu'],refresh:['Refresh message','refresh-cw']};
    const controls=settings.order||['context','cost','model','message_id','row_details','schema','refresh'];
    for(const name of settings.enabled===false?[]:controls) {
      if(role!=='agent'&&['row_details','schema','refresh'].includes(name))continue;
      const entry=settings.controls?.[name];if(entry===false||entry?.enabled===false||!names[name])continue;
      const [label,icon]=names[name],row=document.createElement('button');row.type='button';row.className='bubble-more-row';row.dataset.moreControl=name;row.setAttribute('role','menuitem');
      const left=document.createElement('span');left.className='bubble-more-label';left.innerHTML='<i data-lucide="'+icon+'" style="width:13px;height:13px"></i>';
      const text=document.createElement('span');text.textContent=label;left.append(text);
      const value=document.createElement('span');value.className='bubble-more-value';
      row.append(left,value);menu.append(row);
      if(name==='context'){value.textContent=ctx?ctx.envelope_bytes.toLocaleString()+' / '+ctx.max_bytes.toLocaleString()+' bytes':'Not measured';row.onclick=event=>{event.stopPropagation();close();showContext(run);};}
      if(name==='cost'){value.textContent=usage&&!usage.unknown_cost_calls?'$'+Number(usage.known_cost_usd||0).toFixed(4):'Not reported';row.disabled=true;if(!usage||usage.unknown_cost_calls)row.classList.add('v3-backend-pending');}
      if(name==='model'){value.textContent=run?.model?.model||'Not recorded';row.disabled=true;}
      if(name==='message_id'){value.textContent=item.id.slice(0,12)+'…';row.title='Copy message ID';row.onclick=()=>copy(row,item.id);}
      if(name==='row_details'||name==='schema')unavailable(row,label);
      if(name==='refresh'){row.onclick=async()=>{row.disabled=true;text.textContent='Refreshing…';try{await refreshMessage();close();}catch(error){if(menu.isConnected){text.textContent='Refresh failed — retry';row.title=error.message;}}finally{row.disabled=false;}};}
      if(entry?.locked){row.disabled=true;row.title=entry.locked_message||'Unavailable';}
    }
    function close(){abort.abort();menu.remove();trigger.classList.remove('open');trigger.setAttribute('aria-expanded','false');if(overlay?.trigger===trigger)overlay=null;}
    function position(){if(!trigger.isConnected){close();return;}const rect=trigger.getBoundingClientRect();menu.style.left=Math.max(6,Math.min(rect.right-menu.offsetWidth,innerWidth-menu.offsetWidth-6))+'px';menu.style.top=(rect.bottom+menu.offsetHeight+6>innerHeight?Math.max(6,rect.top-menu.offsetHeight-6):rect.bottom+6)+'px';}
    document.body.append(menu);trigger.classList.add('open');trigger.setAttribute('aria-expanded','true');window.lucide?.createIcons();position();
    document.addEventListener('click',event=>{if(!menu.contains(event.target)&&!trigger.contains(event.target))close();},{signal:abort.signal});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'){close();trigger.focus();}else if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();const choices=[...menu.querySelectorAll('button:not(:disabled)')];const index=choices.indexOf(document.activeElement);choices[(index+(event.key==='ArrowDown'?1:-1)+choices.length)%choices.length]?.focus();}},{signal:abort.signal});
    window.addEventListener('resize',position,{signal:abort.signal});document.addEventListener('scroll',position,{capture:true,signal:abort.signal});
    overlay={trigger,close};menu.querySelector('button:not(:disabled)')?.focus();
  }
  for(const name of spec.enabled===false?[]:spec.order||defaults) {
    const entry=spec.controls?.[name];if(entry===false||entry?.enabled===false)continue;
    let control;
    if(name==='time'){control=document.createElement('time');control.className='turn-gutter-time bubble-time';const date=new Date(item.created_at*1000);if(Number.isFinite(date.getTime())){control.dateTime=date.toISOString();control.dataset.gutterCreatedAt=String(date.getTime());control.textContent=formatRelativeTime(date.getTime());control.title=date.toLocaleString();}}
    if(name==='full_view')control=iconButton('Open full view','maximize-2',node=>{const clone=body.cloneNode(true);clone.hidden=false;openChatElementFullView(clone,node,'Full message');});
    if(name==='copy')control=iconButton('Copy text','copy',node=>copy(node,getText()));
    if(name==='collapse')control=iconButton('Collapse message','chevron-down',node=>{body.hidden=!body.hidden;node.title=body.hidden?'Expand message':'Collapse message';node.setAttribute('aria-label',node.title);node.setAttribute('aria-expanded',String(!body.hidden));});
    if(name==='read_aloud'){control=iconButton('Read aloud','volume-2',node=>speak(body,node,gutter));if(!window.speechSynthesis)unavailable(control,'Read aloud');}
    if(['undo','delete','fork','schema'].includes(name))control=unavailable(iconButton({undo:'Undo message',delete:'Delete message',fork:'Fork conversation',schema:'View schema sent to LLM'}[name],{undo:'undo-2',delete:'trash-2',fork:'git-fork',schema:'menu'}[name]),name);
    if(name==='more'){control=iconButton('More','ellipsis',more);control.setAttribute('aria-haspopup','menu');control.setAttribute('aria-expanded','false');}
    if(!control)continue;
    if(entry?.locked){control.disabled=true;control.title=entry.locked_message||'Unavailable';}
    control.dataset.gutterControl=name;gutter.append(control);
  }
  return gutter;
}
