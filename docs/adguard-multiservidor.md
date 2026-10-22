# Un grupo de AdGuard Home

Parental admite un origen y hasta tres réplicas. Cada servidor conserva su URL web, usuario y contraseña. Las tarjetas, ámbitos y solicitudes siguen usando los clientes del origen; una réplica se vincula por el conjunto de identificadores IP/CIDR/MAC/ClientID, con independencia del nombre. Si falta un cliente o sus identificadores difieren, se muestra sin vincular y se rechaza la concesión. No se fusionan clientes solo por compartir nombre.

## Activación

1. Actualiza Parental conservando el volumen de datos.
2. En **Ajustes → Integraciones → AdGuard → Conexión**, conserva el origen y añade una réplica con su URL web completa, por ejemplo `http://192.168.1.3:8053`. Introduce sus propias credenciales. Una contraseña vacía conserva la ya guardada; no se muestra al navegador.
3. Pulsa **Probar conexión**. El contenedor debe alcanzar las dos APIs, incluidas rutas del host o de la VPN si se utilizan. Que tu teléfono alcance ambos AdGuard no demuestra conectividad desde Docker.
4. Prepara las exclusiones de adguard-sync que se indican abajo y confirma la casilla correspondiente. Guarda el grupo. La conexión de un solo servidor sigue siendo válida sin activar réplicas.
5. En Clientes consulta **Estado por servidor** y verifica que cada dispositivo está vinculado en ambos. Los clientes creados fuera de Parental deben existir con los mismos identificadores en las réplicas antes de concederles permisos. Los clientes creados desde Parental se dan de alta en todo el grupo.

Para añadir, eliminar o cambiar URLs de servidores deben haber finalizado las concesiones y restauraciones pendientes. No se eliminan permisos para permitir ese cambio. En un grupo existente se pueden corregir credenciales sin cambiar su topología para recuperar una API inaccesible. Usuarios, solicitudes, avatares y ámbitos de la instalación se conservan.

## Convivencia con adguard-sync

Parental administra directamente la configuración de clientes, servicios y protección global en cada AdGuard. El sincronizador sigue copiando las categorías que no se solapan, como listas de filtros, reglas y reescrituras.

En **bakito/adguardhome-sync**, la configuración documentada permite estas exclusiones:

```yaml
features:
  clientSettings: false
  services: false
  generalSettings: false
  protectionStatus: false
```

O sus variables de entorno:

```yaml
environment:
  FEATURES_CLIENT_SETTINGS: "false"
  FEATURES_SERVICES: "false"
  FEATURES_GENERAL_SETTINGS: "false"
  FEATURES_PROTECTION_STATUS: "false"
```

Revisa también cualquier configuración `features` específica de la réplica: puede modificar la configuración general. `protectionStatus` depende de `generalSettings`. La imagen y versión del sincronizador deben ofrecer estas opciones; para otra herramienta consulta su documentación. El intervalo de 10 minutos puede mantenerse para las categorías restantes. Parental no cambia automáticamente un contenedor externo ni verifica estas exclusiones a través de su API: la casilla confirma tu configuración.

Haz la transición coordinando la activación del grupo y las exclusiones. Una vez excluidos esos campos, gestiona los ajustes compartidos desde Parental. Las ediciones directas en un AdGuard no se convierten automáticamente en una política compartida: las diferencias se deben revisar. No ejecutes dos procesos de Parental contra el mismo volumen SQLite/grupo.

## Permisos, confirmación y fallos

Una aprobación crea una sola concesión con responsable y vencimiento absoluto. Parental comprueba la identidad, el catálogo y la política base de todos los destinos antes de conceder. Si difieren las restricciones efectivas, solicita resolver la diferencia. Cada escritura se registra antes de enviarla y se confirma leyendo la configuración.

Si una concesión no se confirma en todos los servidores, Parental retira esa nueva concesión y compensa los cambios que pueda haber aplicado. Si tampoco puede confirmar la compensación, muestra restauración pendiente y conserva el estado para reintentarlo. No se considera éxito una escritura parcial ni se amplía el plazo al reintentar. Las concesiones de otros servicios siguen activas.

Al caducar o cancelar, la configuración se restaura en todos los destinos; se combina con otras concesiones y con las ediciones de base realizadas desde Parental. Reiniciar conserva el trabajo pendiente. Una réplica que vuelve después del vencimiento recibe la política vigente; no se le reproduce tiempo ya caducado.

Los guardados permanentes de cliente y globales también se confirman en cada servidor. Un guardado parcial queda registrado y se reintenta; mientras está pendiente se bloquean otros guardados/concesiones nuevos para evitar pisar intenciones. Si una API no confirma la retirada pero su DNS sigue atendiendo consultas, no puede garantizarse una caducidad estricta hasta recuperar esa API. Una garantía de ese tipo requeriría además retirar ese DNS del tráfico.

## Actividad y avisos

Parental consulta los registros de todos los AdGuard, conserva la procedencia y agrupa los intentos bajo el cliente lógico. La caída de una lectura no impide recoger la otra. Los datos del origen pueden mostrarse desde la última lectura guardada, señalando que el servidor no está accesible; las concesiones nuevas siempre exigen lecturas actuales.

Cada lectura obtiene hasta 500 consultas por servidor. Mucho tráfico, caché DNS, clientes excluidos del registro o DNS alternativos pueden dejar intentos sin detectar. Las consultas DNS pueden proceder de actividad en segundo plano y no prueban que alguien estuviera viendo contenido.

El icono suma **solicitudes pendientes + clientes con avisos nuevos**. Los avisos se agrupan por cliente y servicio durante 24 horas desde la primera detección; las repeticiones no reinician esa ventana. El historial se conserva hasta 30 días. Consulta [la guía de avisos](avisos-bloqueos.md).

Las pruebas automatizadas usan dos APIs independientes, fallos parciales y recuperación. No sustituyen la validación de conectividad, exclusiones y resolución DNS en tu propia infraestructura.
