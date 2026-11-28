# Registro de incidencias

Historial de los fallos encontrados en Parental, su causa y cómo se resolvieron. Sirve para revisar decisiones pasadas, detectar problemas que se repiten y ver cómo ha ido creciendo la aplicación. Cada incidencia tiene además su issue en GitHub con el detalle completo.

Las entradas más recientes van primero. No se incluyen IPs, usuarios ni credenciales.

## Cómo añadir una incidencia

1. Abre el issue con título `[Error] …` y etiqueta `bug`: problema, causa, solución y propuesta.
2. Añade una ficha en «Fichas detalladas» con esta plantilla y una fila en el «Índice».

```markdown
### AAAA-MM-DD · Título breve (#issue)

- **Síntoma:** qué se veía y dónde.
- **Causa:** por qué ocurría, con la comprobación que lo confirma.
- **Solución:** qué se cambió (archivos, versiones) y cómo se verificó.
- **Despliegue:** qué hay que hacer en la instalación (imagen, reconexión, nada).
- **Lecciones / pendiente:** qué evitaría que se repita.
```

## Fichas detalladas

### 2026-11-28 · Nintendo rechaza las consultas con «update_required» (#123)

- **Síntoma:** la integración con Nintendo deja de leer las consolas y de conceder tiempo. En el registro solo aparece `Error interno de sincronización … GroupError en app.py:558 reconcile`, sin mencionar a Nintendo.
- **Causa:** `pynintendoparental==2.6.3` se identifica ante la API como la app móvil Nintendo Switch Parental Controls **2.6.0 (build 700)**. Nintendo dejó de aceptar esa versión y responde `HTTP 400 (update_required)`. Comprobado desde el contenedor en producción, solo con lecturas y la sesión guardada: con 2.6.0/700 falla y con 2.7.0/740 responde bien.
- **Solución:** `requirements.txt` pasa a `pynintendoparental==2.7.1` (26/11/2026), que envía la versión 2.7.0/740. Esta versión de la biblioteca solo añade cambios compatibles: `discover_devices()`, el parámetro opcional `monthly_summary` en `update()` y la creación de jugadores a partir del resumen diario. Los métodos que usa `nintendo.py` no cambian. Pasan los 258 tests de la CI y `test_http.py`.
- **Despliegue:** reconstruir la imagen y volver a crear el contenedor. No hace falta volver a conectar la cuenta Nintendo.
- **Lecciones / pendiente:** Nintendo exige cada cierto tiempo una versión nueva de la app, y eso rompe la integración sin aviso. Hay que fusionar pronto los PR de Dependabot del grupo `nintendo` y mostrar un mensaje claro cuando Nintendo responda `update_required`.

## Índice

| Fecha | Issue | Área | Incidencia |
|---|---|---|---|
| 2026-11-28 | [#123](https://github.com/jesusgarrigues/parental/issues/123) | Nintendo | Nintendo rechaza las consultas con «update_required» por la versión de app de pynintendoparental 2.6.3 |
| 2026-11-03 | [#119](https://github.com/jesusgarrigues/parental/issues/119) | AdGuard | Un cliente creado en AdGuard que aún no está en la réplica impide guardar o conceder tiempo |
| 2026-11-03 | [#118](https://github.com/jesusgarrigues/parental/issues/118) | AdGuard | Los fallos de AdGuard llegan como «respuesta JSON no válida (HTTP 502)» detrás de Cloudflare |
| 2026-11-03 | [#116](https://github.com/jesusgarrigues/parental/issues/116) | Interfaz | Los desplegables se cierran solos al refrescar |
| 2026-11-03 | [#115](https://github.com/jesusgarrigues/parental/issues/115) | CI | La prueba de navegador falla por avisos de CSP con el SVG hostil del sanitizador |
| 2026-11-03 | [#113](https://github.com/jesusgarrigues/parental/issues/113) | Interfaz | El desplegable «Dispositivos detectados automáticamente» se cierra solo al refrescar |
| 2026-11-03 | [#112](https://github.com/jesusgarrigues/parental/issues/112) | AdGuard | La zona horaria del horario de servicios se trata como configuración distinta entre servidores |
| 2026-11-03 | [#111](https://github.com/jesusgarrigues/parental/issues/111) | AdGuard | Un alta pendiente en la réplica bloquea todos los permisos y guardados |
| 2026-10-06 | [#83](https://github.com/jesusgarrigues/parental/issues/83) | Avisos | Avisos de bloqueos en la bandeja sin notificación del sistema en el móvil |
| 2026-10-05 | [#74](https://github.com/jesusgarrigues/parental/issues/74) | AdGuard | Detectar bloqueos de servicios con los motivos reales del registro |
| 2026-10-05 | [#73](https://github.com/jesusgarrigues/parental/issues/73) | Authentik | «Flow does not apply to current user» al volver de Google a Authentik |
| 2026-10-05 | [#58](https://github.com/jesusgarrigues/parental/issues/58) | Avisos | Activar avisos con estado visible, errores y compatibilidad con iPhone |
| 2026-10-05 | [#57](https://github.com/jesusgarrigues/parental/issues/57) | Interfaz | Elegir y guardar las caras o avatares de los usuarios |
| 2026-10-04 | [#55](https://github.com/jesusgarrigues/parental/issues/55) | PWA | Instalación como PWA en iPhone y Android |
| 2026-10-04 | [#45](https://github.com/jesusgarrigues/parental/issues/45) | Interfaz | Guardar ajustes del cliente no da resultado visible ni muestra errores |
| 2026-10-04 | [#44](https://github.com/jesusgarrigues/parental/issues/44) | Interfaz | Ocultar la edición de servicios mientras el cliente hereda los globales |
| 2026-10-04 | [#43](https://github.com/jesusgarrigues/parental/issues/43) | Interfaz | Ajustar el modal de configuración de clientes al ancho del móvil |
| 2026-10-04 | [#34](https://github.com/jesusgarrigues/parental/issues/34) | Nintendo | *(abierta)* Una concesión aceptada no aplica los minutos en la consola real |
| 2026-10-04 | [#32](https://github.com/jesusgarrigues/parental/issues/32) | Nintendo | Cerrar el seguimiento cuando Nintendo cambia desde otra app |
| 2026-10-04 | [#31](https://github.com/jesusgarrigues/parental/issues/31) | Nintendo | No dejar pendiente un segundo paso por una propuesta de horario incompleta |
| 2026-10-04 | [#30](https://github.com/jesusgarrigues/parental/issues/30) | Nintendo | No exigir cambio de hora para confirmar solo minutos extra |
| 2026-10-04 | [#28](https://github.com/jesusgarrigues/parental/issues/28) | Nintendo | Gestionar tiempo extra con límite diario de 0 y avisos ocultos |
| 2026-10-04 | [#13](https://github.com/jesusgarrigues/parental/issues/13) | Interfaz | La actualización borraba datos introducidos en formularios |
| 2026-10-04 | [#12](https://github.com/jesusgarrigues/parental/issues/12) | AdGuard | Diagnosticar fallos de conexión sin exponer credenciales |
| 2026-10-04 | [#11](https://github.com/jesusgarrigues/parental/issues/11) | Arranque | Fallo SQLite «unable to open database file» al arrancar |
