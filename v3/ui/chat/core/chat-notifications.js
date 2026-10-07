// One composer surface for confirmations issued by chat controls and the runtime.
export function mountChatNotifications({root,guard}) {
  const host=root.querySelector('#chat-notifications');
  const modeSlot=document.createElement('div');
  const toolSlot=document.createElement('div');
  host.append(modeSlot,toolSlot);
  let finishMode=null;

  function action(label,kind,handler) {
    const button=document.createElement('button');
    button.type='button';
    button.className='chat-notification-action chat-notification-'+kind;
    button.textContent=label;
    button.onclick=guard(handler);
    return button;
  }
  function card({title,message,details,actions}) {
    const node=document.createElement('section');
    node.className='chat-notification';
    node.setAttribute('role','group');
    node.setAttribute('aria-label',title);
    const heading=document.createElement('div');
    heading.className='chat-notification-heading';
    const label=document.createElement('strong');
    label.textContent=title;
    heading.append(label);
    if(details)heading.append(details.toggle);
    node.append(heading);
    if(message){
      const text=document.createElement('p');
      text.textContent=message;
      node.append(text);
    }
    if(details)node.append(details.body);
    const buttons=document.createElement('div');
    buttons.className='chat-notification-actions';
    for(const item of actions)buttons.append(action(item.label,item.kind,item.run));
    node.append(buttons);
    return node;
  }
  function cancelMode() {
    finishMode?.(false);
  }
  function confirmMode(label) {
    cancelMode();
    return new Promise(resolve=>{
      const previous=document.activeElement;
      const settle=accepted=>{
        if(finishMode!==settle)return;
        finishMode=null;
        modeSlot.replaceChildren();
        if(previous?.isConnected)previous.focus({preventScroll:true});
        resolve(accepted);
      };
      finishMode=settle;
      const node=card({
        title:'This will allow all write permitted tools',
        actions:[
          {label:'Approve Auto mode',kind:'approve',run:()=>settle(true)},
          {label:'Deny',kind:'deny',run:()=>settle(false)}
        ]
      });
      node.addEventListener('keydown',event=>{
        if(event.key==='Escape'){event.preventDefault();settle(false);}
      });
      modeSlot.replaceChildren(node);
      node.querySelector('.chat-notification-deny').focus({preventScroll:true});
    });
  }
  function showTool({approval,run,active,expanded,decide,cancel}) {
    const details=document.createElement('pre');
    details.id='approval-details-'+approval.id;
    details.hidden=!expanded.has(approval.id);
    details.textContent=JSON.stringify(approval.arguments,null,2);
    const toggle=document.createElement('button');
    toggle.type='button';
    toggle.className='chat-notification-detail-toggle';
    toggle.textContent=approval.name;
    toggle.title='Show tool call details';
    toggle.setAttribute('aria-controls',details.id);
    toggle.setAttribute('aria-expanded',String(!details.hidden));
    toggle.onclick=()=>{
      details.hidden=!details.hidden;
      if(details.hidden)expanded.delete(approval.id);
      else expanded.add(approval.id);
      toggle.setAttribute('aria-expanded',String(!details.hidden));
    };
    const actions=[
      {label:'Approve',kind:'approve',run:()=>decide('approve')},
      {label:'Deny',kind:'deny',run:()=>decide('deny')}
    ];
    if(active?.id===run.id)actions.push({label:'Cancel run',kind:'neutral',run:cancel});
    toolSlot.append(card({title:'Need approval:',details:{toggle,body:details},actions}));
  }
  function clearTools(){toolSlot.replaceChildren();}
  function destroy(){cancelMode();clearTools();host.replaceChildren();}
  return {confirmMode,cancelMode,showTool,clearTools,destroy};
}
