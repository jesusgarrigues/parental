/* Polling is independent of screen rendering and unsaved forms. */
(() => {
  let callbacks={},current=null,device=null,registered=false,push=false,running=false,items=[],groups=null,unread=0,diagnostic=null,lastError='';
  const identifiers=new Map();
  const make=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n;};
  function id(account){if(identifiers.has(account.id))return identifiers.get(account.id);const key='parental-alert-device-'+account.id;let value;try{value=localStorage.getItem(key);}catch{}
    if(!/^[a-zA-Z0-9_-]{16,80}$/.test(value||'')){value=crypto.randomUUID();try{localStorage.setItem(key,value);}catch{}}identifiers.set(account.id,value);return value;}
  const registration=()=>navigator.serviceWorker?.getRegistration?.();
  function status(){if(lastError)return lastError;
    const delivery=diagnostic?.last_delivery;
    const labels={pending:'Aviso pendiente',sending:'Enviando aviso',retry:'Entrega fallida; se volverá a intentar',failed:'Entrega fallida: vuelve a activar los avisos',accepted:'Último aviso aceptado por el navegador o proveedor',skipped:'Envío omitido: leído, resuelto, antiguo o fuera de preferencias'};
    return (push?'Envío desde el servidor activado, también con Parental cerrada.':'Sin Push registrado: entrega automática solo con Parental abierta. Pulsa Activar avisos para completar la activación.')+(delivery?' '+(labels[delivery.status]||delivery.status)+(delivery.error?' ('+delivery.error+')'+hint(delivery.error):''):'');}
  // Plain-language next step for provider rejections; codes come from the server without private details.
  function hint(error){
    if(/BadJwtToken|VapidPkHashMismatch|push_provider_403/.test(error))return ' El proveedor de avisos ha rechazado la firma del servidor: actualiza Parental, revisa WEB_PUSH_CONTACT y vuelve a pulsar Activar avisos.';
    if(/push_subscription_expired|push_provider_(404|410)/.test(error))return ' La suscripción de este dispositivo ya no es válida: pulsa Activar avisos.';
    if(error==='push_network_or_configuration')return ' Docker no ha podido contactar con el proveedor de avisos: revisa la salida a Internet del contenedor.';
    return '';}
  // Shown in the inbox, not only in Ajustes, when this phone will not receive system notifications.
  function warning(){
    if(!diagnostic||!window.ParentalNotifications)return null;
    const delivery=diagnostic.last_delivery,failing=push&&delivery&&['retry','failed'].includes(delivery.status);
    if(push&&!failing)return null;
    const box=make('div');box.className='card notification-warning';box.setAttribute('role','status');
    box.append(make('strong',failing?'Los avisos no están llegando a este dispositivo':'Este dispositivo no recibirá avisos con Parental cerrada'));
    box.append(make('p',status()));
    const state=window.ParentalNotifications.state();
    if(state.available){const button=make('button','Activar avisos');button.type='button';button.onclick=async()=>{button.disabled=true;try{await window.ParentalNotifications.enable();await poll();}finally{button.disabled=false;}};box.append(button);}
    else box.append(make('p',state.hint));
    return box;}
  function renderStatus(){for(const n of document.querySelectorAll('[data-auto-notification-status]'))n.textContent=status();for(const n of document.querySelectorAll('[data-auto-notification-detail]'))n.textContent=diagnostic?JSON.stringify(diagnostic,null,2):'Todavía no hay diagnóstico de este dispositivo.';window.ParentalNotifications?.update();}
  async function poll(){const account=callbacks.account?.();if(!account){current=null;registered=false;items=[];groups=null;unread=0;push=false;window.ParentalUI?.setAlerts(0,null);renderLists();return;}if(running)return;running=true;
    try{
      if(current!==account.id){current=account.id;device=id(account);registered=false;push=false;items=[];groups=null;lastError='';}
      const own=current;
      if(!registered){const reg=await registration(),sub=await reg?.pushManager?.getSubscription?.();if(callbacks.account?.()?.id!==own)return;
        const saved=await callbacks.request('me/alerts/device',{device,subscription:sub?.toJSON()||null});if(callbacks.account?.()?.id!==own)return;registered=true;push=saved.push;}
      const data=await callbacks.request('me/alerts?device='+encodeURIComponent(device));if(callbacks.account?.()?.id!==own)return;
      items=data.items||[];groups=data.groups||null;unread=data.unread||0;diagnostic=data.diagnostic;push=!!diagnostic?.push;lastError='';window.ParentalUI?.setRequests(data.requests||[],account);window.ParentalUI?.setAlerts(unread,account);renderStatus();renderLists();
      if(!push&&window.ParentalNotifications?.state().enabled)for(const item of data.deliveries||[]){
        if(callbacks.account?.()?.id!==own)return;const claim=await callbacks.request('me/alerts/result',{device,id:item.id,status:'claim'});if(!claim.claimed)continue;
        const title=item.kind==='request'?'Nueva solicitud de '+item.detail.username:item.client+' · '+callbacks.name(item.service);
        const body=item.kind==='request'?item.client+' · '+item.detail.minutes+' minutos':'Consulta bloqueada. Pulsa para revisar y autorizar tiempo.';
        const accepted=await window.ParentalNotifications.notify(title,{body,tag:'parental-alert-'+item.id,target:item.target});if(callbacks.account?.()?.id!==own)return;
        await callbacks.request('me/alerts/result',{device,id:item.id,status:accepted?'accepted':'failed'});
      }
    }catch(error){if(error.status===401){items=[];groups=null;unread=0;registered=false;push=false;diagnostic=null;window.ParentalUI?.setRequests([],null);window.ParentalUI?.setAlerts(0,null);renderLists();}lastError='No se pudo comprobar o entregar los avisos: '+error.message;renderStatus();}finally{running=false;}}
  async function activate(){const account=callbacks.account?.();if(!account)throw Error('Inicia sesión para activar los avisos');device=id(account);
    const data=await callbacks.request('me/alerts'),reg=await registration();if(!reg?.active||!reg.pushManager)throw Error('El navegador no ofrece Push en este modo. En iPhone abre Parental desde el icono instalado.');
    let sub=await reg.pushManager.getSubscription();const bytes=Uint8Array.from(atob(data.public_key.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-data.public_key.length%4)%4)),c=>c.charCodeAt(0));
    if(sub&&diagnostic?.last_delivery?.error==='push_subscription_expired'){await sub.unsubscribe();sub=null;}
    if(sub&&sub.options?.applicationServerKey){const old=new Uint8Array(sub.options.applicationServerKey);if(old.length!==bytes.length||old.some((v,i)=>v!==bytes[i])){await sub.unsubscribe();sub=null;}}
    if(!sub)sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes});if(callbacks.account?.()?.id!==account.id){await sub.unsubscribe();throw Error('La sesión ha cambiado; vuelve a activar los avisos');}
    const saved=await callbacks.request('me/alerts/device',{device,subscription:sub.toJSON()});current=account.id;registered=true;push=saved.push;lastError='';renderStatus();return true;}
  async function disconnect(){const account=callbacks.account?.();if(account)await callbacks.request('me/alerts/disconnect',{device:id(account)});try{const reg=await registration(),sub=await reg?.pushManager?.getSubscription?.();if(sub)await sub.unsubscribe();}catch{/* The server has already revoked delivery; browser cleanup must not block logout. */}current=null;registered=false;push=false;items=[];groups=null;unread=0;diagnostic=null;window.ParentalUI?.setAlerts(0,null);renderLists();}
  async function read(identifier){await callbacks.request('me/alerts/read',{id:identifier});await poll();}
  async function readTarget(target){if(target?.alert_id)try{await read(target.alert_id);}catch(error){lastError='No se pudo marcar el aviso leído: '+error.message;renderStatus();}}
  function renderLists(){for(const root of document.querySelectorAll('[data-alert-inbox]')){root.replaceChildren();const row=make('div');row.className='row';row.append(make('h2','Avisos'));
    const all=make('button','Marcar todos como leídos');all.type='button';all.disabled=!unread;all.onclick=async()=>{all.disabled=true;try{await read(null);}catch(e){root.prepend(make('p',e.message));all.disabled=false;}};row.append(all);root.append(row);
    const alert=warning();if(alert)root.append(alert);
    root.append(make('p',unread+' '+(unread===1?'cliente con avisos nuevos':'clientes con avisos nuevos')+'. El icono suma estos clientes y las solicitudes pendientes.'));
    root.append(make('p','Los intentos se agrupan por servicio durante 24 horas desde el primero. El historial se conserva hasta 30 días. Leer avisos no concede permisos.'));
    const blocks=groups||items.filter(i=>i.kind==='blocked').map(i=>({...i,is_new:!i.read_at,attempts:1,first_seen:i.created,last_seen:i.created}));
    if(!blocks.length)root.append(make('p','Sin avisos de bloqueos seleccionados. La actividad completa aparece debajo.'));
    const clients=new Map();for(const item of blocks){if(!clients.has(item.client))clients.set(item.client,[]);clients.get(item.client).push(item);}
    const history=make('details');history.className='alert-history';history.append(make('summary','Historial de avisos revisados o de más de 24 horas'));
    function entry(item){const card=make('article');card.className='card event '+(item.is_new?'blocked':'');card.append(make('strong',callbacks.name(item.service)+(item.is_new?' · Nuevo':'')));
      card.append(make('p',(item.attempts||1)+' consultas detectadas · Primera: '+new Date((item.first_seen||item.created)*1000).toLocaleString()+' · Última: '+new Date((item.last_seen||item.created)*1000).toLocaleString()));
      const details=make('details');details.append(make('summary','Ver detalle'));if(item.detail.domain)details.append(make('p',item.detail.domain));if(item.detail.servers?.length)details.append(make('p','AdGuard: '+item.detail.servers.join(' · ')));card.append(details);
      const button=make('button','Revisar permiso');button.type='button';button.onclick=()=>callbacks.navigate(item.target);card.append(button);return card;}
    for(const [client,list] of clients){const fresh=list.filter(i=>i.is_new),older=list.filter(i=>!i.is_new);
      if(fresh.length){const section=make('section');section.className='alert-client';section.append(make('h3',client));const mark=make('button','Marcar avisos de este cliente como leídos');mark.type='button';mark.onclick=async()=>{mark.disabled=true;try{await callbacks.request('me/alerts/read',{client});await poll();}catch(e){section.prepend(make('p',e.message));mark.disabled=false;}};section.append(mark);for(const item of fresh)section.append(entry(item));root.append(section);}
      if(older.length){const section=make('section');section.append(make('h3',client));for(const item of older)section.append(entry(item));history.append(section);}}
    if(blocks.some(i=>!i.is_new))root.append(history);root.append(make('h2','Actividad reciente'));}}
  function inbox(root){const section=make('section');section.dataset.alertInbox='';root.append(section);renderLists();poll();}
  async function testPush(){if(!push)throw Error('Activa primero el envío desde el servidor');await callbacks.request('me/alerts/test',{device});await poll();}
  function init(options){callbacks=options;window.ParentalNotifications?.attach?.({activate,connected:()=>push,status,testPush});setInterval(poll,10000);document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')poll();});}
  window.ParentalAlertCenter={init,poll,inbox,readTarget,disconnect,activate,status,testPush};
})();
