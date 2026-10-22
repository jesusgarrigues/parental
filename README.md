# Parental

Antes AdGuard Familia. Repositorio: [jesusgarrigues/parental](https://github.com/jesusgarrigues/parental). Imagen: `jesusgarrigues/parental`. El servicio `companion` y el volumen `companion-data` conservan su nombre para mantener los datos de instalaciones existentes.

Panel familiar autohospedable para AdGuard Home y Nintendo Switch. Lee los clientes y sus ajustes dinámicamente, gestiona la configuración global y las personalizaciones y permite solicitar y aprobar excepciones temporales.

## Instalar desde Docker Hub

Una vez publicada la imagen `<usuario-dockerhub>/parental`:

1. Descarga `compose.yaml` y `.env.example` de este repositorio.
2. Copia `.env.example` a `.env`, introduce `DOCKERHUB_USERNAME` y sustituye `APP_TOKEN` por una clave aleatoria.
3. Ejecuta:

```sh
docker compose pull
docker compose up -d
```

Abre http://localhost:8080, crea el primer administrador usando APP_TOKEN y configura la URL, usuario y contraseña de AdGuard en **Ajustes → Integraciones → AdGuard Home**. Puedes probar la conexión antes de guardarla. La configuración se conserva en el volumen; la contraseña no se devuelve al navegador. Puedes usar variables ADGUARD_* en .env como configuración inicial.

Para cambiar el puerto publicado, define `APP_PORT=8090` (o el puerto que prefieras) en `.env` y ejecuta `docker compose up -d`. El contenedor y su comprobación de salud siguen usando 8080 internamente. `BIND_ADDRESS` conserva su función. Si tu Compose anterior tenía un puerto personalizado escrito directamente, pasa ese valor a `APP_PORT` antes de reemplazar el archivo para conservarlo.

Para acceder desde tu LAN, configura `BIND_ADDRESS=0.0.0.0` y abre la IP del servidor. Usa un proxy inverso con HTTPS para acceso remoto y notificaciones de navegador desde la LAN. La URL de AdGuard incluye su puerto web, no su puerto DNS. `localhost` dentro del contenedor apunta a la app; para otro contenedor usa una red Docker compartida y el nombre de servicio.

Las imágenes publicadas por el workflow admiten `linux/amd64` y `linux/arm64`. `latest` y `edge` se actualizan tras superar las pruebas en `main`; las etiquetas `v*` añaden versiones numeradas. Puedes fijar `IMAGE_TAG` a una versión publicada.

### Avisos en el móvil (Web Push)

Para recibir avisos con Parental cerrada, publica el panel por HTTPS, instala la PWA y pulsa **Activar avisos** en cada dispositivo. Opcionalmente define en `.env` `WEB_PUSH_CONTACT=mailto:correo@dominio-real` o `https://dominio-real` (sin ruta): Apple rechaza los avisos si el contacto usa `localhost`, `.local`, `.lan` o una IP. Detalles en [Avisos](docs/avisos.md) y [activación y diagnóstico](docs/avisos-bloqueos.md).

## Actualizar

Haz una copia del volumen y ejecuta:

```sh
docker compose pull
docker compose up -d
```

No uses `docker compose down -v`: borraría cuentas, permisos y conexiones. Si vienes de la imagen anterior, sigue [Actualizar desde adguard-familia](docs/migracion-adguard-familia.md).

## Guías

| Tema | Guía |
| --- | --- |
| Clientes de AdGuard, herencia, permisos temporales y persistencia | [clientes-y-herencia.md](docs/clientes-y-herencia.md) |
| Usuarios, roles y aprobación de solicitudes | [usuarios-y-roles.md](docs/usuarios-y-roles.md) |
| Avisos, Web Push y perfiles | [avisos.md](docs/avisos.md) · [activación y diagnóstico](docs/avisos-bloqueos.md) |
| Instalar en Android e iPhone (PWA) | [pwa-movil.md](docs/pwa-movil.md) |
| Nintendo Switch: conexión y tiempo extra | [nintendo.md](docs/nintendo.md) · [validación real](docs/nintendo-validacion-real.md) |
| Diagnóstico de conexión con AdGuard | [diagnostico.md](docs/diagnostico.md) |
| Iconos, imágenes y acceso con Authentik | [iconos-y-authentik.md](docs/iconos-y-authentik.md) · [Authentik](docs/authentik.md) |
| Publicar la imagen en Docker Hub | [publicacion.md](docs/publicacion.md) |
| Migrar desde adguard-familia | [migracion-adguard-familia.md](docs/migracion-adguard-familia.md) |

## Desarrollo y demostración

```sh
cp .env.example .env
# Define APP_TOKEN. Opcional: DEMO=true para probar sin AdGuard.
docker compose -f compose.build.yaml up -d --build
```

El modo demostración simula Emma y Martín; no cambia ningún AdGuard real. El diseño sigue la maqueta oscura con clientes, permisos con cuenta atrás, actividad y ajustes.

```sh
python -m pip install -r requirements.txt
python -m unittest -v test_app.py test_diagnostics.py test_auth.py test_nintendo.py test_nintendo_routes.py test_oidc.py test_catalog.py test_blocked_alerts.py test_notification_center.py
python test_http.py
node test_ui.cjs
```

Pruebas de caducidad tras reinicio, permisos simultáneos, herencia global, cambios globales y de cliente durante permisos, recuperación de errores, clientes dinámicos, validación y protección de credenciales. La integración debe verificarse con la versión instalada de AdGuard Home; las pruebas locales usan el simulador. El workflow también ejecuta `test_browser.cjs` con Playwright para comprobar escritorio, móvil, favoritos, categorías, ajustes y preservación de formularios; sus capturas se guardan en el artefacto `ui-previews`.

## Varios AdGuard y contador de avisos

Configura un origen y réplicas en Ajustes para aplicar permisos y cambios en todos, con confirmación y restauración por servidor. [Guía de instalación y convivencia con adguard-sync](docs/adguard-multiservidor.md). El icono suma solicitudes pendientes y clientes con avisos nuevos; los intentos se agrupan por servicio durante 24 horas y el historial se conserva hasta 30 días.
