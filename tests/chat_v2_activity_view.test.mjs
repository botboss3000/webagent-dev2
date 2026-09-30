import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.events={};this.attrs={};this.hidden=false;this.scrollTop=0;this.scrollHeight=500;this.clientHeight=250;this._text='';this.classList={toggle:(name,value)=>{this[name]=value;}};}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
 set innerHTML(value){this.html=value;this.children=[];}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this._text='';this.children=nodes;}
 get lastElementChild(){return this.children.at(-1);}
 setAttribute(key,value){this.attrs[key]=value;}
 addEventListener(key,fn){this.events[key]=fn;}
 removeEventListener(key){delete this.events[key];}
 querySelectorAll(){return [];}
 cloneNode(){const n=new Element(this.tagName);n.textContent=this.textContent;n.hidden=this.hidden;return n;}
 async click(){await this.events.click({stopPropagation(){}});}
}
async function harness() {
 let text='',viewer=null,closed=0,disconnected=0;
 const source=(await fs.readFile('ui/chat/core/activity-transcript.js','utf8'))
  .replace(/^import .*;$/gm,'').replace(/export /g,'');
 const context=vm.createContext({document:{createElement:tag=>new Element(tag)},window:{},
  copyText:async value=>{text=value;},_refreshLucideIcons(){},
  openChatElementFullView:(value,button)=>{viewer={value,button};},
  closeChatElementFullView:()=>{closed++;},
  ResizeObserver:class {observe(){}disconnect(){disconnected++;}},
 });
 vm.runInContext(source+'\nglobalThis.api={normalizeRow,createTranscriptRow,patchTranscriptRow,mountTranscript};',context);
 return {api:context.api,text:()=>text,viewer:()=>viewer,closed:()=>closed,disconnected:()=>disconnected};
}
const tool=(events)=>({id:'tool:x',category:'tools',label:'lookup · done',events});
const call={kind:'tool_call',payload:{args:{query:'example'}}};
const message={id:'message:x',category:'updates',label:'Update',text:'Readable update',events:[{kind:'agent_step_end',created_at:'2026-01-01T12:00:00Z',payload:{content:'Readable update'}}]};
test('normalization preserves empty, false, null and absent tool result metadata',async()=>{
 const {api}=await harness();
 for(const result of ['',false,null,0])assert.equal(api.normalizeRow(tool([call,{kind:'tool_result',payload:{result}}])).result,result);
 assert.equal(api.normalizeRow(tool([call,{kind:'tool_result',payload:{error:false}}])).result,undefined);
 assert.equal(api.normalizeRow(tool([call])).status,'running');
 assert.equal(api.normalizeRow(tool([call,{kind:'tool_result',payload:{error:true,result:'failed'}}])).status,'error');
});
test('tool patch retains disclosure and control identity while result arrives',async()=>{
 const {api}=await harness(),node=api.createTranscriptRow(tool([call]));
 const disclosure=node.children[0],copy=node._parts.footer.children[0];disclosure.open=true;
 api.patchTranscriptRow(node,tool([call,{kind:'tool_result',payload:{result:'<img onerror=evil()>',error:false}}]));
 assert.equal(node.children[0],disclosure);assert.equal(disclosure.open,true);
 assert.equal(node._parts.footer.children[0],copy);
 assert.equal(node._parts.result.pre.textContent,'<img onerror=evil()>');
 assert.equal(node._parts.result.pre.html,undefined);
 assert.ok(node._parts.raw.lastElementChild.textContent.includes('tool_result'));
});
test('message collapse, copy, full view and safe fallback are independent controls',async()=>{
 const h=await harness(),node=h.api.createTranscriptRow(message),p=node._parts;
 const [,collapse,full,copy]=p.footer.children;
 await collapse.click();assert.equal(p.content.hidden,true);
 await full.click();assert.equal(h.viewer().value.hidden,false);
 assert.equal(p.content.hidden,true);await copy.click();assert.equal(h.text(),'Readable update');
 assert.equal(copy.attrs['aria-label'],'Copied!');
 h.api.patchTranscriptRow(node,{...message,text:'<script>evil()</script>',events:[...message.events,{kind:'response'}]});
 assert.equal(p.content.hidden,true);assert.equal(p.content.textContent,'<script>evil()</script>');assert.equal(p.content.html,undefined);
 await collapse.click();assert.equal(p.content.hidden,false);
});
test('overflow nav follows latest, respects manual scroll and cleans observers',async()=>{
 const h=await harness(),body=new Element('div'),view=h.api.mountTranscript(body);
 view.update();assert.equal(body.scrollTop,500);assert.equal(body.children[0].hidden,false);
 await body.children[0].children[0].click();assert.equal(body.scrollTop,0);
 body.scrollHeight=700;view.update();assert.equal(body.scrollTop,0);
 await body.children[0].children[1].click();assert.equal(body.scrollTop,700);
 body.scrollTop=20;body.events.scroll();view.update();assert.equal(body.scrollTop,20);
 body.scrollHeight=100;view.update();assert.equal(body.children[0].hidden,true);
 view.dispose();assert.equal(h.disconnected(),1);assert.equal(body.events.scroll,undefined);
});
test('v2 headline, journal transport and authorization remain in owning renderer',async()=>{
 const source=await fs.readFile('ui/chat/core/activity-view.js','utf8');
 assert.ok(source.includes("node.firstElementChild.textContent='Activity'"));
 assert.ok(source.includes("'/api/v2/chat/sessions/'"));
 assert.ok(source.includes('!isAdmin()'));assert.ok(source.includes('state.revoked'));
 assert.ok(source.includes('node._transcript?.dispose()'));
 assert.ok(!source.includes('detailsFor(row)'));
 const css=await fs.readFile('ui/chat/core/activity.css','utf8');
 assert.ok(css.includes('max-height: 250px'));assert.ok(css.includes('background: transparent'));
});
