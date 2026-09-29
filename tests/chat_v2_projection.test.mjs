import {test} from 'node:test';
import assert from 'node:assert/strict';
import {reconcilePublicNodes} from '../ui/chat/core/projection.js';
class Node {
  constructor(text=''){this.text=text;this.dataset={};this.parent=null;
    this.classList={add(){},remove(){}};}
  querySelector(){return null;}
  remove(){if(this.parent){const p=this.parent;p.children.splice(p.children.indexOf(this),1);this.parent=null;}}
  replaceWith(other){const p=this.parent;p.insertBefore(other,this);this.remove();}
  get nextSibling(){return this.parent?.children[this.parent.children.indexOf(this)+1] || null;}
}
class Container {
  constructor(){this.children=[];this.moves=0;}
  get firstChild(){return this.children[0] || null;}
  insertBefore(node,before){
    if(node===before)return;
    node.remove();
    const i=before?this.children.indexOf(before):this.children.length;
    assert.ok(i>=0);this.children.splice(i,0,node);node.parent=this;this.moves++;
  }
}
const item=(id,position,kind='answer',extra={})=>({id,position,kind,revision:1,payload:{text:id},...extra});
const pending=id=>({createdAt:0,message:{client_message_id:id,text:'hello'},status:'pending'});
const create=row=>['user','answer','notice','failure','component'].includes(row.kind)?new Node(row.payload.text):null;

test('live, refresh and restored snapshots preserve key order and reuse DOM nodes',()=>{
 const container=new Container(),items=[item('u',1,'user'),item('a',2),item('b',3)];
 reconcilePublicNodes(container,{items:items.slice(0,1)},[],create);
 reconcilePublicNodes(container,{items},[],create);
 const original=[...container.children],moves=container.moves;
 reconcilePublicNodes(container,JSON.parse(JSON.stringify({items})),[],create);
 assert.deepEqual(container.children,original);
 assert.equal(container.moves,moves);
 const restored=new Container();
 reconcilePublicNodes(restored,{items},[],create);
 assert.deepEqual(restored.children.map(n=>n.dataset.publicId),['u','a','b']);
});
test('interleaved activity groups do not move public messages on refresh',()=>{
 const container=new Container();
 const items=[item('u',1,'user'),item('a',2)];
 reconcilePublicNodes(container,{items},[],create);
 const user=container.children[0],answer=container.children[1];
 const activity=new Node('activity');
 container.insertBefore(activity,answer);
 const moves=container.moves;
 reconcilePublicNodes(container,{items},[],create);
 assert.deepEqual(container.children,[user,activity,answer]);
 assert.equal(container.moves,moves);
 reconcilePublicNodes(container,{items:[...items,item('b',3)]},[],create);
 assert.deepEqual(container.children.slice(0,3),[user,activity,answer]);
 reconcilePublicNodes(container,{items:[...items,item('b',3)]},[],create);
 assert.equal(container.children.filter(node=>node===activity).length,1);
 assert.deepEqual(container.children.filter(node=>node.dataset.publicId)
   .map(node=>node.dataset.publicId),['u','a','b']);
});
test('pending messages keep their submission boundary as later publications arrive',()=>{
 const container=new Container();
 const initial=[item('u1',1,'user'),item('a1',2)];
 const queued={...pending('next-user'),afterPosition:2};
 reconcilePublicNodes(container,{items:initial},[queued],create);
 const optimistic=container.children[2];
 reconcilePublicNodes(container,{items:[...initial,item('update',3)]},[queued],create);
 assert.deepEqual(container.children.map(node=>node.dataset.publicId || node.dataset.clientMessageId),
   ['u1','a1','next-user','update']);
 assert.equal(container.children[2],optimistic);
 reconcilePublicNodes(container,{items:[...initial,item('update',3),item('accepted',4,'user',{
   payload:{text:'hello',client_message_id:'next-user'}})]},[queued],create);
 assert.deepEqual(container.children.map(node=>node.dataset.publicId),
   ['u1','a1','update','accepted']);
 assert.equal(container.children[3],optimistic);
});
test('server acknowledgement adopts the optimistic node by client identity',()=>{
 const container=new Container();
 reconcilePublicNodes(container,{items:[]},[pending('client-one')],create);
 const optimistic=container.firstChild;
 reconcilePublicNodes(container,{items:[item('server-one',1,'user',{
  payload:{text:'hello',client_message_id:'client-one'}})]},[pending('client-one')],create);
 assert.equal(container.children.length,1);
 assert.equal(container.firstChild,optimistic);
 assert.equal(optimistic.dataset.publicId,'server-one');
});
test('identical text with different identities stays separate',()=>{
 const container=new Container();
 reconcilePublicNodes(container,{items:[item('one',1,'answer',{payload:{text:'same'}}),
   item('two',2,'answer',{payload:{text:'same'}})]},[],create);
 assert.equal(container.children.length,2);
 assert.notEqual(container.children[0],container.children[1]);
});
test('component revisions replace only that component and tombstones remove it',()=>{
 const container=new Container(),answer=item('answer',1),component=item('component',2,'component');
 reconcilePublicNodes(container,{items:[answer,component]},[],create);
 const original=[...container.children];
 reconcilePublicNodes(container,{items:[answer,{...component,revision:2,payload:{text:'updated'}}]},[],create);
 assert.equal(container.children[0],original[0]);
 assert.notEqual(container.children[1],original[1]);
 assert.equal(container.children[1].text,'updated');
 reconcilePublicNodes(container,{items:[answer,{...component,revision:3,deleted:true}]},[],create);
 assert.deepEqual(container.children,[original[0]]);
});
test('unregistered kinds cannot create customer nodes',()=>{
 const container=new Container();
 reconcilePublicNodes(container,{items:[item('raw',1,'tool_result'),item('ok',2)]},[],create);
 assert.deepEqual(container.children.map(n=>n.dataset.publicId),['ok']);
});
