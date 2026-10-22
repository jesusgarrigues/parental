/* Presentation and installation only. Permissions and decisions stay on the server. */
(function () {
  'use strict';
  const make = (tag, text = '', cls = '') => {
    const node = document.createElement(tag);
    node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  let callbacks = {}, user = null, requests = [], deferredInstall = null, unread=0;
  const displayMode = window.matchMedia?.('(display-mode: standalone)');
  const installed = () => !!(navigator.standalone || displayMode?.matches);
  const ios = () => /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const countPending = (items, account, now = Date.now() / 1000) => {
    if (!account) return 0;
    return items.filter(r => r.status === 'pending' &&
      (!r.created || Number(r.created) + 86400 > now) &&
      (account.role === 'admin' || (account.role === 'solicitante'
        ? Number(r.user_id) === Number(account.id)
        : (account.clients || []).includes(r.client)))).length;
  };
  function updateBadges() {
    const count = countPending(requests, user);
    window.ParentalBadges?.update(count+unread,user);
    for(const nav of document.querySelectorAll('.nav[data-page="activity"]')){
      let badge=nav.querySelector('[data-alert-count]');
      if(!badge){badge=make('span','','request-count');badge.dataset.alertCount='';nav.append(badge);}
      badge.textContent=unread>99?'99+':String(unread);badge.hidden=!unread;
      nav.setAttribute('aria-label',unread?'Actividad, '+unread+' clientes con avisos nuevos':'Actividad');
    }
    for (const badge of document.querySelectorAll('[data-request-count]')) {
      badge.textContent = count > 99 ? '99+' : String(count);
      badge.hidden = !count;
    }
    for (const nav of document.querySelectorAll('.nav[data-page="requests"]')) {
      nav.setAttribute('aria-label', count ? 'Solicitudes, ' + count + ' pendientes' : 'Solicitudes');
    }
    const summary = document.getElementById('pending-summary');
    if (summary) summary.textContent = count ? count + (count === 1 ? ' pendiente' : ' pendientes') : 'Sin solicitudes pendientes';
  }
  function setView(view, account) {
    if(!account||user?.id!==account.id)unread=0;
    user = account;
    document.body.dataset.page = view;
    document.body.classList.toggle('signed-in', !!account);
    for (const nav of document.querySelectorAll('.nav')) {
      const parent=['global','server','nintendo','users','audit'].includes(view)?'settings':view==='client'?'clients':view;
      const active = nav.dataset.page === parent;
      nav.classList.toggle('active', active);
      if (active) nav.setAttribute('aria-current', 'page');
      else nav.removeAttribute('aria-current');
    }
    updateBadges();
  }
  function setRequests(items, account) {
    if(!account||user?.id!==account.id)unread=0;
    requests = Array.isArray(items) ? items : [];
    user = account;
    updateBadges();
  }
  function setAlerts(count,account){if(!account||user?.id!==account.id)unread=0;else unread=Number.isSafeInteger(count)&&count>0?count:0;updateBadges();}
  function showInstallGuide() {
    const previous = document.activeElement;
    if (document.getElementById('install-dialog')) return;
    const backdrop = make('div', '', 'install-backdrop');
    backdrop.id = 'install-dialog';
    const panel = make('section', '', 'install-panel');
    panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'install-title');
    const title = make('h2', 'Instalar Parental'); title.id = 'install-title';
    const image = make('img'); image.src = '/apple-touch-icon.png'; image.alt = ''; image.className = 'install-icon';
    panel.append(image, title);
    if (!window.isSecureContext) panel.append(make('p', 'Abre tu instalación mediante HTTPS con un certificado válido para instalarla y usar las funciones del navegador.'));
    else if (ios()) {
      panel.append(make('p', 'En tu iPhone o iPad:'));
      const steps = make('ol');
      for (const step of ['Abre esta dirección en Safari.', 'Pulsa Compartir y elige Añadir a pantalla de inicio.', 'Comprueba el nombre Parental y pulsa Añadir.']) steps.append(make('li', step));
      panel.append(steps);
    } else {
      panel.append(make('p', 'Abre el menú de tu navegador y elige Instalar aplicación o Añadir a pantalla de inicio. Si todavía no aparece, recarga esta página mediante HTTPS y comprueba que tu navegador admite instalación.'));
    }
    panel.append(make('p', 'Desde el icono tendrás acceso a tus clientes y solicitudes. Para gestionar permisos necesitas conexión con tu servidor.'));
    const close = make('button', 'Entendido', 'primary'); close.type = 'button';
    const dismiss = () => { backdrop.remove(); previous?.focus(); };
    close.onclick = dismiss; panel.append(close); backdrop.append(panel); document.body.append(backdrop);
    backdrop.onclick = e => { if (e.target === backdrop) dismiss(); };
    backdrop.onkeydown = e => {
      if (e.key === 'Escape') { e.preventDefault(); dismiss(); }
      if (e.key === 'Tab') { e.preventDefault(); close.focus(); }
    };
    close.focus();
  }
  async function install() {
    if (installed()) return;
    if (deferredInstall) {
      const prompt = deferredInstall; deferredInstall = null;
      try { await prompt.prompt(); await prompt.userChoice; }
      catch { showInstallGuide(); }
      updateInstallBanner();
    } else showInstallGuide();
  }
  function updateInstallBanner() {
    const banner = document.getElementById('install-banner');
    let dismissed = false;
    try { dismissed = Number(localStorage.getItem('parental-install-dismissed')) > Date.now() - 30 * 86400000; } catch {}
    if (banner) banner.hidden = installed() || !window.isSecureContext || dismissed || !(ios() || deferredInstall);
    const settings = document.getElementById('install-settings');
    if (settings) {
      const hint = settings.querySelector('[data-install-hint]');
      hint.textContent = installed() ? 'Parental ya está abierta como aplicación.' : 'Accede desde tu pantalla de inicio.';
      settings.querySelector('button').hidden = installed();
    }
  }
  function renderSettings(root, account) {
    callbacks.settingsStart?.();
    const group = (title) => { const n = make('section', '', 'form-section settings-section'); n.append(make('h3', title)); root.append(n); return n; };
    const action = (parent, label, handler, cls = '') => { const b = make('button', label, cls); b.type = 'button'; b.onclick = handler; parent.append(b); return b; };
    const appHeading=make('h2','Parental','settings-group-title');root.append(appHeading);
    const profile = group('Mi perfil');
    const roles = {admin: 'Administrador', responsable: 'Responsable', solicitante: 'Solicitante', observador: 'Observador'};
    const profileIdentity=make('div','','profile-identity'),preview=window.ParentalIdentity.avatar(account,'account-avatar'),description=make('div');
    description.append(make('strong', account.username), make('p', roles[account.role] || account.role));profileIdentity.append(preview,description);profile.append(profileIdentity);
    const details=make('details','','profile-avatar-details');details.append(make('summary','Cambiar cara'));
    const status=make('p','','profile-feedback');status.setAttribute('role','status');
    const picker=window.ParentalIdentity.picker(account.avatar,()=>{callbacks.avatarDraft();status.textContent='Cara seleccionada. Guarda para conservarla.';});details.append(picker.element);
    const save=action(details,'Guardar cara',async()=>{const chosen=picker.getValue();save.disabled=true;const choices=[...picker.element.querySelectorAll('button,input,select')];for(const choice of choices)choice.disabled=true;try{await callbacks.saveAvatar(chosen,profile);const next=window.ParentalIdentity.avatar({...account,avatar:chosen},'account-avatar');profileIdentity.replaceChild(next,profileIdentity.firstChild);status.textContent='Cara guardada.';}catch(error){status.textContent='No se pudo guardar: '+error.message;}finally{save.disabled=false;for(const choice of choices)choice.disabled=false;}},'primary');details.append(status);profile.append(details);
    window.ParentalAuthentik.profile(profile,account,{request:callbacks.request,beforeRedirect:callbacks.identityLeave});
    action(profile, 'Cerrar sesión', callbacks.signout, 'ghost');
    if (account.role === 'admin') {
      const manage = group('Administración');
      window.ParentalAuthentik.config(root,{request:callbacks.request,draft:callbacks.identityDraft,clean:callbacks.identityClean});
      for (const [view, label] of [['users', 'Usuarios y roles'], ['audit', 'Registro de cambios']]) action(manage, label + ' ›', () => callbacks.navigate(view), 'settings-link');
    }
    const notifications = group('Avisos del navegador');
    notifications.append(make('p', 'Recibe avisos de solicitudes y consultas bloqueadas. Activa el envío desde el servidor en cada dispositivo para recibirlos con Parental cerrada.'));
    window.ParentalNotifications.panel(notifications);
    const badgeHint=make('p','','notification-scope');badgeHint.dataset.appBadgeStatus='';notifications.append(badgeHint);window.ParentalBadges?.render();
    callbacks.blockedAlerts?.(root,account);
    const installation = group('Instalar Parental'); installation.id = 'install-settings';
    const identity = make('div', '', 'install-identity'), image = make('img'); image.src = '/apple-touch-icon.png'; image.alt = ''; image.className = 'install-icon';
    const hint = make('p'); hint.dataset.installHint = ''; identity.append(image, hint); installation.append(identity);
    action(installation, ios() ? 'Cómo instalar en iPhone' : 'Instalar Parental', install, 'primary');
    if(account.role==='admin'){
      root.append(make('h2','Integraciones','settings-group-title'));
      const adguard=group('AdGuard Home');adguard.append(make('p','Conexión y configuración global que heredan los clientes.'));
      action(adguard,'Conexión de AdGuard ›',()=>callbacks.navigate('server'),'settings-link');
      action(adguard,'Configuración global de AdGuard ›',()=>callbacks.navigate('global'),'settings-link');
      const nintendo=group('Nintendo');nintendo.append(make('p','Cuenta, conexión y sincronización de las consolas. Los límites de cada consola están en su detalle.'));
      action(nintendo,'Conexión de Nintendo ›',()=>callbacks.navigate('nintendo'),'settings-link');
    }
    updateInstallBanner();
  }
  function init(options) {
    callbacks = options;
    const banner = document.getElementById('install-banner');
    banner?.querySelector('[data-install]')?.addEventListener('click', install);
    banner?.querySelector('[data-dismiss-install]')?.addEventListener('click', () => {
      try { localStorage.setItem('parental-install-dismissed', String(Date.now())); } catch {}
      banner.hidden = true;
    });
    window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); deferredInstall = event; updateInstallBanner(); });
    window.addEventListener('appinstalled', () => { deferredInstall = null; if (banner) banner.hidden = true; updateInstallBanner(); });
    displayMode?.addEventListener?.('change', updateInstallBanner);
    updateInstallBanner();
    setInterval(updateBadges, 10000);
  }
  window.ParentalUI = {init, setView, setRequests, setAlerts, countPending, renderSettings, showInstallGuide};
})();
