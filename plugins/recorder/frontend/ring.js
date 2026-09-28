// RECORDER-PREBUFFER: bounded, memory-only rolling evidence. Eviction applies DOM
// patches before dropping rows so the baseline describes the retained boundary.
export class Prebuffer {
  constructor({now=()=>performance.now(),limit=32*1024*1024,windowMs=5000,advance=()=>0,baselineBytes=0}={}) {
    Object.assign(this,{now,limit,windowMs,advance,baselineBytes});
    this.groups=[];this.used=baselineBytes;this.started=now();this.boundary=this.started;
    this.limitations=new Set();this.pinned=false;
  }
  push(rows,patches=[],size=0) {
    const at=this.now();this.groups.push({at,rows,patches,size});this.used+=size;this.trim(at);
  }
  trim(at=this.now()) {
    if(!this.pinned) {
      const cutoff=at-this.windowMs;
      while(this.groups.length && this.groups[0].at<cutoff) this.shift();
      this.boundary=Math.max(this.boundary,cutoff);
    }
    while(this.used>this.limit && this.groups.length) {
      const item=this.groups[0];this.shift();this.boundary=Math.max(this.boundary,item.at);
      this.limitations.add('prebuffer-memory-limit');
    }
    if(this.used>this.limit) this.limitations.add('prebuffer-baseline-memory-limit');
  }
  shift() {
    const item=this.groups.shift();this.used-=item.size;
    const next=this.advance(item.patches);
    if(Number.isFinite(next)){this.used+=next-this.baselineBytes;this.baselineBytes=next;}
    return item;
  }
  freeze() {
    this.trim();this.pinned=true;this.trigger=this.now();
    return this.coverage();
  }
  coverage() {
    const at=this.trigger??this.now(),available=Math.max(0,Math.min(this.windowMs,at-this.boundary));
    return {requested_ms:this.windowMs,available_ms:Math.round(available),
      trigger_elapsed_ms:Math.round(available),limitations:[...this.limitations,
        ...(available<this.windowMs && !this.limitations.size?['prebuffer-warming-up']:[])]};
  }
}
const escapeText=value=>String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const escapeAttr=value=>escapeText(value).replace(/"/g,'&quot;');
const voidTags=new Set('area base br col embed hr img input link meta param source track wbr'.split(' '));
export function mirrorHtml(node) {
  if(!node)return '';
  if(node.type===3)return escapeText(node.text);
  if(node.type!==1)return '';
  const start='<'+node.tag+Object.entries(node.attrs).map(([k,v])=>' '+k+'="'+escapeAttr(v)+'"').join('')+'>';
  return start+(voidTags.has(node.tag)?'':node.children.map(mirrorHtml).join('')+'</'+node.tag+'>');
}
export function createMirror(root,{id,excluded,sensitive}) {
  const index=new Map();let used=0,readBudget=0,invalid=false;
  // Account each node once; eviction must not serialize the whole transcript.
  const ownBytes=item=>{const {children,...own}=item;return new Blob([JSON.stringify(own)]).size*2+128;};
  function read(node) {
    if(excluded(node))return null;
    const item={id:id(node),type:node.nodeType};
    if(node.nodeType===3)item.text=sensitive(node)?'[masked]':node.textContent||'';
    else if(node.nodeType===1) {
      item.tag=node.tagName.toLowerCase();item.attrs={};
      for(const attr of node.attributes||[])item.attrs[attr.name]=sensitive(node)&&attr.name==='value'?'[masked]':attr.value;
      item.children=[];
      readBudget+=ownBytes(item);
      if(readBudget>32*1024*1024)throw new Error('prebuffer-baseline-memory-limit');
      for(const child of node.childNodes){const value=read(child);if(value)item.children.push(value);}
    } else return null;
    if(node.nodeType===3){readBudget+=ownBytes(item);if(readBudget>32*1024*1024)throw new Error('prebuffer-baseline-memory-limit');}
    return item;
  }
  function register(item){index.set(item.id,item);used+=ownBytes(item);for(const child of item.children||[])register(child);}
  function remove(item){index.delete(item.id);used-=ownBytes(item);for(const child of item.children||[])remove(child);}
  let tree=read(root);if(tree)register(tree);
  function patches(records) {
    readBudget=0;
    return records.filter(m=>!excluded(m.target)).map(m=>{
      const target=id(m.target);
      if(m.type==='attributes')return {type:'attr',target,name:m.attributeName,value:sensitive(m.target)&&m.attributeName==='value'?'[masked]':m.target.getAttribute(m.attributeName)};
      if(m.type==='characterData')return {type:'text',target,value:sensitive(m.target)?'[masked]':m.target.textContent};
      return {type:'children',target,removed:[...m.removedNodes].map(id),next:m.nextSibling?id(m.nextSibling):null,added:[...m.addedNodes].map(read).filter(Boolean)};
    });
  }
  function advance(items) {
    for(const p of items) {
      const target=index.get(p.target);if(!target)continue;
      // A stale patch must not corrupt the new baseline or throw from a timer.
      // Collector lifecycle normally prevents this; fail closed if evidence
      // nevertheless refers to a node of the wrong DOM type.
      if((p.type==='attr' && !target.attrs) ||
         (p.type==='text' && target.type!==3) ||
         (p.type==='children' && !Array.isArray(target.children))){
        invalid=true;continue;
      }
      const before=ownBytes(target);
      if(p.type==='attr'){if(p.value===null)delete target.attrs[p.name];else target.attrs[p.name]=p.value;}
      else if(p.type==='text')target.text=p.value;
      else if(p.type==='children') {
        for(const rid of p.removed){const i=target.children.findIndex(x=>x.id===rid);if(i>=0){remove(target.children[i]);target.children.splice(i,1);}}
        let at=target.children.findIndex(x=>x.id===p.next);if(at<0)at=target.children.length;
        target.children.splice(at,0,...p.added);p.added.forEach(register);
      }
      used+=ownBytes(target)-before;
    }
    return used;
  }
  return {patches,advance,get invalid(){return invalid;},html:()=>mirrorHtml(tree),size:()=>used};
}
