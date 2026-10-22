// Only allowlisted public assets are cached. Accounts, API responses and HTML are never cached.
const CACHE = 'parental-static-v7';
importScripts('/assets/notification-targets.js','/assets/appearance-catalog.js');
const PUBLIC = new Set([
  '/manifest.webmanifest', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png', '/favicon.png', '/icon.svg',
  '/assets/app.css', '/assets/parental.css', '/assets/app-shell.js', '/assets/client-settings.js', '/assets/fonts/InterVariable.woff2',
  '/assets/appearance-catalog.js', '/assets/authentik.js', '/assets/identity.js', '/assets/notifications.js', '/assets/service-logos.js',
  '/assets/notification-targets.js', '/assets/app-badges.js', '/assets/blocked-notifications.js',
  '/assets/notification-center.js',
  ...ParentalCatalog.avatars.map(item=>'/assets/avatars/'+item.id+'.svg'),
  ...["4chan","500px","9gag","activision_blizzard","aliexpress","amazon","amazon_streaming","amino","apple_streaming","battle_net","betano","betfair","betway","bigo_live","bilibili","blaze","blizzard_entertainment","bluesky","box","canais_globo","chatgpt","claro","claude","cloudflare","clubhouse","coolapk","copilot","crunchyroll","dailymotion","deepseek","deezer","directvgo","discord","discoveryplus","disneyplus","dola","douban","dropbox","ebay","electronic_arts","epic_games","espn","facebook","fdj_united","fifa","flickr","gemini","globoplay","gog","grindr","grok","hbomax","hulu","icloud_private_relay","iheartradio","imgur","instagram","io_interactive","iqiyi","kakaotalk","kik","kook","lazada","leagueoflegends","line","linkedin","lionsgateplus","looke","mail_ru","manus","mastodon","max","mercado_libre","meta_ai","microsoft_teams","minecraft","nebula","netflix","nintendo","nvidia","odysee","ok","olvid","onlyfans","origin","paramountplus","peacock_tv","perplexity","pinterest","playstation","playstore","plenty_of_fish","plex","pluto_tv","privacy","proton","qq","questionai","qwen","rakuten_viki","reddit","riot_games","roblox","rockstar_games","samsung_tv_plus","shein","shell_shockers","shopee","signal","skype","slack","snapchat","soundcloud","spotify","spotify_video","steam","telegram","temu","tidal","tiktok","tinder","tumblr","twitch","twitter","ubisoft","valorant","viber","vimeo","vivo_play","vk","voot","wargaming","warnerbrosgames","wechat","weibo","whatsapp","wizz","xboxlive","xiaohongshu","youtube","yy","zhihu"].map(id=>'/assets/services/'+id+'.svg'),
  ...ParentalCatalog.devices.map(item => '/assets/icons/' + item.id + '.svg')
]);
const OFFLINE = '<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#ffffff"><title>Parental · Sin conexión</title><style>body{font-family:system-ui,sans-serif;padding:56px 24px;max-width:440px;margin:auto;color:#111}img{width:64px;border-radius:16px}h1{font-size:30px;letter-spacing:-1px}p{color:#62666d;line-height:1.6}button{background:#111;color:#fff;border:0;border-radius:99px;padding:14px 24px;font:inherit}</style><img src="/apple-touch-icon.png" alt=""><h1>Sin conexión</h1><p>Conecta con tu servidor para consultar tus clientes, solicitudes y permisos.</p><button onclick="location.reload()">Volver a intentar</button></html>';
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.allSettled([...PUBLIC].filter(path=>!/^\/assets\/(services|icons|avatars)\//.test(path)).map(async path => {
      const response = await fetch(path, {cache: 'reload', credentials: 'omit'});
      if (response.ok && !response.redirected) await cache.put(path, response);
    }));
    await self.skipWaiting();
  })());
});
// Every Push produces a visible notification. Badge-only silent Push is not supported by iOS.
self.addEventListener('push',event=>event.waitUntil((async()=>{
  let data;try{data=event.data?.json();}catch{return;}
  if(!data||typeof data.title!=='string'||data.title.length>512||typeof data.body!=='string'||data.body.length>1024)return;
  const target=ParentalTargets.normalize(data.target);
  const options={body:data.body,icon:'/icon-192.png',badge:'/icon-192.png',tag:typeof data.tag==='string'?data.tag.slice(0,128):'parental-push',data:{target,url:target?ParentalTargets.url(target):'/'}};
  await self.registration.showNotification(data.title,options);
  if(Number.isSafeInteger(data.badge_count)&&data.badge_count>=0){try{if(data.badge_count)await self.navigator.setAppBadge?.(data.badge_count);else await self.navigator.clearAppBadge?.();}catch{}}
  for(const client of await self.clients.matchAll({type:'window'}))client.postMessage({type:'parental:alerts-updated'});
})()));
self.addEventListener('activate', event => event.waitUntil((async () => {
  for (const name of await caches.keys()) if (name.startsWith('parental-static-') && name !== CACHE) await caches.delete(name);
  await self.clients.claim();
})()));
self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => new Response(OFFLINE, {headers: {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'}})));
    return;
  }
  if (!PUBLIC.has(url.pathname)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(request);
      if (response.ok && !response.redirected) await cache.put(url.pathname, response.clone());
      return response;
    } catch {
      return await cache.match(url.pathname) || Response.error();
    }
  })());
});
// Clicking an alert only opens a view; decisions still require authentication and CSRF.
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=ParentalTargets.normalize(event.notification.data?.target);
  const path=target?ParentalTargets.url(target):event.notification.data?.url==='/?view=requests'?'/?view=requests':'/';
  event.waitUntil((async()=>{
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const own=windows.filter(client=>new URL(client.url).origin===self.location.origin);
    const client=own.find(c=>c.visibilityState==='visible')||own[0];
    if(client){client.postMessage({type:'parental:navigate',target,view:path==='/'?'clients':'requests'});return client.focus();}
    return self.clients.openWindow(new URL(path,self.location.origin).href);
  })());
});
