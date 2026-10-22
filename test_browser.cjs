const fs=require('node:fs');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=process.env.APP_SOURCE||process.cwd();
const shots=process.env.SCREENSHOT_DIR||'/tmp/adguard-ui-review';
fs.mkdirSync(shots,{recursive:true});
const cleanSvg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#85efc3" d="M3 3h18v18H3z"/></svg>';
const ids=['youtube','tiktok','netflix','twitch','discord','instagram','facebook','snapchat','reddit','telegram','whatsapp','steam','epic_games','roblox','minecraft','xboxlive','playstation','amazon','ebay','aliexpress',...Array.from({length:45},(_,i)=>'service_'+i)];
const names={youtube:'YouTube',tiktok:'TikTok',netflix:'Netflix',twitch:'Twitch',discord:'Discord',instagram:'Instagram',steam:'Steam',epic_games:'Epic Games',roblox:'Roblox'};
const services=ids.map((id,i)=>({id,name:names[id]||id.replaceAll('_',' '),icon_svg:i%2?cleanSvg:Buffer.from(cleanSvg).toString('base64')}));
const safe={enabled:true,bing:true,duckduckgo:true,ecosia:true,google:true,pixabay:true,yandex:true,youtube:true};
const globalConfig={filtering_enabled:true,parental_enabled:true,safebrowsing_enabled:true,safe_search:safe,blocked_services:{ids,schedule:{time_zone:'Europe/Madrid'}},protection_enabled:true};
const clients=['iMac de Emma','iMac de Martín','iPad familiar',...Array.from({length:7},(_,i)=>'Dispositivo '+(i+1))].map((name,i)=>({name,ids:['192.168.1.'+(20+i)],use_global_settings:true,use_global_blocked_services:true,filtering_enabled:true,parental_enabled:true,safebrowsing_enabled:true,safe_search:safe,blocked_services:[],upstreams:[],tags:['device_pc'],ignore_querylog:false,ignore_statistics:false,upstreams_cache_enabled:false,upstreams_cache_size:0}));
const effective={filtering_enabled:true,parental_enabled:true,safebrowsing_enabled:true,safe_search:safe,blocked_services:ids,blocked_services_schedule:{time_zone:'Europe/Madrid'},protection_enabled:true};
const state={clients,base_clients:Object.fromEntries(clients.map(c=>[c.name,c])),effective:Object.fromEntries(clients.map(c=>[c.name,effective])),base_effective:Object.fromEntries(clients.map(c=>[c.name,effective])),services,global_config:globalConfig,leases:[{client:'iMac de Emma',service:'youtube',expires:Date.now()/1000+1200},{client:'iMac de Emma',service:'service_44',expires:Date.now()/1000+1800}],events:[],requests:[],supported_tags:['device_pc','device_phone','user_child'],auto_clients:[],server:{},demo:true,error:''};
const native={configured:true,devices:[{id:'ABC',key:'nintendo:ABC',name:'Switch de Martín',model:'Switch',used_minutes:0,remaining_minutes:5,limit_minutes:0,extra_minutes:5,bedtime:'20:00',forced_termination:true,alarms_enabled:false,last_sync:Date.now()/1000,console_sync_pending:false,available:true,can_grant:true,can_cancel:true,pending_operation:null,daily_extra_minutes:5,budget_remaining_minutes:5,bedtime_remaining_minutes:600,base_bedtime:'20:00',bedtime_start:'06:00',native_policy:{timerMode:'DAILY',restrictionMode:'FORCED_TERMINATION',dailyRegulations:{timeToPlayInOneDay:{enabled:true,limitTime:0},bedtime:{enabled:true,endingTime:{hour:20,minute:0},startingTime:{hour:6,minute:0}}},eachDayOfTheWeekRegulations:{}},policy_revision:'fixture-revision'}]};
const requests=[{id:1,user_id:2,username:'martin',avatar:'face-04',client:'nintendo:ABC',service:'@nintendo',minutes:20,reason:'Jugar con mis amigos',status:'pending',created:Date.now()/1000,extend_bedtime:true,approved_extend_bedtime:null}];
state.requests=requests;
const user={id:1,username:'admin',avatar:'face-01',role:'admin',clients:[],edit_policy:true,max_minutes:1440};
let authenticEnabled=false,authenticLinked=false,authenticPending=null;const authenticConfig={enabled:false,issuer:'',discovery_url:'',client_id:'',public_url:'',secret_set:false,callback_url:''};
const alertPreferences={mode:'all',services:[],protections:[],cooldown_minutes:5,clients:{}};let alertsFailure=false;
let inboxGroups=null,inboxItems=[],inboxUnread=0,deliveryAccepted=false,deliveryDue=true,deliveryAttempts=0,autoNotifyCalls=0;
// Serve the page with the exact CSP app.py sends, so any violation fails the browser test.
const csp=require('node:child_process').execFileSync('python3',['-c','import sys,security;print(security.content_security_policy(open(sys.argv[1]).read()),end="")',root+'/index.html'],{cwd:root,encoding:'utf8'});
const calls=[];let adguardDown=false,clientFailure=null,readFailures=0,saveDelay=0;
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||['/usr/bin/chromium','/usr/bin/google-chrome','/opt/google/chrome/chrome'].find(fs.existsSync),headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--disable-breakpad','--disable-crash-reporter']});
 try{
 const context=await browser.newContext({viewport:{width:1440,height:1080},serviceWorkers:'block'});
 await context.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.pathname.startsWith('/api/')){
   if(url.pathname==='/api/state'&&adguardDown)return route.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'AdGuard unavailable fixture'})});
   const path=url.pathname.slice(5);let body=null;
   if(req.method()==='POST'){body=req.postDataJSON();calls.push({path,body});}
   if(path==='me/alerts/read'){for(const item of inboxItems)if(body.id===null||item.id===body.id||item.client===body.client)item.read_at=Date.now()/1000;if(inboxGroups){for(const group of inboxGroups)if(body.id===null||group.id===body.id||group.client===body.client){group.read_at=Date.now()/1000;group.is_new=false;}inboxUnread=new Set(inboxGroups.filter(g=>g.is_new).map(g=>g.client)).size;}else inboxUnread=inboxItems.filter(i=>!i.read_at).length;return route.fulfill({contentType:'application/json',body:'{"ok":true}'});}
   if(path==='me/alerts/result'){if(body.status==='claim'){deliveryAttempts++;return route.fulfill({contentType:'application/json',body:JSON.stringify({claimed:deliveryDue&&!deliveryAccepted})});}deliveryAccepted=body.status==='accepted';deliveryDue=false;return route.fulfill({contentType:'application/json',body:'{"ok":true}'});}
   if(path==='me/alerts/device')return route.fulfill({contentType:'application/json',body:'{"ok":true,"push":false}'});
   if(path==='me/alerts')return route.fulfill({contentType:'application/json',body:JSON.stringify({items:inboxItems,groups:inboxGroups,unread:inboxUnread,requests,diagnostic:{push:false},deliveries:deliveryDue&&!deliveryAccepted?inboxItems.filter(i=>!i.read_at):[]})});
   if(path==='state'&&readFailures>0){readFailures--;return route.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'No se pudo actualizar el estado de AdGuard.'})});}
   if(path==='client'&&body){
    if(saveDelay)await new Promise(resolve=>setTimeout(resolve,saveDelay));
    if(clientFailure)return route.fulfill({status:clientFailure.status,contentType:'application/json',body:JSON.stringify({error:clientFailure.message})});
    const original=state.base_clients[body.client];Object.assign(original,body.patch);
    state.base_clients[original.name]=original;if(original.name!==body.client)delete state.base_clients[body.client];
    state.base_effective[original.name]={...effective,blocked_services:original.use_global_blocked_services?ids:original.blocked_services};
   }
   if(path==='authentik/config'&&body){Object.assign(authenticConfig,{enabled:body.enabled,issuer:body.issuer,discovery_url:body.discovery_url,client_id:body.client_id,public_url:body.public_url,secret_set:!!body.client_secret,callback_url:body.public_url+'/api/auth/oidc/callback'});authenticEnabled=body.enabled;return route.fulfill({contentType:'application/json',body:JSON.stringify(authenticConfig)});}
   if(path==='me/authentik/confirm'&&body){authenticLinked=true;authenticPending=null;return route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true})});}
   if(path==='authentik/test')return route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,message:'Proveedor simulado: descubrimiento y firma comprobados.'})});
   if(path==='authentik/config')return route.fulfill({contentType:'application/json',body:JSON.stringify(authenticConfig)});
   if(path==='me/authentik')return route.fulfill({contentType:'application/json',body:JSON.stringify({enabled:authenticEnabled,linked:authenticLinked,identity:authenticLinked?{label:'adulto-authentik',issuer:'https://auth.test/application/o/parental/'}:null,pending:authenticPending})});
   if(path==='me/notifications'){
    if(body&&alertsFailure)return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'No se pudo guardar el aviso de prueba'})});
    if(body)Object.assign(alertPreferences,body);
    return route.fulfill({contentType:'application/json',body:JSON.stringify({preferences:alertPreferences,updated:Date.now()/1000,services,clients:clients.map(c=>({name:c.name,ignore_querylog:c.ignore_querylog}))})});
   }
   if(path==='me/avatar'&&body){user.avatar=body.avatar;return route.fulfill({contentType:'application/json',body:JSON.stringify({user})});}
   if(path==='client/icon'&&body){const device=body.client.startsWith('nintendo:')?native.devices.find(d=>d.key===body.client):state.clients.find(c=>c.name===body.client);device.ui_icon=body.icon==='auto'?null:body.icon;}
   if(path==='nintendo/operation/close'&&body){
    const previous=native.devices[0].pending_operation;
    native.devices[0].pending_operation=null;native.devices[0].can_grant=true;
    native.devices[0].last_operation={...previous,operation_id:body.operation_id,status:'superseded',message:'Seguimiento cerrado sin reenviar tiempo.'};
    return route.fulfill({contentType:'application/json',body:JSON.stringify(native.devices[0].last_operation)});
   }
   const data=path==='me'?{user,csrf:'fixture-csrf'}:path==='state'?state:path==='nintendo/state'?native:path==='requests'?{requests}:path==='auth/status'?{configured:true}:path==='nintendo/config'?{configured:true,timezone:'Europe/Madrid'}:path==='server'?{url:'http://192.168.1.2:3000',username:'admin',demo:true}:path==='diagnostics'?{entries:[]}:path==='users'?{users:[user,{id:9,username:'lucia',email:'lucia@example.test',role:'solicitante',clients:[],edit_policy:false,max_minutes:120,active:false,avatar:'',authentik_linked:true,signup_pending:true,signup_created:Date.now()/1000,has_password:false}]}:path==='devices'?{devices:clients.map(c=>({key:c.name,name:c.name,provider:'adguard'}))}:body?{ok:true,status:['permit','cancel','nintendo/policy'].includes(path)?'confirmed':undefined}:{ok:true};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  }
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',headers:{'Content-Security-Policy':csp},body:fs.readFileSync(root+'/index.html','utf8')});
  if(['/manifest.webmanifest','/sw.js','/icon-192.png','/icon-512.png','/apple-touch-icon.png','/favicon.png','/icon.svg'].includes(url.pathname)){const file=root+url.pathname;return route.fulfill({contentType:file.endsWith('.png')?'image/png':file.endsWith('.webmanifest')?'application/manifest+json':file.endsWith('.js')?'application/javascript':'image/svg+xml',body:fs.readFileSync(file)});}
  if(url.pathname.startsWith('/assets/services/')){const id=url.pathname.split('/').at(-1).replace('.svg','');const art=JSON.parse(fs.readFileSync(root+'/assets/service-artwork.json','utf8'))[id];if(art)return route.fulfill({contentType:'image/svg+xml',body:art});}
  if(url.pathname.startsWith('/assets/')){
   const file=root+url.pathname;if(fs.existsSync(file))return route.fulfill({contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'application/javascript':file.endsWith('.woff2')?'font/woff2':'image/svg+xml',body:fs.readFileSync(file)});
  }
  return route.fulfill({status:404,body:'not found'});
 });
 const page=await context.newPage();page.setDefaultTimeout(10000);const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(/Content Security Policy/i.test(m.text()))errors.push('CSP: '+m.text());});
 await page.goto('https://preview.local/');await page.locator('[data-client-card]').first().waitFor();
 assert.equal(await page.title(),'Parental');
 assert.equal(await page.locator('.desktop-nav [data-request-count]').innerText(),'1');
 await page.waitForFunction(()=>document.querySelector('.brand img').complete);
 assert.ok(await page.locator('.brand img').evaluate(img=>img.complete&&img.naturalWidth===192));
 await page.evaluate(()=>document.fonts.ready);
 assert.ok(await page.evaluate(()=>document.fonts.check('16px Inter')));
 assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'light');
 assert.equal(await page.locator('[data-client-card]').count(),10);
 assert.equal(await page.locator('[data-client-card] .service-detail').count(),0);
 const sanitizer=await page.evaluate(()=>{const source='<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="window.bad=1"><script>window.bad=1</script><foreignObject><div>evil</div></foreignObject><path d="M0 0h2v2z" fill="url(https://evil.test/a)" style="fill:red" onclick="window.bad=2" xlink:href="https://evil.test/a"/><image href="https://evil.test/a"/></svg>';const svg=safeServiceSvg(source);return {markup:svg?.outerHTML,bad:window.bad||null,base64:!!safeServiceSvg(btoa('<svg><path d="M0 0h1v1z"/></svg>'))}});
 assert.ok(sanitizer.base64);assert.equal(sanitizer.bad,null);assert.ok(!/script|foreignObject|onload|onclick|href|style=|url\(/.test(sanitizer.markup));
 await page.screenshot({path:shots+'/desktop-clientes.png',fullPage:true});
 await page.locator('[data-client-card="iMac de Emma"]').click();
 await page.locator('#adguard-dialog-backdrop').waitFor();
 assert.ok(await page.locator('#adguard-service-list [data-service-row="service_44"]').isVisible());
 await page.screenshot({path:shots+'/desktop-favoritos.png'});
 await page.getByRole('button',{name:'Todos los servicios',exact:true}).click();
 assert.ok(await page.locator('#adguard-service-list .service-category').count()>2);
 assert.equal(await page.locator('#adguard-service-list .service-category[open]').count(),0);
 await page.screenshot({path:shots+'/desktop-todos.png'});
 await page.locator('#adguard-service-list .service-category').filter({hasText:'Juegos'}).locator('summary').click();
 await page.locator('#adguard-service-list [data-favorite="steam"]').click();
 assert.ok(await page.locator('#adguard-service-list .service-category').filter({hasText:'Juegos'}).evaluate(d=>d.open));
 await page.evaluate(()=>refresh(true));
 assert.ok(await page.locator('#adguard-service-list .service-category').filter({hasText:'Juegos'}).evaluate(d=>d.open));
 await page.locator('#adguard-service-search').fill('Steam');
 assert.equal(await page.locator('#adguard-service-list [data-service-row]').count(),1);
 await page.getByRole('button',{name:'⚙ Ajustes',exact:true}).click();
 await page.locator('#c-name').fill('iMac de Emma · borrador');
 await page.evaluate(()=>refresh(false));assert.equal(await page.locator('#c-name').inputValue(),'iMac de Emma · borrador');
 await page.screenshot({path:shots+'/desktop-ajustes.png'});
 page.once('dialog',d=>d.accept());await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.screenshot({path:shots+'/movil-clientes.png',fullPage:true});
 await page.locator('[data-client-card="iMac de Emma"]').click();
 await page.getByRole('button',{name:'Todos los servicios',exact:true}).click();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.screenshot({path:shots+'/movil-todos.png'});
 await page.getByRole('button',{name:'⚙ Ajustes',exact:true}).click();
 assert.equal(await page.locator('#c-service-picker').isVisible(),false);
 assert.equal(await page.locator('#c-service-schedule').isVisible(),false);
 await page.locator('#c-global-services').uncheck();
 assert.equal(await page.locator('#c-service-picker').isVisible(),true);
 assert.equal(await page.locator('input[data-picker="client"]:checked').count(),0);
 await page.locator('#c-service-picker .service-search').fill('YouTube');
 await page.locator('input[data-picker="client"][data-service="youtube"]').check();
 await page.locator('#c-global-services').check();
 assert.equal(await page.locator('#c-service-picker').isVisible(),false);
 await page.locator('#c-global-services').uncheck();
 assert.equal(await page.locator('input[data-picker="client"][data-service="youtube"]').isChecked(),true);
 for(const width of [320,375,390,430]){
  await page.setViewportSize({width,height:844});
  const bounds=await page.locator('.adguard-modal').evaluate(m=>({scroll:m.scrollWidth,client:m.clientWidth}));
  assert.ok(bounds.scroll<=bounds.client+1,`Settings overflow at ${width}: ${JSON.stringify(bounds)}`);
  const controls=await page.locator('#adguard-dialog-content').evaluate(root=>[...root.querySelectorAll('input,textarea,select,button')].filter(n=>n.getClientRects().length).every(n=>{const r=n.getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1}));
  assert.ok(controls,`Settings controls outside viewport at ${width}`);
 }
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:shots+'/movil-ajustes.png'});
 // Local validation must be visible in the modal and never issue a write.
 const clientWrites=()=>calls.filter(c=>c.path==='client').length;
 let writes=clientWrites();await page.locator('#c-name').fill('');await page.locator('#c-save').click();
 assert.ok(await page.locator('#c-feedback').isVisible());assert.ok((await page.locator('#c-feedback').innerText()).includes('nombre'));assert.equal(clientWrites(),writes);
 await page.locator('#c-name').fill('iMac de Emma');
 await page.locator('#c-schedule-mon').check();await page.locator('#c-schedule-monstart').fill('bad');await page.locator('#c-save').click();
 assert.ok((await page.locator('#c-feedback').innerText()).includes('Lunes'));assert.equal(clientWrites(),writes);await page.locator('#c-schedule-mon').uncheck();
 for(const status of [400,401,403,502]){
  clientFailure={status,message:`Error de guardado ${status}: no se pudo completar el cambio.`};
  await page.locator('#c-save').click();await page.waitForFunction(()=>!busy);
  assert.ok(await page.locator('#c-feedback').isVisible());assert.ok((await page.locator('#c-feedback').innerText()).includes(String(status)));
  assert.equal(await page.locator('#c-global-services').isChecked(),false);assert.equal(await page.locator('input[data-picker="client"][data-service="youtube"]').isChecked(),true);
 }
 clientFailure=null;saveDelay=250;readFailures=1;writes=clientWrites();
 await page.locator('#c-save').click();assert.equal(await page.locator('#c-save').isDisabled(),true);
 await page.waitForFunction(()=>!busy);assert.equal(clientWrites(),writes+1);
 assert.ok((await page.locator('#c-feedback').innerText()).includes('aceptado'));assert.equal(await page.locator('#c-save').innerText(),'Comprobar guardado');
 saveDelay=0;await page.locator('#c-save').click();await page.waitForFunction(()=>!busy);
 assert.equal(clientWrites(),writes+1);assert.equal(await page.locator('#adguard-dialog-backdrop').count(),0);
 const saved=calls.filter(c=>c.path==='client').at(-1).body.patch;
 assert.deepEqual(saved.blocked_services,['youtube']);assert.equal(saved.use_global_blocked_services,false);assert.equal(saved.use_global_settings,true);
 await page.locator('[data-client-card="iMac de Emma"]').click();await page.getByRole('button',{name:'⚙ Ajustes',exact:true}).click();
 assert.equal(await page.locator('#c-global-services').isChecked(),false);assert.equal(await page.locator('input[data-picker="client"][data-service="youtube"]').isChecked(),true);
 await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 // Device icons use the same saved selection after closing and reopening.
 await page.locator('[data-client-card="iMac de Emma"]').click();await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Cambiar icono',exact:true}).click();
 assert.equal(await page.locator('#icon-selector .icon-choice').count(),92);
 await page.locator('#icon-selector [data-icon="tv"]').click();await page.getByRole('button',{name:'Guardar icono',exact:true}).click();
 await page.locator('#icon-selector').waitFor({state:'detached'});
 assert.ok(await page.locator('[data-client-card="iMac de Emma"] img[src="/assets/icons/tv.svg"]').isVisible());
 await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Cambiar icono',exact:true}).click();assert.equal(await page.locator('#icon-selector [data-icon="tv"]').getAttribute('aria-pressed'),'true');
 await page.locator('#icon-selector').getByRole('button',{name:'Cancelar',exact:true}).click();await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.locator('[data-console="nintendo:ABC"]').click();
 assert.ok(await page.getByRole('button',{name:'Añadir tiempo extra',exact:true}).isEnabled());
 assert.ok(await page.getByRole('button',{name:'Retirar ampliación de hoy',exact:true}).isVisible());
 assert.ok(await page.locator('[data-nintendo-preview]').innerText().then(t=>t.includes('40 minutos más')));
 native.devices[0].bedtime_remaining_minutes=10;
 await page.evaluate(()=>refreshNintendoOnly());
 await page.getByLabel('Minutos extra de juego').selectOption('15');
 assert.equal(await page.locator('[data-extend-bedtime]').isChecked(),false);
 assert.equal(await page.getByRole('button',{name:'Añadir tiempo extra',exact:true}).isDisabled(),false);
 assert.ok((await page.locator('[data-nintendo-preview]').innerText()).includes('se mantiene la hora tope'));
 await page.getByRole('button',{name:'Añadir tiempo extra',exact:true}).click();
 await page.waitForFunction(()=>!busy);
 const budgetOnly=calls.find(c=>c.path==='permit'&&c.body.client==='nintendo:ABC');
 assert.equal(budgetOnly.body.minutes,15);assert.equal(budgetOnly.body.extend_bedtime,false);
 native.devices[0].bedtime_remaining_minutes=600;
 await page.evaluate(()=>refreshNintendoOnly());
 assert.ok(await page.getByRole('button',{name:'+60 min',exact:true}).isVisible());
 await page.locator('[data-extend-bedtime]').check();
 await page.screenshot({path:shots+'/movil-nintendo.png'});
 await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.locator('[data-console="nintendo:ABC"]').click();assert.equal(await page.locator('[data-extend-bedtime]').isChecked(),true);await page.evaluate(()=>refreshNintendoOnly());assert.equal(await page.locator('[data-extend-bedtime]').isChecked(),true);
 native.devices[0].extra_minutes=10;native.devices[0].daily_extra_minutes=10;native.devices[0].remaining_minutes=10;native.devices[0].budget_remaining_minutes=10;
 await page.evaluate(()=>refreshNintendoOnly());
 assert.equal(await page.locator('[data-nintendo-detail="Tiempo extra diario concedido"]').innerText(),'Tiempo extra diario concedido\n10 min');
 assert.equal(await page.locator('[data-extend-bedtime]').isChecked(),true);
 page.once('dialog',async d=>{assert.ok(d.message().includes('app oficial'));await d.accept()});
 await page.getByRole('button',{name:'Retirar ampliación de hoy',exact:true}).click();
 await page.waitForFunction(()=>!busy);
 const cancellation=calls.find(c=>c.path==='cancel');assert.ok(cancellation);
 assert.equal(cancellation.body.cancel_all_today,true);assert.equal(cancellation.body.expected_extra_minutes,10);assert.equal(cancellation.body.expected_bedtime,'20:00');
 native.devices[0].can_cancel=false;native.devices[0].extra_minutes=0;native.devices[0].daily_extra_minutes=0;
 await page.evaluate(()=>refreshNintendoOnly());
 await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Ajustes',exact:true}).click();
 await page.locator('#np-daily-limit').fill('90');
 await page.evaluate(()=>refresh(false));assert.equal(await page.locator('#np-daily-limit').inputValue(),'90');
 await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Jugar solo con permiso · 0 min diarios',exact:true}).click();
 assert.equal(await page.locator('#np-daily-limit').inputValue(),'0');
 assert.equal(await page.locator('#n-policy-forced').isChecked(),true);
 await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Guardar ajustes habituales',exact:true}).click();
 await page.waitForFunction(()=>!busy);
 const policy=calls.find(c=>c.path==='nintendo/policy');assert.ok(policy);assert.equal(policy.body.patch.daily.limit_minutes,0);assert.equal(policy.body.revision,'fixture-revision');
 await page.screenshot({path:shots+'/movil-nintendo-ajustes.png'});
 native.devices[0].extra_minutes=native.devices[0].daily_extra_minutes=75;
 native.devices[0].used_minutes=40;native.devices[0].remaining_minutes=native.devices[0].budget_remaining_minutes=35;
 native.devices[0].can_grant=false;native.devices[0].can_cancel=false;native.devices[0].last_read_at=Date.now()/1000;
 native.devices[0].pending_operation={operation_id:'direct:1:pending-fixture',kind:'grant',status:'pending',stage:'update_sent',minutes:15,confirmed_minutes:0,last_step_acknowledged:true,baseline_daily_extra_minutes:45,expected_daily_extra_minutes:60,expected_bedtime:'20:00',native_response_status:'TO_ADDED',waiting_seconds:300};
 await page.evaluate(()=>{dirty=false;nintendoModalTab='extra';return refreshNintendoOnly()});
 const tracking=page.locator('#nintendo-dialog-backdrop');
 assert.ok((await tracking.innerText()).includes('Aceptada · Falta verificar en Nintendo'));
 await tracking.locator('summary').filter({hasText:'Detalles del seguimiento'}).click();
 assert.ok((await tracking.locator('pre').innerText()).includes('"observed_daily_extra": 75'));
 assert.ok((await tracking.locator('pre').innerText()).includes('"expected_daily_extra": 60'));
 const nativeWrites=calls.filter(c=>['permit','cancel','nintendo/policy'].includes(c.path)).length;
 page.once('dialog',async d=>{assert.ok(d.message().includes('No se añade, retira ni reenvía tiempo'));await d.accept()});
 await tracking.getByRole('button',{name:'Cerrar seguimiento',exact:true}).click();
 await page.waitForFunction(()=>!busy);
 assert.equal(calls.filter(c=>['permit','cancel','nintendo/policy'].includes(c.path)).length,nativeWrites);
 const closed=calls.find(c=>c.path==='nintendo/operation/close');assert.equal(closed.body.expected_daily_extra_minutes,75);assert.equal(closed.body.acknowledge_uncertain,true);
 assert.equal(await tracking.getByRole('button',{name:'Cerrar seguimiento',exact:true}).count(),0);
 assert.ok((await tracking.innerText()).includes('Seguimiento cerrado sin reenviar tiempo'));
 assert.equal(native.devices[0].extra_minutes,75);
 await tracking.locator('summary').filter({hasText:'Detalles del seguimiento'}).click();
 assert.ok((await tracking.locator('pre').innerText()).includes('"expected_daily_extra": 60'));
 assert.ok((await tracking.locator('pre').innerText()).includes('"status": "superseded"'));
 await page.screenshot({path:shots+'/movil-nintendo-recuperado.png'});

 await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.evaluate(()=>{me={...me,role:'solicitante',id:2,clients:['iMac de Emma','nintendo:ABC']};go('clients')});
 await page.locator('[data-client-card="iMac de Emma"]').click();assert.equal(await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:/Ajustes/}).count(),0);assert.ok(await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Solicitar',exact:true}).count()>0);assert.equal(await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Permitir',exact:true}).count(),0);
 await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.locator('[data-console="nintendo:ABC"]').click();
 assert.ok(await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Solicitar tiempo',exact:true}).isVisible());
 assert.equal(await page.getByRole('button',{name:'Añadir tiempo extra',exact:true}).count(),0);
 assert.equal(await page.getByRole('button',{name:'Retirar ampliación de hoy',exact:true}).count(),0);
 await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Ajustes',exact:true}).click();assert.equal(await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Guardar ajustes habituales',exact:true}).count(),0);assert.equal(await page.locator('#np-daily-limit').isDisabled(),true);
 await page.locator('#nintendo-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.evaluate(()=>{me={...me,role:'responsable',edit_policy:false,max_minutes:30};go('requests')});
 await page.locator('#review-bedtime-1').waitFor();assert.equal(await page.locator('#review-bedtime-1').isChecked(),false);
 requests[0].minutes=40;await page.evaluate(()=>{me={...me,max_minutes:35};go('requests')});await page.locator('#review-1').waitFor();assert.equal(await page.locator('#review-1').inputValue(),'30');assert.ok(await page.getByRole('button',{name:'Aprobar 30 min',exact:true}).isVisible());requests[0].minutes=20;await page.evaluate(()=>go('requests'));await page.locator('#review-1').waitFor();
 await page.locator('#review-bedtime-1').check();const beforeDraft=await page.locator('#review-1').inputValue();
 await page.evaluate(()=>refreshPendingCount());
 assert.equal(await page.locator('#review-bedtime-1').isChecked(),true);assert.equal(await page.locator('#review-1').inputValue(),beforeDraft);
 assert.equal(await page.locator('.mobile-nav [data-request-count]').innerText(),'1');
 await page.evaluate(()=>{dirty=false;go('requests')});await page.locator('#review-bedtime-1').waitFor();
 await page.screenshot({path:shots+'/parental-movil-solicitudes.png'});
 for(const width of [320,375,390,430]){
  await page.setViewportSize({width,height:844});assert.ok(await page.locator('.mobile-nav').isVisible());assert.equal(await page.locator('.mobile-nav .nav').count(),4);
  const bounds=await page.locator('.mobile-nav .nav').evaluateAll(nodes=>nodes.every(n=>{const r=n.getBoundingClientRect();return r.width>=44&&r.height>=44&&r.left>=0&&r.right<=innerWidth}));assert.ok(bounds,'Mobile navigation does not fit at '+width);
  const overflow=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,nodes:[...document.body.querySelectorAll('*')].filter(n=>n.getClientRects().length&&n.getBoundingClientRect().right>innerWidth+1).map(n=>({tag:n.tagName,id:n.id,cls:n.getAttribute('class'),right:n.getBoundingClientRect().right,width:n.clientWidth,scroll:n.scrollWidth,text:n.textContent.slice(0,100)})).slice(0,25)}));if(overflow.scroll>width+1){console.log('OVERFLOW',JSON.stringify(overflow));await page.screenshot({path:shots+'/parental-overflow-'+width+'.png'})}assert.ok(overflow.scroll<=width+1,'Page overflow at '+width);
 }
 await page.setViewportSize({width:390,height:844});await page.locator('.mobile-nav [data-page="settings"]').click();
 assert.ok(await page.getByRole('heading',{name:'Ajustes',exact:true}).isVisible());assert.equal(await page.getByRole('button',{name:'Conexión de AdGuard ›',exact:true}).count(),0);
 await page.waitForFunction(()=>document.querySelector('#install-settings .install-icon').complete);
 assert.ok(await page.locator('#install-settings .install-icon').evaluate(img=>img.complete&&img.naturalWidth===180));
 await page.screenshot({path:shots+'/parental-movil-ajustes.png'});
 await page.getByRole('button',{name:'Instalar Parental',exact:true}).click();await page.locator('#install-dialog').waitFor();
 await page.getByRole('button',{name:'Entendido',exact:true}).click();assert.equal(await page.locator('#install-dialog').count(),0);
 await page.evaluate(()=>{Object.defineProperty(navigator,'userAgent',{value:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1',configurable:true});go('settings')});
 await page.getByRole('button',{name:'Cómo instalar en iPhone',exact:true}).click();assert.ok((await page.locator('#install-dialog').innerText()).includes('Añadir a pantalla de inicio'));
 await page.screenshot({path:shots+'/parental-instalar-iphone.png'});await page.getByRole('button',{name:'Entendido',exact:true}).click();
 await page.evaluate(()=>{window.promptCalled=false;const e=new Event('beforeinstallprompt');e.prompt=async()=>{window.promptCalled=true};e.userChoice=Promise.resolve({outcome:'accepted'});dispatchEvent(e)});
 assert.ok(await page.locator('#install-banner').isVisible());await page.locator('#install-banner [data-install]').click();assert.ok(await page.evaluate(()=>window.promptCalled));
 await page.evaluate(()=>{Object.defineProperty(navigator,'standalone',{value:true,configurable:true});dispatchEvent(new Event('appinstalled'));go('settings')});
 assert.equal(await page.locator('#install-banner').isVisible(),false);assert.ok((await page.locator('#install-settings').innerText()).includes('ya está abierta'));
 await page.evaluate(()=>{Object.defineProperty(navigator,'standalone',{value:false,configurable:true});me={...me,role:'admin'};go('settings')});
 assert.ok(await page.getByRole('button',{name:'Conexión de AdGuard ›',exact:true}).isVisible());await page.getByRole('button',{name:'Conexión de AdGuard ›',exact:true}).click();await page.locator('#s-url').waitFor();
 // Avatars, independent integrations, permission feedback and brand artwork.
 await page.evaluate(()=>go('settings'));
 await page.getByText('Cambiar cara',{exact:true}).click();
 await page.locator('[data-avatar="face-06"]').click();
 await page.getByRole('button',{name:'Guardar cara',exact:true}).click();
 await page.getByText('Cara guardada.',{exact:true}).waitFor();
 assert.equal(await page.locator('.account-avatar').getAttribute('src'),'/assets/avatars/face-06.svg');
 await page.reload();await page.locator('[data-client-card]').first().waitFor();
 await page.evaluate(()=>go('settings'));assert.equal(await page.locator('.account-avatar').getAttribute('src'),'/assets/avatars/face-06.svg');
 for(const width of [320,375,390,430]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Settings overflow at '+width);}
 await page.getByRole('button',{name:'Conexión de Nintendo ›',exact:true}).click();await page.locator('#n-timezone').waitFor();assert.equal(await page.locator('#s-url').count(),0);
 await page.getByRole('button',{name:'‹ Ajustes',exact:true}).click();
 await page.getByRole('button',{name:'Conexión de AdGuard ›',exact:true}).click();await page.locator('#s-url').waitFor();assert.equal(await page.locator('#nintendo-settings').count(),0);
 await page.getByRole('button',{name:'‹ Ajustes',exact:true}).click();
 await page.evaluate(()=>{Object.defineProperty(navigator,'userAgent',{value:'Desktop test',configurable:true});const api={permission:'default',requestPermission:async()=>{throw Error('Error de prueba de permisos')}};Object.defineProperty(window,'Notification',{value:api,configurable:true});go('settings')});
 await page.getByRole('button',{name:'Activar avisos',exact:true}).click();assert.ok((await page.locator('[data-notification-feedback]').innerText()).includes('Error de prueba de permisos'));
 await page.screenshot({path:shots+'/parental-avisos-error.png'});
 await page.evaluate(()=>{Object.defineProperty(window,'Notification',{value:{permission:'denied'},configurable:true});go('settings')});assert.equal(await page.locator('[data-notification-state]').innerText(),'Bloqueados');assert.equal(await page.getByRole('button',{name:'Activar avisos',exact:true}).isDisabled(),true);
 await page.screenshot({path:shots+'/parental-ajustes-separados.png'});
 await page.evaluate(()=>go('clients'));assert.equal(await page.locator('[data-client-card] .device-icon img').first().evaluate(n=>getComputedStyle(n).filter),'none');
 const logos=await page.evaluate(()=>['youtube','spotify','discord','manus'].map(id=>{const n=serviceIcon(id);return {id,src:n.querySelector('img')?.getAttribute('src'),color:n.style.color}}));assert.ok(logos.every(n=>n.src==='/assets/services/'+n.id+'.svg'));assert.equal(logos[0].color,'rgb(255, 0, 0)');
 await page.evaluate(()=>{me={...me,role:'solicitante',id:2};go('requests')});await page.locator('[data-request-id="1"]').waitFor();assert.equal(await page.locator('.request-decisions').count(),0);assert.ok(await page.getByRole('button',{name:'Retirar solicitud',exact:true}).isVisible());
 await page.evaluate(()=>{me={...me,role:'responsable'};go('clients')});
 adguardDown=true;await page.evaluate(()=>{state=null;dirty=false;go('clients');return refresh(true)});assert.ok(await page.locator('[data-console="nintendo:ABC"]').isVisible());
 await page.screenshot({path:shots+'/parental-movil-clientes.png'});
 adguardDown=false;await page.goto('https://preview.local/?view=requests#request=1');
 await page.locator('[data-request-id="1"].notification-target').waitFor();
 assert.ok(await page.getByRole('button',{name:'Aprobar 20 min',exact:true}).isVisible());assert.ok(!page.url().includes('#request='));
 await page.evaluate(()=>go('settings'));await page.getByText('Cambiar cara',{exact:true}).click();await page.locator('[data-avatar="face-07"]').click();
 page.once('dialog',d=>d.dismiss());await page.evaluate(()=>navigateNotification({kind:'request',id:1}));assert.equal(await page.evaluate(()=>page),'settings');assert.equal(await page.evaluate(()=>dirty),true);
 page.once('dialog',d=>d.accept());await page.evaluate(()=>navigateNotification({kind:'request',id:1}));await page.locator('[data-request-id="1"].notification-target').waitFor();
 await page.evaluate(()=>navigator.serviceWorker.dispatchEvent(new MessageEvent('message',{data:{type:'parental:navigate',target:{kind:'client',client:'iMac de Emma',service:'youtube'}}})));
 await page.locator('#blocked-authorization').waitFor();assert.equal(await page.locator('#blocked-authorization-title').innerText(),'Permitir YouTube');
 assert.equal(await page.locator('#blocked-authorization #authorization-submit').count(),0); // existing temporary permission, no duplicate grant
 await page.locator('#blocked-authorization').getByRole('button',{name:'Cerrar',exact:true}).click();
 state.leases=state.leases.filter(l=>l.service!=='youtube');
 await page.evaluate(()=>navigateNotification({kind:'client',client:'iMac de Martín',service:'youtube'}));
 for(const width of [320,375,390,430]){await page.setViewportSize({width,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);const size=await page.locator('.authorization-modal').evaluate(n=>({scroll:n.scrollWidth,width:n.clientWidth}));assert.ok(size.scroll<=size.width+1);}
 await page.locator('#authorization-minutes').selectOption('15');
 const previousPermits=calls.filter(c=>c.path==='permit').length;
 await page.locator('#authorization-submit').click();await page.waitForFunction(()=>document.querySelector('#authorization-feedback').textContent.includes('Permiso concedido'));
 assert.equal(calls.filter(c=>c.path==='permit').length,previousPermits+1);assert.deepEqual(calls.filter(c=>c.path==='permit').at(-1).body,{client:'iMac de Martín',service:'youtube',minutes:15,from_blocked_event:true});
 await page.screenshot({path:shots+'/movil-autorizacion-youtube.png'});await page.screenshot({path:shots+'/parental-aviso-destino-servicio.png'});
 await page.locator('#blocked-authorization').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.evaluate(()=>go('settings'));await page.locator('#alert-default-mode').waitFor();
 await page.locator('#alert-default-mode').selectOption('selected');await page.locator('.alert-defaults input[type=search]').fill('YouTube');
 await page.locator('.alert-defaults [data-alert-service="youtube"]').check();
 await page.locator('.alert-client-group>summary').click();
 await page.locator('[data-alert-client="iMac de Martín"]>summary').click();
 await page.locator('[data-alert-inherit="iMac de Martín"]').uncheck();
 await page.locator('[data-alert-client="iMac de Martín"] select').selectOption('none');
 await page.locator('#alert-cooldown').fill('10');
 for(const width of [320,375,390,430]){await page.setViewportSize({width,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
 alertsFailure=true;await page.locator('#alert-save').click();await page.waitForFunction(()=>document.querySelector('#blocked-alert-settings [role=status]').textContent.includes('No se pudo guardar'));
 assert.equal(await page.evaluate(()=>dirty),true);alertsFailure=false;
 await page.locator('#alert-save').click();await page.waitForFunction(()=>document.querySelector('#blocked-alert-settings [role=status]').textContent.includes('Avisos guardados'));
 assert.equal(alertPreferences.cooldown_minutes,10);assert.ok(alertPreferences.services.includes('youtube'));assert.equal(alertPreferences.clients['iMac de Martín'].mode,'none');
 await page.screenshot({path:shots+'/movil-preferencias-avisos.png',fullPage:true});
 await page.evaluate(()=>go('clients'));await page.evaluate(()=>go('settings'));await page.locator('#alert-default-mode').waitFor();assert.equal(await page.locator('#alert-default-mode').inputValue(),'selected');

 await page.evaluate(()=>go('clients'));
 await page.evaluate(()=>navigateNotification({kind:'request',id:999}));await page.waitForFunction(()=>document.getElementById('message').textContent.includes('no está disponible'));
 await page.evaluate(()=>{window.badgeCalls=[];Object.defineProperty(navigator,'setAppBadge',{value:async n=>window.badgeCalls.push(['set',n]),configurable:true});Object.defineProperty(navigator,'clearAppBadge',{value:async()=>window.badgeCalls.push(['clear']),configurable:true});ParentalUI.setRequests([],null);ParentalUI.setRequests(state.requests,me)});
 await page.waitForFunction(()=>window.badgeCalls.some(c=>c[0]==='set'&&c[1]===1));await page.evaluate(()=>ParentalUI.setRequests([],null));await page.waitForFunction(()=>window.badgeCalls.at(-1)[0]==='clear');
 await page.evaluate(()=>ParentalUI.setRequests(state.requests,me));
 // Expanded catalogs and Authentik account/configuration controls.
 await page.evaluate(()=>{me={...me,role:'admin'};dirty=false;go('settings')});
 await page.locator('#authentik-settings').waitFor();
 const authPanel=page.locator('#authentik-settings');
 await authPanel.getByLabel('Issuer esperado',{exact:true}).fill('https://auth.test/application/o/parental/');
 await authPanel.getByLabel('URL de descubrimiento OIDC',{exact:true}).fill('https://auth.test/application/o/parental/.well-known/openid-configuration');
 await authPanel.getByLabel('Client ID',{exact:true}).fill('parental-client');
 await authPanel.getByLabel('Client secret (vacío conserva el guardado)',{exact:true}).fill('simulated-provider-secret');
 await authPanel.getByLabel('URL pública HTTPS de Parental',{exact:true}).fill('https://preview.local');
 await authPanel.getByLabel('Tu contraseña local para guardar cambios',{exact:true}).fill('simulated-local-password');
 await authPanel.locator('input[type=checkbox]').first().check();assert.equal(await authPanel.locator('input[type=checkbox]').nth(1).isChecked(),false);
 assert.equal(await authPanel.getByLabel('Redirect URI para copiar en Authentik',{exact:true}).inputValue(),'https://preview.local/api/auth/oidc/callback');
 // Saving the avatar must not mark an unsaved identity configuration as clean.
 await page.getByText('Cambiar cara',{exact:true}).click();await page.locator('[data-avatar="face-07"]').click();
 await page.getByRole('button',{name:'Guardar cara',exact:true}).click();await page.getByText('Cara guardada.',{exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>dirty),true);
 await authPanel.getByRole('button',{name:'Guardar Authentik',exact:true}).click();
 await authPanel.getByText('Configuración guardada.',{exact:false}).waitFor();assert.equal(await page.evaluate(()=>dirty),false);
 assert.equal(await authPanel.getByLabel('Client secret (vacío conserva el guardado)',{exact:true}).inputValue(),'');
 await authPanel.getByRole('button',{name:'Probar configuración guardada',exact:true}).click();await authPanel.getByText('Proveedor simulado: descubrimiento y firma comprobados.',{exact:true}).waitFor();
 authenticPending={confirmation:'fixture-confirmation',label:'adulto-authentik',expires:Date.now()/1000+300};
 await page.evaluate(()=>go('settings'));await page.getByRole('button',{name:'Vincular estas cuentas',exact:true}).waitFor();
 await page.getByRole('button',{name:'Vincular estas cuentas',exact:true}).click();await page.getByText('Vinculado con adulto-authentik',{exact:true}).waitFor();
 assert.ok(calls.some(c=>c.path==='me/authentik/confirm'&&c.body.confirmation==='fixture-confirmation'));
 await page.getByText('Cambiar cara',{exact:true}).click();
 await page.getByLabel('Categoría de imágenes',{exact:true}).selectOption('Animales');
 assert.equal(await page.locator('.avatar-options [data-avatar^=animal]:visible').count(),24);
 await page.getByLabel('Buscar imagen de usuario',{exact:true}).fill('Unicornio');await page.locator('[data-avatar="animal-24"]').click();
 await page.getByRole('button',{name:'Guardar cara',exact:true}).click();await page.getByText('Cara guardada.',{exact:true}).waitFor();
 assert.equal(await page.locator('.account-avatar').getAttribute('src'),'/assets/avatars/animal-24.svg');
 for(const width of [320,375,390,430]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Expanded settings overflow at '+width);}
 await page.screenshot({path:shots+'/parental-authentik-mobile.png',fullPage:true});
 await page.evaluate(()=>go('clients'));await page.locator('[data-client-card="iMac de Emma"]').click();await page.getByRole('button',{name:'Cambiar icono',exact:true}).click();
 await page.getByLabel('Categoría de iconos',{exact:true}).selectOption('Domótica');await page.getByLabel('Buscar icono',{exact:true}).fill('Home Assistant');
 assert.equal(await page.locator('#icon-selector [data-icon="home-assistant"]').isVisible(),true);
 await page.locator('#icon-selector [data-icon="home-assistant"]').click();await page.getByRole('button',{name:'Guardar icono',exact:true}).click();
 await page.locator('#icon-selector').waitFor({state:'detached'});assert.ok(await page.locator('[data-client-card="iMac de Emma"] img[src="/assets/icons/home-assistant.svg"]').isVisible());
 await page.locator('#adguard-dialog-backdrop').getByRole('button',{name:'Cerrar',exact:true}).click();
 // Automatic delivery is independent of Requests and dirty connection forms.
 await page.evaluate(()=>go('server'));await page.locator('#s-url').waitFor();
 await page.evaluate(()=>{document.querySelector('#s-url').value='http://unsaved.example:3000';dirty=true;window.__notificationState=window.ParentalNotifications.state;window.__notificationNotify=window.ParentalNotifications.notify;window.ParentalNotifications.state=()=>({enabled:true});window.__autoNotifications=0;window.__acceptAutomatic=false;window.ParentalNotifications.notify=async()=>{window.__autoNotifications++;return window.__acceptAutomatic;};});
 inboxItems=[{id:902,kind:'blocked',client:'iMac de Martín',service:'youtube',created:Date.now()/1000-660,read_at:null,detail:{domain:'accounts.youtube.com'},target:{kind:'client',client:'iMac de Martín',service:'youtube',alert_id:902}}];inboxUnread=1;deliveryDue=true;deliveryAccepted=false;
 await page.evaluate(()=>ParentalAlertCenter.poll());
 assert.equal(await page.evaluate(()=>window.__autoNotifications),1);assert.equal(deliveryAccepted,false);
 assert.equal(await page.locator('#s-url').inputValue(),'http://unsaved.example:3000');assert.equal(await page.evaluate(()=>dirty),true);
 deliveryDue=true;await page.evaluate(async()=>{window.__acceptAutomatic=true;await ParentalAlertCenter.poll();});assert.equal(deliveryAccepted,true);
 assert.equal(await page.evaluate(()=>window.__autoNotifications),2);await page.evaluate(()=>ParentalAlertCenter.poll());assert.equal(await page.evaluate(()=>window.__autoNotifications),2);
 await page.evaluate(()=>{dirty=false;go('requests');});
 assert.equal(await page.locator('[data-alert-count]').first().innerText(),'1');
 await page.evaluate(()=>go('activity'));await page.locator('[data-alert-inbox]').getByRole('button',{name:'Revisar permiso',exact:true}).waitFor();
 for(const width of [320,375,390,430]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Alerts overflow at '+width);}
 await page.screenshot({path:shots+'/movil-avisos-no-leidos.png',fullPage:true});
 await page.locator('[data-alert-inbox]').getByRole('button',{name:'Revisar permiso',exact:true}).click();
 await page.locator('#blocked-authorization').waitFor();await page.waitForFunction(()=>document.querySelector('[data-alert-count]').hidden);assert.equal(await page.locator('#blocked-authorization h2').innerText(),'Permitir YouTube');
 await page.locator('#blocked-authorization').getByRole('button',{name:'Cerrar',exact:true}).click();
 await page.evaluate(()=>{window.ParentalNotifications.state=window.__notificationState;window.ParentalNotifications.notify=window.__notificationNotify;go('settings');});await page.locator('[data-auto-notification-status]').waitFor();await page.screenshot({path:shots+'/movil-diagnostico-entrega.png',fullPage:true});
 // Grouped attention count and two-server settings at all supported mobile widths.
 inboxItems=[{id:910,kind:'blocked',client:'iMac de Emma',service:'youtube',created:Date.now()/1000,read_at:null,detail:{domain:'youtube.com'},target:{kind:'client',client:'iMac de Emma',service:'youtube',alert_id:910}},{id:911,kind:'blocked',client:'iMac de Emma',service:'tiktok',created:Date.now()/1000,read_at:null,detail:{domain:'tiktok.com'},target:{kind:'client',client:'iMac de Emma',service:'tiktok',alert_id:911}},{id:912,kind:'blocked',client:'iMac de Martín',service:'youtube',created:Date.now()/1000,read_at:null,detail:{domain:'youtube.com'},target:{kind:'client',client:'iMac de Martín',service:'youtube',alert_id:912}}];
 inboxGroups=inboxItems.map(i=>({...i,is_new:true,attempts:154,first_seen:i.created-3600,last_seen:i.created,expires:i.created+82800,records:154}));inboxUnread=2;deliveryDue=false;
 await page.evaluate(()=>go('activity'));await page.evaluate(()=>ParentalAlertCenter.poll());await page.waitForFunction(()=>document.querySelector('[data-alert-count]').textContent==='2');
 assert.equal(await page.locator('[data-alert-inbox] .alert-client').count(),2);assert.equal(await page.locator('[data-alert-inbox] .alert-client article').count(),3);
 for(const width of [320,375,390,430]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Grouped alerts overflow at '+width);}
 await page.screenshot({path:shots+'/movil-avisos-agrupados.png',fullPage:true});
 await page.locator('.alert-client').filter({has:page.getByRole('heading',{name:'iMac de Emma',exact:true})}).getByRole('button',{name:'Marcar avisos de este cliente como leídos',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('[data-alert-count]').textContent==='1');assert.equal(await page.locator('.alert-history article').count(),2);
 await page.evaluate(()=>go('server'));await page.locator('#s-url').waitFor();await page.getByRole('button',{name:'Añadir réplica',exact:true}).click();
 await page.getByLabel('Nombre de la réplica',{exact:true}).fill('AdGuard secundario');await page.getByLabel('URL de la réplica',{exact:true}).fill('http://replica.test:8053');await page.getByLabel('Usuario de la réplica',{exact:true}).fill('admin');await page.getByLabel('Contraseña de la réplica',{exact:true}).fill('private-test-only');await page.locator('#s-demo').uncheck();await page.locator('#s-sync-ack').check();
 for(const width of [320,375,390,430]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Replica settings overflow at '+width);}
 // Do not retain credentials in review screenshots.
 await page.getByLabel('Contraseña de la réplica',{exact:true}).fill('');await page.screenshot({path:shots+'/movil-dos-adguard.png',fullPage:true});await page.evaluate(()=>{dirty=false;});
 await page.setViewportSize({width:1440,height:1000});
 await page.evaluate(async()=>{
  token='';for(const n of document.body.children)n.style.display='none';document.body.style.display='block';document.body.style.padding='32px';
  const board=document.createElement('section');board.style.cssText='max-width:1380px;margin:auto';document.body.append(board);
  const group=(label)=>{const h=document.createElement('h2');h.textContent=label;board.append(h);const grid=document.createElement('div');grid.style.cssText='display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px;margin:24px 0 40px';board.append(grid);return grid;};
  const devices=group('Parental · 91 iconos de dispositivos y software');for(const [id,label]of deviceIconCatalog){const item=document.createElement('div');item.append(clientDeviceIcon({ui_icon:id,name:label}));const name=document.createElement('small');name.textContent=label;item.append(name);devices.append(item);}
  const faces=group('80 imágenes editables');for(const face of ParentalIdentity.faces)faces.append(ParentalIdentity.avatar({avatar:face.id}));
  const brands=group('142 logos locales · Catálogo oficial de AdGuard');for(const id of Object.keys(ParentalServices)){const item=document.createElement('div');item.style.cssText='display:flex;align-items:center;flex-direction:column;gap:8px;padding:12px 4px;border:1px solid #eee;border-radius:12px';item.append(serviceIcon(id));const name=document.createElement('small');name.textContent=id;name.style.cssText='font-size:10px;overflow-wrap:anywhere;text-align:center';item.append(name);brands.append(item);}
  await Promise.all([...board.querySelectorAll('img')].map(img=>img.complete?(img.naturalWidth?Promise.resolve():Promise.reject(Error(img.src))):new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(Error(img.src))})));
 });
 await page.screenshot({path:shots+'/parental-catalogo-completo.png',fullPage:true});
 {const users=await context.newPage();users.setDefaultTimeout(10000);users.on('pageerror',e=>errors.push(e.message));await users.goto(page.url().split('?')[0]+'?view=users');await users.getByText('Altas pendientes de aprobación').waitFor();assert.equal(await users.getByText('Crear o editar cuenta').count(),1);
  await users.getByRole('button',{name:'Revisar y activar'}).click();assert.equal(await users.locator('#u-name').inputValue(),'lucia');assert.equal(await users.locator('#u-email').inputValue(),'lucia@example.test');assert.equal(await users.locator('#u-active').isChecked(),true);await users.close();}
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({passed:true,screenshots:shots,clientSettings:'mobile widths, own/global services, validation, API errors, read-only confirmation and saved device icons',sanitizer,calls},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e.stack);process.exitCode=1});
