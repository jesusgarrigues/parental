(() => {
  let queue=Promise.resolve(),key=null,hint='';
  const supported=()=>typeof navigator.setAppBadge==='function'&&typeof navigator.clearAppBadge==='function';
  function status(){return hint||(supported()?'El icono suma solicitudes pendientes y clientes con avisos nuevos si el sistema lo permite.':'Este navegador no permite fijar el contador del icono. En Android la burbuja puede depender de los avisos activos y del fabricante.');}
  function render(){for(const n of document.querySelectorAll('[data-app-badge-status]'))n.textContent=status();}
  function update(count,account){
    const safe=account&&Number.isSafeInteger(count)&&count>0?count:0;
    const next=(account?.id||'none')+':'+safe+':'+(window.Notification?.permission||'');
    if(!supported()){hint='';render();return Promise.resolve();}
    if(next===key){render();return queue;}key=next;
    queue=queue.then(async()=>{try{if(safe)await navigator.setAppBadge(safe);else await navigator.clearAppBadge();hint='';}catch{hint='El sistema no ha permitido actualizar la burbuja del icono. Los pendientes siguen visibles en Solicitudes.';if(key===next)key=null;}render();});
    return queue;
  }
  window.ParentalBadges={update,status,supported,render};
})();
