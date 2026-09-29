import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

test('account pages enrich both session shapes and acknowledgement updates one source',async()=>{
  const requests=[],events=[],app={currentUserId:'alice'};
  const notifications=[
    {session_id:'a',cursor:1,unread:true,flag:'complete',message:'Done'},
    {session_id:'c',cursor:3,unread:true,flag:'error',message:'Review'},
  ];
  const fetch=async(url,options={})=>{
    requests.push({url,options});
    if(url.includes('/acknowledgement'))
      return {ok:true,json:async()=>({cursor:3,unread:false})};
    if(url.endsWith('/notifications'))
      return {ok:true,json:async()=>({notifications:[notifications[0]],has_more:true,next_after:'b'})};
    if(url.includes('?after=b'))
      return {ok:true,json:async()=>({notifications:[notifications[1]],has_more:false,next_after:null})};
    throw new Error('Unexpected request: '+url);
  };
  const context=vm.createContext({fetch,Date,Map,JSON,encodeURIComponent,
    window:{addEventListener(){},dispatchEvent:event=>events.push(event)},
    document:{addEventListener(){}},
    CustomEvent:class{constructor(type,init){this.type=type;this.detail=init?.detail}},
  });
  const values={
    '../../shared/js/state.js':{app},
    '../../shared/js/config.js':{apiPath:path=>path},
    '../../shared/js/left-login.js':{authHeaders:()=>({Authorization:'test'})},
  };
  const module=new vm.SourceTextModule(
    await fs.readFile(new URL('../ui/chat/core/notifications.js',import.meta.url),'utf8'),{context});
  await module.link(async spec=>{
    const exports=values[spec];assert.ok(exports,spec);
    return new vm.SyntheticModule(Object.keys(exports),function(){
      for(const [key,value] of Object.entries(exports)) this.setExport(key,value);
    },{context});
  });
  await module.evaluate();
  const rows=await module.namespace.enrichSessions([{id:'a'},{session_id:'c'},{id:'b'}],'alice');
  assert.equal(rows[0].has_unread,true);
  assert.equal(rows[1].closing_cta,'Review');
  assert.equal(rows[2].v2_notification,undefined);
  assert.equal(requests.filter(row=>row.url.includes('/notifications')).length,2);
  assert.equal(await module.namespace.setNotificationUnread('c',false),true);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body),{cursor:3,unread:false});
  assert.equal(events.at(-1).type,'v2-notifications-changed');
  const updated=await module.namespace.enrichSessions([{session_id:'c'}],'alice');
  assert.equal(updated[0].has_unread,false);
});
