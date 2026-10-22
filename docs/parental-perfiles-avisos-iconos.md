# Perfiles, avisos e integraciones

## Ajustes por ámbito

**Parental** agrupa Mi perfil, Usuarios y roles, avisos del navegador, instalación y auditoría. **Integraciones** ofrece conexiones independientes para AdGuard Home y Nintendo. La configuración global de AdGuard se identifica expresamente; las políticas de cada cliente o consola siguen en su detalle. Las pantallas administrativas conservan los permisos existentes.

## Caras de usuarios

Mi perfil permite seleccionar una de doce caras originales o conservar la inicial, y guardar con confirmación en el mismo bloque. El administrador puede asignarlas desde Usuarios y roles. Se conservan en `/data/accounts.db`; una migración añade el campo sin recrear usuarios. Cambiar solo la cara conserva las sesiones. Cambiar permisos o credenciales sigue revocándolas. Las solicitudes muestran la cara actual del solicitante. Los menores no obtienen permisos de aprobación por editar su perfil.

## Avisos del navegador

Se muestra el estado del permiso y cualquier error junto a Activar avisos. Probar aviso verifica la entrega al sistema, utilizando el service worker cuando está activo. En iPhone/iPad se requiere HTTPS, iOS/iPadOS 16.4 o posterior y abrir Parental desde su instalación en la pantalla de inicio. Un permiso bloqueado debe habilitarse desde los ajustes del sistema/navegador; la app no puede concedérselo a sí misma.

Al activar avisos se registra una suscripción Web Push en Docker para recibirlos **también con Parental cerrada**. Sin suscripción Push, la entrega local requiere el panel abierto. Los otros proveedores externos y sus acciones siguen pendientes en #48/#50. Pulsar un aviso abre la vista correspondiente; no concede permisos ni ejecuta una aprobación. Si hay cambios sin guardar, se conserva la confirmación de navegación. [Activación y diagnóstico](avisos-bloqueos.md).

## Recursos gráficos

Doce dispositivos y doce caras SVG originales, bajo MIT: `assets/licenses/parental-artwork.txt`. Conservan las claves del catálogo de dispositivos y las elecciones almacenadas; Automático sigue usando nombre/etiquetas. Los antiguos recursos Lucide y su licencia se conservan donde corresponda.

Los logos locales de 142 servicios proceden del catálogo de AdGuard HostlistsRegistry, revisión `19e7e0ac9705fc9ad261d8cf6645ce59f4ee94cb`. Son un conjunto de recursos separado bajo GPL-3.0; licencia completa en `assets/licenses/hostlists-registry.txt`, SVG originales editables en `assets/service-artwork.json` y procedencia por ID en `assets/service-catalog.json`. Se modificó la pintura `currentColor` para usar colores de marca y se resolvió la variable de Manus, preservando sus máscaras y recortes. Google Play conserva sus cuatro segmentos con colores distintos e Instagram incorpora un gradiente de marca. El nombre y los controles de cada servicio siguen procediendo de la instancia conectada: el catálogo de logos no añade servicios bloqueables.

Las paletas utilizan metadatos de Simple Icons cuando están disponibles (revisión `98820a4dc8c363ca72fa2c0d294ea4a0a9bba75d`) y una selección visual documentada para los demás; no se afirma que todos los valores sean guías oficiales vigentes. Se mantienen identidades monocromas donde corresponde. Los recursos no implican afiliación a sus marcas.

Los servicios conocidos usan SVG locales como imágenes; no necesitan llamar a terceros desde el navegador. Un servicio nuevo utiliza el SVG saneado que envía AdGuard o una alternativa de categoría. El saneador no amplía sus permisos para aceptar scripts, eventos o cargas remotas. Las rutas de imágenes conocidas se sirven desde una lista cerrada. El service worker solo almacena activos públicos, nunca cuentas, respuestas API ni HTML privado.

## Validación

Pruebas de migración, persistencia, sesiones, permisos de perfil/CSRF, identidad en solicitudes, notificaciones y caché PWA. Navegador Chromium con fixtures de proveedores y vistas de 320/375/390/430 px. El comportamiento real de Safari/iPhone y la sincronización con Nintendo requieren validación con esos dispositivos; no se atribuye esa validación a Chromium.

## Burbuja del icono y destino del aviso

El contador usa Badging API si el sistema la ofrece, sumando solicitudes pendientes y clientes con avisos nuevos agrupados por servicio en ventanas de 24 horas filtrados por cuenta/rol/cliente. Cero y cierre de sesión borran la burbuja. Se actualiza al consultar datos y al recibir Web Push. Leer en otro dispositivo se refleja al abrir Parental o recibir el siguiente Push; no se envían mensajes silenciosos solo para actualizar el contador. En iPhone la visibilidad depende de la instalación y permisos. Android puede usar burbujas de notificaciones activas del launcher; no existe garantía de contador numérico para todos los navegadores o fabricantes.

Los avisos de aprobación abren la tarjeta concreta en Solicitudes, resaltada y enfocada. Los de consultas bloqueadas abren cliente y servicio. Se conserva el destino al iniciar sesión y se respetan los formularios sin guardar. Los datos del destino viajan en un fragmento URL interno, sin enviarse al proxy. IDs y campos se validan tanto en la página como en el service worker; pulsar nunca ejecuta una aprobación.
